"""Real-money executor for MEXC.

Mirrors the paper executor but talks to MEXC. Places maker limit entries,
attaches exchange-side TP/SL stop-plan orders, and updates SL via
change_stop_plan_price as the price moves toward fair value.
"""

from __future__ import annotations

import asyncio
import logging
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
                 store: Store, mexc_trader) -> None:
        self.cfg = cfg
        self.state = state
        self.agg = agg
        self.opp = opp
        self.alloc = alloc
        self.store = store
        self.trader = mexc_trader

        self._quotes: Dict[str, _Quote] = {}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._equity_log_last_ts = 0.0
        self._balance_refresh_last_ts = 0.0
        self._max_lev_cache: Dict[str, int] = {}
        self._raw_missing_counts: Dict[str, int] = {}
        self._positions_refresh_error_last_ts = 0.0
        self._foreign_position_warned: Set[str] = set()
        self._positions_cache_ts = 0.0
        self._positions_cache_raw: list[Dict[str, Any]] = []
        self._zero_fee_skip_log_ts: Dict[str, float] = {}

    def stop(self) -> None:
        self._stop.set()

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

        # Warn if taker_entry=True and entry_latency_ms>0
        taker_entry = bool(getattr(self.cfg.strategy, "taker_entry", False))
        entry_latency_ms = int(getattr(self.cfg.strategy, "entry_latency_ms", 200) or 200)
        if taker_entry and entry_latency_ms > 0:
            logger.warning(
                "taker_entry=True with entry_latency_ms=%d; forcing entry_latency_ms=0 for immediate execution",
                entry_latency_ms
            )
            self.cfg.strategy.entry_latency_ms = 0

    async def _fetch_account_balances(self) -> tuple[float, float]:
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

        Priority: symbol_override.sl_pct → global sl_pct_crypto/stocks.
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

    def _entry_latency_ms_for(self, symbol: str) -> int:
        ov = self._override_for(symbol)
        if ov and ov.entry_latency_ms is not None:
            return int(ov.entry_latency_ms)
        return int(getattr(self.cfg.strategy, "entry_latency_ms", 200) or 200)

    def _signal_max_age_ms_for(self, symbol: str) -> int:
        ov = self._override_for(symbol)
        if ov and ov.signal_max_age_ms is not None:
            return int(ov.signal_max_age_ms)
        return int(getattr(self.cfg.strategy, "signal_max_age_ms", 0) or 0)

    def _signal_age_grace_ms_for(self, symbol: str) -> float:
        entry_latency_ms = float(max(0, self._entry_latency_ms_for(symbol)))
        return max(125.0, min(350.0, 200.0 + entry_latency_ms + 25.0))

    def _loop_tick_sec(self) -> float:
        return max(0.01, float(getattr(self.cfg.strategy, "paper_tick_sec", 0.2) or 0.2))

    @staticmethod
    def _fresh_books_for_age_grace(st: Any) -> bool:
        mexc_age = getattr(st, "mexc_book_age_ms", None)
        binance_age = getattr(st, "binance_book_age_ms", None)
        if mexc_age is not None and float(mexc_age) > 200.0:
            return False
        if binance_age is not None and float(binance_age) > 200.0:
            return False
        return True

    def _pre_submit_max_spread_drift_bps_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.pre_submit_max_spread_drift_bps is not None:
            return float(ov.pre_submit_max_spread_drift_bps)
        return float(getattr(self.cfg.strategy, "pre_submit_max_spread_drift_bps", 0.0) or 0.0)

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
    ) -> tuple[bool, str, Optional[Any]]:
        st = self.agg.compute_stats(symbol)
        ov = self._override_for(symbol)
        if ov is not None and ov.algorithms:
            self.opp.evaluate_multi(symbol, st, ov)
        else:
            self.opp.evaluate(symbol, st)

        # Calculate age_ms for logging
        age_ms = None
        if signal_ts > 0:
            age_ms = (time.time() - signal_ts) * 1000.0

        strict_filters = signal_ts <= 0 and spread_bps_at_quote is None
        if strict_filters:
            if st.blocked_reason:
                self._log_signal_decision(symbol, side, st, "rejected", st.blocked_reason, age_ms)
                return False, st.blocked_reason, st
            if not st.side_hint:
                self._log_signal_decision(symbol, side, st, "rejected", "no_side_hint", age_ms)
                return False, "no_side_hint", st
            if st.side_hint != side:
                reason = f"side_flip={st.side_hint}"
                self._log_signal_decision(symbol, side, st, "rejected", reason, age_ms)
                return False, reason, st
            min_entry_score = self._min_entry_score_for(symbol)
            if st.score < min_entry_score:
                reason = f"score {st.score:.2f} < {min_entry_score:.2f}"
                self._log_signal_decision(symbol, side, st, "rejected", reason, age_ms)
                return False, reason, st
        elif st.side_hint and st.side_hint != side:
            reason = f"side_flip={st.side_hint}"
            self._log_signal_decision(symbol, side, st, "rejected", reason, age_ms)
            return False, reason, st
        max_age_ms = self._signal_max_age_ms_for(symbol)
        if max_age_ms > 0 and signal_ts > 0:
            age_limit_ms = float(max_age_ms)
            if age_ms > age_limit_ms:
                grace_ms = self._signal_age_grace_ms_for(symbol)
                if not (
                    age_ms <= age_limit_ms + grace_ms
                    and self._fresh_books_for_age_grace(st)
                ):
                    reason = f"signal_age={age_ms:.0f}ms"
                    self._log_signal_decision(symbol, side, st, "rejected", reason, age_ms)
                    return False, reason, st
        max_spread_drift = self._pre_submit_max_spread_drift_bps_for(symbol)
        if max_spread_drift > 0 and spread_bps_at_quote is not None and st.spread_bps is not None:
            if side == "LONG":
                spread_drift = st.spread_bps - spread_bps_at_quote
            else:
                spread_drift = spread_bps_at_quote - st.spread_bps
            if spread_drift > max_spread_drift:
                reason = f"spread_drift={spread_drift:.2f}bps"
                self._log_signal_decision(symbol, side, st, "rejected", reason, age_ms)
                return False, reason, st

        # Signal accepted
        self._log_signal_decision(symbol, side, st, "accepted", None, age_ms)
        return True, "", st

    def _log_signal_decision(
        self,
        symbol: str,
        side: str,
        st: Any,
        decision: str,
        reason: Optional[str],
        age_ms: Optional[float],
    ) -> None:
        """Log signal decision to persistence for observability."""
        try:
            import asyncio
            asyncio.create_task(
                self.state.store.log_signal_decision(
                    symbol=symbol,
                    decision=decision,
                    side=side,
                    strategy=getattr(st, "selected_algorithm", None),
                    z_score=getattr(st, "z_score", None),
                    spread_bps=getattr(st, "spread_bps", None),
                    fair=getattr(st, "fair", None),
                    mexc_mid=getattr(st, "mexc_mid", None),
                    reason=reason,
                    age_ms=age_ms,
                )
            )
        except Exception:
            pass

    def _log_latency_probe(
        self,
        symbol: str,
        *,
        binance_depth_age_ms: Optional[float] = None,
        mexc_depth_age_ms: Optional[float] = None,
        stats_compute_ms: Optional[float] = None,
        decision_ms: Optional[float] = None,
        submit_latency_ms: Optional[float] = None,
        fill_latency_ms: Optional[float] = None,
    ) -> None:
        """Log latency measurements to persistence for performance analysis."""
        try:
            import asyncio
            asyncio.create_task(
                self.state.store.log_latency_probe(
                    symbol=symbol,
                    binance_depth_age_ms=binance_depth_age_ms,
                    mexc_depth_age_ms=mexc_depth_age_ms,
                    stats_compute_ms=stats_compute_ms,
                    decision_ms=decision_ms,
                    submit_latency_ms=submit_latency_ms,
                    fill_latency_ms=fill_latency_ms,
                )
            )
        except Exception:
            pass

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
        interval = float(self.cfg.strategy.fast_tick_sec if hasattr(self.cfg.strategy, 'fast_tick_sec') else 0.05)
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
        interval = float(self.cfg.strategy.slow_tick_sec if hasattr(self.cfg.strategy, 'slow_tick_sec') else 1.0)
        while not self._stop.is_set():
            try:
                await self._refresh_balance_periodically()
                await self._log_equity_periodically()
            except Exception:
                logger.exception("slow_loop tick error")
            await asyncio.sleep(interval)

    async def on_signal(self, opp: Opportunity) -> None:
        async with self._lock:
            await self._maybe_place_quote(opp)

    # ----------------------------- internals -----------------------------

    async def _tick(self) -> None:
        # Deprecated - now split into _fast_loop and _slow_loop
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

    async def _get_positions_raw_cached(
        self,
        *,
        max_age_sec: float = 0.15,
        force: bool = False,
    ) -> list[Dict[str, Any]]:
        now = time.monotonic()
        if (
            not force
            and self._positions_cache_raw
            and now - self._positions_cache_ts <= max(0.0, max_age_sec)
        ):
            return [dict(item) for item in self._positions_cache_raw]
        raw = await self.trader.get_positions_raw()
        self._positions_cache_raw = [dict(item) for item in raw if isinstance(item, dict)]
        self._positions_cache_ts = now
        return [dict(item) for item in self._positions_cache_raw]

    async def _maybe_place_quote(self, opp: Opportunity) -> None:
        sym = opp.symbol
        if sym in self._quotes:
            return
        if sym in self.state.positions:
            return

        # Latency probe: measure book ages and decision time
        t_decision_start = time.perf_counter()

        book = self.agg.get_book(sym)
        if not book or book.best_bid is None or book.best_ask is None:
            return

        # Measure book ages
        now = time.time()
        mexc_depth_age_ms = (now - book.ts) * 1000.0 if book.ts > 0 else None
        binance_book = self.agg.get_binance_book(sym)
        binance_depth_age_ms = (now - binance_book.ts) * 1000.0 if binance_book and binance_book.ts > 0 else None

        # Optional 0-fee live check (the symbol can lose its zero-fee status).
        require_zero_fee = bool(getattr(self.cfg.strategy, "real_require_zero_fee", False))
        if require_zero_fee:
            try:
                zero = await self.trader.is_zero_fee_symbol(sym)
            except Exception:
                zero = False
            if not zero:
                last = float(self._zero_fee_skip_log_ts.get(sym, 0.0) or 0.0)
                if now - last >= 30.0:
                    self._zero_fee_skip_log_ts[sym] = now
                    await self.state.add_log(
                        "debug",
                        f"[real] skip {sym}: not zero-fee (real_require_zero_fee=true)",
                    )
                return

        lev_max = await self._max_leverage(sym)
        contract_size = self.agg.contract_size_for(sym)
        depth = book.top_notional(10, contract_size=contract_size)
        spread_bps_at_quote = None
        if opp.fair > 0 and opp.entry_price > 0:
            spread_bps_at_quote = ((opp.entry_price - opp.fair) / opp.fair) * 1e4

        min_entry_score = self._min_entry_score_for(sym)
        if opp.score < min_entry_score:
            await self.state.add_log(
                "debug",
                f"[real] reject {sym} {opp.side}: score {opp.score:.2f} < {min_entry_score:.2f}",
            )
            return

        # Per-symbol sizing overrides
        _ov = self._override_for(sym)
        decision = self.alloc.decide(
            opp, self.state,
            balance_free=self._free_balance(),
            max_leverage_for_symbol=lev_max,
            book_top_notional=depth,
            margin_pct_override=(_ov.margin_pct if _ov else None),
            leverage_override=(_ov.leverage if _ov else None),
            max_notional_override=(_ov.max_notional_usdt if _ov else None),
        )
        if not decision.accept:
            return

        taker_entry = bool(getattr(self.cfg.strategy, "taker_entry", False))
        tick = await self._tick_size(sym)

        if taker_entry:
            latency_ms = max(0, self._entry_latency_ms_for(sym))
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
                taker_submit_at=now_ts + latency_ms / 1000.0,
                signal_ts=opp.signal_ts,
                entry_algo=opp.algorithm,
                entry_score=opp.score,
                spread_bps_at_quote=spread_bps_at_quote,
            )
            self._quotes[sym] = q
            await self.state.add_log(
                "debug",
                f"[real] taker queued {sym} {opp.side} (submit in {latency_ms}ms, q_age={queue_age_ms:.0f}ms)",
            )
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
                await self.state.add_log("error", f"open_limit failed {sym}: {e}")
                return

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

        # Latency probe: log decision time and submit latency
        decision_ms = (time.perf_counter() - t_decision_start) * 1000.0
        submit_latency_ms = (time.time() - opp.signal_ts) * 1000.0 if opp.signal_ts > 0 else None
        self._log_latency_probe(
            sym,
            binance_depth_age_ms=binance_depth_age_ms,
            mexc_depth_age_ms=mexc_depth_age_ms,
            decision_ms=decision_ms,
            submit_latency_ms=submit_latency_ms,
        )

        await self.state.add_log(
            "info",
            f"[real] {'ioc' if taker_entry else 'quote'} {sym} {opp.side} @ {price:.6g} "
            f"(notional={decision.notional_usdt:.2f}, lev={decision.leverage}, oid={order_id})",
        )

    async def _reconcile_quotes(self) -> None:
        now = time.time()
        for sym, q in list(self._quotes.items()):
            book = self.agg.get_book(sym)
            if q.taker_submit_at is not None and q.order_id is None:
                if not book or book.best_bid is None or book.best_ask is None:
                    continue
                if now < q.taker_submit_at:
                    continue
                ok_signal, why_signal, agg_stats = self._signal_valid_now(
                    sym,
                    q.side,
                    signal_ts=q.signal_ts,
                    spread_bps_at_quote=q.spread_bps_at_quote,
                )
                if not ok_signal:
                    self._quotes.pop(sym, None)
                    await self.state.add_log("debug", f"[real] skip {sym} {q.side}: stale signal ({why_signal})")
                    continue
                raw_price = float(book.best_ask if q.side == "LONG" else book.best_bid)
                if raw_price <= 0:
                    self._quotes.pop(sym, None)
                    continue
                buf_bps = self._taker_ioc_price_buffer_bps_for(sym)
                if q.side == "LONG":
                    raw_price *= (1.0 + buf_bps / 1e4)
                else:
                    raw_price *= (1.0 - buf_bps / 1e4)
                tick = await self._tick_size(sym)
                price, _ = _snap_price(float(raw_price), tick, q.side, for_sl=False)
                fair = float(agg_stats.fair or 0.0) if agg_stats else 0.0
                ok_fill, why_fill = self._fill_respects_fair(
                    sym, q.side, q.entry_algo, price, fair
                )
                if not ok_fill:
                    self._quotes.pop(sym, None)
                    await self.state.add_log("debug", f"[real] skip {sym} {q.side}: {why_fill}")
                    continue
                try:
                    res = await self.trader.open_ioc(
                        sym, q.side, q.notional, q.leverage, price,
                    )
                except Exception as e:
                    self._quotes.pop(sym, None)
                    await self.state.add_log("error", f"open_ioc failed {sym}: {e}")
                    continue
                if not res.get("success"):
                    self._quotes.pop(sym, None)
                    await self.state.add_log("warn", f"open_ioc reject {sym}: {res.get('message')}")
                    continue

                order_id = None
                data = res.get("data") or {}
                if isinstance(data, dict):
                    order_id = data.get("orderId") or data.get("order_id")
                try:
                    q.order_id = int(order_id) if order_id is not None else None
                except Exception:
                    q.order_id = None
                q.price = price
                q.placed_ts = now
                q.fair_at_quote = fair if fair > 0 else q.fair_at_quote
                q.sigma_at_quote = float(agg_stats.sigma_spread or q.sigma_at_quote or 0.0)
                q.z_at_quote = float(agg_stats.z_score or q.z_at_quote or 0.0)
                q.taker_submit_at = None
                try:
                    q.requested_vol = float(res.get("_requested_vol") or 0.0)
                except Exception:
                    q.requested_vol = None
                try:
                    final_vol = float(res.get("_final_vol") or 0.0)
                except Exception:
                    final_vol = 0.0
                if q.requested_vol and q.requested_vol > 0 and final_vol > 0:
                    q.fill_ratio = final_vol / q.requested_vol
                await self.state.add_log(
                    "info",
                    f"[real] ioc {sym} {q.side} @ {price:.6g} "
                    f"(notional={q.notional:.2f}, lev={q.leverage}, oid={q.order_id})",
                )
                immediate_fill = await self._materialize_filled_quote(
                    q,
                    fair=agg_stats.fair,
                    sigma=agg_stats.sigma_spread,
                    retry_delays=(0.0, max(0.02, self._loop_tick_sec()), max(0.04, self._loop_tick_sec() * 1.5)),
                )
                if immediate_fill:
                    continue
                continue

            agg_stats = self.agg.compute_stats(sym)
            z = agg_stats.z_score
            if z is not None and abs(z) < float(self.cfg.strategy.cancel_z):
                await self._cancel_quote(q, reason="z_collapsed")
                continue

            if now - q.placed_ts > float(self.cfg.strategy.quote_timeout_sec):
                await self._cancel_quote(q, reason="timeout")
                continue

            ok_signal, why_signal, agg_stats_recheck = self._signal_valid_now(
                sym,
                q.side,
                signal_ts=q.signal_ts,
                spread_bps_at_quote=q.spread_bps_at_quote,
            )
            if not ok_signal:
                await self._cancel_quote(q, reason=f"stale_signal:{why_signal}")
                continue

            # Detect fill: query order state OR check positions
            filled = await self._is_quote_filled(q)
            if filled:
                # Use the recheck stats if available, otherwise fall back to earlier compute
                stats_for_fill = agg_stats_recheck if agg_stats_recheck else agg_stats
                materialized = await self._materialize_filled_quote(
                    q,
                    fair=stats_for_fill.fair,
                    sigma=stats_for_fill.sigma_spread,
                    retry_delays=(0.0, 0.3),
                )
                if not materialized:
                    await self.state.add_log("warn", f"[real] fill detected for {sym} but no position")
                continue

    async def _is_quote_filled(self, q: _Quote) -> bool:
        # 1) Query order state first (if we have id) - more accurate than positions
        if q.order_id:
            try:
                res = await self.trader.query_order(int(q.order_id))
                items = (res or {}).get("data") or []
                if items:
                    state = int(items[0].get("state") or 0)
                    deal_vol = float(items[0].get("dealVol") or 0)
                    if q.requested_vol and q.requested_vol > 0 and deal_vol > 0:
                        q.fill_ratio = deal_vol / q.requested_vol
                    if state == 3 or deal_vol > 0:
                        return True
                    if state in (4, 5):
                        # canceled/invalid → drop quote
                        self._quotes.pop(q.symbol, None)
                        return False
            except Exception:
                pass

        # 2) Fallback: check positions (with TTL=0.5s cache)
        try:
            raw = await self._get_positions_raw_cached(max_age_sec=0.5)
            for p in raw:
                if str(p.get("symbol") or "").upper() != q.symbol:
                    continue
                pt = int(p.get("positionType") or 0)
                want = 1 if q.side == "LONG" else 2
                if pt == want and float(p.get("holdVol") or 0) > 0:
                    return True
        except Exception as e:
            logger.warning("quote fill check by positions failed for %s: %s", q.symbol, e)

        return False

    async def _find_position(self, symbol: str, side: str) -> Optional[Dict[str, Any]]:
        try:
            raw = await self._get_positions_raw_cached(force=True, max_age_sec=0.0)
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
        pos_raw: Optional[Dict[str, Any]] = None
        for delay_sec in retry_delays:
            if delay_sec > 0:
                await asyncio.sleep(delay_sec)
            pos_raw = await self._find_position(q.symbol, q.side)
            if pos_raw is not None:
                break
        if pos_raw is None:
            return False

        self._quotes.pop(q.symbol, None)
        await self._open_position_from_exchange(q, pos_raw, fair, sigma)
        pos = self.state.positions.get(q.symbol)
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

    async def _cancel_quote(self, q: _Quote, *, reason: str) -> None:
        try:
            await self.trader.cancel_all_for(q.symbol)
        except Exception as e:
            logger.warning("cancel_all_for %s failed: %s", q.symbol, e)
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

    @staticmethod
    def _history_position_side(pos_raw: Dict[str, Any]) -> Optional[str]:
        return RealExecutor._position_side(pos_raw)

    @staticmethod
    def _history_position_realized(pos_raw: Dict[str, Any]) -> Optional[float]:
        for key in ("realised", "realized", "profit", "pnl"):
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
                score += 1_000.0

        close_ts = self._exchange_ts_to_epoch(row.get("updateTime") or row.get("closeTime"))
        if close_ts > 0:
            score += min(abs(close_ts - now_ts), 600.0)

        open_ts = self._exchange_ts_to_epoch(row.get("createTime") or row.get("openTime"))
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
        now_ts = time.time()
        default_realized: Optional[float] = None
        if abs(float(pos.last_pnl_usdt or 0.0)) > 1e-12:
            default_realized = float(pos.last_pnl_usdt)

        best_row: Optional[Dict[str, Any]] = None
        best_score: Optional[float] = None
        start_ms = int(max(0.0, pos.open_ts - 300.0) * 1000.0) if pos.open_ts > 0 else None
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
            entry = float(
                pos_raw.get("newOpenAvgPrice") or pos_raw.get("holdAvgPrice")
                or pos_raw.get("openAvgPrice") or 0
            )
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
        stats = self.agg.compute_stats(q.symbol)
        entry_spread_bps = None
        if f_value and f_value > 0:
            if q.side == "LONG":
                entry_spread_bps = ((entry - f_value) / f_value) * 1e4
            else:
                entry_spread_bps = ((f_value - entry) / f_value) * 1e4
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
            entry_algo=q.entry_algo,
            entry_score=q.entry_score,
            max_hold_sec=self._max_hold_sec_for(q.symbol),
            entry_spread_bps=entry_spread_bps,
            entry_mexc_book_age_ms=stats.mexc_book_age_ms,
            entry_binance_book_age_ms=stats.binance_book_age_ms,
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
            try:
                # Pure trailing-stop strategy: only attach SL on the exchange.
                # No fixed TP — bot trails SL upward locally and lets it hit.
                res = await self.trader.place_stop_by_position(
                    pid,
                    stop_loss_price=sl_price,
                    take_profit_price=None,
                    side=q.side,
                )
                if res.get("success"):
                    data = res.get("data")
                    if isinstance(data, list):
                        data = data[0] if data else None
                    if isinstance(data, dict):
                        for k in ("stopPlanOrderId", "stopPlanOrderID", "stopPlanId", "id"):
                            if k in data and data[k] is not None:
                                try:
                                    pos.mexc_stop_plan_id = int(float(data[k]))
                                    break
                                except Exception:
                                    pass
                else:
                    await self.state.add_log("warn", f"[real] SL place failed {q.symbol}: {res.get('message')}")
            except Exception as e:
                await self.state.add_log("error", f"[real] SL place exception {q.symbol}: {e}")

        async with self.state.lock:
            self.state.positions[q.symbol] = pos
        await self._persist_managed_position(pos)
        self._raw_missing_counts.pop(q.symbol, None)
        await self.state.add_log(
            "info",
            f"[real] OPEN {q.symbol} {q.side} @ {entry:.6g} "
            f"(F={pos.fair_at_open:.6g}, SL={sl_price:.6g}, qty={hold_vol:.6g})",
        )

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
            raw_list = await self._get_positions_raw_cached(max_age_sec=0.12)
        except Exception as e:
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
            if move_bps_now > pos.best_realized_bps:
                pos.best_realized_bps = move_bps_now
            residual_edge_bps = _residual_edge_bps(pos, current_fair, exit_price_now)
            age_sec = now - pos.open_ts

            # Fair-cross exit with hysteresis: close when price returns to fair
            # with a small neutral band to avoid noise.
            exit_neutral_band_bps = float(getattr(self.cfg.strategy, "exit_neutral_band_bps", 0.5))
            min_hold_sec = float(getattr(self.cfg.strategy, "min_hold_sec", 3.0))
            if current_fair is not None and current_fair > 0 and pos.entry_price > 0:
                cur_dev_bps = (mid - current_fair) / current_fair * 1e4
                # LONG was opened when MEXC < fair (negative dev), wait for return to ~0
                if pos.side == "LONG" and cur_dev_bps >= -exit_neutral_band_bps:
                    if age_sec >= min_hold_sec:
                        pos.exit_signal_ts = now
                        await self._close_market(pos, raw, reason="fair_cross")
                        continue
                # SHORT was opened when MEXC > fair (positive dev), wait for return to ~0
                if pos.side == "SHORT" and cur_dev_bps <= exit_neutral_band_bps:
                    if age_sec >= min_hold_sec:
                        pos.exit_signal_ts = now
                        await self._close_market(pos, raw, reason="fair_cross")
                        continue

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

    async def _close_market(self, pos: ManagedPosition, raw: Optional[Dict[str, Any]], *, reason: str) -> None:
        raw = raw or {}
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
            await self.state.add_log("warn", f"[real] close_market skipped {pos.symbol}: no hold volume")
            return
        if lev <= 0:
            await self.state.add_log("warn", f"[real] close_market skipped {pos.symbol}: no leverage")
            return

        res: Dict[str, Any] = {}
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                res = await self.trader.close_market(pos.symbol, pos.side, hold_vol, lev, margin_mode=1)
                last_error = None
            except Exception as e:
                last_error = e
                if attempt >= 2:
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
            await self.state.add_log("error", f"[real] close_market exception {pos.symbol}: {last_error}")
            return
        if not res.get("success"):
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
        pos.exit_signal_ts = time.time()  # Mark exit decision time
        await self._mark_closed_externally(
            pos,
            exit_price=exit_price or pos.entry_price,
            reason=reason,
            price_source=price_source,
            order_id=order_id,
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
        for key in ("avgDealPrice", "dealAvgPrice", "avgPrice", "priceAvg", "dealPrice"):
            val = row.get(key)
            if val is None:
                continue
            try:
                price = float(val)
            except Exception:
                continue
            if price > 0:
                return price
        return None

    async def _resolve_close_price(
        self,
        symbol: str,
        *,
        order_id: Optional[int],
        fallback_exit_price: float,
    ) -> tuple[float, bool]:
        if order_id is None:
            return fallback_exit_price, False
        for _ in range(4):
            try:
                query = await self.trader.query_order(order_id)
            except Exception:
                query = None
            price = self._extract_order_avg_price(query or {})
            if price is not None and price > 0:
                return price, True
            await asyncio.sleep(0.05)
        await self.state.add_log(
            "debug",
            f"[real] no order avg for {symbol} oid={order_id}, using executable-book fallback",
        )
        return fallback_exit_price, False

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
        now = float(close_ts or time.time())
        # Calculate exit latency (decision → actual close)
        if pos.exit_signal_ts > 0:
            pos.exit_latency_ms = (now - pos.exit_signal_ts) * 1000.0

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
                "exit_latency_ms": pos.exit_latency_ms,
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
                    "exit_latency_ms": pos.exit_latency_ms,
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
        current_equity = max(0.0, float(self.state.balance or 0.0))
        now = time.time()
        if now - self.state.day_start_ts > 86400:
            self.state.day_start_ts = now
            self.state.day_start_balance = current_equity

        day_loss_pct = (self.state.day_start_balance - current_equity) / max(1e-9, self.state.day_start_balance)
        if day_loss_pct >= float(self.cfg.risk.daily_loss_pct_kill):
            self.state.kill_switch = True
            self.state.last_kill_reason = f"daily loss {day_loss_pct*100:.1f}%"
            await self.state.add_log("error", f"KILL: {self.state.last_kill_reason}")
            return

        if current_equity > self.state.session_peak_balance:
            self.state.session_peak_balance = current_equity
        peak = self.state.session_peak_balance or self.state.session_starting_balance
        if peak > 0:
            dd = (peak - current_equity) / peak
            if dd >= float(self.cfg.risk.max_drawdown_pct_kill):
                self.state.kill_switch = True
                self.state.last_kill_reason = f"drawdown {dd*100:.1f}%"
                await self.state.add_log("error", f"KILL: {self.state.last_kill_reason}")
