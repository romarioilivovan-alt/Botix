"""Paper trading executor.

Maintains a state machine per symbol-position: idle → quoted → filled → manage
→ closed. Simulates fills against MEXC best bid/ask. Applies the SL ladder.
Writes trades to the SQLite store.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .aggregator import Aggregator
from .allocator import CapitalAllocator, AllocationDecision
from .opportunity import Opportunity, OpportunityEngine
from .persistence import Store
from .state import AppState, ManagedPosition, OrderBook


logger = logging.getLogger(__name__)

STOCK_SYMBOLS = frozenset({
    "NVIDIA_USDT",
    "MSTRSTOCK_USDT",
    "TSLA_USDT",
    "INTC_USDT",
    "NVDA_USDT",
    "MSTR_USDT",
})


@dataclass
class _Quote:
    """A pending paper limit order, or a deferred taker order awaiting latency."""
    symbol: str
    side: str
    price: float
    qty: float                   # contracts
    contract_size: float
    notional: float
    margin: float
    leverage: int
    placed_ts: float
    fair_at_quote: float
    sigma_at_quote: float
    z_at_quote: float
    # Taker market orders are queued for `entry_latency_ms` to mimic the
    # signal-to-exchange round-trip. Until `now >= taker_open_at` we don't fill.
    taker_open_at: Optional[float] = None
    # Speed tracking (NEW)
    signal_ts: float = 0.0
    entry_algo: Optional[str] = None
    entry_score: float = 0.0
    spread_bps_at_quote: Optional[float] = None
    fill_ratio: Optional[float] = None
    levels_eaten: Optional[int] = None


def _trail_long(entry: float, best: float, sigma: float, hard_sl_pct: float,
                breakeven_at_sigma: float, trail_dist_sigma: float,
                prev_stop: Optional[float]) -> float:
    """Trailing SL for a LONG position — pure trailing stop, no fixed TP.

    Stages:
      profit < `breakeven_at_sigma`·σ : SL stays at the existing `prev_stop`
                                        (set on open with a spread-aware floor)
      profit ≥ `breakeven_at_sigma`·σ : SL = max(entry, best − `trail_dist_sigma`·σ)

    `hard_sl_pct` is kept only as a safety net when prev_stop is unset.
    """
    profit = max(0.0, best - entry)
    s = max(0.0, sigma)

    if s <= 0 or profit < breakeven_at_sigma * s:
        # Don't tighten further; keep whatever the executor placed at open.
        sl = prev_stop if prev_stop is not None else entry * (1.0 - max(0.0, hard_sl_pct))
    else:
        sl = max(entry, best - trail_dist_sigma * s)

    if prev_stop is not None:
        sl = max(sl, prev_stop)
    return sl


def _trail_short(entry: float, best: float, sigma: float, hard_sl_pct: float,
                 breakeven_at_sigma: float, trail_dist_sigma: float,
                 prev_stop: Optional[float]) -> float:
    profit = max(0.0, entry - best)
    s = max(0.0, sigma)

    if s <= 0 or profit < breakeven_at_sigma * s:
        sl = prev_stop if prev_stop is not None else entry * (1.0 + max(0.0, hard_sl_pct))
    else:
        sl = min(entry, best + trail_dist_sigma * s)

    if prev_stop is not None:
        sl = min(sl, prev_stop)
    return sl


def _r_trail_long(entry: float, best: float, R: float,
                  breakeven_R: float, lock_R: float, trail_R: float,
                  prev_stop: Optional[float]) -> float:
    """R-based trailing for LONG. R = initial SL distance (positive price units).

    Stages by profit measured in multiples of R:
      profit < breakeven_R · R : SL stays at prev_stop (initial -1R)
      breakeven_R ≤ p < lock_R : SL → entry (breakeven, locks 0)
      lock_R ≤ p               : SL → max(entry + 0.5R, best − trail_R · R)
    Stop only ratchets up, never loosens.
    """
    if R <= 0:
        return prev_stop if prev_stop is not None else entry
    profit = max(0.0, best - entry)
    p = profit / R

    if p < breakeven_R:
        sl = prev_stop if prev_stop is not None else (entry - R)
    elif p < lock_R:
        sl = entry
    else:
        sl = max(entry + 0.5 * R, best - trail_R * R)

    if prev_stop is not None:
        sl = max(sl, prev_stop)
    return sl


def _r_trail_short(entry: float, best: float, R: float,
                   breakeven_R: float, lock_R: float, trail_R: float,
                   prev_stop: Optional[float]) -> float:
    if R <= 0:
        return prev_stop if prev_stop is not None else entry
    profit = max(0.0, entry - best)
    p = profit / R

    if p < breakeven_R:
        sl = prev_stop if prev_stop is not None else (entry + R)
    elif p < lock_R:
        sl = entry
    else:
        sl = min(entry - 0.5 * R, best + trail_R * R)

    if prev_stop is not None:
        sl = min(sl, prev_stop)
    return sl


def _vwap_by_notional(levels, notional_target: float, *, contract_size: float = 1.0):
    """Walk through book levels to fill `notional_target` USDT worth.

    For LONG entry pass `book.asks` (we BUY). For SHORT entry pass `book.bids` (we SELL).
    Levels must be sorted in the natural order (asks ascending, bids descending).
    Returns (vwap_price, qty_filled, notional_filled, levels_eaten) or (None, 0, 0, 0).
    """
    if not levels or notional_target <= 0:
        return None, 0.0, 0.0, 0
    total_value = 0.0
    total_qty = 0.0
    eaten = 0
    for p, q in levels:
        try:
            p = float(p); q = float(q)
        except Exception:
            continue
        if p <= 0 or q <= 0:
            continue
        level_notional = p * q * contract_size
        if total_value + level_notional >= notional_target:
            remaining = notional_target - total_value
            partial_qty = remaining / (p * contract_size)
            total_value += partial_qty * p * contract_size
            total_qty += partial_qty
            eaten += 1
            denom = total_qty * max(contract_size, 1e-12)
            return total_value / denom, total_qty, total_value, eaten
        total_value += level_notional
        total_qty += q
        eaten += 1
    if total_qty <= 0:
        return None, 0.0, 0.0, 0
    denom = total_qty * max(contract_size, 1e-12)
    return total_value / denom, total_qty, total_value, eaten


def _vwap_by_notional_capped(
    levels,
    notional_target: float,
    *,
    side: str,
    limit_price: float,
    contract_size: float = 1.0,
):
    """IOC-like fill: walk book only while prices are within a limit cap.

    LONG  -> consume asks with price <= limit_price
    SHORT -> consume bids with price >= limit_price
    """
    if not levels or notional_target <= 0 or limit_price <= 0:
        return None, 0.0, 0.0, 0
    total_value = 0.0
    total_qty = 0.0
    eaten = 0
    side_u = str(side or "").upper()
    for p, q in levels:
        try:
            p = float(p)
            q = float(q)
        except Exception:
            continue
        if p <= 0 or q <= 0:
            continue
        if side_u == "LONG" and p > limit_price:
            break
        if side_u == "SHORT" and p < limit_price:
            break
        level_notional = p * q * contract_size
        if total_value + level_notional >= notional_target:
            remaining = notional_target - total_value
            partial_qty = remaining / (p * contract_size)
            total_value += partial_qty * p * contract_size
            total_qty += partial_qty
            eaten += 1
            denom = total_qty * max(contract_size, 1e-12)
            return total_value / denom, total_qty, total_value, eaten
        total_value += level_notional
        total_qty += q
        eaten += 1
    if total_qty <= 0:
        return None, 0.0, 0.0, 0
    denom = total_qty * max(contract_size, 1e-12)
    return total_value / denom, total_qty, total_value, eaten


def _vwap_by_qty(levels, qty_target: float, *, contract_size: float = 1.0):
    """Walk through book levels to fill `qty_target` contracts.

    For LONG close pass `book.bids` (we SELL). For SHORT close pass `book.asks` (we BUY).
    Returns (vwap_price, qty_filled, notional_filled, levels_eaten) or (None, 0, 0, 0).
    """
    if not levels or qty_target <= 0:
        return None, 0.0, 0.0, 0
    total_value = 0.0
    total_qty = 0.0
    eaten = 0
    for p, q in levels:
        try:
            p = float(p); q = float(q)
        except Exception:
            continue
        if p <= 0 or q <= 0:
            continue
        if total_qty + q >= qty_target:
            remaining_qty = qty_target - total_qty
            total_value += remaining_qty * p * contract_size
            total_qty += remaining_qty
            eaten += 1
            denom = total_qty * max(contract_size, 1e-12)
            return total_value / denom, total_qty, total_value, eaten
        total_value += p * q * contract_size
        total_qty += q
        eaten += 1
    if total_qty <= 0:
        return None, 0.0, 0.0, 0
    denom = total_qty * max(contract_size, 1e-12)
    return total_value / denom, total_qty, total_value, eaten


def _realisable_exit_price(pos: ManagedPosition, book: OrderBook) -> float:
    """Estimate the actual close price we could hit right now.

    Fast exit logic must be based on executable price, not on the pretty mid.
    Otherwise a trade can look profitable on mid while the real bid/ask VWAP
    still closes it flat or negative.
    """
    close_levels = book.bids if pos.side == "LONG" else book.asks
    vwap, _, _, _ = _vwap_by_qty(close_levels, pos.qty, contract_size=pos.contract_size)
    if vwap is not None:
        return vwap
    if pos.side == "LONG":
        return book.best_bid if book.best_bid is not None else pos.entry_price
    return book.best_ask if book.best_ask is not None else pos.entry_price


def _realized_bps_at_price(pos: ManagedPosition, exit_price: float) -> float:
    if pos.entry_price <= 0:
        return 0.0
    if pos.side == "LONG":
        return ((exit_price - pos.entry_price) / pos.entry_price) * 1e4
    return ((pos.entry_price - exit_price) / pos.entry_price) * 1e4


def _residual_edge_bps(pos: ManagedPosition, fair: Optional[float], exit_price: float) -> Optional[float]:
    if fair is None or fair <= 0:
        return None
    if pos.side == "LONG":
        return ((fair - exit_price) / fair) * 1e4
    return ((exit_price - fair) / fair) * 1e4


def _should_profit_protect_exit(
    current_bps: float,
    best_bps: float,
    residual_edge_bps: Optional[float],
    *,
    arm_bps: float,
    giveback_bps: float,
    fast_arm_bps: float,
    fast_giveback_bps: float,
    min_profit_bps: float,
    edge_collapse_bps: float,
) -> bool:
    if (
        arm_bps <= 0
        or giveback_bps <= 0
        or best_bps < arm_bps
        or current_bps < min_profit_bps
        or residual_edge_bps is None
        or residual_edge_bps > edge_collapse_bps
    ):
        return False
    active_giveback = giveback_bps
    if fast_arm_bps > 0 and best_bps >= fast_arm_bps and fast_giveback_bps > 0:
        active_giveback = fast_giveback_bps
    floor_bps = max(min_profit_bps, best_bps - active_giveback)
    return current_bps <= floor_bps


def _update_settled_profit_state(
    pos: ManagedPosition,
    now: float,
    current_bps: float,
    residual_edge_bps: Optional[float],
    *,
    hold_sec: float,
    min_bps: float,
    max_drift_bps: float,
    edge_bps: float,
) -> tuple[bool, str]:
    if (
        hold_sec <= 0
        or min_bps <= 0
        or max_drift_bps < 0
        or edge_bps <= 0
        or residual_edge_bps is None
        or current_bps < min_bps
        or residual_edge_bps > edge_bps
    ):
        pos.settled_profit_since = 0.0
        pos.settled_profit_anchor_bps = 0.0
        return False, ""
    if (
        not pos.settled_profit_since
        or abs(current_bps - pos.settled_profit_anchor_bps) > max_drift_bps
    ):
        pos.settled_profit_since = now
        pos.settled_profit_anchor_bps = current_bps
        return False, ""
    stable_sec = now - pos.settled_profit_since
    if stable_sec >= hold_sec:
        return True, (
            f"settled profit {stable_sec:.2f}s current={current_bps:.2f}bps "
            f"edge={residual_edge_bps:.2f}bps"
        )
    return False, ""


def _should_bad_entry_exit(
    pos: ManagedPosition,
    age_sec: float,
    current_bps: float,
    residual_edge_bps: Optional[float],
    *,
    guard_sec: float,
    min_age_sec: float,
    bad_entry_spread_bps: float,
    exit_bps: float,
    edge_collapse_bps: float,
) -> bool:
    if (
        guard_sec <= 0
        or bad_entry_spread_bps <= 0
        or pos.entry_spread_bps is None
        or pos.entry_spread_bps < bad_entry_spread_bps
        or residual_edge_bps is None
        or residual_edge_bps > edge_collapse_bps
        or age_sec < max(0.0, min_age_sec)
        or age_sec > guard_sec
    ):
        return False
    return current_bps <= exit_bps


def _hard_sl_price_fraction(cfg, leverage: float) -> float:
    """Return SL distance as fraction of entry price (positive value).

    Prefers `hard_sl_margin_pct` (margin-based, leverage-aware) and falls
    back to legacy `hard_sl_pct` (raw price fraction) when margin-pct is 0.
    """
    s = cfg.strategy
    margin_pct = float(getattr(s, "hard_sl_margin_pct", 0.0) or 0.0)
    if margin_pct > 0:
        lev = max(1.0, float(leverage))
        return margin_pct / 100.0 / lev
    return max(0.0, float(getattr(s, "hard_sl_pct", 0.0) or 0.0))


class PaperExecutor:
    def __init__(self, cfg, state: AppState, agg: Aggregator,
                 opp: OpportunityEngine, alloc: CapitalAllocator,
                 store: Store, mexc_trader=None) -> None:
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
        self._max_lev_cache: Dict[str, int] = {}
        self._contract_size_cache: Dict[str, float] = {}

    def stop(self) -> None:
        self._stop.set()

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

    def _sl_pct_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.sl_pct is not None:
            return float(ov.sl_pct)
        if symbol in STOCK_SYMBOLS:
            return float(getattr(self.cfg.strategy, "sl_pct_stocks", 0.0010))
        return float(getattr(self.cfg.strategy, "sl_pct_crypto", 0.0025))

    def _max_hold_sec_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.max_hold_sec is not None:
            return float(ov.max_hold_sec)
        return float(self.cfg.strategy.max_hold_sec)

    def _min_entry_score_for(self, symbol: str) -> float:
        ov = self._override_for(symbol)
        if ov and ov.min_entry_score is not None:
            return float(ov.min_entry_score)
        return 0.0

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
        tick_ms = float(getattr(self.cfg.strategy, "paper_tick_sec", 0.2) or 0.2) * 1000.0
        entry_latency_ms = float(max(0, self._entry_latency_ms_for(symbol)))
        return max(75.0, min(250.0, tick_ms + entry_latency_ms + 25.0))

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

    def _signal_valid_for_fill(
        self,
        symbol: str,
        side: str,
        quote: Optional[_Quote] = None,
    ) -> tuple[bool, str]:
        """Re-check the signal right before simulated IOC fill."""
        st = self.agg.compute_stats(symbol)
        ov = self._override_for(symbol)
        if ov is not None and ov.algorithms:
            self.opp.evaluate_multi(symbol, st, ov)
        else:
            self.opp.evaluate(symbol, st)
        # A signal only needs to be "ideal" when we first decide to trade it.
        # Right before the simulated IOC fill, the microstructure can flatten
        # for a few milliseconds without invalidating the original thesis.
        # At fill time we keep the hard sanity checks (age, drift, explicit
        # side flip) but avoid reapplying every entry blocker.
        if quote is None:
            if st.blocked_reason:
                return False, st.blocked_reason
            if not st.side_hint:
                return False, "no_side_hint"
            if st.side_hint != side:
                return False, f"side_flip={st.side_hint}"
            min_entry_score = self._min_entry_score_for(symbol)
            if st.score < min_entry_score:
                return False, f"score {st.score:.2f} < {min_entry_score:.2f}"
        elif st.side_hint and st.side_hint != side:
            return False, f"side_flip={st.side_hint}"
        if quote is not None:
            max_age_ms = self._signal_max_age_ms_for(symbol)
            if max_age_ms > 0 and quote.signal_ts > 0:
                age_ms = (time.time() - quote.signal_ts) * 1000.0
                age_limit_ms = float(max_age_ms)
                if age_ms > age_limit_ms:
                    grace_ms = self._signal_age_grace_ms_for(symbol)
                    if not (
                        age_ms <= age_limit_ms + grace_ms
                        and self._fresh_books_for_age_grace(st)
                    ):
                        return False, f"signal_age={age_ms:.0f}ms"
            max_spread_drift = self._pre_submit_max_spread_drift_bps_for(symbol)
            if (
                max_spread_drift > 0
                and quote.spread_bps_at_quote is not None
                and st.spread_bps is not None
            ):
                if side == "LONG":
                    spread_drift = st.spread_bps - quote.spread_bps_at_quote
                else:
                    spread_drift = quote.spread_bps_at_quote - st.spread_bps
                if spread_drift > max_spread_drift:
                    return False, f"spread_drift={spread_drift:.2f}bps"
        return True, ""

    def _fill_respects_fair(
        self,
        symbol: str,
        side: str,
        algo: Optional[str],
        fill_price: float,
        fair: float,
    ) -> tuple[bool, str]:
        """Final sanity check using the actual executable fill price."""
        if fair <= 0 or fill_price <= 0:
            return True, ""
        raw_spread_bps = ((fill_price - fair) / fair) * 1e4
        max_chase_bps = self._max_chase_bps_for(symbol, algo)
        if side == "LONG" and raw_spread_bps > max_chase_bps:
            return False, f"fill_chasing_long={raw_spread_bps:.2f}bps"
        if side == "SHORT" and raw_spread_bps < -max_chase_bps:
            return False, f"fill_chasing_short={raw_spread_bps:.2f}bps"
        return True, ""

    async def init_balance(self) -> None:
        async with self.state.lock:
            self.state.balance = float(self.cfg.paper_starting_balance)
            self.state.available_balance = self.state.balance
            self.state.session_starting_balance = self.state.balance
            self.state.session_peak_balance = self.state.balance
            self.state.strategy_realized_pnl = 0.0
            self.state.strategy_session_starting_balance = self.state.balance
            self.state.strategy_session_peak_balance = self.state.balance
            self.state.day_start_ts = time.time()
            self.state.day_start_balance = self.state.balance

    async def loop(self) -> None:
        """Main management tick. Runs ~5x/sec."""
        await self.init_balance()
        tick_sec = max(0.01, float(getattr(self.cfg.strategy, "paper_tick_sec", 0.2) or 0.2))
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.exception("paper tick error: %s", e)
            await asyncio.sleep(tick_sec)

    async def on_signal(self, opp: Opportunity) -> None:
        """Called by engine when a high-score opportunity emerges."""
        async with self._lock:
            await self._maybe_place_quote(opp)

    # ----------------------------- internals -----------------------------

    async def _tick(self) -> None:
        # Walk current quotes — fill or expire
        await self._process_quotes()
        # Walk current positions — update SL, check TP/SL/timeout
        await self._process_positions()
        # Periodic equity log
        await self._log_equity_periodically()
        # Periodic kill-switch check
        await self._check_kill_switch()

    async def _max_leverage(self, symbol: str) -> int:
        """Resolve max leverage from MEXC contract details, cached per symbol."""
        if symbol in self._max_lev_cache:
            return self._max_lev_cache[symbol]
        lev = 0
        if self.trader is not None:
            try:
                lev = int(await self.trader.get_max_leverage(symbol) or 0)
            except Exception:
                lev = 0
        if lev <= 0:
            # Fallback to config (fixed_leverage) if MEXC didn't answer.
            lev = int(getattr(self.cfg.risk, "fixed_leverage", 20) or 20)
        self._max_lev_cache[symbol] = lev
        return lev

    async def _contract_size(self, symbol: str) -> float:
        if symbol in self._contract_size_cache:
            return self._contract_size_cache[symbol]
        size = 1.0
        if self.trader is not None:
            try:
                detail = await self.trader.get_contract_detail(symbol)
                size = float((detail or {}).get("contractSize") or 1.0)
            except Exception:
                size = 1.0
        if size <= 0:
            size = 1.0
        self._contract_size_cache[symbol] = size
        return size

    def _free_balance(self) -> float:
        # Free margin = balance - sum of position margins
        used = sum(p.margin_usdt for p in self.state.positions.values())
        return max(0.0, self.state.balance - used)

    def _current_equity(self) -> float:
        unreal = sum(p.last_pnl_usdt for p in self.state.positions.values())
        return float(self.state.balance or 0.0) + unreal

    async def _record_candidate_reject(
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

    async def _maybe_place_quote(self, opp: Opportunity) -> None:
        if self.state.kill_switch:
            return
        sym = opp.symbol
        if sym in self._quotes:
            return
        if sym in self.state.positions:
            return

        book = self.agg.get_book(sym)
        if not book or book.best_bid is None or book.best_ask is None:
            return

        contract_size = await self._contract_size(sym)
        depth = book.top_notional(10, contract_size=contract_size)
        spread_bps_at_quote = None
        if opp.fair > 0 and opp.entry_price > 0:
            spread_bps_at_quote = ((opp.entry_price - opp.fair) / opp.fair) * 1e4

        # Resolve leverage. "max" mode queries MEXC for the contract's actual
        # max leverage (cached per symbol). "fixed" uses cfg.fixed_leverage.
        if str(self.cfg.risk.leverage_mode).lower() == "fixed":
            max_lev_hint = int(self.cfg.risk.fixed_leverage)
        else:
            max_lev_hint = await self._max_leverage(sym)

        ov = self._override_for(sym)
        min_entry_score = self._min_entry_score_for(sym)
        if opp.score < min_entry_score:
            await self.state.add_log(
                "debug",
                f"reject {sym} {opp.side}: score {opp.score:.2f} < {min_entry_score:.2f}",
            )
            await self._record_candidate_reject(
                symbol=sym,
                side=opp.side,
                score=opp.score,
                z=opp.z,
                blocked=f"score_below={min_entry_score:.2f}",
                fair=opp.fair,
                mexc=(book.best_bid + book.best_ask) / 2.0,
                depth=depth,
            )
            return
        decision = self.alloc.decide(
            opp,
            self.state,
            balance_free=self._free_balance(),
            max_leverage_for_symbol=max_lev_hint,
            book_top_notional=depth,
            margin_pct_override=(ov.margin_pct if ov else None),
            leverage_override=(ov.leverage if ov else None),
            max_notional_override=(ov.max_notional_usdt if ov else None),
        )
        if not decision.accept:
            await self.state.add_log("debug", f"reject {sym} {opp.side}: {decision.reason}")
            await self._record_candidate_reject(
                symbol=sym,
                side=opp.side,
                score=opp.score,
                z=opp.z,
                blocked=decision.reason,
                fair=opp.fair,
                mexc=(book.best_bid + book.best_ask) / 2.0,
                depth=depth,
            )
            return

        taker_entry = bool(getattr(self.cfg.strategy, "taker_entry", False))
        if taker_entry:
            # Taker: queue with entry_latency to mimic signal→exchange round-trip.
            # Actual VWAP fill happens on a later tick using the THEN-current book,
            # which is what a real market order would hit.
            latency_ms = max(0, self._entry_latency_ms_for(sym))
            quote_price = book.best_ask if opp.side == "LONG" else book.best_bid
            now_ts = time.time()
            queue_age_ms = max(0.0, (now_ts - opp.signal_ts) * 1000.0) if opp.signal_ts > 0 else 0.0
            q = _Quote(
                symbol=sym,
                side=opp.side,
                price=quote_price,                 # placeholder; real fill at VWAP later
                qty=decision.notional_usdt / max(1e-12, quote_price * contract_size),
                contract_size=contract_size,
                notional=decision.notional_usdt,
                margin=decision.margin_usdt,
                leverage=decision.leverage,
                placed_ts=now_ts,
                fair_at_quote=opp.fair,
                sigma_at_quote=opp.sigma,
                z_at_quote=opp.z,
                taker_open_at=now_ts + latency_ms / 1000.0,
                signal_ts=opp.signal_ts,
                entry_algo=opp.algorithm,
                entry_score=opp.score,
                spread_bps_at_quote=spread_bps_at_quote,
            )
            self._quotes[sym] = q
            await self.state.add_log(
                "debug",
                f"[paper] taker queued {sym} {opp.side} (fill in {latency_ms}ms, q_age={queue_age_ms:.0f}ms)",
            )
            return
        else:
            # Maker: quote inside the spread; only fills when price comes to us.
            offset_ticks = max(0, int(getattr(self.cfg.strategy, "quote_offset_ticks", 0)))
            spread = max(1e-12, book.best_ask - book.best_bid)
            tick_est = spread / max(1, offset_ticks + 2)
            if opp.side == "LONG":
                quote_price = book.best_bid + tick_est * offset_ticks
                if quote_price >= book.best_ask:
                    quote_price = book.best_ask - tick_est
            else:
                quote_price = book.best_ask - tick_est * offset_ticks
                if quote_price <= book.best_bid:
                    quote_price = book.best_bid + tick_est
        if quote_price <= 0:
            return

        qty = decision.notional_usdt / max(1e-12, quote_price * contract_size)

        if taker_entry:
            # Cross the spread now — fill immediately, no queue.
            # Use current fair / sigma since it's effectively also the "fill" moment.
            fair_now = (book.best_bid + book.best_ask) / 2.0  # MEXC mid as fallback
            agg_stats = self.agg.compute_stats(sym)
            fair_for_open = agg_stats.fair if agg_stats.fair is not None else fair_now
            sigma_for_open = agg_stats.sigma_spread or 0.0
            ok_fill, why_fill = self._fill_respects_fair(
                sym, opp.side, opp.algorithm, quote_price, fair_for_open
            )
            if not ok_fill:
                await self.state.add_log(
                    "debug",
                    f"[paper] skip {sym} {opp.side}: {why_fill}",
                )
                return
            q = _Quote(
                symbol=sym,
                side=opp.side,
                price=quote_price,
                qty=qty,
                contract_size=contract_size,
                notional=decision.notional_usdt,
                margin=decision.margin_usdt,
                leverage=decision.leverage,
                placed_ts=time.time(),
                fair_at_quote=fair_for_open,
                sigma_at_quote=sigma_for_open,
                z_at_quote=opp.z,
                signal_ts=opp.signal_ts,
                entry_algo=opp.algorithm,
                entry_score=opp.score,
                spread_bps_at_quote=spread_bps_at_quote,
            )
            await self.state.add_log(
                "info",
                f"[paper] taker {sym} {opp.side} @ {quote_price:.6g} "
                f"(notional={decision.notional_usdt:.2f}, lev={decision.leverage})",
            )
            await self._open_position(q, fill_price=quote_price,
                                      fair=fair_for_open, sigma=sigma_for_open)
            return

        q = _Quote(
            symbol=sym,
            side=opp.side,
            price=quote_price,
            qty=qty,
            contract_size=contract_size,
            notional=decision.notional_usdt,
            margin=decision.margin_usdt,
            leverage=decision.leverage,
            placed_ts=time.time(),
            fair_at_quote=opp.fair,
            sigma_at_quote=opp.sigma,
            z_at_quote=opp.z,
            signal_ts=opp.signal_ts,
            entry_algo=opp.algorithm,
            entry_score=opp.score,
            spread_bps_at_quote=spread_bps_at_quote,
        )
        self._quotes[sym] = q
        await self.state.add_log(
            "info",
            f"[paper] quote {sym} {opp.side} @ {quote_price:.6g} "
            f"(notional={decision.notional_usdt:.2f}, lev={decision.leverage}, z={opp.z:.2f}, "
            f"q_age={max(0.0, (time.time() - opp.signal_ts) * 1000.0) if opp.signal_ts > 0 else 0.0:.0f}ms)",
        )

    async def _process_quotes(self) -> None:
        now = time.time()
        for sym, q in list(self._quotes.items()):
            book = self.agg.get_book(sym)
            if not book or book.best_bid is None or book.best_ask is None:
                continue

            # ---- Deferred taker entry (latency-aware) ----
            if q.taker_open_at is not None:
                if now < q.taker_open_at:
                    continue  # still within latency window — wait
                ok, why = self._signal_valid_for_fill(sym, q.side, q)
                if not ok:
                    await self.state.add_log(
                        "debug",
                        f"[paper] skip {sym} {q.side}: stale signal ({why})",
                    )
                    await self._record_candidate_reject(
                        symbol=sym,
                        side=q.side,
                        score=q.entry_score,
                        z=q.z_at_quote,
                        blocked=f"stale_signal:{why}",
                        fair=q.fair_at_quote,
                        mexc=(book.best_bid + book.best_ask) / 2.0,
                        depth=book.top_notional(10, contract_size=q.contract_size),
                    )
                    self._quotes.pop(sym, None)
                    continue
                # Latency elapsed. Walk current (post-latency) book for fill.
                target_levels = book.asks if q.side == "LONG" else book.bids
                if bool(getattr(self.cfg.strategy, "taker_ioc_simulation", False)):
                    buf_bps = self._taker_ioc_price_buffer_bps_for(sym)
                    min_fill_ratio = self._taker_ioc_min_fill_ratio_for(sym)
                    if q.side == "LONG":
                        limit_price = q.price * (1.0 + buf_bps / 1e4)
                    else:
                        limit_price = q.price * (1.0 - buf_bps / 1e4)
                    vwap, qty_real, notional_real, eaten = _vwap_by_notional_capped(
                        target_levels,
                        q.notional,
                        side=q.side,
                        limit_price=limit_price,
                        contract_size=q.contract_size,
                    )
                else:
                    min_fill_ratio = 0.5
                    vwap, qty_real, notional_real, eaten = _vwap_by_notional(
                        target_levels, q.notional, contract_size=q.contract_size
                    )
                if vwap is None or qty_real <= 0:
                    await self._record_candidate_reject(
                        symbol=sym,
                        side=q.side,
                        score=q.entry_score,
                        z=q.z_at_quote,
                        blocked="no_fill",
                        fair=q.fair_at_quote,
                        mexc=(book.best_bid + book.best_ask) / 2.0,
                        depth=book.top_notional(10, contract_size=q.contract_size),
                    )
                    self._quotes.pop(sym, None)
                    continue
                if notional_real < q.notional * min_fill_ratio:
                    await self.state.add_log(
                        "debug",
                        f"[paper] skip {sym} {q.side}: ioc/latency thin fill "
                        f"(filled {notional_real:.0f}/{q.notional:.0f} USDT in {eaten} levels)",
                    )
                    await self._record_candidate_reject(
                        symbol=sym,
                        side=q.side,
                        score=q.entry_score,
                        z=q.z_at_quote,
                        blocked=f"thin_fill={notional_real:.0f}/{q.notional:.0f}",
                        fair=q.fair_at_quote,
                        mexc=(book.best_bid + book.best_ask) / 2.0,
                        depth=book.top_notional(10, contract_size=q.contract_size),
                    )
                    self._quotes.pop(sym, None)
                    continue
                # Adjust qty/notional to actually-filled amounts.
                requested_notional = q.notional
                q.price = vwap
                q.qty = qty_real
                q.notional = notional_real
                q.margin = notional_real / max(1.0, q.leverage)
                q.fill_ratio = notional_real / max(requested_notional, 1e-12) if requested_notional > 0 else None
                q.levels_eaten = eaten
                agg_stats = self.agg.compute_stats(sym)
                fair_for_open = agg_stats.fair if agg_stats.fair is not None else (
                    (book.best_bid + book.best_ask) / 2.0
                )
                sigma_for_open = agg_stats.sigma_spread or q.sigma_at_quote
                ok_fill, why_fill = self._fill_respects_fair(
                    sym, q.side, q.entry_algo, vwap, fair_for_open
                )
                if not ok_fill:
                    await self.state.add_log(
                        "debug",
                        f"[paper] skip {sym} {q.side}: {why_fill}",
                    )
                    await self._record_candidate_reject(
                        symbol=sym,
                        side=q.side,
                        score=q.entry_score,
                        z=q.z_at_quote,
                        blocked=why_fill,
                        fair=fair_for_open,
                        mexc=vwap,
                        depth=book.top_notional(10, contract_size=q.contract_size),
                    )
                    self._quotes.pop(sym, None)
                    continue
                self._quotes.pop(sym, None)
                await self._open_position(q, fill_price=vwap,
                                          fair=fair_for_open, sigma=sigma_for_open)
                continue

            # ---- Maker (limit) entry path ----
            agg_stats = self.agg.compute_stats(sym)
            fair = agg_stats.fair
            sigma = agg_stats.sigma_spread or q.sigma_at_quote
            z = agg_stats.z_score

            if z is not None and abs(z) < float(self.cfg.strategy.cancel_z):
                self._quotes.pop(sym, None)
                await self.state.add_log("debug", f"[paper] cancel {sym}: z collapsed to {z:.2f}")
                continue

            if now - q.placed_ts > float(self.cfg.strategy.quote_timeout_sec):
                self._quotes.pop(sym, None)
                await self.state.add_log("debug", f"[paper] timeout {sym}")
                continue

            ok, why = self._signal_valid_for_fill(sym, q.side, q)
            if not ok:
                self._quotes.pop(sym, None)
                await self.state.add_log("debug", f"[paper] cancel {sym}: stale signal ({why})")
                continue

            filled = False
            if q.side == "LONG":
                if book.best_ask is not None and book.best_ask <= q.price:
                    filled = True
            else:
                if book.best_bid is not None and book.best_bid >= q.price:
                    filled = True

            if filled and fair is not None:
                self._quotes.pop(sym, None)
                await self._open_position(q, fill_price=q.price, fair=fair, sigma=sigma)

    async def _open_position(self, q: _Quote, *, fill_price: float,
                             fair: float, sigma: float) -> None:
        now = time.time()
        agg_stats = self.agg.compute_stats(q.symbol)

        # Fixed TP at fair value — used by mean-reversion: exit when the
        # MEXC-vs-Binance deviation has collapsed back to the reference.
        tp_at_fair = fair if self._use_fair_tp_for(q.symbol) else None
        entry_spread_bps = None
        if fair and fair > 0:
            if q.side == "LONG":
                entry_spread_bps = ((fill_price - fair) / fair) * 1e4
            else:
                entry_spread_bps = ((fair - fill_price) / fair) * 1e4
        pos = ManagedPosition(
            symbol=q.symbol,
            side=q.side,
            entry_price=fill_price,
            notional_usdt=q.notional,
            margin_usdt=q.margin,
            leverage=q.leverage,
            qty=q.qty,
            contract_size=q.contract_size,
            open_ts=now,
            fair_at_open=fair,
            sigma_at_open=max(sigma, 0.0),
            tp_price=tp_at_fair,
            best_excursion=fill_price,
            quote_ts=q.placed_ts,
            signal_ts=q.signal_ts,
            entry_latency_ms=(now - q.signal_ts) * 1000.0 if q.signal_ts > 0 else 0.0,
            entry_algo=q.entry_algo,
            entry_score=q.entry_score,
            max_hold_sec=self._max_hold_sec_for(q.symbol),
            entry_fill_ratio=q.fill_ratio,
            entry_levels_eaten=q.levels_eaten,
            entry_spread_bps=entry_spread_bps,
            entry_ofi=agg_stats.ofi,
            entry_imbalance=agg_stats.mexc_book_imbalance,
            entry_fv1=agg_stats.fair_velocity_bps_per_sec,
            entry_fv5=agg_stats.fair_velocity_5s_bps,
            entry_fv30=agg_stats.fair_velocity_30s_bps,
            entry_mexc_book_age_ms=agg_stats.mexc_book_age_ms,
            entry_binance_book_age_ms=agg_stats.binance_book_age_ms,
        )
        # Initial SL: widest of (margin-based, 2× current spread). The
        # spread floor protects against being stopped out by the natural
        # tick-level noise that occurs immediately after a maker fill.
        sl_frac = self._sl_pct_for(q.symbol)
        sl_dist_margin = pos.entry_price * sl_frac
        cur_book = self.agg.get_book(q.symbol)
        spread_now = 0.0
        if cur_book and cur_book.best_bid and cur_book.best_ask:
            spread_now = max(0.0, cur_book.best_ask - cur_book.best_bid)
        # Spread floor: SL must be at least N spreads from entry. For taker
        # scalping the entry already pays 1 spread; without enough headroom
        # the first 1-2 ticks of noise stop us out before the signal-flip
        # exit (avg ~3s) can fire. 6× empirically gives the flip enough room.
        sl_dist = sl_dist_margin
        if q.side == "LONG":
            pos.stop_price = pos.entry_price - sl_dist
        else:
            pos.stop_price = pos.entry_price + sl_dist
        # Remember R (initial SL distance) so that R-based trailing can reference it.
        pos.initial_sl_distance = sl_dist

        async with self.state.lock:
            self.state.positions[q.symbol] = pos
        await self.state.add_log(
            "info",
            f"[paper] OPEN {q.symbol} {q.side} @ {fill_price:.6g} "
            f"(F={fair:.6g}, σ={sigma:.6g}, qty={q.qty:.6g})",
        )

    async def _process_positions(self) -> None:
        s = self.cfg.strategy
        now = time.time()
        for sym, pos in list(self.state.positions.items()):
            book = self.agg.get_book(sym)
            if not book or book.best_bid is None or book.best_ask is None:
                continue

            mid = book.mid
            if mid is None:
                continue

            # Update best excursion
            if pos.side == "LONG":
                if pos.best_excursion is None or mid > pos.best_excursion:
                    pos.best_excursion = mid
            else:
                if pos.best_excursion is None or mid < pos.best_excursion:
                    pos.best_excursion = mid

            # Live PnL
            if pos.side == "LONG":
                pos.last_pnl_usdt = (mid - pos.entry_price) * pos.qty * pos.contract_size
            else:
                pos.last_pnl_usdt = (pos.entry_price - mid) * pos.qty * pos.contract_size
            if pos.margin_usdt > 0:
                pos.last_pnl_pct = pos.last_pnl_usdt / pos.margin_usdt * 100.0

            stats = self.agg.compute_stats(sym)
            current_fair = float(stats.fair) if getattr(stats, "fair", None) else None
            current_imbalance = getattr(stats, "mexc_book_imbalance", None)
            exit_price_now = _realisable_exit_price(pos, book)
            move_bps_now = _realized_bps_at_price(pos, exit_price_now)
            if move_bps_now > pos.best_realized_bps:
                pos.best_realized_bps = move_bps_now
            residual_edge_bps = _residual_edge_bps(pos, current_fair, exit_price_now)
            age_sec = now - pos.open_ts

            # Fixed-TP exit (mean-reversion at fair): exit when MEXC mid has
            # crossed the fair-value reference recorded at open. Fires before
            # SL/signal-flip so a successful revert is realised cleanly.
            if pos.tp_price is not None:
                tp_hit = False
                if pos.side == "LONG" and book.best_bid is not None and book.best_bid >= pos.tp_price:
                    tp_hit = True
                if pos.side == "SHORT" and book.best_ask is not None and book.best_ask <= pos.tp_price:
                    tp_hit = True
                if tp_hit:
                    close_levels = book.bids if pos.side == "LONG" else book.asks
                    vwap, _, _, _ = _vwap_by_qty(close_levels, pos.qty, contract_size=pos.contract_size)
                    exit_price = vwap if vwap is not None else pos.tp_price
                    await self._close_position(pos, exit_price=exit_price, reason="tp")
                    continue

            # Quick scalp TP: take the move as soon as we get a small favorable
            # excursion. This is closer to the live IOC scalp style than waiting
            # for a time exit to decide the trade.
            scalp_tp_bps = self._scalp_take_profit_bps_for(sym)
            if scalp_tp_bps > 0 and pos.entry_price > 0:
                if move_bps_now >= scalp_tp_bps:
                    pos.exit_signal_ts = now
                    await self._close_position(pos, exit_price=exit_price_now, reason="scalp_tp")
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
                await self._close_position(pos, exit_price=exit_price_now, reason="profit_protect")
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
                await self._close_position(pos, exit_price=exit_price_now, reason="settled_profit")
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
                await self._close_position(pos, exit_price=exit_price_now, reason="bad_entry")
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
                await self._close_position(pos, exit_price=exit_price_now, reason="edge_loss")
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
                await self._close_position(pos, exit_price=exit_price_now, reason="dead_trade")
                continue

            # Scratch exit: if a momentum trade did not move in our favor quickly,
            # get out before it rots into a time-loss.
            scratch_exit_sec = self._scratch_exit_sec_for(sym)
            scratch_exit_bps = self._scratch_exit_bps_for(sym)
            if scratch_exit_sec > 0 and pos.entry_price > 0 and age_sec >= scratch_exit_sec:
                if move_bps_now <= scratch_exit_bps:
                    pos.exit_signal_ts = now
                    await self._close_position(pos, exit_price=exit_price_now, reason="scratch")
                    continue

            # Signal-flip exit (book-imbalance scalping): if MEXC top-5 imbalance
            # has reversed sign relative to our position, the thesis is dead —
            # exit at market BEFORE the SL has a chance to widen losses.
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
                        close_levels = book.bids if pos.side == "LONG" else book.asks
                        vwap, _, _, _ = _vwap_by_qty(close_levels, pos.qty, contract_size=pos.contract_size)
                        exit_price = vwap if vwap is not None else (
                            book.best_bid if pos.side == "LONG" else book.best_ask
                        )
                        await self._close_position(pos, exit_price=exit_price, reason="signal_flip")
                        continue

            # SL trailing update (throttled)
            if now - pos.last_sl_update_ts >= float(s.sl_update_throttle_sec):
                hard_sl = _hard_sl_price_fraction(self.cfg, pos.leverage)
                use_r = bool(getattr(s, "use_r_trail", False))
                if use_r and pos.initial_sl_distance and pos.initial_sl_distance > 0:
                    R = float(pos.initial_sl_distance)
                    if pos.side == "LONG":
                        new_sl = _r_trail_long(
                            pos.entry_price, pos.best_excursion or pos.entry_price,
                            R, s.trail_breakeven_R, s.trail_lock_R, s.trail_dist_R,
                            pos.stop_price,
                        )
                    else:
                        new_sl = _r_trail_short(
                            pos.entry_price, pos.best_excursion or pos.entry_price,
                            R, s.trail_breakeven_R, s.trail_lock_R, s.trail_dist_R,
                            pos.stop_price,
                        )
                else:
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
                if pos.stop_price is None or abs(new_sl - pos.stop_price) / max(1e-12, abs(pos.stop_price)) > 1e-6:
                    pos.stop_price = new_sl
                pos.last_sl_update_ts = now

            # Hit SL? Realistic fill: walk through book for qty (slippage past trigger).
            sl_triggered = False
            if pos.side == "LONG" and pos.stop_price is not None and book.best_bid <= pos.stop_price:
                sl_triggered = True
            if pos.side == "SHORT" and pos.stop_price is not None and book.best_ask >= pos.stop_price:
                sl_triggered = True

            if sl_triggered:
                close_levels = book.bids if pos.side == "LONG" else book.asks
                vwap, qty_filled, _, _ = _vwap_by_qty(close_levels, pos.qty, contract_size=pos.contract_size)
                exit_price = vwap if vwap is not None else pos.stop_price
                pos.exit_signal_ts = now  # Mark exit decision time
                await self._close_position(pos, exit_price=exit_price, reason="sl")
                continue

            # Time-exit backstop — also goes through book.
            max_hold_sec = pos.max_hold_sec if pos.max_hold_sec > 0 else float(s.max_hold_sec)
            if now - pos.open_ts > max_hold_sec:
                close_levels = book.bids if pos.side == "LONG" else book.asks
                vwap, qty_filled, _, _ = _vwap_by_qty(close_levels, pos.qty, contract_size=pos.contract_size)
                exit_price = vwap if vwap is not None else (
                    book.best_bid if pos.side == "LONG" else book.best_ask
                )
                pos.exit_signal_ts = now  # Mark exit decision time
                await self._close_position(pos, exit_price=exit_price, reason="time")
                continue

    async def _close_position(self, pos: ManagedPosition, *, exit_price: float, reason: str) -> None:
        now = time.time()
        # Calculate exit latency (decision → actual close)
        if pos.exit_signal_ts > 0:
            pos.exit_latency_ms = (now - pos.exit_signal_ts) * 1000.0

        if pos.side == "LONG":
            realized = (exit_price - pos.entry_price) * pos.qty * pos.contract_size
        else:
            realized = (pos.entry_price - exit_price) * pos.qty * pos.contract_size
        # Apply trading fees. For taker entries both sides are taker; for maker
        # entries only the close (market) is taker. On 0-fee promo symbols both
        # are 0 by default.
        s = self.cfg.strategy
        taker_bps = float(getattr(s, "taker_fee_bps", 0.0) or 0.0)
        maker_bps = float(getattr(s, "maker_fee_bps", 0.0) or 0.0)
        is_taker_entry = bool(getattr(s, "taker_entry", False))
        entry_bps = taker_bps if is_taker_entry else maker_bps
        exit_bps = taker_bps                         # close is always market
        fee_drag = (entry_bps + exit_bps) / 1e4 * pos.notional_usdt
        realized -= fee_drag

        # Liquidation cap: in real trading the exchange force-closes when margin
        # is exhausted, so a single trade can lose at most its locked margin
        # minus a tiny buffer.
        if pos.margin_usdt > 0 and realized < -pos.margin_usdt * 0.99:
            realized = -pos.margin_usdt * 0.99
            reason = f"{reason}_liq"
        pos.realized_pnl = realized
        pos.closed = True
        pos.close_reason = reason
        pos.close_ts = now
        pos.close_price = exit_price

        async with self.state.lock:
            self.state.balance += realized
            if self.state.balance > self.state.session_peak_balance:
                self.state.session_peak_balance = self.state.balance
            self.state.strategy_realized_pnl += realized
            if self.state.balance > self.state.strategy_session_peak_balance:
                self.state.strategy_session_peak_balance = self.state.balance
            self.state.positions.pop(pos.symbol, None)

            cd_min = self._cooldown_min_sec_for(pos.symbol)
            cd_max = self._cooldown_max_sec_for(pos.symbol)
            self.state.cooldown_until[pos.symbol] = now + random.uniform(cd_min, max(cd_min, cd_max))

            self.state.recent_trades.append({
                "ts": now,
                "symbol": pos.symbol,
                "side": pos.side,
                "entry": pos.entry_price,
                "exit": exit_price,
                "pnl": realized,
                "pnl_pct": (realized / pos.margin_usdt * 100.0) if pos.margin_usdt > 0 else 0.0,
                "reason": reason,
                "duration": now - pos.open_ts,
                "entry_latency_ms": pos.entry_latency_ms,
                "exit_latency_ms": pos.exit_latency_ms,
                "entry_algo": pos.entry_algo,
                "entry_score": pos.entry_score,
            })

        await self.state.add_log(
            "info" if realized >= 0 else "warn",
            f"[paper] CLOSE {pos.symbol} {pos.side} @ {exit_price:.6g} "
            f"({reason}) PnL={realized:+.4f} USDT",
        )

        try:
            entry_latency = (pos.open_ts - pos.quote_ts) if pos.quote_ts > 0 else None
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
                "ts": now,
                "mode": "paper",
                "symbol": pos.symbol,
                "side": pos.side,
                "entry": pos.entry_price,
                "exit": exit_price,
                "qty": pos.qty,
                "notional": pos.notional_usdt,
                "margin": pos.margin_usdt,
                "leverage": pos.leverage,
                "open_ts": pos.open_ts,
                "close_ts": now,
                "duration_sec": now - pos.open_ts,
                "pnl_usdt": realized,
                "pnl_pct": (realized / pos.margin_usdt * 100.0) if pos.margin_usdt > 0 else 0.0,
                "fair_at_open": pos.fair_at_open,
                "sigma_at_open": pos.sigma_at_open,
                "z_at_open": None,
                "close_reason": reason,
                "extra": {
                    "best_excursion": pos.best_excursion,
                    "best_excursion_bps": best_excursion_bps,
                    "realized_bps": realized_bps,
                    "entry_algo": pos.entry_algo,
                    "entry_score": pos.entry_score,
                    "entry_latency_ms": pos.entry_latency_ms,
                    "exit_latency_ms": pos.exit_latency_ms,
                    "contract_size": pos.contract_size,
                    "entry_fill_ratio": pos.entry_fill_ratio,
                    "entry_levels_eaten": pos.entry_levels_eaten,
                    "entry_spread_bps": pos.entry_spread_bps,
                    "entry_ofi": pos.entry_ofi,
                    "entry_imbalance": pos.entry_imbalance,
                    "entry_fv1": pos.entry_fv1,
                    "entry_fv5": pos.entry_fv5,
                    "entry_fv30": pos.entry_fv30,
                    "entry_mexc_book_age_ms": pos.entry_mexc_book_age_ms,
                    "entry_binance_book_age_ms": pos.entry_binance_book_age_ms,
                },
                "entry_latency_sec": entry_latency,
            })
        except Exception as e:
            logger.warning("trade insert failed: %s", e)

    async def _log_equity_periodically(self) -> None:
        now = time.time()
        if now - self._equity_log_last_ts < 5.0:
            return
        self._equity_log_last_ts = now

        equity = self._current_equity()
        if equity > self.state.session_peak_balance:
            self.state.session_peak_balance = equity
        if equity > self.state.strategy_session_peak_balance:
            self.state.strategy_session_peak_balance = equity

        async with self.state.lock:
            self.state.equity_history.append({
                "ts": now,
                "balance": self.state.balance,
                "equity": equity,
                "open_positions": len(self.state.positions),
            })
            self.state.strategy_equity_history.append({
                "ts": now,
                "balance": self.state.balance,
                "equity": equity,
                "open_positions": len(self.state.positions),
            })

        try:
            await self.store.insert_equity(now, "paper", self.state.balance, equity, len(self.state.positions))
        except Exception:
            pass

    async def _check_kill_switch(self) -> None:
        if self.state.kill_switch:
            return
        current_equity = self._current_equity()
        # Daily reset
        now = time.time()
        if now - self.state.day_start_ts > 86400:
            self.state.day_start_ts = now
            self.state.day_start_balance = current_equity

        # Daily loss kill
        day_loss_pct = (self.state.day_start_balance - current_equity) / max(1e-9, self.state.day_start_balance)
        if day_loss_pct >= float(self.cfg.risk.daily_loss_pct_kill):
            self.state.kill_switch = True
            self.state.last_kill_reason = f"daily loss {day_loss_pct*100:.1f}%"
            await self.state.add_log("error", f"KILL: {self.state.last_kill_reason}")
            return

        # Drawdown kill
        if current_equity > self.state.session_peak_balance:
            self.state.session_peak_balance = current_equity
        peak = self.state.session_peak_balance or self.state.session_starting_balance
        if peak > 0:
            dd = (peak - current_equity) / peak
            if dd >= float(self.cfg.risk.max_drawdown_pct_kill):
                self.state.kill_switch = True
                self.state.last_kill_reason = f"drawdown {dd*100:.1f}%"
                await self.state.add_log("error", f"KILL: {self.state.last_kill_reason}")
