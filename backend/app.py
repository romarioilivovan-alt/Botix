from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config, save_config, SymbolOverride
from .engine import Engine
from .state import AppState


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"


app = FastAPI(title="0fee Scanner Bot", version="0.2")

cfg = load_config()
state = AppState()
engine = Engine(cfg, state)


@app.on_event("startup")
async def on_startup() -> None:
    await engine.start()
    asyncio.create_task(_ws_push_loop(), name="ws_push")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await engine.shutdown()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ---------------------------- API ----------------------------

@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(await state.snapshot())


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
        if not s.endswith("_USDT") and "_" not in s:
            s = f"{s}_USDT"
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
    return JSONResponse({"items": rows})


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
    from dataclasses import asdict as _asdict
    return JSONResponse({"items": [_asdict(ov) for ov in (cfg.symbol_overrides or [])]})


@app.post("/api/symbol-overrides")
async def api_symbol_overrides_save(payload: Dict[str, Any]) -> JSONResponse:
    raw = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return JSONResponse({"success": False, "message": "items must be a list"}, status_code=400)
    parsed = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or "")
        if not sym:
            continue
        ov = SymbolOverride(symbol=sym)
        for f in ("enabled", "leverage", "margin_pct", "sl_pct",
                  "max_hold_sec", "algorithms", "algo_mode"):
            if f in item:
                setattr(ov, f, item[f])
        parsed.append(ov)
    cfg.symbol_overrides = parsed
    save_config(cfg)
    return JSONResponse({"success": True, "count": len(parsed)})


@app.post("/api/symbol-overrides/{symbol}/toggle")
async def api_symbol_override_toggle(symbol: str) -> JSONResponse:
    symbol = symbol.upper()
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
