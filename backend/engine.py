"""Engine orchestrator.

Wires together: universe, multi-symbol WS, aggregator, opportunity scorer,
allocator, executor (paper or real), persistence. Manages the lifecycle.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, Dict, List, Optional

from .aggregator import Aggregator
from .allocator import CapitalAllocator
from .binance_ws import BinanceMultiWS
from .config import AppConfig, save_config
from .mexc_trader import MexcTrader
from .mexc_ws import MexcMultiWS
from .models import UserAccount
from .opportunity import OpportunityEngine
from .paper import PaperExecutor
from .persistence import Store
from .real import RealExecutor
from .state import AppState
from .universe import UniverseManager, to_binance


logger = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: AppConfig, state: AppState) -> None:
        self.cfg = cfg
        self.state = state

        self.store = Store()
        self.trader: Optional[MexcTrader] = None
        self.universe: Optional[UniverseManager] = None
        self.aggregator = Aggregator(cfg)
        self.opportunity = OpportunityEngine(cfg)
        self.allocator = CapitalAllocator(cfg)

        self.binance_ws: Optional[BinanceMultiWS] = None
        self.mexc_ws: Optional[MexcMultiWS] = None

        self.executor = None  # PaperExecutor | RealExecutor

        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._scoring_task: Optional[asyncio.Task] = None

        # Event-driven scoring: set of symbols needing re-score, woken by WS
        self._score_one: set[str] = set()
        self._score_wake: Optional[asyncio.Event] = None
        self._last_emit_ts: Dict[str, float] = {}

    # ------------------------- lifecycle -------------------------

    async def start(self) -> None:
        self._stop.clear()
        await self.store.open()
        await self._ensure_trader()

        self.universe = UniverseManager(self.trader, self.cfg)
        self.state.engine_mode = self.cfg.mode
        self.state.engine_running = False
        self.state.kill_switch = False

        # Compute first universe set up-front so we have something to subscribe to.
        logger.info("=== Calling universe.refresh() ===")
        await self.universe.refresh()
        ws = self.universe.working_set
        logger.info(f"=== universe.working_set = {ws} ({len(ws)} symbols) ===")
        await self._configure_universe(ws)
        logger.info(f"=== aggregator.symbols() = {self.aggregator.symbols()} ===")


        # WS clients
        self.binance_ws = BinanceMultiWS(
            on_depth=self._on_binance_depth,
            on_trade=self._on_binance_trade,
        )
        self.mexc_ws = MexcMultiWS(on_depth=self._on_mexc_depth)

        # Tasks with exception handling
        async def safe_task(coro, name: str):
            try:
                await coro
            except Exception as e:
                logger.exception(f"Task {name} failed: {e}")

        binance_initial = [self.universe.reference_for(s) for s in self.universe.working_set if self.universe.reference_for(s)]
        logger.info(f"Starting binance_ws with {len(binance_initial)} symbols: {binance_initial}")
        self._tasks.append(asyncio.create_task(safe_task(self.binance_ws.run(binance_initial), "binance_ws"), name="binance_ws"))
        logger.info(f"Starting mexc_ws with {len(self.universe.working_set)} symbols: {self.universe.working_set}")
        self._tasks.append(asyncio.create_task(safe_task(self.mexc_ws.run(self.universe.working_set), "mexc_ws"), name="mexc_ws"))
        self._tasks.append(asyncio.create_task(safe_task(self.universe.loop(self._on_universe_change), "universe"), name="universe"))
        self._tasks.append(asyncio.create_task(safe_task(self._connectivity_watcher(), "conn_watch"), name="conn_watch"))

        # Auth ping (best-effort)
        if self.cfg.mexc_web.web_uid.strip():
            try:
                # Preconnect warms TCP+TLS sockets so the first trade is fast.
                await self.trader.api.preconnect()
                res = await self.trader.api.auth_ping()
                ok = bool(res.get("success"))
                async with self.state.lock:
                    self.state.mexc_auth_ok = ok
                    self.state.mexc_auth_msg = str(res.get("message") or "")
            except Exception as e:
                async with self.state.lock:
                    self.state.mexc_auth_ok = False
                    self.state.mexc_auth_msg = str(e)

        # Keep MEXC HTTP connection warm with a periodic public ping so the
        # pool never goes cold between trades (saves ~80ms cold handshake).
        self._tasks.append(asyncio.create_task(self._http_keepalive_loop(), name="http_keepalive"))

        # Executor
        await self._configure_executor()

        await self.state.add_log("info", f"Engine started in mode={self.cfg.mode}")

        if self.cfg.autostart:
            await self.run()

    async def shutdown(self) -> None:
        self._stop.set()
        await self.run_stop()
        if self.binance_ws:
            self.binance_ws.stop()
        if self.mexc_ws:
            self.mexc_ws.stop()
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
        if self.trader:
            try:
                await self.trader.close()
            except Exception:
                pass
        await self.store.close()

    # ------------------------- modes -------------------------

    async def run(self) -> None:
        if self.state.engine_running:
            return
        async with self.state.lock:
            self.state.engine_running = True
            self.state.kill_switch = False

        # Start scoring + executor loops
        if self._scoring_task is None or self._scoring_task.done():
            self._scoring_task = asyncio.create_task(self._scoring_loop(), name="scoring_loop")
            self._tasks.append(self._scoring_task)

        if self.executor is not None:
            self.executor._stop.clear()
            self._tasks.append(asyncio.create_task(self.executor.loop(), name="executor_loop"))

        await self.state.add_log("info", "Engine: RUN")

    async def run_stop(self) -> None:
        async with self.state.lock:
            self.state.engine_running = False
        if self.executor:
            try:
                self.executor.stop()
            except Exception:
                pass
        await self.state.add_log("info", "Engine: STOP")

    async def kill_all(self) -> Dict[str, Any]:
        """Cancel quotes and close all open positions."""
        async with self.state.lock:
            self.state.kill_switch = True
            self.state.last_kill_reason = "manual"
        # Try to close positions on exchange (real mode); paper closes naturally on next tick.
        if isinstance(self.executor, RealExecutor) and self.trader is not None:
            try:
                raw_list = await self.trader.get_positions_raw()
                allowed_symbols = {
                    str(sym or "").upper()
                    for sym in (self.state.universe or [])
                    if str(sym or "").strip()
                }
                managed_symbols = {
                    str(sym or "").upper()
                    for sym in self.state.positions.keys()
                    if str(sym or "").strip()
                }
                target_symbols = managed_symbols or allowed_symbols
                scoped_raw = []
                for p in raw_list:
                    sym = str(p.get("symbol") or "").upper()
                    if target_symbols and sym not in target_symbols:
                        continue
                    scoped_raw.append(p)
                    side = "LONG" if int(p.get("positionType") or 0) == 1 else "SHORT"
                    hold = float(p.get("holdVol") or 0.0)
                    lev = int(float(p.get("leverage") or 1))
                    if hold > 0:
                        try:
                            await self.trader.close_market(sym, side, hold, lev)
                        except Exception:
                            pass
                # cancel all open orders
                for sym in {str(p.get("symbol") or "").upper() for p in scoped_raw}:
                    try:
                        await self.trader.cancel_all_for(sym)
                    except Exception:
                        pass
            except Exception as e:
                await self.state.add_log("error", f"kill_all error: {e}")
        await self.state.add_log("warn", "KILL ALL invoked")
        return {"success": True}

    async def set_mode(self, mode: str) -> Dict[str, Any]:
        mode = mode.strip().lower()
        if mode not in ("paper", "real", "logger"):
            return {"success": False, "message": "mode must be paper|real|logger"}
        await self.run_stop()
        self.cfg.mode = mode
        save_config(self.cfg)
        await self._configure_executor()
        async with self.state.lock:
            self.state.engine_mode = mode
        await self.state.add_log("info", f"Mode switched to {mode}")
        return {"success": True}

    # ------------------------- internals -------------------------

    async def _ensure_trader(self) -> None:
        uid = (self.cfg.mexc_web.web_uid or "").strip()
        did = (self.cfg.mexc_web.device_id or "").strip()
        if uid and not did:
            did = hashlib.md5(uid.encode("utf-8")).hexdigest()
            self.cfg.mexc_web.device_id = did
            save_config(self.cfg)

        mhash = (self.cfg.mexc_web.mhash or "").strip()
        if uid and did:
            derived = hashlib.md5(f"{uid}{did}".encode("utf-8")).hexdigest()
            if mhash != derived:
                mhash = derived
                self.cfg.mexc_web.mhash = derived
                save_config(self.cfg)

        if self.trader is not None:
            try:
                await self.trader.close()
            except Exception:
                pass

        acc = UserAccount(
            uid=uid, device_id=did, mhash=mhash,
            proxy=self.cfg.mexc_web.proxy,
        )
        self.trader = MexcTrader(acc, proxy=self.cfg.mexc_web.proxy)

    async def _configure_universe(self, working_set: List[str]) -> None:
        m2b: Dict[str, Optional[str]] = {}
        factors: Dict[str, float] = {}
        contract_sizes: Dict[str, float] = {}
        for sym in working_set:
            ref = self.universe.reference_for(sym) if self.universe else None
            m2b[sym] = ref
            if self.universe:
                factors[sym] = self.universe.price_factor_for(sym)
            size = 1.0
            if self.trader is not None:
                try:
                    detail = await self.trader.get_contract_detail(sym)
                    size = float((detail or {}).get("contractSize") or 1.0)
                except Exception:
                    size = 1.0
            contract_sizes[sym] = size if size > 0 else 1.0
        self.aggregator.configure_symbols(
            m2b,
            price_factors=factors,
            contract_sizes=contract_sizes,
        )
        async with self.state.lock:
            self.state.universe = list(working_set)
            self.state.universe_refs = {k: v for k, v in m2b.items() if v}

    async def _on_universe_change(self, working_set: List[str]) -> None:
        await self._configure_universe(working_set)
        if self.binance_ws:
            await self.binance_ws.update_symbols(
                [self.universe.reference_for(s) for s in working_set if self.universe and self.universe.reference_for(s)]
            )
        if self.mexc_ws:
            await self.mexc_ws.update_symbols(working_set)
        await self.state.add_log("info", f"universe -> {len(working_set)} symbols")

    async def _configure_executor(self) -> None:
        if self.executor is not None:
            try:
                self.executor.stop()
            except Exception:
                pass
        if self.cfg.mode == "real":
            if not self.trader:
                await self._ensure_trader()
            self.executor = RealExecutor(
                self.cfg, self.state, self.aggregator,
                self.opportunity, self.allocator, self.store, self.trader,
            )
        elif self.cfg.mode == "logger":
            self.executor = None
        else:
            if not self.trader:
                await self._ensure_trader()
            self.executor = PaperExecutor(
                self.cfg, self.state, self.aggregator,
                self.opportunity, self.allocator, self.store,
                mexc_trader=self.trader,
            )

    async def _connectivity_watcher(self) -> None:
        while not self._stop.is_set():
            try:
                async with self.state.lock:
                    self.state.binance_ws_ok = bool(self.binance_ws and self.binance_ws.is_connected)
                    self.state.mexc_ws_ok = bool(self.mexc_ws and self.mexc_ws.is_connected)
            except Exception:
                pass
            await asyncio.sleep(1.0)

    async def _http_keepalive_loop(self) -> None:
        """Periodically hit a cheap MEXC public endpoint so the TCP+TLS
        pool stays warm. Without this, an idle socket is dropped after
        ~60s and the next order pays for a fresh handshake (~80ms)."""
        while not self._stop.is_set():
            # Interval must be shorter than keepalive_timeout (120s).
            await asyncio.sleep(45.0)
            if self.trader is None:
                continue
            try:
                await self.trader.api._request_market("api/v1/contract/ping")
            except Exception:
                pass

    # WS callbacks
    async def _on_binance_depth(self, sym: str, bids: list, asks: list, ts: float) -> None:
        self.aggregator.on_binance_depth(sym, bids, asks, ts)
        # Event-driven scoring: trigger immediately on fresh Binance data
        # for the specific symbol. This is ~100ms faster than polling with
        # paper_tick_sec=0.2s because we don't wait for the next tick.
        mexc_sym = self.aggregator.mexc_symbol_for_binance(sym)
        if mexc_sym:
            self._score_one.add(mexc_sym)
            # Wake the scoring loop if it is asleep
            if self._score_wake is not None and not self._score_wake.is_set():
                self._score_wake.set()

    async def _on_binance_trade(self, sym: str, price: float, qty: float, buyer_is_maker: bool, ts: float) -> None:
        self.aggregator.on_binance_trade(sym, price, qty, buyer_is_maker, ts)

    async def _on_mexc_depth(self, sym: str, bids: list, asks: list, ts: float) -> None:
        self.aggregator.on_mexc_depth(sym, bids, asks, ts)
        # Fresh MEXC top-of-book also matters for lag/chase filters.
        self._score_one.add(sym)
        if self._score_wake is not None and not self._score_wake.is_set():
            self._score_wake.set()

    def _override_for(self, symbol: str):
        """Return SymbolOverride for symbol, or None."""
        for ov in (self.cfg.symbol_overrides or []):
            if ov.symbol == symbol:
                return ov
        return None

    async def _scoring_loop(self) -> None:
        """Event-driven scoring: triggered on fresh WS data, not polling.

        Score and emit signals within ~1-2ms of a Binance/MEXC depth update,
        instead of waiting up to paper_tick_sec (200ms) for the next tick.
        This was the single largest source of entry latency before.
        """
        if self._score_wake is None:
            self._score_wake = asyncio.Event()
        cleanup_last = 0.0
        rank_last = 0.0
        logger.info("=== SCORING LOOP STARTED (event-driven) ===")

        while not self._stop.is_set():
            try:
                if not self.state.engine_running or self.state.kill_switch:
                    # Drain any pending work quickly then sleep longer
                    self._score_one.clear()
                    self._score_wake.clear()
                    await asyncio.sleep(0.2)
                    continue

                # Wait either for a WS-triggered score request, or a 100ms
                # watchdog so that stats/UI keep updating even when no depth
                # pushes arrive (rare but happens during flat periods).
                try:
                    await asyncio.wait_for(self._score_wake.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
                self._score_wake.clear()

                # Snapshot the symbols that need scoring and reset the bag
                pending = list(self._score_one)
                self._score_one.clear()

                now = time.time()

                # If we came from a watchdog with no pending symbols, do a
                # light housekeeping pass: update candidate ranking once a
                # second for the UI, then continue waiting.
                if not pending:
                    if now - rank_last > 1.0:
                        rank_last = now
                        await self._refresh_rank_snapshot()
                    continue

                # Score ONLY the symbols that changed. This is the hot path.
                for sym in pending:
                    ov = self._override_for(sym)
                    if ov is not None and not ov.enabled:
                        continue
                    st = self.aggregator.compute_stats(sym)
                    if ov is not None and ov.algorithms:
                        self.opportunity.evaluate_multi(sym, st, ov)
                    else:
                        self.opportunity.evaluate(sym, st)

                    # Publish stats for UI without holding the lock long
                    self.state.stats[sym] = st

                    # Fire a signal immediately if it qualifies
                    if (
                        st.side_hint
                        and not st.blocked_reason
                        and float(st.score or 0.0) > 0.0
                    ):
                        last = self._last_emit_ts.get(sym, 0.0)
                        if now - last >= 0.5:
                            opp = self.opportunity.make_opportunity(sym, st)
                            if opp is not None:
                                self._last_emit_ts[sym] = now
                                if self.executor is None:
                                    try:
                                        await self.store.insert_candidate(
                                            now, sym, opp.side, opp.score, opp.z, st.spread_bps,
                                            st.fair, st.mexc_mid, st.mexc_book_top10_notional,
                                            None, accepted=True,
                                        )
                                    except Exception:
                                        pass
                                    await self.state.add_log(
                                        "info",
                                        f"[logger] {sym} {opp.side} score={opp.score:.2f} z={opp.z:.2f}",
                                    )
                                else:
                                    try:
                                        await self.executor.on_signal(opp)
                                    except Exception as e:
                                        logger.warning("executor.on_signal error: %s", e)
                                    # Persist off hot path
                                    try:
                                        await self.store.insert_candidate(
                                            now, sym, opp.side, opp.score, opp.z, st.spread_bps,
                                            st.fair, st.mexc_mid, st.mexc_book_top10_notional,
                                            None, accepted=True,
                                        )
                                    except Exception:
                                        pass

                # Refresh UI ranking at most 1Hz
                if now - rank_last > 1.0:
                    rank_last = now
                    await self._refresh_rank_snapshot()

                # Periodic cleanup
                if now - cleanup_last > 5.0:
                    cleanup_last = now
                    self.aggregator.cleanup_old_samples()

            except Exception as e:
                logger.exception("scoring loop error: %s", e)
                await asyncio.sleep(0.05)

    async def _refresh_rank_snapshot(self) -> None:
        """Rebuild the full candidate ranking for the UI. Called at ~1Hz,
        off the trade-critical path."""
        stats_dict: Dict[str, Any] = {}
        for sym in self.aggregator.symbols():
            st = self.state.stats.get(sym)
            if st is None:
                st = self.aggregator.compute_stats(sym)
                self.state.stats[sym] = st
            stats_dict[sym] = st
        ranked = self.opportunity.rank(stats_dict)
        self.state.candidates = ranked

