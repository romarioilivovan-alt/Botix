"""Real-money executor for MEXC.

Mirrors the paper executor but talks to MEXC. Places maker limit entries,
attaches exchange-side TP/SL stop-plan orders, and updates SL via
change_stop_plan_price as the price moves toward fair value.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import random
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Dict, Optional, Set, Tuple

from .aggregator import Aggregator
from .allocator import CapitalAllocator, AllocationDecision
from .opportunity import Opportunity, OpportunityEngine
from .paper import (
    _hard_sl_price_fraction,
    _realisable_exit_price,
    _realized_bps_at_price,
    _residual_edge_bps,
    _should_bad_entry_exit,
    _should_profit_protect_exit,
    _trail_long,
    _trail_short,
    _update_settled_profit_state,
)
from .persistence import Store
from .state import AppState, ManagedPosition

STOCK_SYMBOLS = frozenset({
    "NVIDIA_USDT",
    "MSTRSTOCK_USDT",
    "TSLA_USDT",
    "INTC_USDT",
    "NVDA_USDT",
    "MSTR_USDT",
})

logger = logging.getLogger(__name__)


@dataclass
class _Quote:
    symbol: str
    side: str
    price: float
    notional: float
    margin: float
    leverage: int
    placed_ts: float
    fair_at_quote: float
    sigma_at_quote: float
    z_at_quote: float
    taker_submit_at: Optional[float] = None
    order_id: Optional[int] = None
    signal_ts: float = 0.0
    entry_algo: Optional[str] = None
    entry_score: float = 0.0
    spread_bps_at_quote: Optional[float] = None
    requested_vol: Optional[float] = None
    fill_ratio: Optional[float] = None
    submit_started_ts: float = 0.0
    submit_ack_ts: float = 0.0
    submit_latency_ms: float = 0.0
    fill_seen_ts: float = 0.0
    submit_in_flight: bool = False
    last_order_query_ts: float = 0.0
    is_ioc: bool = False
    order_mode: str = ""
    confirmation_deadline_ts: float = 0.0
    materialize_in_flight: bool = False
    final_vol: Optional[float] = None
    avg_entry_price: Optional[float] = None
    synthetic_pos_raw: Optional[Dict[str, Any]] = None


def _snap_price(price: float, tick: Decimal, side: str, *, for_sl: bool) -> Tuple[float, str]:
    """Round price to exchange tick. For SL: LONG rounds down, SHORT rounds up.
    For limit ENTRY: LONG (BUY) rounds DOWN, SHORT (SELL) rounds UP, to be safer maker.
    """
    d = Decimal(str(price))
    rounding = ROUND_DOWN if side.upper() == "LONG" else ROUND_UP
    snapped = (d / tick).to_integral_value(rounding=rounding) * tick
    s = format(snapped, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return float(snapped), s


class RealExecutor:
    def __init__(self, cfg, state: AppState, agg: Aggregator,
                 opp: OpportunityEngine, alloc: CapitalAllocator,
                 store: Store, mexc_trader, mexc_private_ws=None) -> None:
        self.cfg = cfg
        self.state = state
        self.agg = agg
        self.opp = opp
        self.alloc = alloc
        self.store = store
        self.trader = mexc_trader
        self.mexc_private_ws = mexc_private_ws

        self._quotes: Dict[str, _Quote] = {}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._equity_log_last_ts = 0.0
        self._balance_refresh_last_ts = 0.0
        self._max_lev_cache: Dict[str, int] = {}
        self._raw_missing_counts: Dict[str, int] = {}
        self._raw_missing_first_ts: Dict[str, float] = {}
        self._positions_refresh_error_last_ts = 0.0
        self._foreign_position_warned: Set[str] = set()
        self._positions_cache_ts = 0.0
        self._positions_cache_raw: list[Dict[str, Any]] = []
        self._positions_force_refresh_ts = 0.0
        self._positions_rate_limited_until = 0.0
        self._positions_rate_limit_count = 0
        self._positions_rate_limit_last_log_ts = 0.0
        self._private_api_error_streak = 0
        self._auth_error_streak = 0
        self._stale_data_started_ts = 0.0
        self._emergency_close_in_progress = False
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_cleanup_done = False
        self._closed_position_keys: Set[str] = set()
        self._stop_attach_in_flight: Set[str] = set()
        self._external_close_history_retry_delays: Tuple[float, ...] = (0.0, 0.25, 0.75, 1.50, 3.00)
        self._external_close_fallback_min_missing_sec = 8.0
        self._execution_events: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=5000)
        self._execution_event_drops = 0
        self._execution_telemetry_task: Optional[asyncio.Task] = None
        self._signal_queue: asyncio.Queue[Tuple[Opportunity, float]] = asyncio.Queue(
            maxsize=int(getattr(getattr(self.cfg, "strategy", None), "live_signal_queue_max", 64) or 64)
        )
        self._signal_worker_task: Optional[asyncio.Task] = None
        self._signal_worker_busy = False
        self._queued_signal_symbols: Set[str] = set()
        self._signal_queue_drops = 0

    def stop(self) -> None:
        self._stop.set()

    def _book_age_fields(self, symbol: Optional[str]) -> Dict[str, Optional[float]]:
        if not symbol:
            return {"mexc_book_age_ms": None, "binance_book_age_ms": None, "mexc_mid": None}
        now = time.time()
        mexc_age = None
        binance_age = None
        mexc_mid = None
        try:
            book = self.agg.get_book(symbol)
            if book is not None:
                mexc_mid = book.mid
                ts = float(getattr(book, "ts", 0.0) or 0.0)
                if ts > 0:
                    mexc_age = max(0.0, (now - ts) * 1000.0)
        except Exception:
            pass
        try:
            bbook = self.agg.get_binance_book(symbol)
            if bbook is not None:
                ts = float(getattr(bbook, "ts", 0.0) or 0.0)
                if ts > 0:
                    binance_age = max(0.0, (now - ts) * 1000.0)
        except Exception:
            pass
        return {
            "mexc_book_age_ms": mexc_age,
            "binance_book_age_ms": binance_age,
            "mexc_mid": mexc_mid,
        }

    def _queue_execution_event(
        self,
        phase: str,
        *,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        reason: Optional[str] = None,
        quote: Optional[_Quote] = None,
        pos: Optional[ManagedPosition] = None,
        signal_ts: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
        **fields: Any,
    ) -> None:
        try:
            now = time.time()
            if quote is not None:
                symbol = symbol or quote.symbol
                side = side or quote.side
                fields.setdefault("order_mode", quote.order_mode or ("ioc" if quote.is_ioc else "quote"))
                fields.setdefault("order_id", quote.order_id)
                fields.setdefault("fair", quote.fair_at_quote)
                fields.setdefault("price", quote.price)
                fields.setdefault("notional", quote.notional)
                fields.setdefault("fill_ratio", quote.fill_ratio)
                fields.setdefault("entry_spread_bps", quote.spread_bps_at_quote)
                signal_ts = signal_ts if signal_ts is not None else quote.signal_ts
                if quote.submit_ack_ts > 0 and quote.submit_started_ts > 0:
                    fields.setdefault("submit_ack_ms", max(0.0, (quote.submit_ack_ts - quote.submit_started_ts) * 1000.0))
                if quote.fill_seen_ts > 0 and quote.submit_ack_ts > 0:
                    fields.setdefault("fill_delay_ms", max(0.0, (quote.fill_seen_ts - quote.submit_ack_ts) * 1000.0))
            if pos is not None:
                symbol = symbol or pos.symbol
                side = side or pos.side
                fields.setdefault("position_id", pos.mexc_position_id)
                fields.setdefault("fair", pos.fair_at_open)
                fields.setdefault("price", pos.entry_price)
                fields.setdefault("notional", pos.notional_usdt)
                fields.setdefault("fill_ratio", pos.entry_fill_ratio)
                fields.setdefault("entry_spread_bps", pos.entry_spread_bps)
                fields.setdefault("submit_ack_ms", pos.submit_latency_ms)
                fields.setdefault("fill_delay_ms", pos.fill_seen_latency_ms)
                fields.setdefault("managed_delay_ms", pos.managed_latency_ms)
                fields.setdefault("close_submit_ms", pos.close_submit_latency_ms)
                fields.setdefault("close_ack_to_closed_ms", pos.close_ack_to_closed_ms)
                signal_ts = signal_ts if signal_ts is not None else pos.signal_ts
            if signal_ts and signal_ts > 0:
                fields.setdefault("signal_age_ms", max(0.0, (now - float(signal_ts)) * 1000.0))
            ages = self._book_age_fields(symbol)
            for key, value in ages.items():
                fields.setdefault(key, value)
            private_ws = getattr(self, "mexc_private_ws", None)
            row: Dict[str, Any] = {
                "ts": now,
                "mode": "real",
                "symbol": symbol,
                "side": side,
                "phase": phase,
                "reason": reason,
                "private_ws_connected": bool(getattr(private_ws, "is_connected", False)) if private_ws is not None else False,
                "auth_error_streak": self._auth_error_streak,
                "extra": extra or {},
            }
            row.update(fields)
            self._execution_events.put_nowait(row)
        except asyncio.QueueFull:
            self._execution_event_drops += 1
        except Exception:
            pass

    async def _execution_telemetry_loop(self) -> None:
        while True:
            row = await self._execution_events.get()
            try:
                await self.store.insert_execution_event(row)
            except Exception as e:
                logger.debug("execution telemetry insert failed: %s", e)
            finally:
                self._execution_events.task_done()

    async def _flush_execution_events(self, timeout: float = 2.0) -> None:
        try:
            await asyncio.wait_for(self._execution_events.join(), timeout=max(0.05, timeout))
        except Exception:
            pass
        task = self._execution_telemetry_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._execution_telemetry_task = None

    def dispatch_signal(self, opp: Opportunity) -> bool:
        """Queue a live signal without blocking the engine scoring loop."""
        if self._stop.is_set() or self._emergency_close_in_progress or not self.state.engine_running:
            self._queue_execution_event(
                "signal_dropped",
                symbol=opp.symbol,
                side=opp.side,
                reason="executor_stopped",
                signal_ts=opp.signal_ts,
                fair=opp.fair,
                price=opp.entry_price,
                extra={"score": opp.score, "z": opp.z},
            )
            return False
        if opp.symbol in self._queued_signal_symbols:
            self._queue_execution_event(
                "signal_dropped",
                symbol=opp.symbol,
                side=opp.side,
                reason="signal_queue_duplicate",
                signal_ts=opp.signal_ts,
                fair=opp.fair,
                price=opp.entry_price,
                extra={"score": opp.score, "z": opp.z, "queue_size": self._signal_queue.qsize()},
            )
            return False
        try:
            self._signal_queue.put_nowait((opp, time.time()))
            self._queued_signal_symbols.add(opp.symbol)
            self._queue_execution_event(
                "signal_queued",
                symbol=opp.symbol,
                side=opp.side,
                reason=opp.algorithm,
                signal_ts=opp.signal_ts,
                fair=opp.fair,
                price=opp.entry_price,
                extra={"score": opp.score, "z": opp.z, "queue_size": self._signal_queue.qsize()},
            )
            return True
        except asyncio.QueueFull:
            self._signal_queue_drops += 1
            self._queue_execution_event(
                "signal_dropped",
                symbol=opp.symbol,
                side=opp.side,
                reason="signal_queue_full",
                signal_ts=opp.signal_ts,
                fair=opp.fair,
                price=opp.entry_price,
                extra={
                    "score": opp.score,
                    "z": opp.z,
                    "queue_size": self._signal_queue.qsize(),
                    "drops": self._signal_queue_drops,
                },
            )
            return False

    async def _signal_worker_loop(self) -> None:
        while True:
            opp, queued_at = await self._signal_queue.get()
            self._queued_signal_symbols.discard(opp.symbol)
            self._signal_worker_busy = True
            try:
                wait_ms = max(0.0, (time.time() - queued_at) * 1000.0)
                self._queue_execution_event(
                    "signal_dequeued",
                    symbol=opp.symbol,
                    side=opp.side,
                    reason=opp.algorithm,
                    signal_ts=opp.signal_ts,
                    fair=opp.fair,
                    price=opp.entry_price,
                    extra={"score": opp.score, "z": opp.z, "queue_wait_ms": wait_ms, "queue_size": self._signal_queue.qsize()},
                )
                await self.on_signal(opp)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("live signal worker error: %s", e)
                self._queue_execution_event(
                    "signal_dropped",
                    symbol=opp.symbol,
                    side=opp.side,
                    reason=f"signal_worker_error:{e}",
                    signal_ts=opp.signal_ts,
                )
            finally:
                self._signal_worker_busy = False
                self._signal_queue.task_done()

    async def _stop_signal_worker(self, timeout: float = 5.0) -> None:
        deadline = time.time() + max(0.1, timeout)
        while self._signal_worker_busy and time.time() < deadline:
            await asyncio.sleep(0.05)
        while True:
            try:
                opp, _queued_at = self._signal_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queued_signal_symbols.discard(opp.symbol)
            self._queue_execution_event(
                "signal_dropped",
                symbol=opp.symbol,
                side=opp.side,
                reason="shutdown_queue_drain",
                signal_ts=opp.signal_ts,
            )
            self._signal_queue.task_done()
        task = self._signal_worker_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._signal_worker_task = None
        self._queued_signal_symbols.clear()

    def _closed_position_key(self, pos: ManagedPosition) -> str:
        if pos.mexc_position_id not in (None, ""):
            return f"pid:{pos.mexc_position_id}"
        return (
            f"managed:{pos.symbol}:{pos.side}:"
            f"{pos.open_ts:.6f}:{pos.entry_price:.12g}:{pos.qty:.12g}"
        )

    def _external_close_history_delays(self) -> Tuple[float, ...]:
        if str(getattr(self.cfg, "mode", "") or "").lower() != "real":
            return (0.0,)
        delays = getattr(self, "_external_close_history_retry_delays", (0.0,))
        return tuple(float(max(0.0, d)) for d in delays) or (0.0,)

    async def graceful_stop(self, reason: str = "manual_stop") -> None:
        async with self.state.lock:
            self.state.engine_running = False
        self.stop()
        if bool(getattr(self.cfg.risk, "emergency_close_on_stop", True)):
            await self._emergency_flatten_all(reason=f"graceful_stop:{reason}")
            self._shutdown_cleanup_done = True

    async def init_balance(self) -> None:
        equity, available = await self._fetch_account_balances()
        async with self.state.lock:
            self.state.balance = equity
            self.state.available_balance = available
            self.state.session_starting_balance = equity
            self.state.session_peak_balance = equity
            self.state.strategy_realized_pnl = 0.0
            self.state.strategy_session_starting_balance = equity
            self.state.strategy_session_peak_balance = equity
            self.state.day_start_ts = time.time()
            self.state.day_start_balance = equity
        try:
            raw_positions = await self._get_positions_raw_cached(
                force=True,
                max_age_sec=0.0,
                min_interval_sec=0.0,
            )
            self._queue_execution_event(
                "positions_cache_ready",
                extra={"open_positions": len(raw_positions)},
            )
        except Exception as e:
            self._queue_execution_event("positions_cache_failed", reason=str(e))
            await self.state.add_log("warn", f"[real] initial open_positions check failed: {e}")
        await self._prewarm_symbol_meta()

    async def _fetch_account_balances(self) -> tuple[float, float]:
        api = getattr(self.trader, "api", None)
        get_account_info = getattr(api, "get_account_info", None)
        extract_snapshot = getattr(api, "_extract_usdt_balance_snapshot", None)
        if callable(get_account_info) and callable(extract_snapshot):
            try:
                info = await get_account_info()
                self._register_private_api_result(info, "account_assets")
                if bool(info.get("success")):
                    snap = extract_snapshot(info.get("data") or [])
                    if isinstance(snap, dict):
                        equity = float(snap.get("equity") or 0.0)
                        available = float(snap.get("available") or 0.0)
                        if equity <= 0 and available > 0:
                            equity = available
                        return max(0.0, equity), max(0.0, available)
            except Exception:
                self._register_private_api_exception("account_assets")
            equity = float(self.state.balance or 0.0)
            available = float(self.state.available_balance or equity)
            return equity, available
        try:
            snap = await self.trader.get_usdt_balance_snapshot()
            equity = float(snap.get("equity") or 0.0)
            available = float(snap.get("available") or 0.0)
            if equity <= 0 and available > 0:
                equity = available
            return max(0.0, equity), max(0.0, available)
        except Exception:
            equity = float(self.state.balance or 0.0)
            available = float(self.state.available_balance or equity)
            return equity, available

    @staticmethod
    def _looks_like_auth_failure(message: str, code: Any = None) -> bool:
        msg = str(message or "").lower()
        try:
            numeric_code = int(code)
        except Exception:
            numeric_code = None
        if numeric_code == 401:
            return True
        return (
            "not logged in" in msg
            or "login has expired" in msg
            or "unauthorized" in msg
            or "auth" in msg and "fail" in msg
        )

    @staticmethod
    def _looks_like_rate_limit(message: str, code: Any = None) -> bool:
        msg = str(message or "").lower()
        try:
            numeric_code = int(code)
        except Exception:
            numeric_code = None
        return (
            numeric_code in (429, 510)
            or "too frequent" in msg
            or "too many requests" in msg
            or "rate limit" in msg
        )

    def _register_private_rate_limit(self, context: str, res: Optional[Dict[str, Any]] = None) -> None:
        self.state.private_api_error_count += 1
        now_mono = time.monotonic()
        if context == "open_positions":
            self._positions_rate_limit_count += 1
            backoff = min(3.0, 0.75 * (2 ** min(self._positions_rate_limit_count - 1, 3)))
            self._positions_rate_limited_until = max(self._positions_rate_limited_until, now_mono + backoff)
        message = str((res or {}).get("message") or "")
        code = (res or {}).get("code")
        logger.warning(
            "private API rate limit in %s: code=%r message=%r",
            context,
            code,
            message,
        )

    def _register_private_api_exception(self, context: str) -> None:
        self._private_api_error_streak += 1
        self.state.private_api_error_count += 1
        logger.warning("private API exception in %s (streak=%d)", context, self._private_api_error_streak)

    def _register_private_api_result(self, res: Optional[Dict[str, Any]], context: str) -> None:
        if not isinstance(res, dict):
            return
        if bool(res.get("success")):
            self._private_api_error_streak = 0
            self._auth_error_streak = 0
            if context == "open_positions":
                self._positions_rate_limit_count = 0
                self._positions_rate_limited_until = 0.0
            self.state.mexc_auth_ok = True
            if context == "auth_ping":
                self.state.mexc_auth_msg = str(res.get("message") or "")
            return

        message = str(res.get("message") or "")
        code = res.get("code")
        if self._looks_like_rate_limit(message, code):
            self._register_private_rate_limit(context, res)
            return

        self._private_api_error_streak += 1
        self.state.private_api_error_count += 1
        if self._looks_like_auth_failure(message, code):
            self._auth_error_streak += 1
            self.state.auth_error_count += 1
            self.state.mexc_auth_ok = False
            self.state.mexc_auth_msg = message
        logger.warning(
            "private API error in %s: success=%r code=%r message=%r (streak=%d auth_streak=%d)",
            context,
            res.get("success"),
            code,
            message,
            self._private_api_error_streak,
            self._auth_error_streak,
        )

    async def _max_leverage(self, symbol: str) -> int:
        if symbol in self._max_lev_cache:
            return self._max_lev_cache[symbol]
        try:
            lev = int(await self.trader.get_max_leverage(symbol) or 0)
        except Exception:
            lev = 0
        if lev <= 0:
            lev = int(self.cfg.risk.fixed_leverage)
        self._max_lev_cache[symbol] = lev
        return lev

    async def _tick_size(self, symbol: str) -> Decimal:
        try:
            info = await self.trader.api.get_contract_info_cached(symbol)
        except Exception:
            info = None
        if info:
            pu = info.get("priceUnit")
            ps = info.get("priceScale")
            try:
                if pu is not None and float(pu) > 0:
                    return Decimal(str(pu))
            except Exception:
                pass
            try:
                if ps is not None:
                    return Decimal(1).scaleb(-int(ps))
            except Exception:
                pass
        return Decimal("0.00000001")

    def _sl_pct_for(self, symbol: str) -> float:
        """Return backstop SL price fraction for this symbol.

        Priority: symbol_override.sl_pct Ð Ð†Ð²Ð‚Â Ð²Ð‚â„¢ global sl_pct_crypto/stocks.
        At 100x leverage: 0.25% price = 25% margin loss, well above 2s hold noise.
        """
        for ov in (self.cfg.symbol_overrides or []):
            if ov.symbol == symbol and ov.sl_pct is not None:
                return float(ov.sl_pct)
        if symbol in STOCK_SYMBOLS:
            return float(getattr(self.cfg.strategy, "sl_pct_stocks", 0.0010))
        return float(getattr(self.cfg.strategy, "sl_pct_crypto", 0.0025))

    def _override_for(self, symbol: str):
        for ov in (self.cfg.symbol_overrides or []):
            if ov.symbol == symbol:
                return ov
        return None

    def _evaluated_stats(self, symbol: str):
        st = self.agg.compute_stats(symbol)
        ov = self._override_for(symbol)
        if ov is not None and ov.algorithms and hasattr(self.opp, "evaluate_multi"):
            self.opp.evaluate_multi(symbol, st, ov)
        elif hasattr(self.opp, "evaluate"):
            self.opp.evaluate(symbol, st)
        return st

    def _float_setting_for(self, symbol: str, field_name: str, default: float = 0.0) -> float:
        ov = self._override_for(symbol)
        if ov is not None:
            value = getattr(ov, field_name, None)
            if value is not None:
                return float(value)
        return float(getattr(self.cfg.strategy, field_name, default) or default)

    def _min_entry_score_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.min_entry_score is not None:
            return float(ov.min_entry_score)
        return 0.0

    def _max_hold_sec_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.max_hold_sec is not None:
            return float(ov.max_hold_sec)
        return float(self.cfg.strategy.max_hold_sec)

    def _managed_symbols(self) -> Optional[Set[str]]:
        """Symbols this executor is allowed to recover/manage on a shared account."""
        working = {
            str(sym or "").upper()
            for sym in (self.state.universe or [])
            if str(sym or "").strip()
        }
        if working:
            return working

        include_only = {
            str(sym or "").upper()
            for sym in (self.cfg.universe.include_only or [])
            if str(sym or "").strip()
        }
        if include_only:
            return include_only

        force_include = {
            str(sym or "").upper()
            for sym in (self.cfg.universe.force_include_symbols or [])
            if str(sym or "").strip()
        }
        if force_include:
            return force_include

        enabled_overrides = {
            str(ov.symbol or "").upper()
            for ov in (self.cfg.symbol_overrides or [])
            if getattr(ov, "enabled", True) and str(getattr(ov, "symbol", "") or "").strip()
        }
        return enabled_overrides or None

    async def _persist_managed_position(self, pos: ManagedPosition) -> None:
        upsert = getattr(self.store, "upsert_managed_position", None)
        if not callable(upsert):
            return
        try:
            await upsert("real", pos.symbol, asdict(pos))
        except Exception as e:
            logger.warning("persist managed position failed for %s: %s", pos.symbol, e)

    async def _delete_persisted_position(self, symbol: str) -> None:
        delete = getattr(self.store, "delete_managed_position", None)
        if not callable(delete):
            return
        try:
            await delete("real", symbol)
        except Exception as e:
            logger.warning("delete managed position failed for %s: %s", symbol, e)

    async def _load_persisted_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        getter = getattr(self.store, "get_managed_position", None)
        if not callable(getter):
            return None
        try:
            payload = await getter("real", symbol)
        except Exception as e:
            logger.warning("load managed position failed for %s: %s", symbol, e)
            return None
        return dict(payload) if isinstance(payload, dict) else None

    def _exchange_position_open_ts(self, raw: Dict[str, Any], now: float) -> float:
        exchange_open_ts = self._exchange_ts_to_epoch(
            raw.get("createTime") or raw.get("ctime") or raw.get("openTime") or raw.get("timestamp") or raw.get("updateTime")
        )
        if exchange_open_ts <= 0 or exchange_open_ts > now:
            exchange_open_ts = now
        return exchange_open_ts

    def _persisted_position_matches(
        self,
        payload: Dict[str, Any],
        *,
        raw: Dict[str, Any],
        side: str,
        entry_price: float,
        exchange_open_ts: float,
    ) -> bool:
        payload_side = str(payload.get("side") or "").upper()
        if payload_side and payload_side != side:
            return False

        raw_pos_id = self._extract_position_id(raw)
        try:
            payload_pos_id = int(payload.get("mexc_position_id")) if payload.get("mexc_position_id") is not None else None
        except Exception:
            payload_pos_id = None
        if raw_pos_id is not None and payload_pos_id is not None and raw_pos_id != payload_pos_id:
            return False

        try:
            payload_open_ts = float(payload.get("open_ts") or 0.0)
        except Exception:
            payload_open_ts = 0.0
        if payload_open_ts > 0 and exchange_open_ts > 0 and abs(payload_open_ts - exchange_open_ts) > 30.0:
            return False

        try:
            payload_entry = float(payload.get("entry_price") or 0.0)
        except Exception:
            payload_entry = 0.0
        if payload_entry > 0 and entry_price > 0:
            rel_diff = abs(payload_entry - entry_price) / entry_price
            if rel_diff > 0.0035:
                return False

        return True

    def _cooldown_min_sec_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.cooldown_min_sec is not None:
            return float(ov.cooldown_min_sec)
        return float(getattr(self.cfg.strategy, "cooldown_min_sec", 0.0) or 0.0)

    def _cooldown_max_sec_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.cooldown_max_sec is not None:
            return float(ov.cooldown_max_sec)
        return float(getattr(self.cfg.strategy, "cooldown_max_sec", 0.0) or 0.0)

    def _taker_ioc_price_buffer_bps_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.taker_ioc_price_buffer_bps is not None:
            return float(ov.taker_ioc_price_buffer_bps)
        return float(getattr(self.cfg.strategy, "taker_ioc_price_buffer_bps", 0.0) or 0.0)

    def _taker_ioc_min_fill_ratio_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.taker_ioc_min_fill_ratio is not None:
            return float(ov.taker_ioc_min_fill_ratio)
        return float(getattr(self.cfg.strategy, "taker_ioc_min_fill_ratio", 0.2) or 0.2)

    def _taker_market_min_fill_ratio_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and getattr(ov, "taker_market_min_fill_ratio", None) is not None:
            return float(ov.taker_market_min_fill_ratio)
        return float(getattr(self.cfg.strategy, "taker_market_min_fill_ratio", 0.98) or 0.98)

    def _taker_order_mode_for(self, symbol: str) -> str:
        ov = self._override_for(symbol)
        raw = None
        if ov and getattr(ov, "taker_order_mode", None) is not None:
            raw = ov.taker_order_mode
        if raw is None:
            raw = getattr(self.cfg.strategy, "taker_order_mode", "")
        mode = str(raw or "").strip().lower()
        if mode in {"ioc", "limit_ioc", "limit-ioc", "order_type_3", "3"}:
            return "ioc"
        if mode in {"market", "mkt", "order_type_5", "5"}:
            return "market"
        return "ioc" if bool(getattr(self.cfg.strategy, "taker_ioc_simulation", False)) else "market"

    def _entry_latency_ms_for(self, symbol: str) -> int:
        ov = self._override_for(symbol)
        if ov and ov.entry_latency_ms is not None:
            return int(ov.entry_latency_ms)
        raw = getattr(self.cfg.strategy, "entry_latency_ms", None)
        return 200 if raw is None else int(raw)

    def _signal_max_age_ms_for(self, symbol: str) -> int:
        ov = self._override_for(symbol)
        if ov and ov.signal_max_age_ms is not None:
            return int(ov.signal_max_age_ms)
        return int(getattr(self.cfg.strategy, "signal_max_age_ms", 0) or 0)

    def _signal_age_grace_ms_for(self, symbol: str) -> float:
        entry_latency_ms = float(max(0, self._entry_latency_ms_for(symbol)))
        return max(125.0, min(350.0, 200.0 + entry_latency_ms + 25.0))

    def _loop_tick_sec(self) -> float:
        raw = getattr(self.cfg.strategy, "paper_tick_sec", None)
        return max(0.01, float(0.2 if raw is None else raw))

    def _positions_cache_max_age_sec(self) -> float:
        return max(0.50, min(0.90, self._loop_tick_sec() * 12.0))

    def _positions_force_refresh_floor_sec(self) -> float:
        return max(0.35, min(0.70, self._loop_tick_sec() * 8.0))

    def _quote_order_query_min_interval_sec(self) -> float:
        return max(0.05, min(0.15, self._loop_tick_sec() * 3.0))

    def _quote_order_query_first_delay_sec(self) -> float:
        return max(0.02, min(0.08, self._loop_tick_sec() * 1.5))

    def _fast_market_volume(self, symbol: str, notional: float, price: float) -> Optional[float]:
        if notional <= 0 or price <= 0:
            return None
        contract_size = float(self.agg.contract_size_for(symbol) or 1.0)
        if contract_size <= 0:
            contract_size = 1.0
        raw = notional / (price * contract_size)
        if raw <= 0:
            return None
        return float(max(1, math.floor(raw)))

    @staticmethod
    def _fresh_books_for_age_grace(st: Any) -> bool:
        mexc_age = getattr(st, "mexc_book_age_ms", None)
        binance_age = getattr(st, "binance_book_age_ms", None)
        if mexc_age is not None and float(mexc_age) > 200.0:
            return False
        if binance_age is not None and float(binance_age) > 200.0:
            return False
        return True

    def _entry_book_age_block_reason(self, st: Any) -> str:
        max_age_ms = float(getattr(self.cfg.risk, "stale_book_age_ms_kill", 0.0) or 0.0)
        if max_age_ms <= 0:
            return ""
        mexc_age = getattr(st, "mexc_book_age_ms", None)
        if mexc_age is not None and float(mexc_age) > max_age_ms:
            return f"stale_mexc_book={float(mexc_age):.0f}ms"
        binance_age = getattr(st, "binance_book_age_ms", None)
        if binance_age is not None and float(binance_age) > max_age_ms:
            return f"stale_binance_book={float(binance_age):.0f}ms"
        return ""

    def _pre_submit_max_spread_drift_bps_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.pre_submit_max_spread_drift_bps is not None:
            return float(ov.pre_submit_max_spread_drift_bps)
        return float(getattr(self.cfg.strategy, "pre_submit_max_spread_drift_bps", 0.0) or 0.0)

    def _late_impulse_setting_for(self, symbol: str, name: str, default: Any = 0.0) -> Any:
        ov = self._override_for(symbol)
        if ov is not None and hasattr(ov, name):
            val = getattr(ov, name)
            if val is not None:
                return val
        return getattr(self.cfg.strategy, name, default)

    def _late_impulse_reject_reason(
        self,
        symbol: str,
        side: str,
        *,
        fair: float,
        mexc_mid: Optional[float],
        exit_price: float,
        fair_age_ms: Optional[float],
    ) -> str:
        enabled = bool(self._late_impulse_setting_for(symbol, "late_impulse_reject_enabled", False))
        if not enabled or fair <= 0 or exit_price <= 0:
            return ""

        max_fair_age_ms = float(self._late_impulse_setting_for(symbol, "late_impulse_max_fair_age_ms", 0.0) or 0.0)
        if max_fair_age_ms > 0 and fair_age_ms is not None and float(fair_age_ms) > max_fair_age_ms:
            return f"late_fair_age={float(fair_age_ms):.0f}ms"

        min_edge_bps = float(self._late_impulse_setting_for(symbol, "late_impulse_min_edge_bps", 0.0) or 0.0)
        max_chase_bps = float(self._late_impulse_setting_for(symbol, "late_impulse_max_chase_bps", 0.0) or 0.0)
        if side == "LONG":
            edge_bps = ((fair - exit_price) / fair) * 1e4
            chase_bps = (((mexc_mid if mexc_mid and mexc_mid > 0 else exit_price) - fair) / fair) * 1e4
            if min_edge_bps > 0 and edge_bps < min_edge_bps:
                return f"late_edge_long={edge_bps:.2f}bps"
            if max_chase_bps > 0 and chase_bps > max_chase_bps:
                return f"late_chase_long={chase_bps:.2f}bps"
        else:
            edge_bps = ((exit_price - fair) / fair) * 1e4
            chase_bps = ((fair - (mexc_mid if mexc_mid and mexc_mid > 0 else exit_price)) / fair) * 1e4
            if min_edge_bps > 0 and edge_bps < min_edge_bps:
                return f"late_edge_short={edge_bps:.2f}bps"
            if max_chase_bps > 0 and chase_bps > max_chase_bps:
                return f"late_chase_short={chase_bps:.2f}bps"
        return ""

    def _scalp_take_profit_bps_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.scalp_take_profit_bps is not None:
            return float(ov.scalp_take_profit_bps)
        return float(getattr(self.cfg.strategy, "scalp_take_profit_bps", 0.0) or 0.0)

    def _scratch_exit_sec_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.scratch_exit_sec is not None:
            return float(ov.scratch_exit_sec)
        return float(getattr(self.cfg.strategy, "scratch_exit_sec", 0.0) or 0.0)

    def _scratch_exit_bps_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.scratch_exit_bps is not None:
            return float(ov.scratch_exit_bps)
        return float(getattr(self.cfg.strategy, "scratch_exit_bps", 0.0) or 0.0)

    def _use_fair_tp_for(self, symbol: str) -> bool:
        ov = self._override_for(symbol)
        if ov and ov.use_fair_tp is not None:
            return bool(ov.use_fair_tp)
        return bool(getattr(self.cfg.strategy, "use_fair_tp", False))

    def _max_chase_bps_for(self, symbol: str, algo: Optional[str]) -> float:
        ov = self._override_for(symbol)
        if ov and ov.max_chase_bps is not None:
            return float(ov.max_chase_bps)

        algo_l = str(algo or "").lower()
        if algo_l == "raw_momentum":
            return float(getattr(self.cfg.strategy, "raw_momentum_max_chase_bps", 0.0) or 0.0)
        if algo_l == "confluence":
            return float(getattr(self.cfg.strategy, "confluence_max_chase_bps", 0.0) or 0.0)
        if algo_l == "ofi":
            return float(getattr(self.cfg.strategy, "ofi_max_chase_bps", 0.0) or 0.0)
        if algo_l == "imbalance":
            return float(getattr(self.cfg.strategy, "imbalance_max_chase_bps", 0.0) or 0.0)
        return 0.0

    def _signal_valid_now(
        self,
        symbol: str,
        side: str,
        *,
        signal_ts: float = 0.0,
        spread_bps_at_quote: Optional[float] = None,
    ) -> tuple[bool, str]:
        st = self._evaluated_stats(symbol)
        stale_reason = self._entry_book_age_block_reason(st)
        if stale_reason:
            return False, stale_reason
        strict_filters = signal_ts <= 0 and spread_bps_at_quote is None
        if strict_filters:
            if st.blocked_reason:
                return False, st.blocked_reason
            if not st.side_hint:
                return False, "no_side_hint"
            if st.side_hint != side:
                return False, f"side_flip={st.side_hint}"
            min_entry_score = self._min_entry_score_for(symbol)
            if st.score < min_entry_score:
                return False, f"score {st.score:.2f} < {min_entry_score:.2f}"
        else:
            if not st.side_hint:
                return False, "no_side_hint_fill"
            if st.side_hint != side:
                return False, f"side_flip={st.side_hint}"
            min_entry_score = self._min_entry_score_for(symbol)
            if st.score < min_entry_score:
                return False, f"score {st.score:.2f} < {min_entry_score:.2f}"
        max_age_ms = self._signal_max_age_ms_for(symbol)
        if max_age_ms > 0 and signal_ts > 0:
            age_ms = (time.time() - signal_ts) * 1000.0
            age_limit_ms = float(max_age_ms)
            if age_ms > age_limit_ms:
                grace_ms = self._signal_age_grace_ms_for(symbol)
                if not (
                    age_ms <= age_limit_ms + grace_ms
                    and self._fresh_books_for_age_grace(st)
                ):
                    return False, f"signal_age={age_ms:.0f}ms"
        max_spread_drift = self._pre_submit_max_spread_drift_bps_for(symbol)
        if max_spread_drift > 0 and spread_bps_at_quote is not None and st.spread_bps is not None:
            if side == "LONG":
                spread_drift = st.spread_bps - spread_bps_at_quote
            else:
                spread_drift = spread_bps_at_quote - st.spread_bps
            if spread_drift > max_spread_drift:
                return False, f"spread_drift={spread_drift:.2f}bps"
        book = self.agg.get_book(symbol)
        if book and book.best_bid is not None and book.best_ask is not None:
            exit_price = float(book.best_ask if side == "LONG" else book.best_bid)
            late_reason = self._late_impulse_reject_reason(
                symbol,
                side,
                fair=float(getattr(st, "fair", 0.0) or 0.0),
                mexc_mid=float(getattr(st, "mexc_mid", 0.0) or 0.0),
                exit_price=exit_price,
                fair_age_ms=getattr(st, "binance_book_age_ms", None),
            )
            if late_reason:
                return False, late_reason
        return True, ""

    def _fill_respects_fair(
        self,
        symbol: str,
        side: str,
        algo: Optional[str],
        fill_price: float,
        fair: float,
    ) -> tuple[bool, str]:
        if fair <= 0 or fill_price <= 0:
            return True, ""
        raw_spread_bps = ((fill_price - fair) / fair) * 1e4
        max_chase_bps = self._max_chase_bps_for(symbol, algo)
        if side == "LONG" and raw_spread_bps > max_chase_bps:
            return False, f"fill_chasing_long={raw_spread_bps:.2f}bps"
        if side == "SHORT" and raw_spread_bps < -max_chase_bps:
            return False, f"fill_chasing_short={raw_spread_bps:.2f}bps"
        return True, ""

    async def loop(self) -> None:
        if self._execution_telemetry_task is None or self._execution_telemetry_task.done():
            self._execution_telemetry_task = asyncio.create_task(
                self._execution_telemetry_loop(),
                name="real_execution_telemetry",
            )
        await self.init_balance()
        if self._signal_worker_task is None or self._signal_worker_task.done():
            self._signal_worker_task = asyncio.create_task(
                self._signal_worker_loop(),
                name="real_signal_worker",
            )
        tick_sec = self._loop_tick_sec()
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception as e:
                    logger.exception("real tick error: %s", e)
                    await self.state.add_log("error", f"[real] tick exception: {e}")
                await asyncio.sleep(tick_sec)
        finally:
            await self._stop_signal_worker()
            if bool(getattr(self.cfg.risk, "emergency_close_on_stop", True)) and not self._shutdown_cleanup_done:
                await self._emergency_flatten_all(reason="loop_exit")
                self._shutdown_cleanup_done = True
            await self._flush_execution_events()

    async def on_signal(self, opp: Opportunity) -> None:
        if self._stop.is_set() or self._emergency_close_in_progress or not self.state.engine_running:
            return
        sym = opp.symbol

        # Fast path: skip zero_fee check entirely (all symbols pre-validated in config)
        # Skip execution event logging on hot path (moved to after submit)
        
        # Fast leverage lookup (cached, no await needed after first call)
        if sym not in self._max_lev_cache:
            try:
                lev = int(await self.trader.get_max_leverage(sym) or 0)
            except Exception:
                lev = 0
            if lev <= 0:
                lev = int(self.cfg.risk.fixed_leverage)
            self._max_lev_cache[sym] = lev
        lev_max_hint = self._max_lev_cache[sym]

        async with self._lock:
            await self._maybe_place_quote(opp, zero_fee_ok=True, lev_max_hint=lev_max_hint)

    # ----------------------------- internals -----------------------------

    async def _tick(self) -> None:
        if self._emergency_close_in_progress:
            return
        await self._refresh_balance_periodically()
        await self._reconcile_quotes()
        await self._reconcile_positions()
        await self._log_equity_periodically()
        await self._check_kill_switch()

    async def _refresh_balance_periodically(self) -> None:
        now = time.time()
        if now - self._balance_refresh_last_ts < 5.0:
            return
        self._balance_refresh_last_ts = now
        try:
            equity, available = await self._fetch_account_balances()
        except Exception:
            return
        async with self.state.lock:
            self.state.balance = equity
            self.state.available_balance = available
            if equity > self.state.session_peak_balance:
                self.state.session_peak_balance = equity

    def _free_balance(self) -> float:
        if self.state.available_balance > 0 or self.state.positions:
            return max(0.0, self.state.available_balance)
        used = sum(p.margin_usdt for p in self.state.positions.values())
        return max(0.0, self.state.balance - used)

    async def _record_live_candidate_reject(
        self,
        *,
        symbol: str,
        side: Optional[str],
        score: float,
        z: Optional[float],
        blocked: str,
        fair: Optional[float],
        mexc: Optional[float],
        depth: Optional[float],
    ) -> None:
        try:
            key = str(blocked or "unknown").split("=", 1)[0].split(":", 1)[0].strip() or "unknown"
            counts = getattr(self.state, "grid_reject_counts", None)
            if counts is None:
                counts = {}
                setattr(self.state, "grid_reject_counts", counts)
            counts[key] = int(counts.get(key, 0) or 0) + 1
        except Exception:
            pass
        if not bool(getattr(self.cfg.strategy, "grid_log_candidates", True)):
            return
        try:
            await self.store.insert_candidate(
                time.time(),
                symbol,
                side,
                score,
                z,
                None if fair is None or mexc is None or fair <= 0 else ((mexc - fair) / fair) * 1e4,
                fair,
                mexc,
                depth,
                blocked,
                accepted=False,
            )
        except Exception:
            pass

    async def _get_positions_raw_cached(
        self,
        *,
        max_age_sec: Optional[float] = None,
        force: bool = False,
        min_interval_sec: Optional[float] = None,
    ) -> list[Dict[str, Any]]:
        if max_age_sec is None:
            max_age_sec = self._positions_cache_max_age_sec()
        if min_interval_sec is None:
            min_interval_sec = self._positions_force_refresh_floor_sec() if force else 0.0
        now = time.monotonic()
        cache_age = now - self._positions_cache_ts if self._positions_cache_ts > 0 else None
        if cache_age is not None and cache_age <= max(0.0, max_age_sec):
            return [dict(item) for item in self._positions_cache_raw]
        if (
            force
            and cache_age is not None
            and cache_age <= max(0.0, min_interval_sec)
        ):
            return [dict(item) for item in self._positions_cache_raw]
        if now < self._positions_rate_limited_until and self._positions_cache_ts > 0:
            return [dict(item) for item in self._positions_cache_raw]
        api = getattr(self.trader, "api", None)
        get_positions = getattr(api, "get_positions", None)
        raw_items: list[Dict[str, Any]] = []
        if callable(get_positions):
            res = await get_positions()
            if not bool(res.get("success")):
                message = str(res.get("message") or "open_positions failed")
                code = res.get("code")
                if self._looks_like_rate_limit(message, code):
                    self._register_private_rate_limit("open_positions", res)
                    if self._positions_cache_ts > 0:
                        now_ts = time.time()
                        if now_ts - self._positions_rate_limit_last_log_ts >= 2.0:
                            self._positions_rate_limit_last_log_ts = now_ts
                            await self.state.add_log(
                                "warn",
                                "[real] open_positions rate-limited; using cached snapshot",
                            )
                        return [dict(item) for item in self._positions_cache_raw]
                self._register_private_api_result(res, "open_positions")
                raise RuntimeError(f"open_positions failed code={code} message={message}")
            self._register_private_api_result(res, "open_positions")
            raw = res.get("data") or []
            if isinstance(raw, list):
                raw_items = [dict(item) for item in raw if isinstance(item, dict)]
        else:
            raw = await self.trader.get_positions_raw()
            raw_items = [dict(item) for item in raw if isinstance(item, dict)]
        self._positions_cache_raw = raw_items
        self._positions_cache_ts = now
        if force:
            self._positions_force_refresh_ts = now
        return [dict(item) for item in self._positions_cache_raw]

    async def _prewarm_symbol_meta(self) -> None:
        syms = sorted(self._managed_symbols() or [])
        if not syms:
            return

        async def _warm_one(sym: str) -> None:
            try:
                await self.trader.warm_symbol_meta(sym, ttl=600.0)
            except Exception:
                return

        await asyncio.gather(*(_warm_one(sym) for sym in syms), return_exceptions=True)

    async def _maybe_place_quote(
        self,
        opp: Opportunity,
        *,
        zero_fee_ok: Optional[bool] = None,
        lev_max_hint: Optional[int] = None,
    ) -> None:
        sym = opp.symbol
        if self._stop.is_set() or self._emergency_close_in_progress or not self.state.engine_running:
            return
        if sym in self._quotes:
            self._queue_execution_event("signal_rejected", symbol=sym, side=opp.side, reason="quote_pending", signal_ts=opp.signal_ts)
            await self.state.add_log("debug", f"[real] skip place {sym}: quote_pending")
            return
        if sym in self.state.positions:
            self._queue_execution_event("signal_rejected", symbol=sym, side=opp.side, reason="position_open", signal_ts=opp.signal_ts)
            await self.state.add_log("debug", f"[real] skip place {sym}: position_open")
            return

        book = self.agg.get_book(sym)
        if not book or book.best_bid is None or book.best_ask is None:
            self._queue_execution_event("signal_rejected", symbol=sym, side=opp.side, reason="no_book", signal_ts=opp.signal_ts)
            await self.state.add_log("debug", f"[real] skip place {sym}: no_book")
            return

        # 0-fee live check Ð Ð†Ð â€šÐ²Ð‚Ñœ SKIPPED (pre-validated via config zero_fee_symbols)
        zero = True
        if not bool(zero):
            self._queue_execution_event("signal_rejected", symbol=sym, side=opp.side, reason="zero_fee_disabled", signal_ts=opp.signal_ts)
            await self.state.add_log("debug", f"[real] skip place {sym}: zero_fee_disabled")
            return
        if self._positions_cache_ts <= 0:
            self._queue_execution_event("signal_rejected", symbol=sym, side=opp.side, reason="positions_not_confirmed", signal_ts=opp.signal_ts)
            await self.state.add_log("debug", f"[real] skip place {sym}: positions_not_confirmed")
            return

        lev_max = int(lev_max_hint or 0)
        if lev_max <= 0:
            lev_max = await self._max_leverage(sym)
        contract_size = self.agg.contract_size_for(sym)
        depth = book.top_notional(10, contract_size=contract_size)
        spread_bps_at_quote = None
        if opp.fair > 0 and opp.entry_price > 0:
            spread_bps_at_quote = ((opp.entry_price - opp.fair) / opp.fair) * 1e4

        # Per-symbol sizing overrides
        _ov = self._override_for(sym)
        decision = self.alloc.decide(
            opp, self.state,
            balance_free=self._free_balance(),
            max_leverage_for_symbol=lev_max,
            book_top_notional=depth,
            margin_pct_override=(_ov.margin_pct if _ov else None),
            leverage_override=(_ov.leverage if _ov else None),
            book_depth_consume_pct_override=(_ov.book_depth_consume_pct if _ov else None),
            max_notional_usdt_override=(_ov.max_notional_usdt if _ov else None),
        )
        if not decision.accept:
            self._queue_execution_event(
                "signal_rejected",
                symbol=sym,
                side=opp.side,
                reason=f"alloc_reject:{decision.reason}",
                signal_ts=opp.signal_ts,
                fair=opp.fair,
                price=opp.entry_price,
                notional=decision.notional_usdt,
                extra={"score": opp.score, "z": opp.z, "depth": depth},
            )
            await self.state.add_log("debug", f"[real] skip place {sym}: alloc_reject={decision.reason}")
            return

        taker_entry = bool(getattr(self.cfg.strategy, "taker_entry", False))
        tick = await self._tick_size(sym)

        if taker_entry:
            latency_ms = max(0, self._entry_latency_ms_for(sym))
            order_mode = self._taker_order_mode_for(sym)
            raw_price = float(book.best_ask if opp.side == "LONG" else book.best_bid)
            now_ts = time.time()
            queue_age_ms = max(0.0, (now_ts - opp.signal_ts) * 1000.0) if opp.signal_ts > 0 else 0.0
            q = _Quote(
                symbol=sym,
                side=opp.side,
                price=raw_price,
                notional=decision.notional_usdt,
                margin=decision.margin_usdt,
                leverage=decision.leverage,
                placed_ts=now_ts,
                fair_at_quote=opp.fair,
                sigma_at_quote=opp.sigma,
                z_at_quote=opp.z,
                taker_submit_at=now_ts,
                signal_ts=opp.signal_ts,
                entry_algo=opp.algorithm,
                entry_score=opp.score,
                spread_bps_at_quote=spread_bps_at_quote,
                is_ioc=True,
                order_mode=order_mode,
            )
            q.submit_in_flight = True
            self._quotes[sym] = q
            await self.state.add_log(
                "debug",
                f"[real] taker armed {sym} {opp.side} mode={order_mode} "
                f"(q_age={queue_age_ms:.0f}ms, modeled_submit={latency_ms}ms)",
            )
            await self._submit_due_taker_quote(sym, q, now=now_ts, claimed=True)
            return
        else:
            # Limit-maker entry
            raw_price = book.best_bid if opp.side == "LONG" else book.best_ask
            if raw_price <= 0:
                return
            price, _ = _snap_price(float(raw_price), tick, opp.side, for_sl=False)
            try:
                res = await self.trader.open_limit(
                    sym, opp.side, decision.notional_usdt, decision.leverage, price,
                )
            except Exception as e:
                self._register_private_api_exception("open_limit")
                await self.state.add_log("error", f"open_limit failed {sym}: {e}")
                return
            self._register_private_api_result(res, "open_limit")

        if not res.get("success"):
            await self.state.add_log("warn", f"open_limit reject {sym}: {res.get('message')}")
            return

        order_id = None
        data = res.get("data") or {}
        if isinstance(data, dict):
            order_id = data.get("orderId") or data.get("order_id")
        try:
            order_id = int(order_id) if order_id is not None else None
        except Exception:
            order_id = None

        q = _Quote(
            symbol=sym,
            side=opp.side,
            price=price,
            notional=decision.notional_usdt,
            margin=decision.margin_usdt,
            leverage=decision.leverage,
            placed_ts=time.time(),
            fair_at_quote=opp.fair,
            sigma_at_quote=opp.sigma,
            z_at_quote=opp.z,
            order_id=order_id,
            signal_ts=opp.signal_ts,
            entry_algo=opp.algorithm,
            entry_score=opp.score,
            spread_bps_at_quote=spread_bps_at_quote,
        )
        self._quotes[sym] = q
        await self.state.add_log(
            "info",
            f"[real] {'ioc' if taker_entry else 'quote'} {sym} {opp.side} @ {price:.6g} "
            f"(notional={decision.notional_usdt:.2f}, lev={decision.leverage}, oid={order_id})",
        )

    async def _submit_due_taker_quote(
        self,
        sym: str,
        q: _Quote,
        *,
        now: Optional[float] = None,
        claimed: bool = False,
    ) -> bool:
        if self._stop.is_set() or self._emergency_close_in_progress or not self.state.engine_running:
            if claimed:
                q.submit_in_flight = False
            self._quotes.pop(sym, None)
            await self.state.add_log("debug", f"[real] skip {sym} {q.side}: shutting_down")
            return True
        book = self.agg.get_book(sym)
        if not book or book.best_bid is None or book.best_ask is None:
            if claimed:
                q.submit_in_flight = False
            return False
        now_ts = now if now is not None else time.time()
        if q.submit_in_flight and not claimed:
            return True
        if q.taker_submit_at is None or q.order_id is not None or now_ts < q.taker_submit_at:
            if claimed:
                q.submit_in_flight = False
            return False
        if not claimed:
            q.submit_in_flight = True

        ok_signal, why_signal = self._signal_valid_now(
            sym,
            q.side,
            signal_ts=q.signal_ts,
            spread_bps_at_quote=q.spread_bps_at_quote,
        )
        if not ok_signal:
            q.submit_in_flight = False
            self._quotes.pop(sym, None)
            self._queue_execution_event("signal_rejected", quote=q, reason=f"stale_signal:{why_signal}")
            await self.state.add_log("debug", f"[real] skip {sym} {q.side}: stale signal ({why_signal})")
            return True

        raw_price = float(book.best_ask if q.side == "LONG" else book.best_bid)
        if raw_price <= 0:
            q.submit_in_flight = False
            self._quotes.pop(sym, None)
            return True
        order_mode = q.order_mode or self._taker_order_mode_for(sym)
        q.order_mode = order_mode
        if order_mode == "ioc":
            buf_bps = self._taker_ioc_price_buffer_bps_for(sym)
            if q.side == "LONG":
                raw_price *= (1.0 + buf_bps / 1e4)
            else:
                raw_price *= (1.0 - buf_bps / 1e4)
        tick = await self._tick_size(sym)
        price, _ = _snap_price(float(raw_price), tick, q.side, for_sl=False)
        agg_stats = self.agg.compute_stats(sym)
        fair = float(agg_stats.fair or 0.0)
        ok_fill, why_fill = self._fill_respects_fair(
            sym, q.side, q.entry_algo, price, fair
        )
        if not ok_fill:
            q.submit_in_flight = False
            self._quotes.pop(sym, None)
            self._queue_execution_event("signal_rejected", quote=q, reason=why_fill, price=price, fair=fair)
            await self.state.add_log("debug", f"[real] skip {sym} {q.side}: {why_fill}")
            return True
        try:
            q.submit_started_ts = time.time()
            if order_mode == "market":
                endpoint_name = "open_market"
                self._queue_execution_event("submit_started", quote=q, endpoint=endpoint_name, price=price)
                res = await self.trader.open_market(
                    sym,
                    q.side,
                    q.notional,
                    q.leverage,
                    vol_override=self._fast_market_volume(sym, q.notional, price),
                )
            else:
                endpoint_name = "open_ioc"
                self._queue_execution_event("submit_started", quote=q, endpoint=endpoint_name, price=price)
                res = await self.trader.open_ioc(
                    sym, q.side, q.notional, q.leverage, price,
                )
        except Exception as e:
            q.submit_in_flight = False
            endpoint_name = "open_market" if order_mode == "market" else "open_ioc"
            self._register_private_api_exception(endpoint_name)
            self._quotes.pop(sym, None)
            self._queue_execution_event("submit_rejected", quote=q, reason=str(e), endpoint=endpoint_name, price=price)
            await self.state.add_log("error", f"{endpoint_name} failed {sym}: {e}")
            return True
        q.submit_in_flight = False
        self._register_private_api_result(res, endpoint_name)
        if not res.get("success"):
            self._quotes.pop(sym, None)
            self._queue_execution_event(
                "submit_rejected",
                quote=q,
                reason=str(res.get("message") or res.get("code") or "reject"),
                endpoint=endpoint_name,
                price=price,
                submit_ack_ms=res.get("_latency_ms"),
            )
            await self.state.add_log("warn", f"{endpoint_name} reject {sym}: {res.get('message')}")
            return True
        q.submit_ack_ts = time.time()
        try:
            q.submit_latency_ms = float(res.get("_latency_ms") or 0.0)
        except Exception:
            q.submit_latency_ms = 0.0
        if q.submit_latency_ms <= 0 and q.submit_started_ts > 0:
            q.submit_latency_ms = max(0.0, (q.submit_ack_ts - q.submit_started_ts) * 1000.0)
        self._queue_execution_event(
            "submit_ack",
            quote=q,
            endpoint=endpoint_name,
            price=price,
            submit_ack_ms=q.submit_latency_ms,
        )

        order_id = None
        data = res.get("data") or {}
        if isinstance(data, dict):
            order_id = data.get("orderId") or data.get("order_id")
        try:
            q.order_id = int(order_id) if order_id is not None else None
        except Exception:
            q.order_id = None
        q.price = price
        q.placed_ts = now_ts
        q.fair_at_quote = fair if fair > 0 else q.fair_at_quote
        q.sigma_at_quote = float(agg_stats.sigma_spread or q.sigma_at_quote or 0.0)
        q.z_at_quote = float(agg_stats.z_score or q.z_at_quote or 0.0)
        q.taker_submit_at = None
        q.confirmation_deadline_ts = q.submit_ack_ts + max(1.25, self._loop_tick_sec() * 10.0)
        try:
            q.requested_vol = float(res.get("_requested_vol") or 0.0)
        except Exception:
            q.requested_vol = None
        try:
            final_vol = float(res.get("_final_vol") or 0.0)
        except Exception:
            final_vol = 0.0
        q.final_vol = final_vol if final_vol > 0 else None
        try:
            avg_entry_price = float(res.get("_avg_price") or 0.0)
        except Exception:
            avg_entry_price = 0.0
        q.avg_entry_price = avg_entry_price if avg_entry_price > 0 else None
        if q.requested_vol and q.requested_vol > 0 and final_vol > 0:
            q.fill_ratio = final_vol / q.requested_vol
            if q.fill_seen_ts <= 0:
                q.fill_seen_ts = time.time()
                self._queue_execution_event(
                    "fill_seen",
                    quote=q,
                    reason="order_detail",
                    extra={"fill_detail_latency_ms": res.get("_fill_detail_latency_ms")},
                )
            q.synthetic_pos_raw = self._synthetic_position_from_fill(q, res)
        await self.state.add_log(
            "info",
            f"[real] {order_mode} {sym} {q.side} @ {price:.6g} "
            f"(notional={q.notional:.2f}, lev={q.leverage}, oid={q.order_id})",
        )
        await self._materialize_filled_quote(
            q,
            fair=agg_stats.fair,
            sigma=agg_stats.sigma_spread,
            retry_delays=(0.0, max(0.02, self._loop_tick_sec()), max(0.04, self._loop_tick_sec() * 1.5)),
        )
        return True

    async def _reconcile_quotes(self) -> None:
        now = time.time()
        for sym, q in list(self._quotes.items()):
            if q.taker_submit_at is not None and q.order_id is None:
                await self._submit_due_taker_quote(sym, q, now=now, claimed=False)
                continue

            agg_stats = self.agg.compute_stats(sym)

            # Once an IOC order has been submitted, exchange fill confirmation
            # takes priority over signal freshness. Otherwise we can "forget"
            # a real fill just because open_positions/query_order lags a few
            # hundred milliseconds behind the submit ACK.
            filled = await self._is_quote_filled(q)
            if filled:
                materialized = await self._materialize_filled_quote(
                    q,
                    fair=agg_stats.fair,
                    sigma=agg_stats.sigma_spread,
                    retry_delays=(0.0, 0.3),
                )
                if not materialized:
                    await self.state.add_log("warn", f"[real] fill detected for {sym} but no position")
                continue

            if q.is_ioc and q.submit_ack_ts > 0 and now < max(q.confirmation_deadline_ts, q.submit_ack_ts):
                continue

            z = agg_stats.z_score
            if z is not None and abs(z) < float(self.cfg.strategy.cancel_z):
                await self._cancel_quote(q, reason="z_collapsed")
                continue

            if now - q.placed_ts > float(self.cfg.strategy.quote_timeout_sec):
                await self._cancel_quote(q, reason="timeout")
                continue

            ok_signal, why_signal = self._signal_valid_now(
                sym,
                q.side,
                signal_ts=q.signal_ts,
                spread_bps_at_quote=q.spread_bps_at_quote,
            )
            if not ok_signal:
                await self._cancel_quote(q, reason=f"stale_signal:{why_signal}")
                continue

    async def _is_quote_filled(self, q: _Quote) -> bool:
        # 1) Fast private WS confirmation, when available.
        private_ws = getattr(self, "mexc_private_ws", None)
        try:
            find_position = getattr(private_ws, "find_position", None)
            if callable(find_position):
                ws_pos = await find_position(q.symbol, q.side, max_age_sec=2.0)
                if ws_pos is not None:
                    if q.fill_seen_ts <= 0:
                        q.fill_seen_ts = time.time()
                        self._queue_execution_event("fill_seen", quote=q, reason="private_ws")
                    return True
        except Exception as e:
            logger.debug("quote fill check by private WS failed for %s: %s", q.symbol, e)

        # 2) Check positions via REST cache.
        try:
            raw = await self._get_positions_raw_cached(max_age_sec=self._positions_cache_max_age_sec())
            for p in raw:
                if str(p.get("symbol") or "").upper() != q.symbol:
                    continue
                pt = int(p.get("positionType") or 0)
                want = 1 if q.side == "LONG" else 2
                if pt == want and float(p.get("holdVol") or 0) > 0:
                    if q.fill_seen_ts <= 0:
                        q.fill_seen_ts = time.time()
                        self._queue_execution_event("fill_seen", quote=q, reason="open_positions")
                    return True
        except Exception as e:
            logger.warning("quote fill check by positions failed for %s: %s", q.symbol, e)

        # 3) Query order state (if we have id)
        if q.order_id:
            now_ts = time.time()
            if q.submit_ack_ts > 0 and now_ts < q.submit_ack_ts + self._quote_order_query_first_delay_sec():
                return False
            if q.last_order_query_ts > 0 and now_ts - q.last_order_query_ts < self._quote_order_query_min_interval_sec():
                return False
            try:
                q.last_order_query_ts = now_ts
                res = await self.trader.query_order(int(q.order_id))
                self._register_private_api_result(res, "query_order")
                items = (res or {}).get("data") or []
                if items:
                    od = items[0]
                    state = int(od.get("state") or 0)
                    deal_vol = float(od.get("dealVol") or 0)
                    if q.requested_vol and q.requested_vol > 0 and deal_vol > 0:
                        q.fill_ratio = deal_vol / q.requested_vol
                    if state == 3 or deal_vol > 0:
                        if q.fill_seen_ts <= 0:
                            q.fill_seen_ts = time.time()
                            self._queue_execution_event("fill_seen", quote=q, reason="query_order")
                        return True
                    if state in (4, 5):
                        # canceled/invalid Ð Ð†Ð²Ð‚Â Ð²Ð‚â„¢ drop quote
                        err = od.get("errorCode")
                        order_type = od.get("orderType")
                        price = od.get("price") or q.price
                        order_mode = q.order_mode or ("ioc" if q.is_ioc else "quote")
                        await self.state.add_log(
                            "info",
                            f"[real] {order_mode}_no_fill {q.symbol} {q.side}: "
                            f"state={state} errorCode={err} dealVol={deal_vol:g} "
                            f"orderType={order_type} price={price} oid={q.order_id}",
                        )
                        book = self.agg.get_book(q.symbol)
                        mexc_mid = None
                        depth = None
                        if book and book.best_bid is not None and book.best_ask is not None:
                            mexc_mid = (book.best_bid + book.best_ask) / 2.0
                            depth = book.top_notional(10, contract_size=self.agg.contract_size_for(q.symbol))
                        await self._record_live_candidate_reject(
                            symbol=q.symbol,
                            side=q.side,
                            score=q.entry_score,
                            z=q.z_at_quote,
                            blocked=f"{order_mode}_no_fill_state={state}_err={err}",
                            fair=q.fair_at_quote,
                            mexc=mexc_mid,
                            depth=depth,
                        )
                        self._queue_execution_event(
                            "no_fill",
                            quote=q,
                            reason=f"state={state};errorCode={err}",
                            price=price,
                            extra={"deal_vol": deal_vol, "order_type": order_type},
                        )
                        self._quotes.pop(q.symbol, None)
                        return False
            except Exception:
                self._register_private_api_exception("query_order")
                pass
        return False

    async def _find_position(self, symbol: str, side: str) -> Optional[Dict[str, Any]]:
        private_ws = getattr(self, "mexc_private_ws", None)
        try:
            wait_for_position = getattr(private_ws, "wait_for_position", None)
            find_position = getattr(private_ws, "find_position", None)
            if callable(wait_for_position) and bool(getattr(private_ws, "is_connected", False)):
                ws_pos = await wait_for_position(
                    symbol,
                    side,
                    timeout=min(0.22, max(0.05, self._loop_tick_sec() * 2.0)),
                    max_age_sec=2.0,
                )
                if ws_pos is not None:
                    return ws_pos
            elif callable(find_position):
                ws_pos = await find_position(symbol, side, max_age_sec=2.0)
                if ws_pos is not None:
                    return ws_pos
        except Exception as e:
            logger.debug("find_position private WS failed for %s %s: %s", symbol, side, e)

        try:
            raw = await self._get_positions_raw_cached(
                force=True,
                max_age_sec=0.0,
                min_interval_sec=self._positions_force_refresh_floor_sec(),
            )
        except Exception:
            return None
        want = 1 if side == "LONG" else 2
        for p in raw:
            if str(p.get("symbol") or "").upper() != symbol:
                continue
            if int(p.get("positionType") or 0) == want and float(p.get("holdVol") or 0) > 0:
                return p
        return None

    async def _materialize_filled_quote(
        self,
        q: _Quote,
        *,
        fair: Optional[float],
        sigma: Optional[float],
        retry_delays: tuple[float, ...] = (0.0,),
    ) -> bool:
        if q.materialize_in_flight:
            for _ in range(5):
                existing = self.state.positions.get(q.symbol)
                if existing is not None and existing.side == q.side and not existing.closed and existing.qty > 0:
                    return True
                await asyncio.sleep(0.01)
            return False

        q.materialize_in_flight = True
        pos_raw: Optional[Dict[str, Any]] = None
        try:
            if q.synthetic_pos_raw is not None:
                pos_raw = q.synthetic_pos_raw
            else:
                for delay_sec in retry_delays:
                    if delay_sec > 0:
                        await asyncio.sleep(delay_sec)
                    pos_raw = await self._find_position(q.symbol, q.side)
                    if pos_raw is not None:
                        break
            if pos_raw is None:
                return False
            if q.fill_seen_ts <= 0:
                q.fill_seen_ts = time.time()

            self._quotes.pop(q.symbol, None)
            existing = self.state.positions.get(q.symbol)
            if existing is not None and existing.side == q.side and not existing.closed and existing.qty > 0:
                return True
            await self._open_position_from_exchange(q, pos_raw, fair, sigma)
            pos = self.state.positions.get(q.symbol)
            if q.order_mode == "market":
                min_fill_ratio = self._taker_market_min_fill_ratio_for(q.symbol)
            else:
                min_fill_ratio = self._taker_ioc_min_fill_ratio_for(q.symbol)
            if (
                pos is not None
                and q.fill_ratio is not None
                and q.fill_ratio > 0
                and q.fill_ratio < min_fill_ratio
            ):
                await self.state.add_log(
                    "warn",
                    f"[real] {q.symbol} fill_ratio {q.fill_ratio:.2f} < {min_fill_ratio:.2f}; flattening",
                )
                pos.exit_signal_ts = time.time()
                await self._close_market(pos, pos_raw, reason="bad_fill_ratio")
            return True
        finally:
            q.materialize_in_flight = False

    async def _cancel_quote(self, q: _Quote, *, reason: str) -> None:
        try:
            await self.trader.cancel_all_for(q.symbol)
        except Exception as e:
            logger.warning("cancel_all_for %s failed: %s", q.symbol, e)
        self._queue_execution_event("no_fill", quote=q, reason=reason)
        self._quotes.pop(q.symbol, None)
        await self.state.add_log("debug", f"[real] cancel {q.symbol}: {reason}")

    @staticmethod
    def _position_side(pos_raw: Dict[str, Any]) -> Optional[str]:
        try:
            pt = int(pos_raw.get("positionType") or 0)
        except Exception:
            pt = 0
        if pt == 1:
            return "LONG"
        if pt == 2:
            return "SHORT"
        return None

    @staticmethod
    def _exchange_ts_to_epoch(raw_ts: Any) -> float:
        try:
            ts = float(raw_ts or 0.0)
        except Exception:
            return 0.0
        if ts <= 0:
            return 0.0
        if ts >= 1e11:
            ts /= 1000.0
        return ts

    @staticmethod
    def _extract_position_id(pos_raw: Dict[str, Any]) -> Optional[int]:
        for key in ("positionId", "positionID", "position_id", "id"):
            val = pos_raw.get(key)
            if val is None:
                continue
            try:
                return int(float(val))
            except Exception:
                continue
        return None

    def _synthetic_position_from_fill(self, q: _Quote, res: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        vol = q.final_vol
        entry = q.avg_entry_price or q.price
        if vol is None or vol <= 0 or entry <= 0:
            return None
        pos_type = 1 if q.side == "LONG" else 2
        out: Dict[str, Any] = {
            "symbol": q.symbol,
            "positionType": pos_type,
            "holdVol": vol,
            "holdAvgPrice": entry,
            "openAvgPrice": entry,
            "newOpenAvgPrice": entry,
            "leverage": q.leverage,
            "createTime": int(max(q.submit_ack_ts or time.time(), q.submit_started_ts or 0.0) * 1000.0),
        }
        raw_pid = res.get("_position_id")
        if raw_pid is not None:
            try:
                out["positionId"] = int(float(raw_pid))
            except Exception:
                pass
        return out

    @staticmethod
    def _history_position_side(pos_raw: Dict[str, Any]) -> Optional[str]:
        return RealExecutor._position_side(pos_raw)

    @staticmethod
    def _history_position_realized(pos_raw: Dict[str, Any]) -> Optional[float]:
        for key in ("realised", "realized", "closeProfitLoss", "profit", "pnl"):
            val = pos_raw.get(key)
            if val is None:
                continue
            try:
                return float(val)
            except Exception:
                continue
        return None

    @staticmethod
    def _history_position_price(pos_raw: Dict[str, Any], *keys: str) -> Optional[float]:
        for key in keys:
            val = pos_raw.get(f"{key}FullyScale")
            if val is None:
                continue
            try:
                price = float(val)
            except Exception:
                continue
            if price > 0:
                return price
        for key in keys:
            val = pos_raw.get(key)
            if val is None:
                continue
            try:
                price = float(val)
            except Exception:
                continue
            if price > 0:
                return price
        return None

    def _realized_to_exit_price(self, pos: ManagedPosition, realized: float) -> Optional[float]:
        denom = float(pos.qty or 0.0) * float(pos.contract_size or 0.0)
        if pos.entry_price <= 0 or denom <= 0:
            return None
        if pos.side == "LONG":
            return pos.entry_price + (realized / denom)
        return pos.entry_price - (realized / denom)

    def _history_match_score(self, pos: ManagedPosition, row: Dict[str, Any], now_ts: float) -> Optional[float]:
        row_symbol = str(row.get("symbol") or "").upper()
        if row_symbol != pos.symbol:
            return None

        row_side = self._history_position_side(row)
        if row_side != pos.side:
            return None

        try:
            row_state = int(row.get("state") or 0)
        except Exception:
            row_state = 0
        if row_state not in (0, 3):
            return None

        score = 0.0
        row_pid = self._extract_position_id(row)
        if pos.mexc_position_id is not None and row_pid is not None:
            if row_pid == pos.mexc_position_id:
                score -= 1_000_000.0
            else:
                return None

        close_ts = self._exchange_ts_to_epoch(row.get("updateTime") or row.get("closeTime"))
        open_ts = self._exchange_ts_to_epoch(row.get("createTime") or row.get("openTime"))
        if pos.open_ts > 0:
            if close_ts > 0 and close_ts + 0.25 < pos.open_ts:
                return None
            if open_ts > 0 and abs(open_ts - pos.open_ts) > 30.0:
                return None
        if close_ts > 0:
            score += min(abs(close_ts - now_ts), 600.0)

        if open_ts > 0 and pos.open_ts > 0:
            score += min(abs(open_ts - pos.open_ts), 300.0) * 0.1

        row_entry = self._history_position_price(row, "openAvgPrice", "holdAvgPrice")
        if row_entry is not None and pos.entry_price > 0:
            score += min(abs(row_entry - pos.entry_price) / pos.entry_price * 10_000.0, 500.0)

        try:
            row_vol = float(row.get("closeVol") or row.get("holdVol") or 0.0)
        except Exception:
            row_vol = 0.0
        if row_vol > 0 and pos.qty > 0:
            score += min(abs(row_vol - pos.qty) / pos.qty * 100.0, 200.0)

        return score

    async def _resolve_external_close_details(
        self,
        pos: ManagedPosition,
        *,
        fallback_exit_price: float,
    ) -> Dict[str, Any]:
        default_realized: Optional[float] = None
        if abs(float(pos.last_pnl_usdt or 0.0)) > 1e-12:
            default_realized = float(pos.last_pnl_usdt)

        best_row: Optional[Dict[str, Any]] = None
        best_score: Optional[float] = None
        start_ms = int(max(0.0, pos.open_ts - 30.0) * 1000.0) if pos.open_ts > 0 else None
        now_ts = time.time()
        for attempt, delay in enumerate(self._external_close_history_delays()):
            if delay > 0:
                await asyncio.sleep(delay)
            now_ts = time.time()
            end_ms = int((now_ts + 30.0) * 1000.0)
            try:
                rows = await self.trader.get_history_positions(
                    symbol_full=pos.symbol,
                    start_time=start_ms,
                    end_time=end_ms,
                    limit=100,
                )
            except Exception as e:
                await self.state.add_log("debug", f"[real] history_positions lookup failed for {pos.symbol}: {e}")
                rows = []

            for row in rows:
                score = self._history_match_score(pos, row, now_ts)
                if score is None:
                    continue
                if best_score is None or score < best_score:
                    best_score = score
                    best_row = row

            if best_row is not None:
                break
            if attempt > 0:
                await self.state.add_log(
                    "debug",
                    f"[real] history_positions not ready for {pos.symbol}; retry={attempt}",
                )

        if best_row is not None:
            realized = self._history_position_realized(best_row)
            exit_price = self._history_position_price(best_row, "closeAvgPrice", "dealAvgPrice")
            if exit_price is None and realized is not None:
                exit_price = self._realized_to_exit_price(pos, realized)
            if exit_price is None or exit_price <= 0:
                exit_price = fallback_exit_price
            if realized is None:
                if pos.side == "LONG":
                    realized = (exit_price - pos.entry_price) * pos.qty * pos.contract_size
                else:
                    realized = (pos.entry_price - exit_price) * pos.qty * pos.contract_size
            close_ts = self._exchange_ts_to_epoch(best_row.get("updateTime") or best_row.get("closeTime")) or now_ts
            return {
                "exit_price": exit_price,
                "realized_pnl": realized,
                "close_ts": close_ts,
                "price_source": "exchange_history",
                "position_id": self._extract_position_id(best_row),
            }

        if default_realized is not None:
            implied_exit = self._realized_to_exit_price(pos, default_realized)
            if implied_exit is not None and implied_exit > 0:
                return {
                    "exit_price": implied_exit,
                    "realized_pnl": default_realized,
                    "close_ts": now_ts,
                    "price_source": "exchange_unrealized_fallback",
                    "position_id": pos.mexc_position_id,
                }

        if pos.side == "LONG":
            realized = (fallback_exit_price - pos.entry_price) * pos.qty * pos.contract_size
        else:
            realized = (pos.entry_price - fallback_exit_price) * pos.qty * pos.contract_size
        return {
            "exit_price": fallback_exit_price,
            "realized_pnl": realized,
            "close_ts": now_ts,
            "price_source": "exchange_fallback",
            "position_id": pos.mexc_position_id,
        }

    async def _open_position_from_exchange(
        self, q: _Quote, pos_raw: Dict[str, Any],
        fair: Optional[float], sigma: Optional[float],
    ) -> None:
        try:
            entry = float(self._history_position_price(
                pos_raw,
                "newOpenAvgPrice",
                "holdAvgPrice",
                "openAvgPrice",
            ) or 0.0)
            hold_vol = float(pos_raw.get("holdVol") or 0)
        except Exception:
            entry = 0.0
            hold_vol = 0.0
        if entry <= 0 or hold_vol <= 0:
            await self.state.add_log("warn", f"[real] bad position state on {q.symbol}")
            return

        # Keep contracts and contract size separate so live exits can use the
        # same executable-price logic as paper.
        info = await self.trader.api.get_contract_info_cached(q.symbol)
        contract_size = float((info or {}).get("contractSize") or 1.0)

        now = time.time()

        f_value = fair if fair is not None else q.fair_at_quote
        tp_at_fair = f_value if self._use_fair_tp_for(q.symbol) else None
        stats = self._evaluated_stats(q.symbol)
        entry_spread_bps = None
        if f_value and f_value > 0:
            if q.side == "LONG":
                entry_spread_bps = ((entry - f_value) / f_value) * 1e4
            else:
                entry_spread_bps = ((f_value - entry) / f_value) * 1e4
        resolved_entry_score = float(getattr(stats, "score", 0.0) or 0.0)
        if resolved_entry_score <= 0 and q.entry_score:
            resolved_entry_score = float(q.entry_score)
        pos = ManagedPosition(
            symbol=q.symbol,
            side=q.side,
            entry_price=entry,
            notional_usdt=q.notional,
            margin_usdt=q.margin,
            leverage=q.leverage,
            qty=hold_vol,
            open_ts=now,
            fair_at_open=f_value,
            sigma_at_open=max(sigma if sigma is not None else q.sigma_at_quote, 0.0),
            contract_size=contract_size,
            quote_ts=q.placed_ts,
            tp_price=tp_at_fair,
            best_excursion=entry,
            signal_ts=q.signal_ts,
            entry_latency_ms=(now - q.signal_ts) * 1000.0 if q.signal_ts > 0 else 0.0,
            entry_confirm_latency_ms=(q.fill_seen_ts - q.signal_ts) * 1000.0
            if q.signal_ts > 0 and q.fill_seen_ts > 0 else 0.0,
            entry_algo=q.entry_algo,
            entry_score=resolved_entry_score,
            max_hold_sec=self._max_hold_sec_for(q.symbol),
            entry_spread_bps=entry_spread_bps,
            entry_mexc_book_age_ms=stats.mexc_book_age_ms,
            entry_binance_book_age_ms=stats.binance_book_age_ms,
            submit_latency_ms=max(0.0, float(q.submit_latency_ms or 0.0)),
            fill_seen_latency_ms=max(0.0, (q.fill_seen_ts - q.submit_ack_ts) * 1000.0)
            if q.fill_seen_ts > 0 and q.submit_ack_ts > 0 else 0.0,
            managed_latency_ms=max(0.0, (now - q.fill_seen_ts) * 1000.0)
            if q.fill_seen_ts > 0 else 0.0,
            end_to_end_entry_ms=max(0.0, (now - q.signal_ts) * 1000.0)
            if q.signal_ts > 0 else 0.0,
        )
        # Price-based backstop SL. At 100x leverage: 0.25% price = 25% margin
        # loss, above max observed 2s hold excursion (0.205%). Fires only on crash.
        sl_pct = self._sl_pct_for(q.symbol)
        sl_dist = entry * sl_pct
        pos.initial_sl_distance = sl_dist
        if q.side == "LONG":
            sl_raw = entry - sl_dist
        else:
            sl_raw = entry + sl_dist
        tick = await self._tick_size(q.symbol)
        sl_price, _ = _snap_price(sl_raw, tick, q.side, for_sl=True)
        pos.stop_price = sl_price

        # Try to attach exchange SL+TP
        pid = self._extract_position_id(pos_raw)
        if pid is not None:
            pos.mexc_position_id = pid
            asyncio.create_task(self._attach_initial_stop_loss(q.symbol, q.side, pid, sl_price))

        async with self.state.lock:
            self.state.positions[q.symbol] = pos
        await self._persist_managed_position(pos)
        self._queue_execution_event("position_managed", quote=q, pos=pos, reason="open_position_visible")

        async with self.state.lock:
            self.state.positions[q.symbol] = pos
        await self._persist_managed_position(pos)
        self._raw_missing_counts.pop(q.symbol, None)
        await self.state.add_log(
            "info",
            f"[real] OPEN {q.symbol} {q.side} @ {entry:.6g} "
            f"(F={pos.fair_at_open:.6g}, SL={sl_price:.6g}, qty={hold_vol:.6g})",
        )

    async def _attach_initial_stop_loss(self, symbol: str, side: str, position_id: int, sl_price: float) -> None:
        key = f"{symbol}:{position_id}"
        if key in self._stop_attach_in_flight:
            return
        self._stop_attach_in_flight.add(key)
        try:
            res = await self.trader.place_stop_by_position(
                position_id,
                stop_loss_price=sl_price,
                take_profit_price=None,
                side=side,
            )
            if not res.get("success"):
                await self.state.add_log("warn", f"[real] SL place failed {symbol}: {res.get('message')}")
                return
            data = res.get("data")
            if isinstance(data, list):
                data = data[0] if data else None
            stop_plan_id: Optional[int] = None
            if isinstance(data, dict):
                for k in ("stopPlanOrderId", "stopPlanOrderID", "stopPlanId", "id"):
                    if data.get(k) is not None:
                        try:
                            stop_plan_id = int(float(data[k]))
                            break
                        except Exception:
                            pass
            pos = self.state.positions.get(symbol)
            if pos is not None and not pos.closed:
                pos.mexc_position_id = position_id
                if stop_plan_id is not None:
                    pos.mexc_stop_plan_id = stop_plan_id
                async with self.state.lock:
                    self.state.positions[symbol] = pos
                await self._persist_managed_position(pos)
        except Exception as e:
            await self.state.add_log("error", f"[real] SL place exception {symbol}: {e}")
        finally:
            self._stop_attach_in_flight.discard(key)

    async def _recover_untracked_positions(self, raw_by_sym: Dict[str, Dict[str, Any]]) -> None:
        """Recover live exchange positions that are not present in state.positions.

        This covers the ugly but expensive case where the exchange already holds
        a position while our in-memory state forgot it (restart, transient fill
        detection miss, or quote reconciliation race). Ignoring those positions
        leaves them unmanaged and invisible in the UI.
        """
        now = time.time()
        managed_symbols = self._managed_symbols()
        for sym, raw in raw_by_sym.items():
            if managed_symbols is not None and sym not in managed_symbols:
                if sym not in self._foreign_position_warned:
                    self._foreign_position_warned.add(sym)
                    await self.state.add_log(
                        "warn",
                        f"[real] ignoring foreign account position {sym}; outside this line universe",
                    )
                continue
            if sym in self.state.positions:
                continue
            side = self._position_side(raw)
            if side is None:
                continue

            pending = self._quotes.pop(sym, None)
            stats = self.agg.compute_stats(sym)
            if pending is not None:
                if pending.side != side:
                    pending = _Quote(
                        symbol=pending.symbol,
                        side=side,
                        price=pending.price,
                        notional=pending.notional,
                        margin=pending.margin,
                        leverage=pending.leverage,
                        placed_ts=pending.placed_ts,
                        fair_at_quote=pending.fair_at_quote,
                        sigma_at_quote=pending.sigma_at_quote,
                        z_at_quote=pending.z_at_quote,
                        order_id=pending.order_id,
                        signal_ts=pending.signal_ts,
                        entry_algo=pending.entry_algo,
                        entry_score=pending.entry_score,
                    )
                await self._open_position_from_exchange(pending, raw, stats.fair, stats.sigma_spread)
                continue

            metrics = await self.trader.position_metrics(raw)
            if not metrics:
                continue

            entry = float(metrics.get("open_price") or 0.0)
            hold_vol = float(metrics.get("hold_vol") or 0.0)
            if entry <= 0 or hold_vol <= 0:
                continue

            contract_size = self.agg.contract_size_for(sym)
            leverage = int(float(metrics.get("leverage") or raw.get("leverage") or 1.0))
            margin = float(metrics.get("margin") or 0.0)
            notional = float(metrics.get("notional") or 0.0)
            fair_value = float(stats.fair) if (stats.fair is not None and float(stats.fair) > 0) else entry
            sigma_value = max(float(stats.sigma_spread or 0.0), 0.0)
            exchange_open_ts = self._exchange_position_open_ts(raw, now)

            tp_at_fair = fair_value if self._use_fair_tp_for(sym) else None
            entry_spread_bps = None
            if fair_value > 0:
                if side == "LONG":
                    entry_spread_bps = ((entry - fair_value) / fair_value) * 1e4
                else:
                    entry_spread_bps = ((fair_value - entry) / fair_value) * 1e4

            pos: Optional[ManagedPosition] = None
            restored_from_persisted = False
            persisted_payload = await self._load_persisted_position(sym)
            if persisted_payload:
                if self._persisted_position_matches(
                    persisted_payload,
                    raw=raw,
                    side=side,
                    entry_price=entry,
                    exchange_open_ts=exchange_open_ts,
                ):
                    try:
                        pos = ManagedPosition(**persisted_payload)
                        restored_from_persisted = True
                    except Exception as e:
                        logger.warning("rehydrate managed position failed for %s: %s", sym, e)
                        await self._delete_persisted_position(sym)
                else:
                    await self._delete_persisted_position(sym)

            if pos is None:
                pos = ManagedPosition(
                    symbol=sym,
                    side=side,
                    entry_price=entry,
                    notional_usdt=notional,
                    margin_usdt=margin,
                    leverage=leverage,
                    qty=hold_vol,
                    open_ts=exchange_open_ts,
                    fair_at_open=fair_value,
                    sigma_at_open=sigma_value,
                    contract_size=contract_size,
                    quote_ts=exchange_open_ts,
                    signal_ts=exchange_open_ts,
                    entry_algo="recovered",
                    entry_score=float(stats.score or 0.0),
                    max_hold_sec=self._max_hold_sec_for(sym),
                    tp_price=tp_at_fair,
                    best_excursion=entry,
                    entry_spread_bps=entry_spread_bps,
                    entry_mexc_book_age_ms=stats.mexc_book_age_ms,
                    entry_binance_book_age_ms=stats.binance_book_age_ms,
                )

            pos.symbol = sym
            pos.side = side
            pos.entry_price = entry
            pos.notional_usdt = notional
            pos.margin_usdt = margin
            pos.leverage = leverage
            pos.qty = hold_vol
            pos.open_ts = exchange_open_ts
            pos.contract_size = contract_size
            pos.max_hold_sec = self._max_hold_sec_for(sym)
            pos.entry_spread_bps = entry_spread_bps
            pos.entry_mexc_book_age_ms = stats.mexc_book_age_ms
            pos.entry_binance_book_age_ms = stats.binance_book_age_ms
            pos.fair_at_open = pos.fair_at_open if pos.fair_at_open > 0 else fair_value
            pos.sigma_at_open = pos.sigma_at_open if pos.sigma_at_open > 0 else sigma_value
            pos.tp_price = tp_at_fair if tp_at_fair is not None else pos.tp_price
            if pos.quote_ts <= 0:
                pos.quote_ts = exchange_open_ts
            if pos.signal_ts <= 0:
                pos.signal_ts = exchange_open_ts
            if pos.best_excursion is None or pos.best_excursion <= 0:
                pos.best_excursion = entry
            pos.last_pnl_usdt = float(metrics.get("pnl") or 0.0)
            pos.last_pnl_pct = float(metrics.get("pnl_pct") or 0.0)
            pos.mexc_position_id = self._extract_position_id(raw)
            if pos.mexc_position_id is not None and not pos.mexc_stop_plan_id and pos.stop_price:
                asyncio.create_task(
                    self._attach_initial_stop_loss(
                        sym,
                        side,
                        int(pos.mexc_position_id),
                        float(pos.stop_price),
                    )
                )

            sl_pct = self._sl_pct_for(sym)
            sl_dist = entry * sl_pct
            pos.initial_sl_distance = sl_dist
            if side == "LONG":
                sl_raw = entry - sl_dist
            else:
                sl_raw = entry + sl_dist
            tick = await self._tick_size(sym)
            sl_price, _ = _snap_price(sl_raw, tick, side, for_sl=True)
            pos.stop_price = sl_price

            async with self.state.lock:
                self.state.positions[sym] = pos
            await self._persist_managed_position(pos)
            self._raw_missing_counts.pop(sym, None)
            await self.state.add_log(
                "warn",
                (
                    f"[real] restored persisted position {sym} {side} @ {entry:.6g}"
                    if restored_from_persisted
                    else f"[real] recovered untracked position {sym} {side} @ {entry:.6g}"
                ),
            )

    async def _reconcile_positions(self) -> None:
        s = self.cfg.strategy
        now = time.time()

        # Pull fresh position list once per tick
        raw_refresh_failed = False
        try:
            raw_list = await self._get_positions_raw_cached(
                max_age_sec=self._positions_cache_max_age_sec()
            )
        except Exception as e:
            self._register_private_api_exception("open_positions")
            raw_refresh_failed = True
            raw_list = []
            if now - self._positions_refresh_error_last_ts >= 5.0:
                self._positions_refresh_error_last_ts = now
                await self.state.add_log("warn", f"[real] positions refresh failed: {e}")
        raw_by_sym: Dict[str, Dict[str, Any]] = {
            str(p.get("symbol") or "").upper(): p for p in raw_list
        }
        if not raw_refresh_failed and raw_by_sym:
            await self._recover_untracked_positions(raw_by_sym)

        for sym, pos in list(self.state.positions.items()):
            book = self.agg.get_book(sym)
            mid = book.mid if book else None
            raw = raw_by_sym.get(sym)

            if not raw:
                if raw_refresh_failed:
                    # Keep managing the position even when the exchange
                    # snapshot temporarily fails. Forgetting the position here
                    # leaves a live trade unmanaged on the venue.
                    raw = {
                        "symbol": sym,
                        "holdVol": pos.qty,
                        "leverage": pos.leverage,
                        "unrealizedPnl": pos.last_pnl_usdt,
                    }
                else:
                    self._raw_missing_first_ts.setdefault(sym, now)
                    misses = self._raw_missing_counts.get(sym, 0) + 1
                    self._raw_missing_counts[sym] = misses
                    if misses < 2:
                        raw = {
                            "symbol": sym,
                            "holdVol": pos.qty,
                            "leverage": pos.leverage,
                            "unrealizedPnl": pos.last_pnl_usdt,
                        }
                        await self.state.add_log(
                            "warn",
                            f"[real] {sym} missing from positions snapshot; waiting for confirmation",
                        )
                    else:
                        # Position really disappeared from the exchange view:
                        # treat it as exchange-side/manual close.
                        self._raw_missing_counts.pop(sym, None)
                        fallback_exit_price = mid if mid is not None else pos.entry_price
                        close_details = await self._resolve_external_close_details(
                            pos,
                            fallback_exit_price=fallback_exit_price,
                        )
                        if close_details.get("position_id") is not None:
                            pos.mexc_position_id = close_details.get("position_id")
                        if str(close_details.get("price_source") or "") == "exchange_fallback":
                            missing_age = time.time() - float(self._raw_missing_first_ts.get(sym, time.time()))
                            min_missing = float(getattr(self, "_external_close_fallback_min_missing_sec", 8.0) or 0.0)
                            if missing_age < min_missing:
                                await self.state.add_log(
                                    "warn",
                                    f"[real] {sym} still missing but exchange history is not ready; "
                                    f"holding local position ({missing_age:.1f}s/{min_missing:.1f}s)",
                                )
                                continue
                        pos.exit_signal_ts = time.time()  # Mark exit decision time
                        await self._mark_closed_externally(
                            pos,
                            exit_price=float(close_details["exit_price"]),
                            reason="exchange_close",
                            price_source=str(close_details["price_source"]),
                            realized_pnl=float(close_details["realized_pnl"]),
                            close_ts=float(close_details["close_ts"]),
                        )
                        continue
            else:
                self._raw_missing_counts.pop(sym, None)
                self._raw_missing_first_ts.pop(sym, None)

            # Update PnL from raw if available
            try:
                pnl_field = (raw.get("unrealizedPnl") or raw.get("unrealizedProfit")
                             or raw.get("unRealizedProfit"))
                if pnl_field is not None:
                    pos.last_pnl_usdt = float(pnl_field)
            except Exception:
                pass
            if pos.margin_usdt > 0:
                pos.last_pnl_pct = pos.last_pnl_usdt / pos.margin_usdt * 100.0

            # Track the exchange-reported live PnL as bps too. The book-derived
            # exit price can miss a brief profitable excursion; MEXC unrealizedPnl
            # is the ground truth the terminal user sees.
            raw_pnl_bps = None
            try:
                denom = float(pos.entry_price or 0.0) * float(pos.qty or 0.0) * float(pos.contract_size or 0.0)
                if denom > 0:
                    raw_pnl_bps = (float(pos.last_pnl_usdt or 0.0) / denom) * 1e4
                    if raw_pnl_bps > pos.best_realized_bps:
                        pos.best_realized_bps = raw_pnl_bps
            except Exception:
                raw_pnl_bps = None

            max_open_loss = float(getattr(self.cfg.risk, "max_open_loss_per_position_usdt", 0.0) or 0.0)
            if max_open_loss > 0 and pos.last_pnl_usdt <= -max_open_loss:
                pos.exit_signal_ts = now
                await self._close_market(pos, raw, reason="loss_cap")
                continue

            # Time-based emergency close must still work when the book is stale.
            max_hold_sec = pos.max_hold_sec if pos.max_hold_sec > 0 else float(s.max_hold_sec)
            if now - pos.open_ts > max_hold_sec:
                await self._close_market(pos, raw, reason="time")
                continue

            if mid is None:
                continue

            # Update best excursion
            if pos.side == "LONG":
                if pos.best_excursion is None or mid > pos.best_excursion:
                    pos.best_excursion = mid
            else:
                if pos.best_excursion is None or mid < pos.best_excursion:
                    pos.best_excursion = mid

            stats = self.agg.compute_stats(sym)
            current_fair = float(stats.fair) if getattr(stats, "fair", None) else None
            current_imbalance = getattr(stats, "mexc_book_imbalance", None)
            exit_price_now = _realisable_exit_price(pos, book)
            move_bps_now = _realized_bps_at_price(pos, exit_price_now)
            if raw_pnl_bps is not None and raw_pnl_bps > move_bps_now:
                move_bps_now = raw_pnl_bps
            if move_bps_now > pos.best_realized_bps:
                pos.best_realized_bps = move_bps_now
            residual_edge_bps = _residual_edge_bps(pos, current_fair, exit_price_now)
            age_sec = now - pos.open_ts

            # Fair-value TP: mean-reversion trades should realize the reversion
            # instead of waiting for the time backstop.
            if pos.tp_price is not None:
                tp_hit = False
                if pos.side == "LONG" and book.best_bid is not None and book.best_bid >= pos.tp_price:
                    tp_hit = True
                if pos.side == "SHORT" and book.best_ask is not None and book.best_ask <= pos.tp_price:
                    tp_hit = True
                if tp_hit:
                    pos.exit_signal_ts = now
                    await self._close_market(pos, raw, reason="tp")
                    continue

            # Fast profit-taking: this is where paper books many of its small
            # wins, especially on PEPE-style short holds.
            scalp_tp_bps = self._scalp_take_profit_bps_for(sym)
            if scalp_tp_bps > 0 and pos.entry_price > 0:
                if move_bps_now >= scalp_tp_bps:
                    pos.exit_signal_ts = now
                    await self._close_market(pos, raw, reason="scalp_tp")
                    continue

            profit_protect_arm_bps = self._float_setting_for(sym, "profit_protect_arm_bps")
            profit_giveback_bps = self._float_setting_for(sym, "profit_giveback_bps")
            fast_profit_arm_bps = self._float_setting_for(sym, "fast_profit_arm_bps")
            fast_profit_giveback_bps = self._float_setting_for(sym, "fast_profit_giveback_bps")
            profit_protect_min_bps = self._float_setting_for(sym, "profit_protect_min_bps")
            edge_collapse_exit_bps = self._float_setting_for(sym, "edge_collapse_exit_bps")
            active_giveback_bps = profit_giveback_bps
            if (
                fast_profit_arm_bps > 0
                and pos.best_realized_bps >= fast_profit_arm_bps
                and fast_profit_giveback_bps > 0
            ):
                active_giveback_bps = fast_profit_giveback_bps
            if (
                profit_protect_arm_bps > 0
                and active_giveback_bps > 0
                and pos.best_realized_bps >= profit_protect_arm_bps
                and move_bps_now >= profit_protect_min_bps
                and move_bps_now <= max(
                    profit_protect_min_bps,
                    pos.best_realized_bps - active_giveback_bps,
                )
            ):
                pos.exit_signal_ts = now
                await self._close_market(pos, raw, reason="profit_giveback")
                continue
            if _should_profit_protect_exit(
                move_bps_now,
                pos.best_realized_bps,
                residual_edge_bps,
                arm_bps=profit_protect_arm_bps,
                giveback_bps=profit_giveback_bps,
                fast_arm_bps=fast_profit_arm_bps,
                fast_giveback_bps=fast_profit_giveback_bps,
                min_profit_bps=profit_protect_min_bps,
                edge_collapse_bps=edge_collapse_exit_bps,
            ):
                pos.exit_signal_ts = now
                await self._close_market(pos, raw, reason="profit_protect")
                continue

            do_settled_profit_exit, _ = _update_settled_profit_state(
                pos,
                now,
                move_bps_now,
                residual_edge_bps,
                hold_sec=self._float_setting_for(sym, "settled_profit_sec"),
                min_bps=self._float_setting_for(sym, "settled_profit_min_bps"),
                max_drift_bps=self._float_setting_for(sym, "settled_profit_max_drift_bps"),
                edge_bps=self._float_setting_for(sym, "settled_profit_edge_bps"),
            )
            if do_settled_profit_exit:
                pos.exit_signal_ts = now
                await self._close_market(pos, raw, reason="settled_profit")
                continue

            if _should_bad_entry_exit(
                pos,
                age_sec,
                move_bps_now,
                residual_edge_bps,
                guard_sec=self._float_setting_for(sym, "bad_entry_guard_sec"),
                min_age_sec=self._float_setting_for(sym, "bad_entry_min_age_sec"),
                bad_entry_spread_bps=self._float_setting_for(sym, "bad_entry_spread_bps"),
                exit_bps=self._float_setting_for(sym, "bad_entry_exit_bps"),
                edge_collapse_bps=edge_collapse_exit_bps,
            ):
                pos.exit_signal_ts = now
                await self._close_market(pos, raw, reason="bad_entry")
                continue

            edge_loss_after_sec = self._float_setting_for(sym, "edge_loss_after_sec")
            edge_loss_exit_bps = self._float_setting_for(sym, "edge_loss_exit_bps")
            if (
                edge_loss_after_sec > 0
                and age_sec >= edge_loss_after_sec
                and move_bps_now <= edge_loss_exit_bps
                and residual_edge_bps is not None
                and residual_edge_bps <= edge_collapse_exit_bps
            ):
                pos.exit_signal_ts = now
                await self._close_market(pos, raw, reason="edge_loss")
                continue

            # Quick stop: if price moved against us beyond threshold, exit immediately
            # (before dead_trade check so it fires first)
            quick_stop_bps = float(getattr(self.cfg.strategy, "quick_stop_bps", 0.0) or 0.0)
            if quick_stop_bps < 0 and move_bps_now <= quick_stop_bps:
                pos.exit_signal_ts = now
                await self._close_market(pos, raw, reason="quick_stop")
                continue

            dead_trade_after_sec = self._float_setting_for(sym, "dead_trade_after_sec")
            dead_trade_max_bps = self._float_setting_for(sym, "dead_trade_max_bps")
            if (
                dead_trade_after_sec > 0
                and age_sec >= dead_trade_after_sec
                and move_bps_now <= dead_trade_max_bps
                and residual_edge_bps is not None
                and residual_edge_bps <= edge_collapse_exit_bps
            ):
                pos.exit_signal_ts = now
                await self._close_market(pos, raw, reason="dead_trade")
                continue

            # Scratch exit: if the move has not worked quickly enough, flatten
            # before the stale trade degrades into a worse time-close.
            scratch_exit_sec = self._scratch_exit_sec_for(sym)
            scratch_exit_bps = self._scratch_exit_bps_for(sym)
            if scratch_exit_sec > 0 and pos.entry_price > 0 and age_sec >= scratch_exit_sec:
                if move_bps_now <= scratch_exit_bps:
                    pos.exit_signal_ts = now
                    await self._close_market(pos, raw, reason="scratch")
                    continue

            # Signal-flip exit: if order-book pressure has turned against us,
            # leave early instead of leaning on the time stop alone.
            if bool(getattr(s, "signal_flip_exit", False)):
                cur_imb = current_imbalance
                if cur_imb is not None:
                    exit_thr = float(getattr(s, "imbalance_exit_log", 0.0) or 0.0)
                    flipped = False
                    if pos.side == "LONG" and cur_imb < -exit_thr:
                        flipped = True
                    elif pos.side == "SHORT" and cur_imb > exit_thr:
                        flipped = True
                    if flipped:
                        pos.exit_signal_ts = now
                        await self._close_market(pos, raw, reason="signal_flip")
                        continue

            # SL trailing update
            if now - pos.last_sl_update_ts >= float(s.sl_update_throttle_sec):
                hard_sl = _hard_sl_price_fraction(self.cfg, pos.leverage)
                if pos.side == "LONG":
                    new_sl = _trail_long(
                        pos.entry_price, pos.best_excursion or pos.entry_price,
                        pos.sigma_at_open, hard_sl,
                        s.breakeven_at_sigma, s.trail_dist_sigma,
                        pos.stop_price,
                    )
                else:
                    new_sl = _trail_short(
                        pos.entry_price, pos.best_excursion or pos.entry_price,
                        pos.sigma_at_open, hard_sl,
                        s.breakeven_at_sigma, s.trail_dist_sigma,
                        pos.stop_price,
                    )

                if pos.stop_price is None or abs(new_sl - pos.stop_price) > abs(pos.entry_price) * 1e-6:
                    tick = await self._tick_size(sym)
                    snapped, _ = _snap_price(new_sl, tick, pos.side, for_sl=True)
                    if pos.mexc_stop_plan_id:
                        try:
                            res = await self.trader.change_stop_plan_price(
                                int(pos.mexc_stop_plan_id),
                                stop_loss_price=snapped, side=pos.side,
                            )
                            if res.get("success"):
                                pos.stop_price = snapped
                        except Exception as e:
                            logger.warning("change_stop_plan_price failed: %s", e)
                    else:
                        pos.stop_price = snapped
            pos.last_sl_update_ts = now

            await self._persist_managed_position(pos)

    async def _submit_close_reduce_only(
        self,
        symbol: str,
        side: str,
        hold_vol: float,
        lev: int,
        *,
        price: Optional[float] = None,
        position_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        method = self.trader.close_reduce_only
        kwargs: Dict[str, Any] = {"margin_mode": 1}
        try:
            params = inspect.signature(method).parameters
            if "price" in params:
                kwargs["price"] = price
            if "position_id" in params:
                kwargs["position_id"] = position_id
        except (TypeError, ValueError):
            pass
        return await method(symbol, side, hold_vol, lev, **kwargs)

    async def _close_market(self, pos: ManagedPosition, raw: Optional[Dict[str, Any]], *, reason: str) -> None:
        raw = raw or {}
        if pos.exit_signal_ts <= 0:
            pos.exit_signal_ts = time.time()
        self._queue_execution_event("close_signal", pos=pos, reason=reason)
        try:
            hold_vol = float(raw.get("holdVol") or 0.0)
        except Exception:
            hold_vol = 0.0
        if hold_vol <= 0:
            hold_vol = float(pos.qty or 0.0)
        try:
            lev = int(float(raw.get("leverage") or 0.0))
        except Exception:
            lev = 0
        if lev <= 0:
            lev = int(pos.leverage or 0)
        if hold_vol <= 0:
            self._queue_execution_event("close_rejected", pos=pos, reason="no_hold_volume")
            await self.state.add_log("warn", f"[real] close_market skipped {pos.symbol}: no hold volume")
            return
        if lev <= 0:
            self._queue_execution_event("close_rejected", pos=pos, reason="no_leverage")
            await self.state.add_log("warn", f"[real] close_market skipped {pos.symbol}: no leverage")
            return

        res: Dict[str, Any] = {}
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                pos.close_submit_started_ts = time.time()
                book = self.agg.get_book(pos.symbol)
                close_price = _realisable_exit_price(pos, book) if book else pos.entry_price
                position_id = self._extract_position_id(raw) or pos.mexc_position_id
                self._queue_execution_event(
                    "close_submit_started",
                    pos=pos,
                    reason=reason,
                    price=close_price,
                    position_id=position_id,
                )
                res = await self._submit_close_reduce_only(
                    pos.symbol,
                    pos.side,
                    hold_vol,
                    lev,
                    price=close_price,
                    position_id=position_id,
                )
                pos.close_ack_ts = time.time()
                self._register_private_api_result(res, "close_market")
                try:
                    pos.close_submit_latency_ms = float(res.get("_latency_ms") or 0.0)
                except Exception:
                    pos.close_submit_latency_ms = 0.0
                if pos.close_submit_latency_ms <= 0 and pos.close_submit_started_ts > 0:
                    pos.close_submit_latency_ms = max(
                        0.0, (pos.close_ack_ts - pos.close_submit_started_ts) * 1000.0
                    )
                self._queue_execution_event(
                    "close_ack",
                    pos=pos,
                    reason=reason,
                    price=close_price,
                    position_id=position_id,
                    close_submit_ms=pos.close_submit_latency_ms,
                    extra={"success": bool(res.get("success")), "message": res.get("message")},
                )
                last_error = None
            except Exception as e:
                self._register_private_api_exception("close_market")
                last_error = e
                if attempt >= 2:
                    self._queue_execution_event("close_rejected", pos=pos, reason=str(e))
                    await self.state.add_log("error", f"[real] close_market exception {pos.symbol}: {e}")
                    return
                backoff = 0.12 * (attempt + 1)
                await self.state.add_log(
                    "warn",
                    f"[real] close_market retry {attempt + 1}/2 {pos.symbol}: exception {e} "
                    f"(sleep {backoff:.2f}s)",
                )
                await asyncio.sleep(backoff)
                continue

            if res.get("success"):
                break

            if attempt >= 2 or not self._is_retryable_close_reject(res):
                self._queue_execution_event(
                    "close_rejected",
                    pos=pos,
                    reason=str(res.get("message") or res.get("code") or "reject"),
                    close_submit_ms=pos.close_submit_latency_ms,
                )
                await self.state.add_log("warn", f"[real] close_market reject {pos.symbol}: {res.get('message')}")
                return

            backoff = 0.12 * (attempt + 1)
            await self.state.add_log(
                "warn",
                f"[real] close_market retry {attempt + 1}/2 {pos.symbol}: {res.get('message')} "
                f"(sleep {backoff:.2f}s)",
            )
            await asyncio.sleep(backoff)

        if last_error is not None:
            self._queue_execution_event("close_rejected", pos=pos, reason=str(last_error))
            await self.state.add_log("error", f"[real] close_market exception {pos.symbol}: {last_error}")
            return
        if not res.get("success"):
            self._queue_execution_event(
                "close_rejected",
                pos=pos,
                reason=str(res.get("message") or res.get("code") or "reject"),
                close_submit_ms=pos.close_submit_latency_ms,
            )
            await self.state.add_log("warn", f"[real] close_market reject {pos.symbol}: {res.get('message')}")
            return

        book = self.agg.get_book(pos.symbol)
        fallback_exit_price = _realisable_exit_price(pos, book) if book else pos.entry_price
        order_id = self._extract_order_id(res)
        exit_price, used_order_avg = await self._resolve_close_price(
            pos.symbol,
            order_id=order_id,
            fallback_exit_price=fallback_exit_price or pos.entry_price,
        )
        price_source = "order_avg" if used_order_avg else "book_fallback"
        close_details = await self._resolve_external_close_details(
            pos,
            fallback_exit_price=exit_price or fallback_exit_price or pos.entry_price,
        )
        if close_details is not None:
            exit_price = float(close_details.get("exit_price") or exit_price or pos.entry_price)
            details_source = str(close_details.get("price_source") or "")
            if details_source and details_source != "exchange_fallback":
                price_source = details_source
            realized_pnl = float(close_details.get("realized_pnl") or 0.0)
            close_ts = float(close_details.get("close_ts") or time.time())
            if close_details.get("position_id") is not None:
                try:
                    pos.mexc_position_id = int(close_details["position_id"])
                except Exception:
                    pass
        else:
            realized_pnl = None
            close_ts = None
        await self._mark_closed_externally(
            pos,
            exit_price=exit_price or pos.entry_price,
            reason=reason,
            price_source=price_source,
            order_id=order_id,
            realized_pnl=realized_pnl,
            close_ts=close_ts,
        )
        self._queue_execution_event(
            "close_accounting_resolved",
            pos=pos,
            reason=reason,
            price=exit_price or pos.entry_price,
            close_ack_to_closed_ms=pos.close_ack_to_closed_ms,
            extra={"price_source": price_source, "order_id": order_id},
        )

    def _extract_order_id(self, res: Dict[str, Any]) -> Optional[int]:
        data = res.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            data = {}
        order_id = data.get("orderId") or data.get("order_id") or res.get("_order_id")
        try:
            return int(order_id) if order_id is not None else None
        except Exception:
            return None

    def _is_retryable_close_reject(self, res: Dict[str, Any]) -> bool:
        try:
            code = int(res.get("code"))
        except Exception:
            code = 0
        msg = str(res.get("message") or "").lower()
        if code in (429, 500, 502, 503, 504, 510):
            return True
        return (
            "too frequent" in msg
            or "too many requests" in msg
            or "temporarily unavailable" in msg
            or "timeout" in msg
            or "timed out" in msg
        )

    def _extract_order_avg_price(self, query_res: Dict[str, Any]) -> Optional[float]:
        items = (query_res or {}).get("data") or []
        if not items:
            return None
        row = items[0] if isinstance(items, list) else items
        if not isinstance(row, dict):
            return None
        return self._history_position_price(
            row,
            "avgDealPrice",
            "dealAvgPrice",
            "avgPrice",
            "priceAvg",
            "dealPrice",
        )

    async def _resolve_close_price(
        self,
        symbol: str,
        *,
        order_id: Optional[int],
        fallback_exit_price: float,
    ) -> tuple[float, bool]:
        if order_id is None:
            return fallback_exit_price, False
        for attempt in range(3):
            try:
                query = await self.trader.query_order(order_id)
                self._register_private_api_result(query, "query_order")
            except Exception:
                self._register_private_api_exception("query_order")
                query = None
            if isinstance(query, dict) and not bool(query.get("success")):
                if self._looks_like_rate_limit(str(query.get("message") or ""), query.get("code")):
                    break
            price = self._extract_order_avg_price(query or {})
            if price is not None and price > 0:
                return price, True
            await asyncio.sleep(0.12 * (attempt + 1))
        await self.state.add_log(
            "debug",
            f"[real] no order avg for {symbol} oid={order_id}, using executable-book fallback",
        )
        return fallback_exit_price, False

    async def _cancel_all_quotes_for_shutdown(self, reason: str) -> None:
        for sym, q in list(self._quotes.items()):
            try:
                res = await self.trader.cancel_all_for(sym)
                self._register_private_api_result(res, "cancel_all")
            except Exception as e:
                self._register_private_api_exception("cancel_all")
                logger.warning("cancel_all_for %s during %s failed: %s", sym, reason, e)
            finally:
                self._quotes.pop(sym, None)
        await self.state.add_log("warn", f"[real] canceled outstanding quotes ({reason})")

    async def _exchange_positions_for_watchdog(self) -> list[Dict[str, Any]]:
        try:
            return await self._get_positions_raw_cached(
                force=True,
                max_age_sec=0.0,
                min_interval_sec=0.0,
            )
        except Exception as e:
            self._register_private_api_exception("open_positions")
            await self.state.add_log("error", f"[real] watchdog positions fetch failed: {e}")
            return []

    async def _emergency_flatten_all(self, *, reason: str) -> None:
        async with self._shutdown_lock:
            if self._emergency_close_in_progress:
                return
            self._emergency_close_in_progress = True
            try:
                now = time.time()
                self.state.last_emergency_action = reason
                self.state.last_emergency_ts = now
                self.state.emergency_close_count += 1
                self._queue_execution_event(
                    "emergency_close_started",
                    reason=reason,
                    extra={"managed_symbols": sorted(self._managed_symbols() or [])},
                )

                await self._cancel_all_quotes_for_shutdown(reason)

                managed_scope = self._managed_symbols()
                retries = max(1, int(getattr(self.cfg.risk, "emergency_close_retries", 3) or 3))
                remaining_by_sym: Dict[str, Dict[str, Any]] = {}

                for attempt in range(retries):
                    raw_list = await self._exchange_positions_for_watchdog()
                    remaining_by_sym = {}
                    for raw in raw_list:
                        sym = str(raw.get("symbol") or "").upper()
                        if managed_scope is not None and sym not in managed_scope:
                            continue
                        remaining_by_sym[sym] = raw

                    # Add persisted in-memory positions in case exchange snapshot is flaky.
                    for sym, pos in list(self.state.positions.items()):
                        if managed_scope is not None and sym not in managed_scope:
                            continue
                        if sym not in remaining_by_sym:
                            remaining_by_sym[sym] = {
                                "symbol": sym,
                                "positionType": 1 if pos.side == "LONG" else 2,
                                "holdVol": pos.qty,
                                "leverage": pos.leverage,
                            }

                    if not remaining_by_sym:
                        break

                    for sym, raw in list(remaining_by_sym.items()):
                        side = self._position_side(raw)
                        if side is None:
                            continue
                        try:
                            hold_vol = float(raw.get("holdVol") or 0.0)
                        except Exception:
                            hold_vol = 0.0
                        if hold_vol <= 0 and sym in self.state.positions:
                            hold_vol = float(self.state.positions[sym].qty or 0.0)
                        try:
                            lev = int(float(raw.get("leverage") or 0.0))
                        except Exception:
                            lev = 0
                        if lev <= 0 and sym in self.state.positions:
                            lev = int(self.state.positions[sym].leverage or 0)
                        if hold_vol <= 0 or lev <= 0:
                            continue
                        await self.state.add_log(
                            "warn",
                            f"[real] EMERGENCY CLOSE {sym} {side} ({reason}, attempt={attempt + 1}/{retries})",
                        )
                        try:
                            pos = self.state.positions.get(sym)
                            close_price = None
                            position_id = self._extract_position_id(raw)
                            if pos is not None:
                                book = self.agg.get_book(sym)
                                close_price = _realisable_exit_price(pos, book) if book else pos.entry_price
                                position_id = position_id or pos.mexc_position_id
                            res = await self._submit_close_reduce_only(
                                sym,
                                side,
                                hold_vol,
                                lev,
                                price=close_price,
                                position_id=position_id,
                            )
                            self._register_private_api_result(res, "emergency_close")
                            self._queue_execution_event(
                                "emergency_close_submit",
                                symbol=sym,
                                side=side,
                                reason=reason,
                                position_id=position_id,
                                price=close_price,
                                close_submit_ms=res.get("_latency_ms"),
                                extra={"success": bool(res.get("success")), "message": res.get("message")},
                            )
                        except Exception as e:
                            self._register_private_api_exception("emergency_close")
                            self._queue_execution_event(
                                "emergency_close_rejected",
                                symbol=sym,
                                side=side,
                                reason=str(e),
                            )
                            await self.state.add_log("error", f"[real] emergency close failed {sym}: {e}")

                    await asyncio.sleep(0.20 * (attempt + 1))

                    refreshed = await self._exchange_positions_for_watchdog()
                    refreshed_by_sym = {
                        str(raw.get("symbol") or "").upper(): raw
                        for raw in refreshed
                        if isinstance(raw, dict)
                    }
                    for sym, pos in list(self.state.positions.items()):
                        if managed_scope is not None and sym not in managed_scope:
                            continue
                        if sym in refreshed_by_sym:
                            continue
                        book = self.agg.get_book(sym)
                        fallback_exit_price = _realisable_exit_price(pos, book) if book else pos.entry_price
                        close_details = await self._resolve_external_close_details(
                            pos,
                            fallback_exit_price=fallback_exit_price,
                        )
                        if close_details.get("position_id") is not None:
                            pos.mexc_position_id = close_details.get("position_id")
                        if pos.exit_signal_ts <= 0:
                            pos.exit_signal_ts = time.time()
                        await self._mark_closed_externally(
                            pos,
                            exit_price=float(close_details["exit_price"]),
                            reason=f"emergency_{reason}",
                            price_source=str(close_details["price_source"]),
                            realized_pnl=float(close_details["realized_pnl"]),
                            close_ts=float(close_details["close_ts"]),
                        )

                final_positions = await self._exchange_positions_for_watchdog()
                still_open = []
                for raw in final_positions:
                    sym = str(raw.get("symbol") or "").upper()
                    if managed_scope is not None and sym not in managed_scope:
                        continue
                    if float(raw.get("holdVol") or 0.0) > 0:
                        still_open.append(sym)
                if still_open:
                    self._queue_execution_event(
                        "emergency_close_done",
                        reason=f"incomplete:{reason}",
                        extra={"still_open": sorted(set(still_open))},
                    )
                    await self.state.add_log(
                        "error",
                        f"[real] emergency close incomplete ({reason}): still open {sorted(set(still_open))}",
                    )
                else:
                    self._queue_execution_event("emergency_close_done", reason=reason)
                    await self.state.add_log("warn", f"[real] emergency close complete ({reason})")
            finally:
                self._emergency_close_in_progress = False

    async def _trip_kill_switch(self, reason: str, *, emergency_close: bool = True) -> None:
        already_killed = self.state.kill_switch and self.state.last_kill_reason == reason
        self.state.kill_switch = True
        self.state.last_kill_reason = reason
        async with self.state.lock:
            self.state.engine_running = False
        if not already_killed:
            await self.state.add_log("error", f"KILL: {reason}")
        if emergency_close:
            await self._emergency_flatten_all(reason=f"kill:{reason}")
            self._shutdown_cleanup_done = True
        self.stop()

    async def _watchdog_open_positions(self) -> None:
        stale_age_ms = float(getattr(self.cfg.risk, "stale_book_age_ms_kill", 0.0) or 0.0)
        stale_data_kill_sec = float(getattr(self.cfg.risk, "stale_data_kill_sec", 0.0) or 0.0)
        if stale_age_ms <= 0 or stale_data_kill_sec <= 0:
            self._stale_data_started_ts = 0.0
            return

        active_symbols = list(self.state.positions.keys())
        if not active_symbols:
            self._stale_data_started_ts = 0.0
            return

        stale_symbols: list[str] = []
        for sym in active_symbols:
            st = self.agg.compute_stats(sym)
            mexc_age = float(st.mexc_book_age_ms) if st.mexc_book_age_ms is not None else stale_age_ms + 1.0
            binance_age = float(st.binance_book_age_ms) if st.binance_book_age_ms is not None else stale_age_ms + 1.0
            if mexc_age > stale_age_ms or binance_age > stale_age_ms:
                stale_symbols.append(f"{sym}(mexc={mexc_age:.0f}ms,binance={binance_age:.0f}ms)")

        if not stale_symbols:
            self._stale_data_started_ts = 0.0
            return

        if self._stale_data_started_ts <= 0:
            self._stale_data_started_ts = time.time()
            await self.state.add_log(
                "warn",
                f"[real] stale data watchdog armed: {', '.join(stale_symbols[:3])}",
            )
            return

        if time.time() - self._stale_data_started_ts >= stale_data_kill_sec:
            await self._trip_kill_switch(
                f"stale_data>{stale_data_kill_sec:.1f}s ({', '.join(stale_symbols[:3])})",
                emergency_close=True,
            )

    async def _mark_closed_externally(
        self,
        pos: ManagedPosition,
        *,
        exit_price: float,
        reason: str,
        price_source: str = "estimated",
        order_id: Optional[int] = None,
        realized_pnl: Optional[float] = None,
        close_ts: Optional[float] = None,
    ) -> None:
        close_key = self._closed_position_key(pos)
        if pos.closed or close_key in self._closed_position_keys:
            self._raw_missing_counts.pop(pos.symbol, None)
            self._raw_missing_first_ts.pop(pos.symbol, None)
            async with self.state.lock:
                self.state.positions.pop(pos.symbol, None)
            try:
                await self._delete_persisted_position(pos.symbol)
            except Exception:
                pass
            await self.state.add_log("debug", f"[real] duplicate close ignored {pos.symbol} key={close_key}")
            return
        self._closed_position_keys.add(close_key)
        self._raw_missing_counts.pop(pos.symbol, None)
        self._raw_missing_first_ts.pop(pos.symbol, None)

        now = float(close_ts or time.time())
        # Calculate exit latency (decision Ð Ð†Ð²Ð‚Â Ð²Ð‚â„¢ actual close)
        if pos.exit_signal_ts > 0:
            pos.exit_latency_ms = max(0.0, (now - pos.exit_signal_ts) * 1000.0)
        if pos.close_ack_ts > 0:
            pos.close_ack_to_closed_ms = max(0.0, (now - pos.close_ack_ts) * 1000.0)

        if realized_pnl is None:
            if pos.side == "LONG":
                realized = (exit_price - pos.entry_price) * pos.qty * pos.contract_size
            else:
                realized = (pos.entry_price - exit_price) * pos.qty * pos.contract_size
        else:
            realized = float(realized_pnl)
        pos.realized_pnl = realized
        pos.closed = True
        pos.close_reason = reason
        pos.close_ts = now
        pos.close_price = exit_price

        async with self.state.lock:
            self.state.positions.pop(pos.symbol, None)
            self.state.strategy_realized_pnl += realized
            self.state.session_trade_count += 1
            if realized < 0:
                self.state.consecutive_losses += 1
            else:
                self.state.consecutive_losses = 0
            cd_min = self._cooldown_min_sec_for(pos.symbol)
            cd_max = self._cooldown_max_sec_for(pos.symbol)
            self.state.cooldown_until[pos.symbol] = now + random.uniform(cd_min, max(cd_min, cd_max))
            self.state.recent_trades.append({
                "ts": now, "symbol": pos.symbol, "side": pos.side,
                "entry": pos.entry_price, "exit": exit_price,
                "pnl": realized,
                "pnl_pct": (realized / pos.margin_usdt * 100.0) if pos.margin_usdt > 0 else 0.0,
                "reason": reason, "duration": now - pos.open_ts,
                "entry_latency_ms": pos.entry_latency_ms,
                "entry_confirm_latency_ms": pos.entry_confirm_latency_ms,
                "exit_latency_ms": pos.exit_latency_ms,
                "submit_latency_ms": pos.submit_latency_ms,
                "fill_seen_latency_ms": pos.fill_seen_latency_ms,
                "managed_latency_ms": pos.managed_latency_ms,
                "end_to_end_entry_ms": pos.end_to_end_entry_ms,
                "close_submit_latency_ms": pos.close_submit_latency_ms,
                "close_ack_to_closed_ms": pos.close_ack_to_closed_ms,
                "entry_algo": pos.entry_algo,
                "entry_score": pos.entry_score,
                "price_source": price_source,
            })
        await self._delete_persisted_position(pos.symbol)

        await self.state.add_log(
            "info" if realized >= 0 else "warn",
            f"[real] CLOSE {pos.symbol} {pos.side} @ {exit_price:.6g} "
            f"({reason}, {price_source}) PnL={realized:+.4f}",
        )

        try:
            if pos.entry_price > 0:
                if pos.side == "LONG":
                    best_excursion_bps = (((pos.best_excursion or pos.entry_price) - pos.entry_price) / pos.entry_price) * 1e4
                    realized_bps = ((exit_price - pos.entry_price) / pos.entry_price) * 1e4
                else:
                    best_excursion_bps = ((pos.entry_price - (pos.best_excursion or pos.entry_price)) / pos.entry_price) * 1e4
                    realized_bps = ((pos.entry_price - exit_price) / pos.entry_price) * 1e4
            else:
                best_excursion_bps = None
                realized_bps = None
            await self.store.insert_trade({
                "ts": now, "mode": "real",
                "symbol": pos.symbol, "side": pos.side,
                "entry": pos.entry_price, "exit": exit_price,
                "qty": pos.qty, "notional": pos.notional_usdt,
                "margin": pos.margin_usdt, "leverage": pos.leverage,
                "open_ts": pos.open_ts, "close_ts": now,
                "duration_sec": now - pos.open_ts,
                "pnl_usdt": realized,
                "pnl_pct": (realized / pos.margin_usdt * 100.0) if pos.margin_usdt > 0 else 0.0,
                "fair_at_open": pos.fair_at_open,
                "sigma_at_open": pos.sigma_at_open,
                "z_at_open": None,
                "close_reason": reason,
                "entry_latency_sec": (pos.open_ts - pos.quote_ts) if pos.quote_ts > 0 else None,
                "extra": {
                    "best_excursion": pos.best_excursion,
                    "best_excursion_bps": best_excursion_bps,
                    "realized_bps": realized_bps,
                    "entry_algo": pos.entry_algo,
                    "entry_score": pos.entry_score,
                    "entry_latency_ms": pos.entry_latency_ms,
                    "entry_confirm_latency_ms": pos.entry_confirm_latency_ms,
                    "exit_latency_ms": pos.exit_latency_ms,
                    "submit_latency_ms": pos.submit_latency_ms,
                    "fill_seen_latency_ms": pos.fill_seen_latency_ms,
                    "managed_latency_ms": pos.managed_latency_ms,
                    "end_to_end_entry_ms": pos.end_to_end_entry_ms,
                    "close_submit_latency_ms": pos.close_submit_latency_ms,
                    "close_ack_to_closed_ms": pos.close_ack_to_closed_ms,
                    "contract_size": pos.contract_size,
                    "entry_spread_bps": pos.entry_spread_bps,
                    "entry_mexc_book_age_ms": pos.entry_mexc_book_age_ms,
                    "entry_binance_book_age_ms": pos.entry_binance_book_age_ms,
                    "price_source": price_source,
                    "close_order_id": order_id,
                },
            })
        except Exception as e:
            logger.warning("real trade insert failed for %s: %s", pos.symbol, e)

    async def _log_equity_periodically(self) -> None:
        now = time.time()
        if now - self._equity_log_last_ts < 5.0:
            return
        self._equity_log_last_ts = now
        equity = max(0.0, float(self.state.balance or 0.0))
        strategy_start = (
            self.state.strategy_session_starting_balance
            if self.state.strategy_session_starting_balance > 0
            else self.state.session_starting_balance
        )
        strategy_balance = strategy_start + float(self.state.strategy_realized_pnl or 0.0)
        strategy_open_pnl = sum(float(p.last_pnl_usdt or 0.0) for p in self.state.positions.values())
        strategy_equity = strategy_balance + strategy_open_pnl
        if strategy_equity > self.state.strategy_session_peak_balance:
            self.state.strategy_session_peak_balance = strategy_equity
        async with self.state.lock:
            self.state.equity_history.append({
                "ts": now, "balance": equity, "equity": equity,
                "open_positions": len(self.state.positions),
            })
            self.state.strategy_equity_history.append({
                "ts": now,
                "balance": strategy_balance,
                "equity": strategy_equity,
                "open_positions": len(self.state.positions),
            })
        try:
            await self.store.insert_equity(now, "real", equity, equity, len(self.state.positions))
        except Exception:
            pass

    async def _check_kill_switch(self) -> None:
        if self.state.kill_switch:
            return
        await self._watchdog_open_positions()
        if self.state.kill_switch:
            return

        current_equity = max(0.0, float(self.state.balance or 0.0))
        now = time.time()
        if now - self.state.day_start_ts > 86400:
            self.state.day_start_ts = now
            self.state.day_start_balance = current_equity

        run_started_ts = float(self.state.run_started_ts or 0.0)
        max_runtime_sec = float(getattr(self.cfg.risk, "max_runtime_sec", 0.0) or 0.0)
        if run_started_ts > 0 and max_runtime_sec > 0 and now - run_started_ts >= max_runtime_sec:
            await self._trip_kill_switch(f"max_runtime {max_runtime_sec:.0f}s", emergency_close=True)
            return

        max_trades = int(getattr(self.cfg.risk, "max_trades_per_session", 0) or 0)
        if max_trades > 0 and int(self.state.session_trade_count or 0) >= max_trades:
            await self._trip_kill_switch(f"max_trades {max_trades}", emergency_close=True)
            return

        day_loss_pct = (self.state.day_start_balance - current_equity) / max(1e-9, self.state.day_start_balance)
        if day_loss_pct >= float(self.cfg.risk.daily_loss_pct_kill):
            await self._trip_kill_switch(f"daily loss {day_loss_pct*100:.1f}%", emergency_close=True)
            return

        if current_equity > self.state.session_peak_balance:
            self.state.session_peak_balance = current_equity
        peak = self.state.session_peak_balance or self.state.session_starting_balance
        if peak > 0:
            dd = (peak - current_equity) / peak
            if dd >= float(self.cfg.risk.max_drawdown_pct_kill):
                await self._trip_kill_switch(f"drawdown {dd*100:.1f}%", emergency_close=True)
                return

        strategy_start = (
            self.state.strategy_session_starting_balance
            if self.state.strategy_session_starting_balance > 0
            else self.state.session_starting_balance
        )
        strategy_open_pnl = sum(float(p.last_pnl_usdt or 0.0) for p in self.state.positions.values())
        strategy_equity = strategy_start + float(self.state.strategy_realized_pnl or 0.0) + strategy_open_pnl
        session_loss_usdt = max(0.0, strategy_start - strategy_equity)

        session_loss_usdt_cap = float(getattr(self.cfg.risk, "session_loss_usdt_kill", 0.0) or 0.0)
        if session_loss_usdt_cap > 0 and session_loss_usdt >= session_loss_usdt_cap:
            await self._trip_kill_switch(
                f"session loss {session_loss_usdt:.2f} USDT >= {session_loss_usdt_cap:.2f}",
                emergency_close=True,
            )
            return

        session_loss_pct_cap = float(getattr(self.cfg.risk, "session_loss_pct_kill", 0.0) or 0.0)
        if strategy_start > 0 and session_loss_pct_cap > 0:
            session_loss_pct = session_loss_usdt / strategy_start
            if session_loss_pct >= session_loss_pct_cap:
                await self._trip_kill_switch(
                    f"session loss {session_loss_pct*100:.1f}% >= {session_loss_pct_cap*100:.1f}%",
                    emergency_close=True,
                )
                return

        consecutive_losses_kill = int(getattr(self.cfg.risk, "consecutive_losses_kill", 0) or 0)
        if consecutive_losses_kill > 0 and int(self.state.consecutive_losses or 0) >= consecutive_losses_kill:
            await self._trip_kill_switch(
                f"consecutive losses {self.state.consecutive_losses}",
                emergency_close=True,
            )
            return

        auth_error_kill_count = int(getattr(self.cfg.risk, "auth_error_kill_count", 0) or 0)
        if auth_error_kill_count > 0 and self._auth_error_streak >= auth_error_kill_count:
            await self._trip_kill_switch(
                f"auth/private errors {self._auth_error_streak}",
                emergency_close=True,
            )
            return

        private_api_error_kill_count = int(getattr(self.cfg.risk, "private_api_error_kill_count", 0) or 0)
        if private_api_error_kill_count > 0 and self._private_api_error_streak >= private_api_error_kill_count:
            await self._trip_kill_switch(
                f"private api errors {self._private_api_error_streak}",
                emergency_close=True,
            )
            return

