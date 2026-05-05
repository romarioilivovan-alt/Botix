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


@dataclass
class _Quote:
    """A pending paper limit order, or a deferred taker order awaiting latency."""
    symbol: str
    side: str
    price: float
    qty: float
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


def _vwap_by_notional(levels, notional_target: float):
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
        level_notional = p * q
        if total_value + level_notional >= notional_target:
            remaining = notional_target - total_value
            partial_qty = remaining / p
            total_value += partial_qty * p
            total_qty += partial_qty
            eaten += 1
            return total_value / total_qty, total_qty, total_value, eaten
        total_value += level_notional
        total_qty += q
        eaten += 1
    if total_qty <= 0:
        return None, 0.0, 0.0, 0
    return total_value / total_qty, total_qty, total_value, eaten


def _vwap_by_qty(levels, qty_target: float):
    """Walk through book levels to fill `qty_target` tokens.

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
            total_value += remaining_qty * p
            total_qty += remaining_qty
            eaten += 1
            return total_value / total_qty, total_qty, total_value, eaten
        total_value += p * q
        total_qty += q
        eaten += 1
    if total_qty <= 0:
        return None, 0.0, 0.0, 0
    return total_value / total_qty, total_qty, total_value, eaten


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

    def stop(self) -> None:
        self._stop.set()

    async def init_balance(self) -> None:
        async with self.state.lock:
            self.state.balance = float(self.cfg.paper_starting_balance)
            self.state.session_starting_balance = self.state.balance
            self.state.session_peak_balance = self.state.balance
            self.state.day_start_ts = time.time()
            self.state.day_start_balance = self.state.balance

    async def loop(self) -> None:
        """Main management tick. Runs ~5x/sec."""
        await self.init_balance()
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.exception("paper tick error: %s", e)
            await asyncio.sleep(0.2)

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

    def _free_balance(self) -> float:
        # Free margin = balance - sum of position margins
        used = sum(p.margin_usdt for p in self.state.positions.values())
        return max(0.0, self.state.balance - used)

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

        depth = book.top_notional(10)

        # Resolve leverage. "max" mode queries MEXC for the contract's actual
        # max leverage (cached per symbol). "fixed" uses cfg.fixed_leverage.
        if str(self.cfg.risk.leverage_mode).lower() == "fixed":
            max_lev_hint = int(self.cfg.risk.fixed_leverage)
        else:
            max_lev_hint = await self._max_leverage(sym)

        decision = self.alloc.decide(
            opp,
            self.state,
            balance_free=self._free_balance(),
            max_leverage_for_symbol=max_lev_hint,
            book_top_notional=depth,
        )
        if not decision.accept:
            await self.state.add_log("debug", f"reject {sym} {opp.side}: {decision.reason}")
            return

        taker_entry = bool(getattr(self.cfg.strategy, "taker_entry", False))
        if taker_entry:
            # Taker: queue with entry_latency to mimic signal→exchange round-trip.
            # Actual VWAP fill happens on a later tick using the THEN-current book,
            # which is what a real market order would hit.
            latency_ms = max(0, int(getattr(self.cfg.strategy, "entry_latency_ms", 200)))
            quote_price = book.best_ask if opp.side == "LONG" else book.best_bid
            now_ts = time.time()
            q = _Quote(
                symbol=sym,
                side=opp.side,
                price=quote_price,                 # placeholder; real fill at VWAP later
                qty=decision.notional_usdt / max(1e-12, quote_price),
                notional=decision.notional_usdt,
                margin=decision.margin_usdt,
                leverage=decision.leverage,
                placed_ts=now_ts,
                fair_at_quote=opp.fair,
                sigma_at_quote=opp.sigma,
                z_at_quote=opp.z,
                taker_open_at=now_ts + latency_ms / 1000.0,
            )
            self._quotes[sym] = q
            await self.state.add_log(
                "debug",
                f"[paper] taker queued {sym} {opp.side} (fill in {latency_ms}ms)",
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

        qty = decision.notional_usdt / quote_price

        if taker_entry:
            # Cross the spread now — fill immediately, no queue.
            # Use current fair / sigma since it's effectively also the "fill" moment.
            fair_now = (book.best_bid + book.best_ask) / 2.0  # MEXC mid as fallback
            agg_stats = self.agg.compute_stats(sym)
            fair_for_open = agg_stats.fair if agg_stats.fair is not None else fair_now
            sigma_for_open = agg_stats.sigma_spread or 0.0
            q = _Quote(
                symbol=sym,
                side=opp.side,
                price=quote_price,
                qty=qty,
                notional=decision.notional_usdt,
                margin=decision.margin_usdt,
                leverage=decision.leverage,
                placed_ts=time.time(),
                fair_at_quote=fair_for_open,
                sigma_at_quote=sigma_for_open,
                z_at_quote=opp.z,
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
            notional=decision.notional_usdt,
            margin=decision.margin_usdt,
            leverage=decision.leverage,
            placed_ts=time.time(),
            fair_at_quote=opp.fair,
            sigma_at_quote=opp.sigma,
            z_at_quote=opp.z,
        )
        self._quotes[sym] = q
        await self.state.add_log(
            "info",
            f"[paper] quote {sym} {opp.side} @ {quote_price:.6g} "
            f"(notional={decision.notional_usdt:.2f}, lev={decision.leverage}, z={opp.z:.2f})",
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
                # Latency elapsed. Walk current (post-latency) book for VWAP fill.
                target_levels = book.asks if q.side == "LONG" else book.bids
                vwap, qty_real, notional_real, eaten = _vwap_by_notional(
                    target_levels, q.notional
                )
                if vwap is None or qty_real <= 0:
                    self._quotes.pop(sym, None)
                    continue
                if notional_real < q.notional * 0.5:
                    await self.state.add_log(
                        "debug",
                        f"[paper] skip {sym} {q.side}: thin book "
                        f"(filled {notional_real:.0f}/{q.notional:.0f} USDT in {eaten} levels)",
                    )
                    self._quotes.pop(sym, None)
                    continue
                # Adjust qty/notional to actually-filled amounts.
                q.price = vwap
                q.qty = qty_real
                q.notional = notional_real
                q.margin = notional_real / max(1.0, q.leverage)
                agg_stats = self.agg.compute_stats(sym)
                fair_for_open = agg_stats.fair if agg_stats.fair is not None else (
                    (book.best_bid + book.best_ask) / 2.0
                )
                sigma_for_open = agg_stats.sigma_spread or q.sigma_at_quote
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

        # Fixed TP at fair value — used by mean-reversion: exit when the
        # MEXC-vs-Binance deviation has collapsed back to the reference.
        tp_at_fair = fair if bool(getattr(self.cfg.strategy, "use_fair_tp", False)) else None
        pos = ManagedPosition(
            symbol=q.symbol,
            side=q.side,
            entry_price=fill_price,
            notional_usdt=q.notional,
            margin_usdt=q.margin,
            leverage=q.leverage,
            qty=q.qty,
            open_ts=now,
            fair_at_open=fair,
            sigma_at_open=max(sigma, 0.0),
            tp_price=tp_at_fair,
            best_excursion=fill_price,
            quote_ts=q.placed_ts,
        )
        # Initial SL: widest of (margin-based, 2× current spread). The
        # spread floor protects against being stopped out by the natural
        # tick-level noise that occurs immediately after a maker fill.
        sl_frac = _hard_sl_price_fraction(self.cfg, q.leverage)
        sl_dist_margin = pos.entry_price * sl_frac
        cur_book = self.agg.get_book(q.symbol)
        spread_now = 0.0
        if cur_book and cur_book.best_bid and cur_book.best_ask:
            spread_now = max(0.0, cur_book.best_ask - cur_book.best_bid)
        # Spread floor: SL must be at least N spreads from entry. For taker
        # scalping the entry already pays 1 spread; without enough headroom
        # the first 1-2 ticks of noise stop us out before the signal-flip
        # exit (avg ~3s) can fire. 6× empirically gives the flip enough room.
        sl_dist = max(sl_dist_margin, 6.0 * spread_now)
        if q.side == "LONG":
            pos.stop_price = pos.entry_price - sl_dist
        else:
            pos.stop_price = pos.entry_price + sl_dist
        # Remember R (initial SL distance) and the book imbalance at open so
        # that R-based trailing and signal-flip exit can reference them.
        pos.initial_sl_distance = sl_dist
        try:
            agg_stats = self.agg.compute_stats(q.symbol)
            pos.entry_imbalance = agg_stats.mexc_book_imbalance
        except Exception:
            pos.entry_imbalance = None

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
                pos.last_pnl_usdt = (mid - pos.entry_price) * pos.qty
            else:
                pos.last_pnl_usdt = (pos.entry_price - mid) * pos.qty
            if pos.margin_usdt > 0:
                pos.last_pnl_pct = pos.last_pnl_usdt / pos.margin_usdt * 100.0

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
                    vwap, _, _, _ = _vwap_by_qty(close_levels, pos.qty)
                    exit_price = vwap if vwap is not None else pos.tp_price
                    await self._close_position(pos, exit_price=exit_price, reason="tp")
                    continue

            # Signal-flip exit (book-imbalance scalping): if MEXC top-5 imbalance
            # has reversed sign relative to our position, the thesis is dead —
            # exit at market BEFORE the SL has a chance to widen losses.
            if bool(getattr(s, "signal_flip_exit", False)):
                try:
                    cur_imb = self.agg.compute_stats(sym).mexc_book_imbalance
                except Exception:
                    cur_imb = None
                if cur_imb is not None:
                    exit_thr = float(getattr(s, "imbalance_exit_log", 0.0) or 0.0)
                    flipped = False
                    if pos.side == "LONG" and cur_imb < -exit_thr:
                        flipped = True
                    elif pos.side == "SHORT" and cur_imb > exit_thr:
                        flipped = True
                    if flipped:
                        close_levels = book.bids if pos.side == "LONG" else book.asks
                        vwap, _, _, _ = _vwap_by_qty(close_levels, pos.qty)
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
                vwap, qty_filled, _, _ = _vwap_by_qty(close_levels, pos.qty)
                exit_price = vwap if vwap is not None else pos.stop_price
                await self._close_position(pos, exit_price=exit_price, reason="sl")
                continue

            # Time-exit backstop — also goes through book.
            if now - pos.open_ts > float(s.max_hold_sec):
                close_levels = book.bids if pos.side == "LONG" else book.asks
                vwap, qty_filled, _, _ = _vwap_by_qty(close_levels, pos.qty)
                exit_price = vwap if vwap is not None else (
                    book.best_bid if pos.side == "LONG" else book.best_ask
                )
                await self._close_position(pos, exit_price=exit_price, reason="time")
                continue

    async def _close_position(self, pos: ManagedPosition, *, exit_price: float, reason: str) -> None:
        now = time.time()
        if pos.side == "LONG":
            realized = (exit_price - pos.entry_price) * pos.qty
        else:
            realized = (pos.entry_price - exit_price) * pos.qty
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
            self.state.positions.pop(pos.symbol, None)

            cd_min = float(self.cfg.strategy.cooldown_min_sec)
            cd_max = float(self.cfg.strategy.cooldown_max_sec)
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
            })

        await self.state.add_log(
            "info" if realized >= 0 else "warn",
            f"[paper] CLOSE {pos.symbol} {pos.side} @ {exit_price:.6g} "
            f"({reason}) PnL={realized:+.4f} USDT",
        )

        try:
            entry_latency = (pos.open_ts - pos.quote_ts) if pos.quote_ts > 0 else None
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
                "extra": {"best_excursion": pos.best_excursion},
                "entry_latency_sec": entry_latency,
            })
        except Exception as e:
            logger.warning("trade insert failed: %s", e)

    async def _log_equity_periodically(self) -> None:
        now = time.time()
        if now - self._equity_log_last_ts < 5.0:
            return
        self._equity_log_last_ts = now

        # equity = balance + unrealized PnL
        unreal = sum(p.last_pnl_usdt for p in self.state.positions.values())
        equity = self.state.balance + unreal

        async with self.state.lock:
            self.state.equity_history.append({
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
        # Daily reset
        now = time.time()
        if now - self.state.day_start_ts > 86400:
            self.state.day_start_ts = now
            self.state.day_start_balance = self.state.balance

        # Daily loss kill
        day_loss_pct = (self.state.day_start_balance - self.state.balance) / max(1e-9, self.state.day_start_balance)
        if day_loss_pct >= float(self.cfg.risk.daily_loss_pct_kill):
            self.state.kill_switch = True
            self.state.last_kill_reason = f"daily loss {day_loss_pct*100:.1f}%"
            await self.state.add_log("error", f"KILL: {self.state.last_kill_reason}")
            return

        # Drawdown kill
        peak = self.state.session_peak_balance or self.state.session_starting_balance
        if peak > 0:
            dd = (peak - self.state.balance) / peak
            if dd >= float(self.cfg.risk.max_drawdown_pct_kill):
                self.state.kill_switch = True
                self.state.last_kill_reason = f"drawdown {dd*100:.1f}%"
                await self.state.add_log("error", f"KILL: {self.state.last_kill_reason}")
