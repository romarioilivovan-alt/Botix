# FluFlip — Design Spec
**Date:** 2026-05-05  
**Base:** mexc0feesflipper-main (copy + targeted improvements)  
**Goal:** Multi-symbol scalping bot, VPS-ready, remote dashboard, auto-SL, per-symbol config

---

## 1. Context & Goals

Reverse-engineered "Marik" bot (trading report analysis) achieves ~$94/hour on 7 symbols simultaneously. Our existing PEPE bot makes ~$3.7/hour on 1 symbol. Gap explained by: multi-symbol throughput, better asset selection, immediate SL orders, zero fees.

FluFlip combines mexc0feesflipper's clean architecture + Marik's operational insights into a single deployable bot.

**Success criteria:**
- Runs headless on Tokyo VPS, accessible via browser from any PC
- Trades 8 symbols in parallel (ENA, NVDA, MSTR, TAO, BCH, ZEC, TSLA, INTC)
- Places SL order automatically on every position open
- Per-symbol config (leverage, margin, SL%, strategy, enabled) via UI
- Multi-strategy mode per symbol (ANY / BEST / CONSENSUS)

---

## 2. Architecture

### 2.1 Base

Copy `mexc0feesflipper-main` → new folder `fluflip/`. Modify 6 files, leave 6 unchanged.

**Unchanged:** `aggregator.py`, `binance_ws.py`, `mexc_ws.py`, `models.py`, `allocator.py`, `persistence.py`

**Modified:** `engine.py`, `opportunity.py`, `mexc_trader.py`, `config.py`, `app.py`, `frontend/`

### 2.2 Data Flow

```
Binance WS (depth + aggTrade)  ─┐
                                 ├→ Aggregator → SymbolStats
MEXC WS (depth)                ─┘
                                          ↓
                               OpportunityEngine.evaluate_multi()
                               [runs all selected algos per symbol]
                               [applies ANY / BEST / CONSENSUS mode]
                                          ↓
                               Allocator (slot check, sizing)
                                          ↓
                               MexcTrader.open_market()
                                     → place_stop_by_position()  ← NEW: immediate SL
                                          ↓
                               Engine loop: trailing SL, time exit, signal-flip exit
```

### 2.3 Authentication

Web private API only (`web_uid`, `device_id`, `mhash`). No MEXC OpenAPI keys required. Same mechanism as both existing bots.

---

## 3. Trading Parameters

### 3.1 Global Defaults

| Parameter | Value | Notes |
|---|---|---|
| leverage | 100x fixed | Overridable per symbol |
| margin_pct_per_slot | 25% | $25 on $100 balance |
| max_concurrent_positions | 3 | Max 3 open at once |
| sl_pct_crypto | 0.25% | Price move → backstop only |
| sl_pct_stocks | 0.10% | Price move → backstop only |
| max_hold_sec | 300 | 5 min time exit |
| daily_loss_pct_kill | 15% | Kill switch |
| algo_mode | ANY | Default multi-strategy mode |

**SL rationale:** Median hold time from MEXC order history = 2s, max observed price move = 0.205%. SL at 0.25% is above observed max → fires only on bot crash/network loss. At 100x leverage: 0.25% = 25% margin loss (well before 1% liquidation).

### 3.2 Symbol Configuration

Starting symbols with per-symbol overrides:

| Symbol | Type | Enabled | Leverage | Margin% | SL% | Strategies | Mode |
|---|---|---|---|---|---|---|---|
| ENA_USDT | crypto | ✓ | 100 | 30% | 0.25% | meanrev, raw_momentum | ANY |
| NVDA_USDT | stock | ✓ | 100 | 25% | 0.10% | raw_momentum, ofi | ANY |
| MSTR_USDT | stock | ✓ | 100 | 25% | 0.10% | raw_momentum | ANY |
| TAO_USDT | crypto | ✓ | 100 | 25% | 0.25% | meanrev, raw_momentum | CONSENSUS |
| BCH_USDT | crypto | ✓ | 100 | 20% | 0.25% | meanrev | ANY |
| ZEC_USDT | crypto | ✓ | 100 | 20% | 0.25% | meanrev | ANY |
| TSLA_USDT | stock | ✓ | 100 | 20% | 0.10% | raw_momentum | ANY |
| INTC_USDT | stock | ✓ | 100 | 20% | 0.10% | raw_momentum | ANY |
| PENGU_USDT | crypto | ✗ | 50 | 15% | 0.30% | meanrev | ANY |

---

## 4. Code Changes

### 4.1 `config.py`

Add to `StrategyConfig`:
```python
sl_pct_crypto: float = 0.0025       # 0.25% backstop SL for crypto
sl_pct_stocks: float = 0.0010       # 0.10% backstop SL for stocks
algo_mode: str = "ANY"              # ANY | BEST | CONSENSUS
```

Add `SymbolOverride` dataclass:
```python
@dataclass
class SymbolOverride:
    symbol: str
    enabled: bool = True
    leverage: int | None = None
    margin_pct: float | None = None
    sl_pct: float | None = None
    max_hold_sec: int | None = None
    algorithms: list[str] | None = None  # None = use global
    algo_mode: str | None = None         # None = use global
```

Add to `AppConfig`:
```python
symbol_overrides: list[SymbolOverride] = field(default_factory=list)
```

### 4.2 `opportunity.py`

Add `evaluate_multi(stats, override)`:
- Runs `evaluate(stats, algo)` for each algo in `override.algorithms`
- Collects valid signals (score > 0, no blocked_reason)
- Applies mode logic:
  - **ANY**: enter if any algo fires (score > 0); return highest-score signal
  - **BEST**: enter only if highest score exceeds `min_score_threshold` (default 1.2); more selective than ANY
  - **CONSENSUS**: all algos must agree on same side; score = geometric mean; highest conviction entries

