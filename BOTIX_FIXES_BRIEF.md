# Задача для Claude Code: применить пакет фиксов к Botix

## Контекст
Это репозиторий торгового бота для MEXC 0-fee perpetual futures с Binance Futures
в качестве fair-value reference. Бот написан на Python (asyncio). Я работаю над
повышением hit-rate и снижением latency. Параллельно начинаем миграцию hot-path
на Rust (пока только скелет — реальный код позже).

**ВАЖНО про `web_uid`:**
- Это web-сессионный токен MEXC, который МЫ СОЗНАТЕЛЬНО используем вместо OpenAPI
  ради скорости. Не предлагай мне перейти на OpenAPI и не переписывай вызовы под него.
- Секреты УЖЕ вынесены в отдельный config (Fix #0 выполнен), git history очищена.

**СТАТУС Fix #0 (security):** ✅ ВЫПОЛНЕН
- config.json удалён из git tracking и истории
- config.example.json создан с плейсхолдерами
- .gitignore обновлён
- web_uid validation добавлена в backend/config.py
- 401/403 health-check с kill_switch добавлен в backend/mexc_trader.py
- git filter-repo выполнен, force-push сделан

## Что нужно сделать (в этом порядке, по одному коммиту на пункт)

Перед началом работы:
1. Создай ветку `fix/edge-and-latency-pack-1` от `main`.
2. Прочитай файлы `backend/aggregator.py`, `backend/real.py`, `backend/engine.py`,
   `backend/binance_ws.py`, `backend/mexc_ws.py`, `backend/universe.py`,
   `backend/opportunity.py`, `backend/mexc_trader.py`, `backend/persistence.py`,
   `backend/config.py`, `config.json` целиком — пойми реальные имена полей,
   классов и методов. Названия в спецификациях ниже могут не совпадать с кодом
   1-в-1; используй те имена, что в реальном коде.
3. Запусти `python -m pytest -x` (если есть тесты) — зафиксируй baseline.

### Коммит 1: fix stock fair contamination (Bug A)
- В `aggregator.py`:
  - В классе агрегата на символ добавь поле `mexc_ba_samples: Deque[Tuple[float,float]]`
    (maxlen=2000) и атрибут `external_fair_available: bool` в результате stats.
  - В ветке "stock without binance ref" перестань класть в `spread_samples`
    любые значения. Вместо этого пиши bid-ask spread в `mexc_ba_samples`.
    Возвращай `fair=None, spread=None, spread_bps=None, z_score=0.0,
    external_fair_available=False`.
  - Все `logger.info` в горячем пути `compute_stats` и `on_mexc_depth` →
    `logger.debug`.
- В `opportunity.py` (или там где `evaluate_multi`): если
  `external_fair_available=False` — оставлять только стратегии, не требующие
  external fair (как минимум `bb_revert`).

**Детали реализации:**
```python
# В классе _SymbolAgg или эквивалентном dataclass:
mexc_ba_samples: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=2000))

# В compute_stats для стоков без Binance:
if is_stock and agg.binance_book.mid is None:
    # Нет внешнего fair — честно отдаём None, не загрязняем spread_samples
    st.fair = None
    st.spread = None
    st.spread_bps = None
    st.z_score = 0.0
    st.external_fair_available = False
    # Записываем bid-ask спред в ОТДЕЛЬНЫЙ буфер для bb_revert
    if agg.mexc_book.bid is not None and agg.mexc_book.ask is not None:
        ba = (agg.mexc_book.ask - agg.mexc_book.bid)
        agg.mexc_ba_samples.append((now, ba))
    # ВАЖНО: не трогаем agg.spread_samples
else:
    st.external_fair_available = True
    # обычная логика...

# В opportunity.py:
if not getattr(st, "external_fair_available", True):
    # Запретить любые стратегии, основанные на cross-venue spread
    allowed_strategies = {"bb_revert"}
    strategies_to_try = strategies_to_try & allowed_strategies
```

### Коммит 2: entry latency + tick split (Bug B + Bug C)
- В `config.json` и `config.example.json`:
  - `"entry_latency_ms": 0`
  - добавить `"fast_tick_sec": 0.05`, `"slow_tick_sec": 1.0`
  - оставить `paper_tick_sec` как есть для paper-режима
- В `backend/real.py`: разбить `loop()` на `_fast_loop()` (reconcile_quotes,
  reconcile_positions, kill_switch — 50 мс) и `_slow_loop()` (balance refresh,
  equity log — 1 с).
- В `real.py` при старте: если `taker_entry=True` и `entry_latency_ms>0` —
  warning + force-set 0.

**Детали реализации:**
```python
async def loop(self) -> None:
    await self.init_balance()
    self._stop = asyncio.Event()
    fast = asyncio.create_task(self._fast_loop(), name="real_fast_loop")
    slow = asyncio.create_task(self._slow_loop(), name="real_slow_loop")
    try:
        await self._stop.wait()
    finally:
        for t in (fast, slow):
            t.cancel()
        await asyncio.gather(fast, slow, return_exceptions=True)

async def _fast_loop(self) -> None:
    """Hot path: SL/TP management, quote reconcile, kill switch. ~50ms."""
    interval = float(self.cfg.get("fast_tick_sec", 0.05))
    while not self._stop.is_set():
        t0 = time.perf_counter()
        try:
            await self._reconcile_quotes()
            await self._reconcile_positions()
            await self._check_kill_switch()
        except Exception:
            logger.exception("fast_loop tick error")
        elapsed = time.perf_counter() - t0
        sleep_for = max(0.0, interval - elapsed)
        await asyncio.sleep(sleep_for)

async def _slow_loop(self) -> None:
    """Cold path: balance refresh, equity logging, housekeeping. ~1s."""
    interval = float(self.cfg.get("slow_tick_sec", 1.0))
    while not self._stop.is_set():
        try:
            await self._refresh_balance_periodically()
            await self._log_equity_periodically()
            await self._cleanup_stale_state()
        except Exception:
            logger.exception("slow_loop tick error")
        await asyncio.sleep(interval)
```

### Коммит 3: query_order для fill detection (Bug D)
- В `mexc_trader.py`: добавить `async def query_order(self, order_id) -> dict`,
  если нет (web-uid эндпоинт для получения одного ордера).
- В `real.py::_is_quote_filled`: сначала пытаться `query_order(order_id)`,
  fallback на список позиций с TTL=0.5s.

**Детали реализации:**
```python
# В mexc_trader.py (если метода нет):
async def query_order(self, order_id: int) -> Dict[str, Any]:
    """Best-effort order state lookup."""
    return await self.api.get_order(int(order_id))

# В real.py::_is_quote_filled:
async def _is_quote_filled(self, q: _Quote) -> bool:
    # Сначала пробуем точечный запрос по order_id — он легче, чем список позиций
    if q.order_id:
        try:
            order = await self._trader.query_order(q.order_id)
            if order and order.get("state") in ("FILLED", "PARTIALLY_FILLED"):
                return True
            if order and order.get("state") in ("CANCELED", "REJECTED", "EXPIRED"):
                return False
        except Exception:
            logger.debug("query_order failed, falling back to positions", exc_info=True)
    # Fallback на кэшированный список позиций
    raw = await self._get_positions_raw_cached(max_age_sec=0.5)
    return self._position_matches_quote(raw, q)
```

### Коммит 4: reuse SymbolStats (Bug E)
- Изменить сигнатуру `_signal_valid_now` чтобы она возвращала
  `(ok: bool, reason: str, st: Optional[SymbolStats])`.
- В `_reconcile_quotes` использовать возвращённый `st` вместо повторного
  вызова `compute_stats`.

**Детали реализации:**
```python
def _signal_valid_now(self, symbol: str, side: str, ...) -> tuple[bool, str, Optional[SymbolStats]]:
    st = self.agg.compute_stats(symbol)
    if st is None:
        return False, "no_stats", None
    # ... вся валидация ...
    return True, "", st

# В _reconcile_quotes и везде где вызывается _signal_valid_now:
ok, reason, st = self._signal_valid_now(symbol, side, ...)
if ok and st is not None:
    # используем st вместо повторного compute_stats(symbol)
```

### Коммит 5: cache + Welford + drop sort (Bug F + Bug G)
- Добавить в `aggregator.py` класс `RollingWelford`.
- Заменить `spread_samples: Deque` на `spread_welford: RollingWelford(30.0)`.
  Все append → push, все вычисления mean/std через `.stats()`.
- Добавить кэш `compute_stats` в пределах 50 мс по book version.
- В `OrderBook` добавить `version: int = 0`, инкрементировать на каждом обновлении.
- В `on_mexc_depth` убрать `sorted()` и dict-конструкцию: MEXC `depth.full`
  уже отсортирован.
- В `_max_burst_in_window` убрать `.sort()` (deque упорядочен).

**Детали реализации:**
```python
class RollingWelford:
    """O(1) update, O(1) σ query for a time-windowed sample stream."""
    def __init__(self, window_sec: float):
        self.window_sec = window_sec
        self._buf: Deque[Tuple[float, float]] = deque()
        self._sum = 0.0
        self._sum_sq = 0.0
    
    def push(self, ts: float, x: float) -> None:
        self._buf.append((ts, x))
        self._sum += x
        self._sum_sq += x * x
        cutoff = ts - self.window_sec
        while self._buf and self._buf[0][0] < cutoff:
            _, old = self._buf.popleft()
            self._sum -= old
            self._sum_sq -= old * old
    
    def stats(self) -> Optional[Tuple[float, float, int]]:
        n = len(self._buf)
        if n < 2:
            return None
        mean = self._sum / n
        var = max(0.0, self._sum_sq / n - mean * mean)
        return mean, math.sqrt(var), n

# В _SymbolAgg:
spread_welford: RollingWelford = field(default_factory=lambda: RollingWelford(30.0))
_last_stats: Optional[SymbolStats] = None
_last_stats_ts: float = 0.0
_last_stats_book_ver: int = -1

# В OrderBook:
version: int = 0  # инкрементировать при каждом on_mexc_depth/on_binance_depth

# В compute_stats добавить кэш:
def compute_stats(self, symbol: str) -> Optional[SymbolStats]:
    agg = self._symbols.get(symbol)
    if not agg:
        return None
    cur_ver = agg.mexc_book.version + agg.binance_book.version
    now = time.time()
    # Книга не менялась и кэш свежий (< 50ms) — отдаём кэш
    if (agg._last_stats is not None 
        and cur_ver == agg._last_stats_book_ver 
        and (now - agg._last_stats_ts) < 0.05):
        return agg._last_stats
    st = self._compute_stats_impl(symbol, agg, now)
    agg._last_stats = st
    agg._last_stats_ts = now
    agg._last_stats_book_ver = cur_ver
    return st

# В on_mexc_depth убрать sorted():
def on_mexc_depth(self, mexc_symbol: str, bids, asks, ts: float) -> None:
    agg = self._symbols.get(mexc_symbol)
    if not agg:
        return
    try:
        b = [(float(p), float(q)) for p, q in (bids or [])[:50] if float(q) > 0]
        a = [(float(p), float(q)) for p, q in (asks or [])[:50] if float(q) > 0]
    except (TypeError, ValueError):
        return
    if not b or not a:
        return
    # MEXC depth.full уже отсортирован: bids desc, asks asc — не сортируем
    agg.mexc_book.bids = b
    agg.mexc_book.asks = a
    agg.mexc_book.ts = ts
    agg.mexc_book.version += 1
```

### Коммит 6: WS watchdog + MEXC heartbeat task (Bug H + Bug I)
- В `binance_ws.py`: поле `_last_msg_ts`, обновлять при каждом msg, отдельный
  task `_stall_watchdog` (sleep 5s; если silence > 10s → close & reconnect).
- В `mexc_ws.py`: то же + отдельный task `_heartbeat_loop`, шлющий
  `{"method":"ping"}` каждые 15 с независимо от входящего потока.
  Удалить старый inline `if now - last_ping > 15` блок.

**Детали реализации:**
```python
# В binance_ws.py:
self._last_msg_ts: float = 0.0

# В цикле приёма:
async for raw in ws:
    self._last_msg_ts = time.time()
    # ...обработка...

# В __init__ или start:
self._watchdog_task = asyncio.create_task(self._stall_watchdog())

async def _stall_watchdog(self) -> None:
    while not self._stop.is_set():
        await asyncio.sleep(5)
        if self._ws is not None and self._last_msg_ts > 0:
            silence = time.time() - self._last_msg_ts
            if silence > 10.0:
                logger.warning("binance_ws stalled (%.1fs no msg), forcing reconnect", silence)
                try:
                    await self._ws.close()
                except Exception:
                    pass

# В mexc_ws.py аналогично + heartbeat:
async def _heartbeat_loop(self) -> None:
    while not self._stop.is_set():
        await asyncio.sleep(15)
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(json.dumps({"method": "ping"}))
            except Exception:
                logger.debug("mexc_ws heartbeat send failed", exc_info=True)
```

### Коммит 7: stricter raw_momentum filters (Bug J + K)
- В `config.json` и `config.example.json`:
  ```json
  "signal_max_age_ms": 400,
  "raw_momentum_require_5s_agree": true,
  "raw_momentum_require_lag": true,
  "raw_momentum_min_lag_bps": 1.5,
  "raw_momentum_max_chase_bps": 3.0,
  "raw_momentum_anti_fade_30s_bps": 1.5
  ```
- Проверь что код в `opportunity.py` действительно читает эти флаги.

### Коммит 8: universe force_include fix (Bug L)
- В `backend/universe.py::refresh`: сначала безусловно добавить
  `force_include_symbols`, затем добавлять остальные с фильтрацией по
  Binance. Для каждого символа выставлять `external_fair_available`.

**Детали реализации:**
```python
async def refresh(self) -> List[str]:
    zero_fee = await self._fetch_zero_fee_list()
    binance_set = await self._fetch_binance_available()
    
    result: List[str] = []
    forced = set(self.cfg.get("force_include_symbols", []))
    
    # 1. Сначала добавляем force_include БЕЗУСЛОВНО
    for sym in forced:
        result.append(sym)
    
    # 2. Потом добавляем остальные из 0-fee, фильтруя по Binance
    require_bn = self.cfg.get("require_binance_ref", True)
    for sym in zero_fee:
        if sym in forced:
            continue
        if require_bn and self._to_binance(sym) not in binance_set:
            continue
        result.append(sym)
    
    return result
```

### Коммит 9: per-symbol buckets
- В `config.json` добавить секцию `symbol_buckets`:
  ```json
  "symbol_buckets": {
    "major_crypto": {
      "match": ["BTC_USDT", "ETH_USDT", "SOL_USDT", "BNB_USDT"],
      "entry_z": 1.6,
      "sigma_window_sec": 30,
      "min_spread_samples": 60
    },
    "alt_crypto": {
      "match": "*_USDT",
      "entry_z": 2.0,
      "sigma_window_sec": 45,
      "min_spread_samples": 80
    },
    "tokenized_stock": {
      "match": ["TSLA*", "NVDA*", "AAPL*", "*_TOKEN*"],
      "entry_z": 2.4,
      "sigma_window_sec": 60,
      "min_spread_samples": 100,
      "strategies": ["bb_revert"]
    }
  }
  ```
- В `backend/config.py` функция `resolve_symbol_params(symbol) -> dict`
  с glob/list-матчингом.
- Везде в `opportunity.py`/`real.py` где читался глобальный `entry_z`,
  `sigma_window_sec`, `min_spread_samples` — пропускать через
  `resolve_symbol_params`.

### Коммит 10: fair-cross exit with hysteresis
- В `real.py::_reconcile_positions` перед SL-ladder вставить блок проверки
  пересечения fair с учётом `exit_neutral_band_bps` и `min_hold_sec`.
- В `config.json`: `"exit_neutral_band_bps": 0.5, "min_hold_sec": 3.0`.

**Детали реализации:**
```python
# В _reconcile_positions добавить до SL-ладдера:
exit_band_bps = self.cfg.get("exit_neutral_band_bps", 0.5)
for pos in self._open_positions():
    st = self.agg.compute_stats(pos.symbol)
    if st is None or st.fair is None:
        continue
    cur_dev_bps = (pos.last_price - st.fair) / st.fair * 1e4
    # Лонг был открыт при отрицательном dev, ждём возврата к 0
    if pos.side == "BUY" and cur_dev_bps >= -exit_band_bps:
        if pos.holding_sec >= self.cfg.get("min_hold_sec", 3.0):
            await self._market_close(pos, reason="fair_cross")
            continue
    if pos.side == "SELL" and cur_dev_bps <= exit_band_bps:
        if pos.holding_sec >= self.cfg.get("min_hold_sec", 3.0):
            await self._market_close(pos, reason="fair_cross")
            continue
```

### Коммит 11: signal decision logging
- В `persistence.py` добавить таблицу `signal_decisions`:
  ```sql
  CREATE TABLE IF NOT EXISTS signal_decisions (
      ts REAL NOT NULL,
      symbol TEXT NOT NULL,
      side TEXT,
      strategy TEXT,
      z_score REAL,
      spread_bps REAL,
      fair REAL,
      mexc_mid REAL,
      decision TEXT NOT NULL,    -- 'accepted' | 'rejected'
      reason TEXT,
      age_ms REAL
  );
  CREATE INDEX IF NOT EXISTS idx_signal_decisions_ts ON signal_decisions(ts);
  CREATE INDEX IF NOT EXISTS idx_signal_decisions_symbol ON signal_decisions(symbol);
  ```
- В `_signal_valid_now` при любом отказе писать запись с reason.
- Добавить простой analytics-скрипт `scripts/analyze_rejections.py`:
  показать топ-10 reasons и их частоту за последние 24ч.

### Коммит 12: latency probe
- В каждом WS-колбэке запоминать `recv_ts`.
- При формировании сигнала логировать `binance_depth_age_ms`,
  `mexc_depth_age_ms`, `stats_compute_ms`, `decision_ms`.
- При submit/fill: `submit_latency_ms`, `fill_latency_ms`.
- В `persistence.py` таблица `latency_probe`:
  ```sql
  CREATE TABLE IF NOT EXISTS latency_probe (
      ts REAL NOT NULL,
      symbol TEXT NOT NULL,
      binance_depth_age_ms REAL,
      mexc_depth_age_ms REAL,
      stats_compute_ms REAL,
      decision_ms REAL,
      submit_latency_ms REAL,
      fill_latency_ms REAL
  );
  CREATE INDEX IF NOT EXISTS idx_latency_probe_ts ON latency_probe(ts);
  ```

### Коммит 13: rust_core skeleton
- Создать папку `rust_core/` с `Cargo.toml` и минимальным `src/main.rs`
  (`println!("botix_core skeleton ready")`).
- Содержимое Cargo.toml:
  ```toml
  [package]
  name = "botix_core"
  version = "0.1.0"
  edition = "2021"

  [dependencies]
  tokio = { version = "1", features = ["full"] }
  tokio-tungstenite = { version = "0.21", features = ["rustls-tls-webpki-roots"] }
  simd-json = "0.13"
  serde = { version = "1", features = ["derive"] }
  dashmap = "5"
  parking_lot = "0.12"
  tracing = "0.1"
  tracing-subscriber = "0.3"
  anyhow = "1"
  futures-util = "0.3"
  reqwest = { version = "0.12", default-features = false, features = ["rustls-tls", "json"] }

  [profile.release]
  lto = "fat"
  codegen-units = 1
  opt-level = 3
  panic = "abort"
  ```
- Создать `rust_core/README.md` с планом миграции: фаза 1 — WS консьюмеры
  MEXC+Binance с выдачей в Python через unix socket; фаза 2 — aggregator
  и stats; фаза 3 — исполнение.
- НЕ компилировать (не запускать `cargo build`) — у пользователя может не быть
  Rust toolchain. Только файлы.

### Коммит 14: README update
- В `README.md` добавить секцию "Recent changes (Pack 1)" с кратким описанием
  фиксов и какие конфиги ожидать.
- Добавить инструкцию по установке `secrets.json` (уже не актуально, но упомянуть
  что Fix #0 выполнен).

## После всех коммитов

1. Запусти `python -m pytest -x` (если тесты есть) — все должны проходить.
2. `python -c "from backend.config import load_config; load_config()"` —
   убедись что конфиг грузится без ошибок.
3. `python -m py_compile backend/*.py` — синтаксис всех модулей.
4. Сделай `git push origin fix/edge-and-latency-pack-1`.
5. Создай PR через `gh pr create --title "Edge & Latency Pack 1" --body "См. коммиты 1-14"`.

## Правила, которым следуй на каждом шаге
- Один пункт → один коммит с осмысленным message в стиле
  `fix(aggregator): stop contaminating spread_samples for stocks (Bug A)`.
- Не объединяй несвязанные изменения в один коммит.
- Если в реальном коде встретишь конфликт с моей спецификацией (например,
  поле называется по-другому, или логика уже частично есть) — следуй духу
  фикса, а не букве спецификации, и поясни решение в commit body.
- Если какой-то шаг невозможен без дополнительной информации от меня —
  пропусти его, отметь в commit message следующего шага и продолжай с
  остальным. В конце выведи список пропущенных пунктов.
- Не трогай `paper.py` глубоко — там 67KB и риск регрессии высокий. Только
  если изменения в shared-классах (например `_SymbolAgg`) требуют отражения.
- Не запускай ничего, что требует MEXC-ключей или сетевых вызовов к биржам.
