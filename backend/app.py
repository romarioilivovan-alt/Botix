from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config, save_config, SymbolOverride, normalize_symbol_name
from .engine import Engine
from .state import AppState


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"


cfg = load_config()
state = AppState()
engine = Engine(cfg, state)
_exchange_trade_cache: Dict[str, Any] = {"ts": 0.0, "symbols": tuple(), "items": []}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("=== LIFESPAN STARTUP ===", flush=True)
    await engine.start()
    asyncio.create_task(_ws_push_loop(), name="ws_push")
    print("=== LIFESPAN STARTUP COMPLETE ===", flush=True)
    yield
    # Shutdown
    print("=== LIFESPAN SHUTDOWN ===", flush=True)
    await engine.shutdown()


app = FastAPI(title="0fee Scanner Bot", version="0.2", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ---------------------------- API ----------------------------

def _decode_trade_extra(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _normalize_trade_row(row: Dict[str, Any]) -> Dict[str, Any]:
    extra = _decode_trade_extra(row.get("extra"))
    return {
        "ts": float(row.get("close_ts") or row.get("ts") or 0.0),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "entry": row.get("entry"),
        "exit": row.get("exit"),
        "pnl": row.get("pnl_usdt"),
        "pnl_pct": row.get("pnl_pct"),
        "reason": row.get("close_reason"),
        "duration": row.get("duration_sec"),
        "entry_latency_ms": extra.get("entry_latency_ms"),
        "exit_latency_ms": extra.get("exit_latency_ms"),
        "entry_algo": extra.get("entry_algo"),
        "entry_score": extra.get("entry_score"),
        "price_source": extra.get("price_source"),
    }


def _normalize_exchange_history_row(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        position_type = int(row.get("positionType") or 0)
    except Exception:
        position_type = 0
    side = "LONG" if position_type == 1 else "SHORT" if position_type == 2 else None

    def _as_float(key: str) -> float | None:
        val = row.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except Exception:
            return None

    open_ts = _exchange_ts(row.get("createTime"))
    close_ts = _exchange_ts(row.get("updateTime")) or open_ts
    entry = _as_float("openAvgPrice") or _as_float("holdAvgPrice")
    exit_price = _as_float("closeAvgPrice") or entry
    pnl = _as_float("realised")
    margin = _as_float("im") or _as_float("oim") or 0.0
    pnl_pct = (pnl / margin * 100.0) if (pnl is not None and margin and margin > 0) else None
    duration = max(0.0, close_ts - open_ts) if open_ts and close_ts else None
    return {
        "ts": close_ts or open_ts or 0.0,
        "symbol": row.get("symbol"),
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "reason": "exchange_history",
        "duration": duration,
        "price_source": "exchange_history",
    }


def _exchange_ts(raw: Any) -> float:
    try:
        ts = float(raw or 0.0)
    except Exception:
        return 0.0
    if ts <= 0:
        return 0.0
    if ts >= 1e11:
        ts /= 1000.0
    return ts


def _line_symbols() -> List[str]:
    syms = [str(sym or "").upper() for sym in (state.universe or []) if str(sym or "").strip()]
    if syms:
        return syms
    syms = [str(sym or "").upper() for sym in (cfg.universe.include_only or []) if str(sym or "").strip()]
    if syms:
        return syms
    return [
        str(getattr(ov, "symbol", "") or "").upper()
        for ov in (cfg.symbol_overrides or [])
        if getattr(ov, "enabled", True) and str(getattr(ov, "symbol", "") or "").strip()
    ]


async def _load_exchange_history_trades(limit: int = 100) -> List[Dict[str, Any]]:
    if cfg.mode != "real" or engine.trader is None:
        return []

    symbols = tuple(sorted(set(_line_symbols())))
    now = time.time()
    if (
        _exchange_trade_cache["items"]
        and _exchange_trade_cache["symbols"] == symbols
        and now - float(_exchange_trade_cache["ts"] or 0.0) < 5.0
    ):
        return list(_exchange_trade_cache["items"])

    rows: List[Dict[str, Any]] = []
    for sym in symbols:
        try:
            rows.extend(await engine.trader.get_history_positions(symbol_full=sym, limit=limit))
        except Exception as e:
            logging.getLogger(__name__).warning("exchange history fetch failed for %s: %s", sym, e)

    normalized = []
    for row in rows:
        if int(row.get("state") or 0) not in (0, 3):
            continue
        item = _normalize_exchange_history_row(row)
        if item.get("symbol") and item.get("side") and item.get("entry") and item.get("exit") is not None:
            normalized.append(item)
    normalized.sort(key=lambda x: float(x.get("ts") or 0.0))

    _exchange_trade_cache["ts"] = now
    _exchange_trade_cache["symbols"] = symbols
    _exchange_trade_cache["items"] = normalized[-limit:]
    return list(_exchange_trade_cache["items"])


def _merge_recent_trades(snapshot_items: List[Dict[str, Any]], persisted_rows: List[Dict[str, Any]], *, limit: int = 50) -> List[Dict[str, Any]]:
    merged: Dict[tuple[Any, ...], Dict[str, Any]] = {}

    def _key(item: Dict[str, Any]) -> tuple[Any, ...]:
        ts = float(item.get("ts") or 0.0)
        return (
            round(ts, 3),
            item.get("symbol"),
            item.get("side"),
            item.get("entry"),
            item.get("exit"),
        )

    def _merge_dicts(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(existing)
        for k, v in incoming.items():
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            out[k] = v
        return out

    for row in persisted_rows:
        if any(k in row for k in ("close_ts", "pnl_usdt", "extra")):
            item = _normalize_trade_row(row)
        else:
            item = dict(row)
        k = _key(item)
        merged[k] = _merge_dicts(merged.get(k, {}), item)
    for item in snapshot_items:
        k = _key(item)
        merged[k] = _merge_dicts(merged.get(k, {}), item)

    items = sorted(merged.values(), key=lambda x: float(x.get("ts") or 0.0))
    return items[-limit:]

@app.get("/api/state")
async def api_state(include_exchange_history: int = 0) -> JSONResponse:
    snap = await state.snapshot()
    try:
        mode = str((snap.get("engine") or {}).get("mode") or "").strip().lower() or None
        rows = await engine.store.list_trades(limit=200, mode=mode)
        merged = _merge_recent_trades(list(snap.get("recent_trades") or []), rows, limit=200)
        if include_exchange_history and (mode or cfg.mode) == "real":
            exchange_rows = await _load_exchange_history_trades(limit=100)
            snap["recent_trades"] = _merge_recent_trades(merged, exchange_rows, limit=50)
        else:
            snap["recent_trades"] = merged[-50:]
    except Exception as e:
        logging.getLogger(__name__).warning("api_state trade merge failed: %s", e)
    return JSONResponse(snap)


@app.get("/api/config")
async def api_config() -> JSONResponse:
    return JSONResponse(asdict(cfg))


@app.post("/api/config")
async def api_config_patch(patch: Dict[str, Any]) -> JSONResponse:
    """Shallow update of config sections; saves and re-applies safe parts."""
    def _merge(dc, data):
        for k, v in data.items():
            if not hasattr(dc, k):
                continue
            cur = getattr(dc, k)
            if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
                _merge(cur, v)
            else:
                try:
                    setattr(dc, k, v)
                except Exception:
                    pass
    _merge(cfg, patch)
    save_config(cfg)
    return JSONResponse({"success": True})


@app.get("/api/universe")
async def api_universe() -> JSONResponse:
    syms = list(state.universe)
    refs = dict(state.universe_refs)
    return JSONResponse({"size": len(syms), "symbols": syms, "refs": refs})


@app.post("/api/universe/refresh")
async def api_universe_refresh() -> JSONResponse:
    if engine.universe is None:
        return JSONResponse({"success": False, "message": "engine not started"})
    ws = await engine.universe.refresh()
    await engine._on_universe_change(ws)
    return JSONResponse({"success": True, "size": len(ws), "symbols": ws})


@app.get("/api/universe/available")
async def api_universe_available() -> JSONResponse:
    """All MEXC 0-fee symbols that also exist on Binance (the universe pool).

    `selected` reflects current cfg.universe.include_only (empty = trade all).
    `working` is what is actually subscribed right now.
    """
    if engine.universe is None:
        return JSONResponse({"available": [], "selected": [], "working": []})
    return JSONResponse({
        "available": engine.universe.available_pool,
        "selected": list(cfg.universe.include_only or []),
        "working": engine.universe.working_set,
    })


@app.post("/api/universe/selection")
async def api_universe_selection(payload: Dict[str, Any]) -> JSONResponse:
    """Save include_only list. Empty list (or omitted) means "trade all".
    Triggers a universe re-config to apply immediately."""
    raw = payload.get("symbols") if isinstance(payload, dict) else None
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return JSONResponse({"success": False, "message": "symbols must be a list"}, status_code=400)
    cleaned: List[str] = []
    for x in raw:
        s = str(x or "").upper().strip()
        if not s:
            continue
        s = normalize_symbol_name(s)
        cleaned.append(s)
    cfg.universe.include_only = cleaned
    save_config(cfg)
    if engine.universe is not None:
        ws = engine.universe.working_set
        await engine._on_universe_change(ws)
    return JSONResponse({"success": True, "selected": cleaned})


@app.get("/api/candidates")
async def api_candidates() -> JSONResponse:
    return JSONResponse({"items": list(state.candidates)})


@app.get("/api/positions")
async def api_positions() -> JSONResponse:
    out = []
    for p in state.positions.values():
        out.append({
            "symbol": p.symbol, "side": p.side, "entry": p.entry_price,
            "qty": p.qty, "notional": p.notional_usdt,
            "margin": p.margin_usdt, "lev": p.leverage,
            "stop": p.stop_price, "tp": p.tp_price,
            "open_ts": p.open_ts, "pnl": p.last_pnl_usdt, "pnl_pct": p.last_pnl_pct,
        })
    return JSONResponse({"items": out})


@app.get("/api/trades")
async def api_trades(limit: int = 200, mode: str = "") -> JSONResponse:
    rows = await engine.store.list_trades(limit=int(limit), mode=mode or None)
    items = [_normalize_trade_row(row) for row in reversed(rows)]
    if (mode or cfg.mode) == "real":
        try:
            items = _merge_recent_trades(items, await _load_exchange_history_trades(limit=max(int(limit), 100)), limit=int(limit))
        except Exception as e:
            logging.getLogger(__name__).warning("api_trades exchange merge failed: %s", e)
    return JSONResponse({"items": items})


@app.get("/api/stats")
async def api_stats(mode: str = "") -> JSONResponse:
    s = await engine.store.stats_summary(mode=mode or None)
    return JSONResponse(s)


@app.get("/api/equity")
async def api_equity(limit: int = 500, mode: str = "") -> JSONResponse:
    rows = await engine.store.list_equity(limit=int(limit), mode=mode or None)
    return JSONResponse({"items": rows})


@app.post("/api/run/start")
async def api_run_start() -> JSONResponse:
    await engine.run()
    return JSONResponse({"success": True})


@app.post("/api/run/stop")
async def api_run_stop() -> JSONResponse:
    await engine.run_stop()
    return JSONResponse({"success": True})


@app.post("/api/run/kill")
async def api_run_kill() -> JSONResponse:
    res = await engine.kill_all()
    return JSONResponse(res)


@app.post("/api/mode")
async def api_mode(payload: Dict[str, Any]) -> JSONResponse:
    mode = str(payload.get("mode") or "").strip().lower()
    res = await engine.set_mode(mode)
    return JSONResponse(res)


@app.get("/api/symbol-overrides")
async def api_symbol_overrides_get() -> JSONResponse:
    return JSONResponse({"items": [asdict(ov) for ov in (cfg.symbol_overrides or [])]})


@app.post("/api/symbol-overrides")
async def api_symbol_overrides_save(payload: Dict[str, Any]) -> JSONResponse:
    raw = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return JSONResponse({"success": False, "message": "items must be a list"}, status_code=400)
    parsed = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sym = normalize_symbol_name(item.get("symbol") or "")
        if not sym:
            continue
        ov = SymbolOverride(symbol=sym)
        for f in (
            "enabled", "leverage", "margin_pct", "sl_pct", "max_hold_sec",
            "cooldown_min_sec", "cooldown_max_sec",
            "allow_long", "allow_short",
            "min_entry_score", "min_lag_bps", "max_chase_bps",
            "anti_fade_30s_bps", "min_abs_spread_bps", "entry_latency_ms",
            "taker_ioc_price_buffer_bps", "taker_ioc_min_fill_ratio",
            "scalp_take_profit_bps", "scratch_exit_sec", "scratch_exit_bps",
            "use_fair_tp",
            "profit_protect_arm_bps", "profit_giveback_bps",
            "fast_profit_arm_bps", "fast_profit_giveback_bps",
            "profit_protect_min_bps", "edge_collapse_exit_bps",
            "edge_loss_after_sec", "edge_loss_exit_bps",
            "settled_profit_sec", "settled_profit_min_bps",
            "settled_profit_max_drift_bps", "settled_profit_edge_bps",
            "dead_trade_after_sec", "dead_trade_max_bps",
            "bad_entry_guard_sec", "bad_entry_min_age_sec",
            "bad_entry_spread_bps", "bad_entry_exit_bps",
            "algorithms", "algo_mode",
        ):
            if f in item:
                setattr(ov, f, item[f])
        parsed.append(ov)
    cfg.symbol_overrides = parsed
    save_config(cfg)
    return JSONResponse({"success": True, "count": len(parsed)})


@app.post("/api/symbol-overrides/{symbol}/toggle")
async def api_symbol_override_toggle(symbol: str) -> JSONResponse:
    symbol = normalize_symbol_name(symbol)
    found = None
    for ov in (cfg.symbol_overrides or []):
        if ov.symbol == symbol:
            found = ov
            break
    if found is None:
        found = SymbolOverride(symbol=symbol, enabled=False)
        cfg.symbol_overrides = list(cfg.symbol_overrides or []) + [found]
    else:
        found.enabled = not found.enabled
    save_config(cfg)
    return JSONResponse({"success": True, "symbol": symbol, "enabled": found.enabled})


# ---------------------------- WebSocket ----------------------------

class WSManager:
    def __init__(self) -> None:
        self.clients: List[WebSocket] = []
        self.lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.append(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self.lock:
            if ws in self.clients:
                self.clients.remove(ws)

    async def broadcast(self, msg: Dict[str, Any]) -> None:
        data = json.dumps(msg, ensure_ascii=False, default=str)
        async with self.lock:
            clients = list(self.clients)
        for ws in clients:
            try:
                await ws.send_text(data)
            except Exception:
                pass


ws_manager = WSManager()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    await ws_manager.add(ws)
    try:
        snap = await state.snapshot()
        await ws.send_text(json.dumps({"type": "snapshot", "data": snap}, ensure_ascii=False, default=str))
        while True:
            try:
                _ = await ws.receive_text()
            except WebSocketDisconnect:
                break
            except Exception:
                break
    finally:
        await ws_manager.remove(ws)


async def _ws_push_loop() -> None:
    while True:
        try:
            snap = await state.snapshot()
            await ws_manager.broadcast({"type": "snapshot", "data": snap})
        except Exception:
            pass
        await asyncio.sleep(0.5)
