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
from .mexc_private_ws import MexcPrivateWS
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
        self.mexc_private_ws: Optional[MexcPrivateWS] = None

        self.executor = None  # PaperExecutor | RealExecutor

        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._scoring_task: Optional[asyncio.Task] = None

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
        await self.universe.refresh()
        await self._configure_universe(self.universe.working_set)

        # WS clients
        self.binance_ws = BinanceMultiWS(
            on_depth=self._on_binance_depth,
            on_trade=self._on_binance_trade,
        )
        self.mexc_ws = MexcMultiWS(on_depth=self._on_mexc_depth)
        self.mexc_private_ws = None
        if self.cfg.mode == "real" and self.cfg.mexc_web.web_uid.strip():
            self.mexc_private_ws = MexcPrivateWS(self.cfg.mexc_web.web_uid.strip())

        # Tasks
        binance_initial = [self.universe.reference_for(s) for s in self.universe.working_set if self.universe.reference_for(s)]
        self._tasks.append(asyncio.create_task(self.binance_ws.run(binance_initial), name="binance_ws"))
        self._tasks.append(asyncio.create_task(self.mexc_ws.run(self.universe.working_set), name="mexc_ws"))
        if self.mexc_private_ws is not None:
            self._tasks.append(asyncio.create_task(
                self.mexc_private_ws.run(self.universe.working_set),
                name="mexc_private_ws",
            ))
        self._tasks.append(asyncio.create_task(self.universe.loop(self._on_universe_change), name="universe"))
        self._tasks.append(asyncio.create_task(self._connectivity_watcher(), name="conn_watch"))
        if self._needs_mexc_fair_feed():
            self._tasks.append(asyncio.create_task(self._mexc_fair_loop(), name="mexc_fair"))

        # Auth ping (best-effort)
        if self.cfg.mexc_web.web_uid.strip():
            try:
                res = await self.trader.api.auth_ping()
                ok = bool(res.get("success"))
                async with self.state.lock:
                    self.state.mexc_auth_ok = ok
                    self.state.mexc_auth_msg = str(res.get("message") or "")
            except Exception as e:
                async with self.state.lock:
                    self.state.mexc_auth_ok = False
                    self.state.mexc_auth_msg = str(e)

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
        if self.mexc_private_ws:
            self.mexc_private_ws.stop()
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
            self.state.last_kill_reason = ""
            self.state.run_started_ts = time.time()
            self.state.session_trade_count = 0
            self.state.consecutive_losses = 0
            self.state.emergency_close_count = 0
            self.state.last_emergency_action = ""
            self.state.last_emergency_ts = 0.0
            self.state.auth_error_count = 0
            self.state.private_api_error_count = 0

        # Start executor before scoring so live balance/positions caches are warm
        # before the first tradeable candidate can be emitted.
        if self.executor is not None:
            self.executor._stop.clear()
            self._tasks.append(asyncio.create_task(self.executor.loop(), name="executor_loop"))

        if self._scoring_task is None or self._scoring_task.done():
            self._scoring_task = asyncio.create_task(self._scoring_loop(), name="scoring_loop")
            self._tasks.append(self._scoring_task)

        await self.state.add_log("info", "Engine: RUN")

    async def run_stop(self) -> None:
        async with self.state.lock:
            self.state.engine_running = False
        if self.executor:
            try:
                graceful_stop = getattr(self.executor, "graceful_stop", None)
                if callable(graceful_stop):
                    await graceful_stop("manual_stop")
                else:
                    self.executor.stop()
            except Exception:
                pass
        await self.state.add_log("info", "Engine: STOP")

    async def kill_all(self) -> Dict[str, Any]:
        """Cancel quotes and close all open positions."""
        async with self.state.lock:
            self.state.kill_switch = True
            self.state.last_kill_reason = "manual"
        if isinstance(self.executor, RealExecutor):
            try:
                await self.executor._emergency_flatten_all(reason="manual_kill")
                self.executor.stop()
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
        if mode == "real" and self.mexc_private_ws is None and self.cfg.mexc_web.web_uid.strip():
            self.mexc_private_ws = MexcPrivateWS(self.cfg.mexc_web.web_uid.strip())
            if self.universe is not None:
                self._tasks.append(asyncio.create_task(
                    self.mexc_private_ws.run(self.universe.working_set),
                    name="mexc_private_ws",
                ))
        elif mode != "real" and self.mexc_private_ws is not None:
            self.mexc_private_ws.stop()
            self.mexc_private_ws = None
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
            order_submit_path=str(getattr(self.cfg.mexc_web, "order_submit_path", "") or "legacy_submit"),
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
        if self.mexc_private_ws:
            await self.mexc_private_ws.update_symbols(working_set)
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
                mexc_private_ws=self.mexc_private_ws,
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

    def _needs_mexc_fair_feed(self) -> bool:
        mode = str(getattr(self.cfg.strategy, "fair_price_mode", "mid") or "mid").strip().lower()
        return mode in {"mexc_fair", "blend_mexc_fair"}

    async def _mexc_fair_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.trader is not None and self._needs_mexc_fair_feed():
                    poll_sec = max(0.3, float(getattr(self.cfg.strategy, "mexc_fair_poll_sec", 1.0) or 1.0))
                    fair_ttl = max(0.2, min(poll_sec, 1.0))
                    symbols = list(self.aggregator.symbols())
                    for sym in symbols:
                        if self._stop.is_set():
                            break
                        try:
                            fair = await self.trader.api.get_fair_price_cached(sym, ttl=fair_ttl)
                            if fair is not None and fair > 0:
                                self.aggregator.on_mexc_fair_price(sym, fair, time.time())
                        except Exception:
                            continue
                    await asyncio.sleep(poll_sec)
                    continue
            except Exception as e:
                logger.debug("mexc fair loop error: %s", e)
            await asyncio.sleep(1.0)

    # WS callbacks
    async def _on_binance_depth(self, sym: str, bids: list, asks: list, ts: float) -> None:
        self.aggregator.on_binance_depth(sym, bids, asks, ts)

    async def _on_binance_trade(self, sym: str, price: float, qty: float, buyer_is_maker: bool, ts: float) -> None:
        self.aggregator.on_binance_trade(sym, price, qty, buyer_is_maker, ts)

    async def _on_mexc_depth(self, sym: str, bids: list, asks: list, ts: float) -> None:
        self.aggregator.on_mexc_depth(sym, bids, asks, ts)

    def _override_for(self, symbol: str):
        """Return SymbolOverride for symbol, or None."""
        for ov in (self.cfg.symbol_overrides or []):
            if ov.symbol == symbol:
                return ov
        return None

    async def _scoring_loop(self) -> None:
        """Periodic scoring of all symbols + opportunity emission."""
        # rate-limit emissions per symbol
        last_emit_ts: Dict[str, float] = {}
        cleanup_last = 0.0
        while not self._stop.is_set():
            try:
                if not self.state.engine_running:
                    await asyncio.sleep(0.3)
                    continue
                if self.state.kill_switch:
                    # Risk-cap was tripped (daily loss / max DD / manual kill).
                    # Stop emitting new signals; in-flight positions still
                    # manage themselves through the executor's tick loop.
                    await asyncio.sleep(0.5)
                    continue

                # Score all symbols
                stats_dict = {}
                for sym in self.aggregator.symbols():
                    ov = self._override_for(sym)
                    # Skip disabled symbols
                    if ov is not None and not ov.enabled:
                        continue
                    st = self.aggregator.compute_stats(sym)
                    if ov is not None and ov.algorithms:
                        self.opportunity.evaluate_multi(sym, st, ov)
                    else:
                        self.opportunity.evaluate(sym, st)
                    stats_dict[sym] = st
                    async with self.state.lock:
                        self.state.stats[sym] = st

                # Rank for UI
                ranked = self.opportunity.rank(stats_dict)
                async with self.state.lock:
                    self.state.candidates = ranked

                # Emit signals (top candidates only) — once per N seconds per symbol
                now = time.time()
                emitted = 0
                for c in ranked:
                    if not c.get("side") or c.get("blocked") or float(c.get("score") or 0) <= 0:
                        continue
                    sym = c["symbol"]
                    last = last_emit_ts.get(sym, 0.0)
                    if now - last < 0.5:
                        continue
                    st = stats_dict.get(sym)
                    if not st:
                        continue
                    opp = self.opportunity.make_opportunity(sym, st)
                    if not opp:
                        continue
                    last_emit_ts[sym] = now

                    if self.executor is None:
                        try:
                            await self.store.insert_candidate(
                                now, sym, opp.side, opp.score, opp.z, st.spread_bps,
                                st.fair, st.mexc_mid, st.mexc_book_top10_notional,
                                None, accepted=True,
                            )
                        except Exception:
                            pass
                        # logger mode: just log
                        await self.state.add_log(
                            "info",
                            f"[logger] {sym} {opp.side} score={opp.score:.2f} z={opp.z:.2f}",
                        )
                        continue

                    try:
                        # Direct call — bypass signal queue for minimum latency
                        await self.executor.on_signal(opp)
                        accepted = True
                    except Exception as e:
                        logger.warning("executor.on_signal error: %s", e)
                        accepted = False
                    # Persist accepted candidates after the executor has seen the
                    # signal so analytics do not sit on the trade-critical path.
                    try:
                        await self.store.insert_candidate(
                            now, sym, opp.side, opp.score, opp.z, st.spread_bps,
                            st.fair, st.mexc_mid, st.mexc_book_top10_notional,
                            None, accepted=accepted,
                        )
                    except Exception:
                        pass
                    if accepted:
                        emitted += 1
                        if emitted >= 10:
                            break

                # Periodic cleanup
                if now - cleanup_last > 5.0:
                    cleanup_last = now
                    self.aggregator.cleanup_old_samples()

            except Exception as e:
                logger.exception("scoring loop error: %s", e)

            await asyncio.sleep(max(0.01, float(getattr(self.cfg.strategy, "paper_tick_sec", 0.2) or 0.2)))
