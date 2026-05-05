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
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Dict, Optional, Tuple

from .aggregator import Aggregator
from .allocator import CapitalAllocator, AllocationDecision
from .opportunity import Opportunity, OpportunityEngine
from .paper import _trail_long, _trail_short, _hard_sl_price_fraction
from .persistence import Store
from .state import AppState, ManagedPosition

STOCK_SYMBOLS = frozenset({"NVDA_USDT", "MSTR_USDT", "TSLA_USDT", "INTC_USDT"})

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
    order_id: Optional[int] = None


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

    def stop(self) -> None:
        self._stop.set()

    async def init_balance(self) -> None:
        bal = await self._fetch_balance()
        async with self.state.lock:
            self.state.balance = bal
            self.state.session_starting_balance = bal
            self.state.session_peak_balance = bal
            self.state.day_start_ts = time.time()
            self.state.day_start_balance = bal

    async def _fetch_balance(self) -> float:
        try:
            return float(await self.trader.get_available_usdt())
        except Exception:
            return self.state.balance or 0.0

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

    async def loop(self) -> None:
        await self.init_balance()
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.exception("real tick error: %s", e)
            await asyncio.sleep(0.2)

    async def on_signal(self, opp: Opportunity) -> None:
        async with self._lock:
            await self._maybe_place_quote(opp)

    # ----------------------------- internals -----------------------------

    async def _tick(self) -> None:
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
            bal = await self._fetch_balance()
        except Exception:
            return
        async with self.state.lock:
            self.state.balance = bal
            if bal > self.state.session_peak_balance:
                self.state.session_peak_balance = bal

    def _free_balance(self) -> float:
        used = sum(p.margin_usdt for p in self.state.positions.values())
        return max(0.0, self.state.balance - used)

    async def _maybe_place_quote(self, opp: Opportunity) -> None:
        sym = opp.symbol
        if sym in self._quotes:
            return
        if sym in self.state.positions:
            return

        book = self.agg.get_book(sym)
        if not book or book.best_bid is None or book.best_ask is None:
            return

        # 0-fee live check (the symbol can lose its zero-fee status)
        try:
            zero = await self.trader.is_zero_fee_symbol(sym)
        except Exception:
            zero = False
        if not zero:
            return

        lev_max = await self._max_leverage(sym)
        depth = book.top_notional(10)

        # Per-symbol sizing overrides
        _ov = None
        for _o in (self.cfg.symbol_overrides or []):
            if _o.symbol == sym:
                _ov = _o
                break
        decision = self.alloc.decide(
            opp, self.state,
            balance_free=self._free_balance(),
            max_leverage_for_symbol=lev_max,
            book_top_notional=depth,
            margin_pct_override=(_ov.margin_pct if _ov else None),
            leverage_override=(_ov.leverage if _ov else None),
        )
        if not decision.accept:
            return

        taker_entry = bool(getattr(self.cfg.strategy, "taker_entry", False))
        tick = await self._tick_size(sym)

        if taker_entry:
            # Market order: cross the spread, get filled now. On 0-fee promo
            # symbols this is free; on regular symbols this costs 1 bp taker fee.
            try:
                res = await self.trader.open_market(
                    sym, opp.side, decision.notional_usdt, decision.leverage,
                )
            except Exception as e:
                await self.state.add_log("error", f"open_market failed {sym}: {e}")
                return
            price = float(book.best_ask if opp.side == "LONG" else book.best_bid)
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
        )
        self._quotes[sym] = q
        await self.state.add_log(
            "info",
            f"[real] quote {sym} {opp.side} @ {price:.6g} "
            f"(notional={decision.notional_usdt:.2f}, lev={decision.leverage}, oid={order_id})",
        )

    async def _reconcile_quotes(self) -> None:
        now = time.time()
        for sym, q in list(self._quotes.items()):
            book = self.agg.get_book(sym)

            agg_stats = self.agg.compute_stats(sym)
            z = agg_stats.z_score
            if z is not None and abs(z) < float(self.cfg.strategy.cancel_z):
                await self._cancel_quote(q, reason="z_collapsed")
                continue

            if now - q.placed_ts > float(self.cfg.strategy.quote_timeout_sec):
                await self._cancel_quote(q, reason="timeout")
                continue

            # Detect fill: query order state OR check positions
            filled = await self._is_quote_filled(q)
            if filled:
                # Read entry from positions (real fill price)
                pos_raw = await self._find_position(sym, q.side)
                self._quotes.pop(sym, None)
                if pos_raw is None:
                    # very edge: order says filled but position not visible yet
                    await asyncio.sleep(0.3)
                    pos_raw = await self._find_position(sym, q.side)
                if pos_raw is None:
                    await self.state.add_log("warn", f"[real] fill detected for {sym} but no position")
                    continue
                await self._open_position_from_exchange(q, pos_raw, agg_stats.fair, agg_stats.sigma_spread)
                continue

    async def _is_quote_filled(self, q: _Quote) -> bool:
        # 1) Check positions (cheap)
        try:
            raw = await self.trader.get_positions_raw()
            for p in raw:
                if str(p.get("symbol") or "").upper() != q.symbol:
                    continue
                pt = int(p.get("positionType") or 0)
                want = 1 if q.side == "LONG" else 2
                if pt == want and float(p.get("holdVol") or 0) > 0:
                    return True
        except Exception:
            pass

        # 2) Query order state (if we have id)
        if q.order_id:
            try:
                res = await self.trader.query_order(int(q.order_id))
                items = (res or {}).get("data") or []
                if items:
                    state = int(items[0].get("state") or 0)
                    deal_vol = float(items[0].get("dealVol") or 0)
                    if state == 3 or deal_vol > 0:
                        return True
                    if state in (4, 5):
                        # canceled/invalid → drop quote
                        self._quotes.pop(q.symbol, None)
                        return False
            except Exception:
                pass
        return False

    async def _find_position(self, symbol: str, side: str) -> Optional[Dict[str, Any]]:
        try:
            raw = await self.trader.get_positions_raw()
        except Exception:
            return None
        want = 1 if side == "LONG" else 2
        for p in raw:
            if str(p.get("symbol") or "").upper() != symbol:
                continue
            if int(p.get("positionType") or 0) == want and float(p.get("holdVol") or 0) > 0:
                return p
        return None

    async def _cancel_quote(self, q: _Quote, *, reason: str) -> None:
        try:
            await self.trader.cancel_all_for(q.symbol)
        except Exception as e:
            logger.warning("cancel_all_for %s failed: %s", q.symbol, e)
        self._quotes.pop(q.symbol, None)
        await self.state.add_log("debug", f"[real] cancel {q.symbol}: {reason}")

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

        # token amount approximation (paper-style, for PnL display only — exchange tracks the truth)
        info = await self.trader.api.get_contract_info_cached(q.symbol)
        contract_size = float((info or {}).get("contractSize") or 1.0)
        qty_tokens = hold_vol * contract_size

        now = time.time()

        f_value = fair if fair is not None else q.fair_at_quote
        pos = ManagedPosition(
            symbol=q.symbol,
            side=q.side,
            entry_price=entry,
            notional_usdt=q.notional,
            margin_usdt=q.margin,
            leverage=q.leverage,
            qty=qty_tokens,
            open_ts=now,
            fair_at_open=f_value,
            sigma_at_open=max(sigma if sigma is not None else q.sigma_at_quote, 0.0),
            tp_price=None,                  # pure trailing strategy: no fixed TP
            best_excursion=entry,
        )
        # Price-based backstop SL. At 100x leverage: 0.25% price = 25% margin
        # loss, above max observed 2s hold excursion (0.205%). Fires only on crash.
        sl_pct = self._sl_pct_for(q.symbol)
        sl_dist = entry * sl_pct
        if q.side == "LONG":
            sl_raw = entry - sl_dist
        else:
            sl_raw = entry + sl_dist
        tick = await self._tick_size(q.symbol)
        sl_price, _ = _snap_price(sl_raw, tick, q.side, for_sl=True)
        pos.stop_price = sl_price

        # Try to attach exchange SL+TP
        pid = None
        for k in ("positionId", "positionID", "position_id", "id"):
            v = pos_raw.get(k)
            if v is not None:
                try:
                    pid = int(float(v))
                    break
                except Exception:
                    pass
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
        await self.state.add_log(
            "info",
            f"[real] OPEN {q.symbol} {q.side} @ {entry:.6g} "
            f"(F={pos.fair_at_open:.6g}, SL={sl_price:.6g}, qty={qty_tokens:.6g})",
        )

    async def _reconcile_positions(self) -> None:
        s = self.cfg.strategy
        now = time.time()

        # Pull fresh position list once per tick
        try:
            raw_list = await self.trader.get_positions_raw()
        except Exception:
            raw_list = []
        raw_by_sym: Dict[str, Dict[str, Any]] = {
            str(p.get("symbol") or "").upper(): p for p in raw_list
        }

        for sym, pos in list(self.state.positions.items()):
            book = self.agg.get_book(sym)
            mid = book.mid if book else None
            raw = raw_by_sym.get(sym)

            if not raw:
                # Position was closed by exchange (TP/SL hit, or manual)
                # Find realized PnL from history is harder; use last known mid.
                exit_price = mid if mid is not None else pos.entry_price
                await self._mark_closed_externally(pos, exit_price=exit_price, reason="exchange_close")
                continue

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

            if mid is None:
                continue

            # Update best excursion
            if pos.side == "LONG":
                if pos.best_excursion is None or mid > pos.best_excursion:
                    pos.best_excursion = mid
            else:
                if pos.best_excursion is None or mid < pos.best_excursion:
                    pos.best_excursion = mid

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

            # Time-based emergency close (positions stuck without progress)
            if now - pos.open_ts > float(s.max_hold_sec):
                await self._close_market(pos, raw, reason="time")
                continue

    async def _close_market(self, pos: ManagedPosition, raw: Dict[str, Any], *, reason: str) -> None:
        try:
            hold_vol = float(raw.get("holdVol") or 0.0)
            lev = int(float(raw.get("leverage") or pos.leverage))
        except Exception:
            return
        if hold_vol <= 0:
            return

        try:
            res = await self.trader.close_market(pos.symbol, pos.side, hold_vol, lev, margin_mode=1)
        except Exception as e:
            await self.state.add_log("error", f"[real] close_market exception {pos.symbol}: {e}")
            return
        if not res.get("success"):
            await self.state.add_log("warn", f"[real] close_market reject {pos.symbol}: {res.get('message')}")
            return

        book = self.agg.get_book(pos.symbol)
        exit_price = (book.best_bid if pos.side == "LONG" else book.best_ask) if book else pos.entry_price
        await self._mark_closed_externally(pos, exit_price=exit_price or pos.entry_price, reason=reason)

    async def _mark_closed_externally(self, pos: ManagedPosition, *, exit_price: float, reason: str) -> None:
        now = time.time()
        if pos.side == "LONG":
            realized = (exit_price - pos.entry_price) * pos.qty
        else:
            realized = (pos.entry_price - exit_price) * pos.qty

        async with self.state.lock:
            self.state.positions.pop(pos.symbol, None)
            cd_min = float(self.cfg.strategy.cooldown_min_sec)
            cd_max = float(self.cfg.strategy.cooldown_max_sec)
            self.state.cooldown_until[pos.symbol] = now + random.uniform(cd_min, max(cd_min, cd_max))
            self.state.recent_trades.append({
                "ts": now, "symbol": pos.symbol, "side": pos.side,
                "entry": pos.entry_price, "exit": exit_price,
                "pnl": realized,
                "pnl_pct": (realized / pos.margin_usdt * 100.0) if pos.margin_usdt > 0 else 0.0,
                "reason": reason, "duration": now - pos.open_ts,
            })

        await self.state.add_log(
            "info" if realized >= 0 else "warn",
            f"[real] CLOSE {pos.symbol} {pos.side} @ {exit_price:.6g} ({reason}) ~PnL={realized:+.4f}",
        )

        try:
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
                "extra": {"best_excursion": pos.best_excursion},
            })
        except Exception:
            pass

    async def _log_equity_periodically(self) -> None:
        now = time.time()
        if now - self._equity_log_last_ts < 5.0:
            return
        self._equity_log_last_ts = now
        unreal = sum(p.last_pnl_usdt for p in self.state.positions.values())
        equity = self.state.balance + unreal
        async with self.state.lock:
            self.state.equity_history.append({
                "ts": now, "balance": self.state.balance, "equity": equity,
                "open_positions": len(self.state.positions),
            })
        try:
            await self.store.insert_equity(now, "real", self.state.balance, equity, len(self.state.positions))
        except Exception:
            pass

    async def _check_kill_switch(self) -> None:
        if self.state.kill_switch:
            return
        now = time.time()
        if now - self.state.day_start_ts > 86400:
            self.state.day_start_ts = now
            self.state.day_start_balance = self.state.balance

        day_loss_pct = (self.state.day_start_balance - self.state.balance) / max(1e-9, self.state.day_start_balance)
        if day_loss_pct >= float(self.cfg.risk.daily_loss_pct_kill):
            self.state.kill_switch = True
            self.state.last_kill_reason = f"daily loss {day_loss_pct*100:.1f}%"
            await self.state.add_log("error", f"KILL: {self.state.last_kill_reason}")
            return

        peak = self.state.session_peak_balance or self.state.session_starting_balance
        if peak > 0:
            dd = (peak - self.state.balance) / peak
            if dd >= float(self.cfg.risk.max_drawdown_pct_kill):
                self.state.kill_switch = True
                self.state.last_kill_reason = f"drawdown {dd*100:.1f}%"
                await self.state.add_log("error", f"KILL: {self.state.last_kill_reason}")
