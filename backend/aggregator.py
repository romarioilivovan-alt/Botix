"""Per-symbol rolling stats aggregator.

Owns books, recent trades, fair value computation and rolling statistics
(σ_spread, OFI, fair velocity, book depth). Updated on every tick.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from .state import OrderBook, SymbolStats

logger = logging.getLogger(__name__)

STOCK_SYMBOLS = frozenset({
    "NVIDIA_USDT",
    "MSTRSTOCK_USDT",
    "TSLA_USDT",
    "INTC_USDT",
    "NVDA_USDT",
    "MSTR_USDT",
})


def _book_imbalance_log(book: OrderBook, levels: int = 5, contract_size: float = 1.0) -> Optional[float]:
    """log(bid_notional / ask_notional) over top-N levels. Positive = bid-heavy."""
    bid_n = 0.0
    ask_n = 0.0
    for p, q in (book.bids[:levels] if book.bids else []):
        try:
            bid_n += float(p) * float(q) * float(contract_size)
        except Exception:
            pass
    for p, q in (book.asks[:levels] if book.asks else []):
        try:
            ask_n += float(p) * float(q) * float(contract_size)
        except Exception:
            pass
    if bid_n <= 0 or ask_n <= 0:
        return None
    return math.log(bid_n / ask_n)


def _max_burst_in_window(samples: Deque[Tuple[float, float]], now: float,
                         window_sec: float = 5.0, bucket_sec: float = 1.0) -> Optional[float]:
    """Max absolute signed-notional accumulated in any `bucket_sec` slice over `window_sec`.

    Used to detect single-second sweeps. Sign is preserved: a single $50k
    aggressive-sell second returns -50000.
    """
    cutoff = now - window_sec
    relevant = [(t, v) for (t, v) in samples if t >= cutoff]
    if not relevant:
        return None
    best_signed = 0.0
    # Slide a 1s window from earliest to latest sample
    relevant.sort()
    i = 0
    bucket_sum = 0.0
    for j in range(len(relevant)):
        bucket_sum += relevant[j][1]
        while i < j and relevant[j][0] - relevant[i][0] > bucket_sec:
            bucket_sum -= relevant[i][1]
            i += 1
        if abs(bucket_sum) > abs(best_signed):
            best_signed = bucket_sum
    return best_signed


@dataclass
class _SymbolAgg:
    mexc_book: OrderBook = field(default_factory=OrderBook)
    binance_book: OrderBook = field(default_factory=OrderBook)
    mexc_fair: Optional[float] = None

    # Rolling spread samples (ts, MEXC_mid - F) — limited window
    spread_samples: Deque[Tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=4000)
    )
    # Rolling fair samples (ts, F)
    fair_samples: Deque[Tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=400)
    )
    fair_ema: Optional[float] = None
    # Rolling MEXC mid samples for self-reverting strategy
    mexc_mid_samples: Deque[Tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=2000)
    )
    # Rolling Binance trades (ts, signed_notional) — positive = aggressive buy
    trade_samples: Deque[Tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=2000)
    )
    # Rolling MEXC own bid-ask spread in bps (for wide-spread detection)
    mexc_spread_samples: Deque[Tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=400)
    )

    # MEXC raw depth buffers. The websocket client now subscribes to
    # `sub.depth.full`, so every push is treated as a fresh top-of-book
    # snapshot replacement rather than an incremental delta stream.
    mexc_bids_map: Dict[float, float] = field(default_factory=dict)
    mexc_asks_map: Dict[float, float] = field(default_factory=dict)


class Aggregator:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._symbols: Dict[str, _SymbolAgg] = {}
        self._binance_to_mexc: Dict[str, str] = {}
        self._price_factors: Dict[str, float] = {}  # mexc_symbol -> factor
        self._contract_sizes: Dict[str, float] = {}  # mexc_symbol -> base units per contract

    def reset(self) -> None:
        self._symbols.clear()
        self._binance_to_mexc.clear()
        self._contract_sizes.clear()

    def configure_symbols(self, mexc_to_binance: Dict[str, Optional[str]],
                           price_factors: Optional[Dict[str, float]] = None,
                           contract_sizes: Optional[Dict[str, float]] = None) -> None:
        """Register working set and reverse map."""
        self._binance_to_mexc = {
            v: k for k, v in mexc_to_binance.items() if v
        }
        self._price_factors: Dict[str, float] = price_factors or {}
        self._contract_sizes = contract_sizes or {}
        # ensure agg objects exist for working set
        for mexc in mexc_to_binance:
            self._symbols.setdefault(mexc, _SymbolAgg())
        # drop stale
        for old in list(self._symbols.keys()):
            if old not in mexc_to_binance:
                self._symbols.pop(old, None)

    def symbols(self) -> List[str]:
        return list(self._symbols.keys())

    def _fair_price_mode(self) -> str:
        strategy = getattr(getattr(self, "cfg", None), "strategy", None)
        mode = str(getattr(strategy, "fair_price_mode", "mid") or "mid").strip().lower()
        return mode or "mid"

    def _fair_ema_alpha(self) -> float:
        strategy = getattr(getattr(self, "cfg", None), "strategy", None)
        alpha = float(getattr(strategy, "fair_ema_alpha", 0.2) or 0.2)
        return min(1.0, max(0.0, alpha))

    def _binance_fair_value(self, agg: _SymbolAgg) -> Optional[float]:
        mode = self._fair_price_mode()
        if mode in {"ema_mid", "blend_mexc_fair"}:
            return agg.fair_ema if agg.fair_ema is not None else agg.binance_book.mid
        return agg.binance_book.mid

    def _selected_fair_value(self, agg: _SymbolAgg) -> Optional[float]:
        mode = self._fair_price_mode()
        binance_fair = self._binance_fair_value(agg)
        mexc_fair = agg.mexc_fair
        if mode == "mexc_fair":
            return mexc_fair if mexc_fair is not None else binance_fair
        if mode == "blend_mexc_fair":
            if binance_fair is not None and mexc_fair is not None:
                return (binance_fair + mexc_fair) / 2.0
            return mexc_fair if mexc_fair is not None else binance_fair
        return binance_fair

    def _update_fair_reference(self, agg: _SymbolAgg, ts: float) -> None:
        mid = agg.binance_book.mid
        if mid is None:
            return
        alpha = self._fair_ema_alpha()
        if agg.fair_ema is None or alpha >= 1.0:
            agg.fair_ema = mid
        else:
            agg.fair_ema = (alpha * mid) + ((1.0 - alpha) * agg.fair_ema)
        fair_ref = self._selected_fair_value(agg)
        if fair_ref is not None:
            agg.fair_samples.append((ts, fair_ref))

    def get_book(self, mexc_symbol: str) -> Optional[OrderBook]:
        a = self._symbols.get(mexc_symbol)
        if not a:
            return None
        return a.mexc_book

    def get_binance_book(self, mexc_symbol: str) -> Optional[OrderBook]:
        a = self._symbols.get(mexc_symbol)
        if not a:
            return None
        return a.binance_book

    @property
    def contract_sizes(self) -> Dict[str, float]:
        return self._contract_sizes

    def contract_size_for(self, mexc_symbol: str) -> float:
        return float(self._contract_sizes.get(mexc_symbol, 1.0) or 1.0)

    # ---------- ingest callbacks ----------

    def on_binance_depth(self, binance_symbol: str, bids: list, asks: list, ts: float) -> None:
        mexc = self._binance_to_mexc.get(binance_symbol)
        if not mexc:
            return
        agg = self._symbols.get(mexc)
        if not agg:
            return
        factor = self._price_factors.get(mexc, 1.0)
        try:
            b = [[float(p) * factor, float(q)] for p, q in bids if float(q) > 0][:20]
            a = [[float(p) * factor, float(q)] for p, q in asks if float(q) > 0][:20]
        except Exception:
            return
        agg.binance_book = OrderBook(bids=b, asks=a, ts=ts)

        self._update_fair_reference(agg, ts)
        self._update_spread_sample(mexc, ts)

    def on_mexc_fair_price(self, mexc_symbol: str, fair_price: float, ts: float) -> None:
        agg = self._symbols.get(mexc_symbol)
        if not agg:
            return
        try:
            fair = float(fair_price)
        except Exception:
            return
        if fair <= 0:
            return
        agg.mexc_fair = fair
        fair_ref = self._selected_fair_value(agg)
        if fair_ref is not None:
            agg.fair_samples.append((ts, fair_ref))
        self._update_spread_sample(mexc_symbol, ts)

    def on_binance_trade(
        self,
        binance_symbol: str,
        price: float,
        qty: float,
        buyer_is_maker: bool,
        ts: float,
    ) -> None:
        mexc = self._binance_to_mexc.get(binance_symbol)
        if not mexc:
            return
        agg = self._symbols.get(mexc)
        if not agg:
            return
        # buyer_is_maker == True means a market SELL hit the bid (aggressive sell)
        sign = -1.0 if buyer_is_maker else 1.0
        notional = price * qty
        agg.trade_samples.append((ts, sign * notional))

    def on_mexc_depth(self, mexc_symbol: str, bids: list, asks: list, ts: float) -> None:
        agg = self._symbols.get(mexc_symbol)
        if not agg:
            return
        # `sub.depth.full` sends full top-N snapshots. Replace the local book
        # every push so stale levels cannot survive across updates.
        next_bids: Dict[float, float] = {}
        for it in bids or []:
            try:
                p = float(it[0])
                q = float(it[1])
            except Exception:
                continue
            if q > 0:
                next_bids[p] = q
        next_asks: Dict[float, float] = {}
        for it in asks or []:
            try:
                p = float(it[0])
                q = float(it[1])
            except Exception:
                continue
            if q > 0:
                next_asks[p] = q

        agg.mexc_bids_map = next_bids
        agg.mexc_asks_map = next_asks

        # snapshot top
        if agg.mexc_bids_map:
            sorted_bids = sorted(agg.mexc_bids_map.items(), key=lambda x: -x[0])
        else:
            sorted_bids = []
        if agg.mexc_asks_map:
            sorted_asks = sorted(agg.mexc_asks_map.items(), key=lambda x: x[0])
        else:
            sorted_asks = []

        agg.mexc_book = OrderBook(
            bids=[[p, q] for p, q in sorted_bids[:50]],
            asks=[[p, q] for p, q in sorted_asks[:50]],
            ts=ts,
        )
        # Track own MEXC mid for Bollinger-band self-reverting strategy
        m_mid = agg.mexc_book.mid
        if m_mid is not None and m_mid > 0:
            agg.mexc_mid_samples.append((ts, m_mid))
        self._update_spread_sample(mexc_symbol, ts)

        # Track MEXC own spread (bps) for wide-spread detection
        bb = agg.mexc_book.best_bid
        ba = agg.mexc_book.best_ask
        if bb and ba and ba > bb > 0:
            mid = (bb + ba) / 2.0
            sp_bps = (ba - bb) / mid * 1e4
            agg.mexc_spread_samples.append((ts, sp_bps))

    # ---------- compute ----------

    def _update_spread_sample(self, mexc_symbol: str, ts: float) -> None:
        agg = self._symbols.get(mexc_symbol)
        if not agg:
            return
        m_mid = agg.mexc_book.mid
        fair_ref = self._selected_fair_value(agg)
        if m_mid is None or fair_ref is None:
            return
        agg.spread_samples.append((ts, m_mid - fair_ref))

    def compute_stats(self, mexc_symbol: str) -> SymbolStats:
        agg = self._symbols.get(mexc_symbol)
        st = SymbolStats()
        if not agg:
            return st
        contract_size = self.contract_size_for(mexc_symbol)

        now = time.time()
        st.last_update_ts = now
        if getattr(agg.mexc_book, "ts", 0.0) > 0:
            st.mexc_book_age_ms = max(0.0, (now - float(agg.mexc_book.ts)) * 1000.0)
        if getattr(agg.binance_book, "ts", 0.0) > 0:
            st.binance_book_age_ms = max(0.0, (now - float(agg.binance_book.ts)) * 1000.0)

        st.mexc_mid = agg.mexc_book.mid

        # For stocks without Binance reference, use MEXC price as fair
        is_stock = mexc_symbol in STOCK_SYMBOLS
        if is_stock and agg.binance_book.mid is None:
            # Use MEXC price as fair for stocks (no Binance reference available)
            st.fair = st.mexc_mid
            logger.info(f"[STOCK] {mexc_symbol}: Using MEXC price as fair (no Binance ref): {st.fair}")
        else:
            # Normal case: use selected Binance-derived fair reference
            st.fair = self._selected_fair_value(agg)

        if st.fair is None or st.mexc_mid is None:
            return st

        # For stocks using MEXC as fair, spread is 0 (no arbitrage opportunity)
        # Use bid-ask spread instead for volatility estimation
        if is_stock and agg.binance_book.mid is None:
            spread = 0.0
            st.spread = spread
            st.spread_bps = 0.0
            # Use MEXC bid-ask spread for sigma calculation
            if agg.mexc_book.bids and agg.mexc_book.asks:
                bid = float(agg.mexc_book.bids[0][0]) if agg.mexc_book.bids else st.mexc_mid
                ask = float(agg.mexc_book.asks[0][0]) if agg.mexc_book.asks else st.mexc_mid
                ba_spread = ask - bid
                agg.spread_samples.append((now, ba_spread))
        else:
            # Normal case: spread between MEXC and Binance
            spread = st.mexc_mid - st.fair
            st.spread = spread
            st.spread_bps = spread / st.fair * 1e4 if st.fair > 0 else None

        # σ over rolling window
        win = float(self.cfg.strategy.sigma_spread_window_sec)
        cutoff = now - win
        samples = [v for ts_s, v in agg.spread_samples if ts_s >= cutoff]
        min_samples = int(getattr(self.cfg.strategy, "min_spread_samples", 60))
        min_sigma_bps = float(getattr(self.cfg.strategy, "min_sigma_bps", 0.3))
        if len(samples) >= min_samples:
            mean = sum(samples) / len(samples)
            var = sum((x - mean) ** 2 for x in samples) / len(samples)
            sigma = math.sqrt(var) if var > 0 else 0.0
            st.sigma_spread = sigma
            # Reject z computation when σ is unrealistically small (warm-up artifact)
            min_sigma = (st.fair or 0.0) * (min_sigma_bps / 1e4)
            if sigma > 0 and sigma >= min_sigma:
                st.z_score = spread / sigma

        # OFI
        ofi_win = float(self.cfg.strategy.ofi_window_sec)
        ofi_cut = now - ofi_win
        ofi = sum(v for ts_s, v in agg.trade_samples if ts_s >= ofi_cut)
        st.ofi = ofi

        # Fair velocity (bps/sec) using fair samples in a window
        fv_win = float(self.cfg.strategy.fair_velocity_window_sec)
        fv_cut = now - fv_win
        fs = [(t, p) for t, p in agg.fair_samples if t >= fv_cut]
        if len(fs) >= 2:
            t0, p0 = fs[0]
            t1, p1 = fs[-1]
            dt = max(1e-3, t1 - t0)
            if p0 > 0:
                st.fair_velocity_bps_per_sec = (p1 - p0) / p0 * 1e4 / dt

        # Multi-timeframe velocity for trend filters (5s and 30s windows)
        for win, target_attr in ((5.0, "fair_velocity_5s_bps"),
                                  (30.0, "fair_velocity_30s_bps")):
            cut = now - win
            samples = [(t, p) for t, p in agg.fair_samples if t >= cut]
            if len(samples) >= 2:
                t0, p0 = samples[0]
                t1, p1 = samples[-1]
                dt = max(1e-3, t1 - t0)
                if p0 > 0:
                    setattr(st, target_attr, (p1 - p0) / p0 * 1e4 / dt)

        # Book top notional
        st.mexc_book_top10_notional = agg.mexc_book.top_notional(10, contract_size=contract_size)

        # Top-5 book imbalance on MEXC (microstructure signal)
        st.mexc_book_imbalance = _book_imbalance_log(
            agg.mexc_book, levels=5, contract_size=contract_size
        )

        micro_levels = 3
        st.long_path_hole_points = agg.mexc_book.path_hole_points("LONG", levels=micro_levels)
        st.short_path_hole_points = agg.mexc_book.path_hole_points("SHORT", levels=micro_levels)
        st.long_path_shape = agg.mexc_book.level_shape_ratio("LONG", levels=micro_levels)
        st.short_path_shape = agg.mexc_book.level_shape_ratio("SHORT", levels=micro_levels)
        st.long_support_ratio = agg.mexc_book.support_ratio("LONG", levels=micro_levels)
        st.short_support_ratio = agg.mexc_book.support_ratio("SHORT", levels=micro_levels)
        st.long_support_shape = agg.mexc_book.level_shape_ratio("SHORT", levels=micro_levels)
        st.short_support_shape = agg.mexc_book.level_shape_ratio("LONG", levels=micro_levels)
        st.long_back_hole_points = agg.mexc_book.path_hole_points("SHORT", levels=micro_levels)
        st.short_back_hole_points = agg.mexc_book.path_hole_points("LONG", levels=micro_levels)

        # Current MEXC spread + 30s rolling avg
        bb = agg.mexc_book.best_bid
        ba = agg.mexc_book.best_ask
        if bb and ba and ba > bb > 0:
            mid = (bb + ba) / 2.0
            st.mexc_spread_bps = (ba - bb) / mid * 1e4
            avg_cut = now - 30.0
            avg_samples = [v for ts_s, v in agg.mexc_spread_samples if ts_s >= avg_cut]
            if avg_samples:
                st.mexc_spread_bps_avg = sum(avg_samples) / len(avg_samples)

        # Binance burst detector
        st.binance_burst_usdt_1s = _max_burst_in_window(
            agg.trade_samples, now, window_sec=5.0, bucket_sec=1.0
        )

        # MEXC own-price Bollinger over 60-second window
        bb_win = 60.0
        bb_cut = now - bb_win
        bb_samples = [p for t_s, p in agg.mexc_mid_samples if t_s >= bb_cut]
        if len(bb_samples) >= 30:
            mean = sum(bb_samples) / len(bb_samples)
            var = sum((x - mean) ** 2 for x in bb_samples) / len(bb_samples)
            std = math.sqrt(var) if var > 0 else 0.0
            st.mexc_mid_mean_60s = mean
            st.mexc_mid_std_60s = std
            if std > 0 and st.mexc_mid is not None:
                st.mexc_mid_z_60s = (st.mexc_mid - mean) / std

        return st

    def cleanup_old_samples(self) -> None:
        """Trim sample buffers occasionally to bound memory."""
        now = time.time()
        spread_cut = now - max(60.0, self.cfg.strategy.sigma_spread_window_sec * 2)
        ofi_cut = now - max(5.0, self.cfg.strategy.ofi_window_sec * 4)
        fair_cut = now - max(5.0, self.cfg.strategy.fair_velocity_window_sec * 4)
        mexc_spread_cut = now - 60.0

        for agg in self._symbols.values():
            while agg.spread_samples and agg.spread_samples[0][0] < spread_cut:
                agg.spread_samples.popleft()
            while agg.trade_samples and agg.trade_samples[0][0] < ofi_cut:
                agg.trade_samples.popleft()
            while agg.fair_samples and agg.fair_samples[0][0] < fair_cut:
                agg.fair_samples.popleft()
            while agg.mexc_spread_samples and agg.mexc_spread_samples[0][0] < mexc_spread_cut:
                agg.mexc_spread_samples.popleft()
            mexc_mid_cut = now - 90.0
            while agg.mexc_mid_samples and agg.mexc_mid_samples[0][0] < mexc_mid_cut:
                agg.mexc_mid_samples.popleft()