### 4.3 `mexc_trader.py`

Add `symbol_type(symbol) -> str`:
- Returns "stock" if symbol in STOCK_SYMBOLS set
- Returns "crypto" otherwise
- `STOCK_SYMBOLS = {"NVDA_USDT", "MSTR_USDT", "TSLA_USDT", "INTC_USDT", ...}`

Add `sl_pct_for(symbol, config) -> float`:
- Returns override.sl_pct if set, else config.sl_pct_stocks or sl_pct_crypto

Modify `place_stop_by_position()`:
- Called immediately after successful `open_market()` in engine
- Calculates SL price: `entry * (1 - sl_pct)` for LONG, `entry * (1 + sl_pct)` for SHORT
- Uses existing MEXC stop-plan API endpoint

Add `cancel_sl_for(symbol)`:
- Cancel pending SL order when bot closes position by its own logic
- Prevents orphan SL orders after bot-managed exit
- Implementation: engine maintains `_sl_order_ids: dict[str, str]` (symbol → order_id)
- `place_stop_by_position()` stores returned order_id; `cancel_sl_for()` reads and cancels it

### 4.4 `engine.py`

In executor loop, after successful open:
```python
position = await self.trader.open_market(...)
if position:
    sl_pct = self.trader.sl_pct_for(symbol, self.config)
    await self.trader.place_stop_by_position(position, sl_pct)
```

In executor loop, before close:
```python
await self.trader.cancel_sl_for(symbol)
await self.trader.close_market(...)
```

Respect `symbol_overrides` for leverage and margin when sizing via allocator:
- Pass override values to allocator decision.

### 4.5 `app.py`

- Bind: `host="0.0.0.0"` (VPS access from any IP)
- Add endpoints:
  - `GET /api/symbol-overrides` — list current overrides
  - `POST /api/symbol-overrides` — save overrides array
  - `POST /api/symbol-overrides/{symbol}/toggle` — enable/disable single symbol

### 4.6 `frontend/`

**index.html / app.js:**
- Positions table: add SL price column
- Candidates table: add filter tabs (All / Crypto / Stocks)
- New "Symbols" tab (replaces universe management):
  - Table with rows per symbol
  - Columns: Name, Type, Enabled toggle, Leverage, Margin%, SL%, Hold, Strategy pills, Mode dropdown, Historical PnL
  - Strategy pills: click to toggle on/off per symbol
  - Save button → POST /api/symbol-overrides
- Config panel: add sl_pct_crypto and sl_pct_stocks fields

---

## 5. Config File (config.json defaults)

```json
{
  "mexc": { "web_uid": "", "device_id": "", "mhash": "" },
  "universe": {
    "include_only": ["ENA_USDT","NVDA_USDT","MSTR_USDT","TAO_USDT",
                     "BCH_USDT","ZEC_USDT","TSLA_USDT","INTC_USDT","PENGU_USDT"],
    "require_binance_ref": true
  },
  "strategy": {
    "algorithm": "raw_momentum",
    "algo_mode": "ANY",
    "sl_pct_crypto": 0.0025,
    "sl_pct_stocks": 0.0010,
    "max_hold_sec": 300,
    "hard_sl_margin_pct": 0.25
  },
  "risk": {
    "max_concurrent_positions": 3,
    "margin_pct_per_slot": 0.25,
    "leverage_mode": "fixed",
    "fixed_leverage": 100,
    "daily_loss_pct_kill": 0.15
  },
  "app": { "mode": "paper", "autostart": false, "host": "0.0.0.0", "port": 8080 },
  "symbol_overrides": [
    {"symbol":"ENA_USDT","enabled":true,"margin_pct":0.30,"algorithms":["meanrev","raw_momentum"],"algo_mode":"ANY"},
    {"symbol":"NVDA_USDT","enabled":true,"sl_pct":0.0010,"algorithms":["raw_momentum","ofi"],"algo_mode":"ANY"},
    {"symbol":"MSTR_USDT","enabled":true,"sl_pct":0.0010,"algorithms":["raw_momentum"],"algo_mode":"ANY"},
    {"symbol":"TAO_USDT","enabled":true,"algorithms":["meanrev","raw_momentum"],"algo_mode":"CONSENSUS"},
    {"symbol":"BCH_USDT","enabled":true,"margin_pct":0.20,"algorithms":["meanrev"],"algo_mode":"ANY"},
    {"symbol":"ZEC_USDT","enabled":true,"margin_pct":0.20,"algorithms":["meanrev"],"algo_mode":"ANY"},
    {"symbol":"TSLA_USDT","enabled":true,"margin_pct":0.20,"sl_pct":0.0010,"algorithms":["raw_momentum"],"algo_mode":"ANY"},
    {"symbol":"INTC_USDT","enabled":true,"margin_pct":0.20,"sl_pct":0.0010,"algorithms":["raw_momentum"],"algo_mode":"ANY"},
    {"symbol":"PENGU_USDT","enabled":false,"leverage":50,"margin_pct":0.15,"sl_pct":0.0030,"algorithms":["meanrev"],"algo_mode":"ANY"}
  ]
}
```

---

## 6. VPS Deployment

- `start.sh`: `uvicorn backend.app:app --host 0.0.0.0 --port 8080`
- Access dashboard: `http://<vps-ip>:8080`
- No auth required (access control via firewall/IP whitelist on VPS)
- Recommend: `screen` or `systemd` service for persistence

---

## 7. What's NOT in scope

- Official MEXC OpenAPI keys
- Dashboard password protection
- Browser extension / web relay (not needed, uses direct web API)
- Rewriting aggregator, models, persistence, allocator
- PEPE-style ms-level entry confirmation (adds complexity, marginal gain for 2s-hold scalping)
