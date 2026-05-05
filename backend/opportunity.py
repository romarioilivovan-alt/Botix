"""Opportunity scoring engine.

Selects an algorithm via cfg.strategy.algorithm and evaluates per-symbol
signals. All algorithms write into st.score / st.side_hint / st.blocked_reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .state import SymbolStats


@dataclass
class Opportunity:
    symbol: str
    side: str            # LONG / SHORT
    score: float
    entry_price: float
    fair: float
    sigma: float
    z: float


class OpportunityEngine:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def evaluate(self, symbol: str, st: SymbolStats) -> SymbolStats:
        """Mutates st with score / side_hint / blocked_reason based on the
        configured algorithm. Always populates st even when blocked."""
        st.score = 0.0
        st.side_hint = None
        st.blocked_reason = None

        if st.fair is None or st.mexc_mid is None:
            st.blocked_reason = "no_books"
            return st

        algo = str(getattr(self.cfg.strategy, "algorithm", "meanrev")).lower()

        if algo == "momentum":
            self._eval_momentum(symbol, st)
        elif algo == "ofi":
            self._eval_ofi(symbol, st)
        elif algo == "imbalance":
            self._eval_imbalance(symbol, st)
        elif algo == "sweep":
            self._eval_sweep(symbol, st)
        elif algo == "wide_spread":
            self._eval_wide_spread(symbol, st)
        elif algo == "raw_momentum":
            self._eval_raw_momentum(symbol, st)
        elif algo == "book_lean":
            self._eval_book_lean(symbol, st)
        elif algo == "confluence":
            self._eval_confluence(symbol, st)
        elif algo == "bb_revert":
            self._eval_bb_revert(symbol, st)
        else:
            self._eval_meanrev(symbol, st)

        # Optional inversion: flip side after evaluation. Used to validate the
        # observation that all naive directional signals on this venue have been
        # systematically anti-correlated with actual short-term price.
        if bool(getattr(self.cfg.strategy, "invert", False)) and st.side_hint:
            st.side_hint = "SHORT" if st.side_hint == "LONG" else "LONG"
        return st

    def _evaluate_single(self, symbol: str, st: SymbolStats, algo: str) -> SymbolStats:
        """Run a single named algorithm on a shallow copy of st."""
        import copy
        s = copy.copy(st)
        s.score = 0.0
        s.side_hint = None
        s.blocked_reason = None

        if s.fair is None or s.mexc_mid is None:
            s.blocked_reason = "no_books"
            return s

        a = algo.lower()
        if a == "momentum":
            self._eval_momentum(symbol, s)
        elif a == "ofi":
            self._eval_ofi(symbol, s)
        elif a == "imbalance":
            self._eval_imbalance(symbol, s)
        elif a == "sweep":
            self._eval_sweep(symbol, s)
        elif a == "wide_spread":
            self._eval_wide_spread(symbol, s)
        elif a == "raw_momentum":
            self._eval_raw_momentum(symbol, s)
        elif a == "book_lean":
            self._eval_book_lean(symbol, s)
        elif a == "confluence":
            self._eval_confluence(symbol, s)
        elif a == "bb_revert":
            self._eval_bb_revert(symbol, s)
        else:
            self._eval_meanrev(symbol, s)

        if bool(getattr(self.cfg.strategy, "invert", False)) and s.side_hint:
            s.side_hint = "SHORT" if s.side_hint == "LONG" else "LONG"
        return s

    def evaluate_multi(self, symbol: str, st: SymbolStats, override) -> SymbolStats:
        """Evaluate multiple algorithms per override and apply mode logic.

        ANY       — any algo fires; pick highest score
        BEST      — highest score must exceed min_score_threshold (1.2)
        CONSENSUS — all algos must agree on same side; score = geometric mean
        """
        import math

        algos = list(override.algorithms or [])
        if not algos:
            return self.evaluate(symbol, st)

        mode = str(override.algo_mode or "ANY").upper()
        min_score_threshold = 1.2

        signals = []
        for algo in algos:
            result = self._evaluate_single(symbol, st, algo)
            if result.score > 0 and result.side_hint and not result.blocked_reason:
                signals.append((result.score, result.side_hint, result))

        if not signals:
            st.score = 0.0
            st.side_hint = None
            st.blocked_reason = "no_algo_fired"
            return st

        if mode == "CONSENSUS":
            sides = {s for _, s, _ in signals}
            if len(sides) > 1:
                st.score = 0.0
                st.side_hint = None
                st.blocked_reason = "consensus_conflict"
                return st
            product = 1.0
            for sc, _, _ in signals:
                product *= sc
            geo_mean = math.pow(product, 1.0 / len(signals))
            best_st = max(signals, key=lambda x: x[0])[2]
            st.score = geo_mean
            st.side_hint = best_st.side_hint
            st.blocked_reason = None
            return st

        # ANY or BEST: pick highest score
        best_score, best_side, best_st = max(signals, key=lambda x: x[0])

        if mode == "BEST" and best_score < min_score_threshold:
            st.score = 0.0
            st.side_hint = None
            st.blocked_reason = "best_below_threshold"
            return st

        st.score = best_score
        st.side_hint = best_side
        st.blocked_reason = None
        st.z_score = getattr(best_st, "z_score", st.z_score)
        st.sigma_spread = getattr(best_st, "sigma_spread", st.sigma_spread)
        return st

    # ------------------------ meanrev (fade MEXC vs F) ------------------------
    def _eval_meanrev(self, symbol: str, st: SymbolStats) -> SymbolStats:
        s = self.cfg.strategy

        if st.sigma_spread is None or st.sigma_spread <= 0:
            st.blocked_reason = "warming_up"
            return st
        if st.z_score is None:
            st.blocked_reason = "no_z"
            return st

        z = st.z_score
        side = "SHORT" if z > 0 else "LONG"

        if abs(z) < float(s.entry_z):
            st.blocked_reason = "low_z"
            return st

        max_z = float(getattr(s, "max_entry_z", 4.0))
        if max_z > 0 and abs(z) > max_z:
            st.blocked_reason = f"extreme_z={z:.1f}"
            return st

        fv = st.fair_velocity_bps_per_sec or 0.0
        if abs(fv) > float(s.max_fair_velocity_bps_per_sec):
            if (z > 0 and fv > 0) or (z < 0 and fv < 0):
                st.blocked_reason = f"fast_fair_vel={fv:.1f}bps/s"
                return st

        if s.require_ofi_alignment:
            ofi = st.ofi or 0.0
            if z > 0 and ofi > 0 and abs(ofi) > 5_000:
                st.blocked_reason = f"ofi_with_dev={ofi:+.0f}"
                return st
            if z < 0 and ofi < 0 and abs(ofi) > 5_000:
                st.blocked_reason = f"ofi_with_dev={ofi:+.0f}"
                return st

        depth = st.mexc_book_top10_notional or 0.0
        if depth < float(s.min_book_depth_usdt):
            st.blocked_reason = f"thin_book={depth:.0f}"
            return st

        liq_factor = min(1.0, depth / max(1.0, float(s.min_book_depth_usdt) * 5.0))
        regime_penalty = 1.0
        if abs(fv) > 0:
            regime_penalty = max(0.5, 1.0 - abs(fv) / max(1.0, float(s.max_fair_velocity_bps_per_sec) * 2.0))

        st.score = abs(z) * liq_factor * regime_penalty
        st.side_hint = side
        return st

    # ------------------------ momentum (MEXC follows Binance) ------------------------
    def _eval_momentum(self, symbol: str, st: SymbolStats) -> SymbolStats:
        """Follow Binance direction. Best when MEXC is still lagging that direction."""
        s = self.cfg.strategy

        fv = st.fair_velocity_bps_per_sec
        if fv is None:
            st.blocked_reason = "no_velocity"
            return st

        min_v = float(getattr(s, "momentum_min_velocity_bps_per_sec", 1.0))
        max_v = float(getattr(s, "momentum_max_velocity_bps_per_sec", 8.0))

        if abs(fv) < min_v:
            st.blocked_reason = f"flat_fair={fv:.2f}bps/s"
            return st
        if abs(fv) > max_v:
            st.blocked_reason = f"crash_fair={fv:.2f}bps/s"
            return st

        # Direction follows Binance:
        side = "LONG" if fv > 0 else "SHORT"

        # Optional lag check: prefer when MEXC is BEHIND the Binance move.
        # For a UP move (LONG): MEXC mid should be < F (still catching up).
        require_lag = bool(getattr(s, "momentum_require_lag", True))
        if require_lag and st.spread is not None:
            spread = st.spread  # MEXC_mid - F
            if side == "LONG" and spread > 0:
                st.blocked_reason = "no_lag_long"
                return st
            if side == "SHORT" and spread < 0:
                st.blocked_reason = "no_lag_short"
                return st

        depth = st.mexc_book_top10_notional or 0.0
        if depth < float(s.min_book_depth_usdt):
            st.blocked_reason = f"thin_book={depth:.0f}"
            return st

        # OFI alignment as confirmation: positive OFI on UP move strengthens signal.
        ofi = st.ofi or 0.0
        if s.require_ofi_alignment:
            if side == "LONG" and ofi < 0 and abs(ofi) > 5_000:
                st.blocked_reason = f"ofi_against={ofi:+.0f}"
                return st
            if side == "SHORT" and ofi > 0 and abs(ofi) > 5_000:
                st.blocked_reason = f"ofi_against={ofi:+.0f}"
                return st

        # Score: |fv| × liquidity × OFI alignment bonus
        liq_factor = min(1.0, depth / max(1.0, float(s.min_book_depth_usdt) * 5.0))
        ofi_bonus = 1.0
        if (side == "LONG" and ofi > 0) or (side == "SHORT" and ofi < 0):
            ofi_bonus = 1.2

        st.score = abs(fv) / max(1.0, min_v) * liq_factor * ofi_bonus
        st.side_hint = side
        return st

    # ------------------------ ofi (order flow imbalance) ------------------------
    def _eval_ofi(self, symbol: str, st: SymbolStats) -> SymbolStats:
        """Follow aggressive flow on Binance trades."""
        s = self.cfg.strategy

        ofi = st.ofi
        if ofi is None:
            st.blocked_reason = "no_ofi"
            return st

        min_ofi = float(getattr(s, "ofi_min_usdt", 5_000))
        if abs(ofi) < min_ofi:
            st.blocked_reason = f"low_ofi={ofi:+.0f}"
            return st

        # Avoid trading into news / extreme moves
        fv = st.fair_velocity_bps_per_sec or 0.0
        max_v = float(getattr(s, "momentum_max_velocity_bps_per_sec", 8.0))
        if abs(fv) > max_v:
            st.blocked_reason = f"crash_fair={fv:.2f}bps/s"
            return st

        side = "LONG" if ofi > 0 else "SHORT"

        depth = st.mexc_book_top10_notional or 0.0
        if depth < float(s.min_book_depth_usdt):
            st.blocked_reason = f"thin_book={depth:.0f}"
            return st

        liq_factor = min(1.0, depth / max(1.0, float(s.min_book_depth_usdt) * 5.0))
        st.score = abs(ofi) / max(1.0, min_ofi) * liq_factor
        st.side_hint = side
        return st

    # ------------------------ imbalance (MEXC own book) ------------------------
    def _eval_imbalance(self, symbol: str, st: SymbolStats) -> SymbolStats:
        """Top-5 book imbalance on MEXC predicts very-short-term direction.
        DWF-style: be where the latent demand is, not where price has been."""
        s = self.cfg.strategy

        imb = st.mexc_book_imbalance
        if imb is None:
            st.blocked_reason = "no_imbalance"
            return st

        min_imb = float(getattr(s, "imbalance_min_log", 0.7))
        max_imb = float(getattr(s, "imbalance_max_log", 3.0))

        if abs(imb) < min_imb:
            st.blocked_reason = f"low_imb={imb:+.2f}"
            return st
        if abs(imb) > max_imb:
            # extreme = stale book / single huge order, not a real signal
            st.blocked_reason = f"extreme_imb={imb:+.2f}"
            return st

        # Bid-heavy → price will lift → LONG. Ask-heavy → SHORT.
        side = "LONG" if imb > 0 else "SHORT"

        # Don't fight a fast move on Binance
        fv = st.fair_velocity_bps_per_sec or 0.0
        max_v = float(getattr(s, "momentum_max_velocity_bps_per_sec", 8.0))
        if abs(fv) > max_v:
            st.blocked_reason = f"crash_fair={fv:.2f}bps/s"
            return st
        # If Binance moves strongly opposite — block
        if (side == "LONG" and fv < -2.0) or (side == "SHORT" and fv > 2.0):
            st.blocked_reason = f"fv_against={fv:+.2f}"
            return st

        depth = st.mexc_book_top10_notional or 0.0
        if depth < float(s.min_book_depth_usdt):
            st.blocked_reason = f"thin_book={depth:.0f}"
            return st

        liq_factor = min(1.0, depth / max(1.0, float(s.min_book_depth_usdt) * 5.0))
        st.score = abs(imb) / max(1e-6, min_imb) * liq_factor
        st.side_hint = side
        return st

    # ------------------------ sweep (Binance burst) ------------------------
    def _eval_sweep(self, symbol: str, st: SymbolStats) -> SymbolStats:
        """Detect a single-second burst on Binance and front-run on MEXC."""
        s = self.cfg.strategy

        burst = st.binance_burst_usdt_1s
        if burst is None:
            st.blocked_reason = "no_burst_data"
            return st

        min_burst = float(getattr(s, "sweep_min_usdt_1s", 25_000))
        if abs(burst) < min_burst:
            st.blocked_reason = f"no_burst={burst:+.0f}"
            return st

        # Avoid catching the very tail of crashes
        fv = st.fair_velocity_bps_per_sec or 0.0
        max_v = float(getattr(s, "momentum_max_velocity_bps_per_sec", 8.0))
        if abs(fv) > max_v:
            st.blocked_reason = f"crash_fair={fv:.2f}bps/s"
            return st

        # Burst > 0 = aggressive buys on Binance → expect upward continuation → LONG MEXC
        side = "LONG" if burst > 0 else "SHORT"

        depth = st.mexc_book_top10_notional or 0.0
        if depth < float(s.min_book_depth_usdt):
            st.blocked_reason = f"thin_book={depth:.0f}"
            return st

        liq_factor = min(1.0, depth / max(1.0, float(s.min_book_depth_usdt) * 5.0))
        st.score = abs(burst) / max(1.0, min_burst) * liq_factor
        st.side_hint = side
        return st

    # ------------------------ wide_spread (paid spread capture) ------------------------
    def _eval_wide_spread(self, symbol: str, st: SymbolStats) -> SymbolStats:
        """When MEXC spread is anomalously wide, take a maker quote on the
        side where the book is thinner — bet on spread normalisation. Local
        market-making behavior."""
        s = self.cfg.strategy

        cur = st.mexc_spread_bps
        avg = st.mexc_spread_bps_avg
        imb = st.mexc_book_imbalance
        if cur is None or avg is None or avg <= 0:
            st.blocked_reason = "no_spread_baseline"
            return st

        ratio = cur / avg
        if ratio < float(getattr(s, "wide_spread_ratio", 2.5)):
            st.blocked_reason = f"narrow_spread={ratio:.2f}"
            return st
        if cur < float(getattr(s, "wide_spread_min_bps", 5.0)):
            st.blocked_reason = f"trivial_bps={cur:.1f}"
            return st

        if imb is None:
            st.blocked_reason = "no_imb_for_dir"
            return st

        min_imb = float(getattr(s, "wide_spread_min_imbalance", 0.3))
        if abs(imb) < min_imb:
            st.blocked_reason = f"flat_book={imb:+.2f}"
            return st

        # Take side of the heavier book — that's where price will gravitate
        # when the wide spread snaps back.
        side = "LONG" if imb > 0 else "SHORT"

        # Block if Binance is moving strongly against the lean
        fv = st.fair_velocity_bps_per_sec or 0.0
        if (side == "LONG" and fv < -2.0) or (side == "SHORT" and fv > 2.0):
            st.blocked_reason = f"fv_against={fv:+.2f}"
            return st

        depth = st.mexc_book_top10_notional or 0.0
        if depth < float(s.min_book_depth_usdt):
            st.blocked_reason = f"thin_book={depth:.0f}"
            return st

        st.score = ratio * (cur / 10.0) * abs(imb)
        st.side_hint = side
        return st

    # ------------------------ raw_momentum (Binance Δprice direction) ------------------------
    def _eval_raw_momentum(self, symbol: str, st: SymbolStats) -> SymbolStats:
        """Multi-timeframe momentum:
            - 1s velocity must exceed `min_bps` (signal)
            - 5s velocity must agree on direction (no fading 1-sec blips)
            - 30s velocity must NOT strongly oppose us (anti-fade trend filter)
        This avoids the 'short on a 1-second pullback in a strong uptrend' trap.
        """
        s = self.cfg.strategy

        fv = st.fair_velocity_bps_per_sec
        fv5 = st.fair_velocity_5s_bps
        fv30 = st.fair_velocity_30s_bps
        if fv is None:
            st.blocked_reason = "no_velocity"
            return st

        min_v = float(getattr(s, "raw_momentum_min_bps", 2.0))
        max_v = float(getattr(s, "raw_momentum_max_bps", 30.0))

        if abs(fv) < min_v:
            st.blocked_reason = f"flat={fv:.2f}bps/s"
            return st
        if abs(fv) > max_v:
            st.blocked_reason = f"crash={fv:.2f}bps/s"
            return st

        side = "LONG" if fv > 0 else "SHORT"
        sig_v = 1 if fv > 0 else -1

        # 5-second confirmation: 5s velocity must agree on direction.
        # Skips counter-trend 1-sec blips during sustained moves.
        require_5s = bool(getattr(s, "raw_momentum_require_5s_agree", True))
        if require_5s and fv5 is not None:
            sig_5 = 1 if fv5 > 0 else (-1 if fv5 < 0 else 0)
            if sig_5 != 0 and sig_5 != sig_v:
                st.blocked_reason = f"5s_disagrees fv1={fv:.2f} fv5={fv5:.2f}"
                return st

        # 30-second anti-fade filter: don't open against an established trend.
        anti_fade_threshold = float(getattr(s, "raw_momentum_anti_fade_30s_bps", 3.0))
        if fv30 is not None and anti_fade_threshold > 0:
            if abs(fv30) > anti_fade_threshold:
                sig_30 = 1 if fv30 > 0 else -1
                if sig_30 != sig_v:
                    st.blocked_reason = f"against_30s_trend fv1={fv:.2f} fv30={fv30:.2f}"
                    return st

        depth = st.mexc_book_top10_notional or 0.0
        if depth < float(s.min_book_depth_usdt):
            st.blocked_reason = f"thin_book={depth:.0f}"
            return st

        st.score = abs(fv) / max(1.0, min_v)
        st.side_hint = side
        return st

    # ------------------------ book_lean (MEXC top-3 stack ratio) ------------------------
    def _eval_book_lean(self, symbol: str, st: SymbolStats) -> SymbolStats:
        """Even simpler than imbalance: bid notional > N× ask notional → LONG.
        Uses the top-5 imbalance signal from aggregator (in log-ratio).
        Threshold expressed in plain ratio for clarity."""
        s = self.cfg.strategy

        imb = st.mexc_book_imbalance
        if imb is None:
            st.blocked_reason = "no_imbalance"
            return st

        import math as _m
        min_ratio = float(getattr(s, "book_lean_min_ratio", 2.0))
        min_log = _m.log(max(1.01, min_ratio))

        if abs(imb) < min_log:
            st.blocked_reason = f"flat_book={imb:+.2f}"
            return st
        # bid-heavy → expect pop up → LONG
        side = "LONG" if imb > 0 else "SHORT"

        # Don't fight a strong opposite Binance move
        fv = st.fair_velocity_bps_per_sec or 0.0
        if (side == "LONG" and fv < -3.0) or (side == "SHORT" and fv > 3.0):
            st.blocked_reason = f"fv_against={fv:+.2f}"
            return st

        depth = st.mexc_book_top10_notional or 0.0
        if depth < float(s.min_book_depth_usdt):
            st.blocked_reason = f"thin_book={depth:.0f}"
            return st

        st.score = abs(imb) / max(1e-6, min_log)
        st.side_hint = side
        return st

    # ------------------------ confluence (latency-resistant multi-signal) ------------------------
    def _eval_confluence(self, symbol: str, st: SymbolStats) -> SymbolStats:
        """Three indicators must all agree on direction:
            (1) Binance fair_velocity sustained
            (2) Binance OFI same sign (real flow, not noise)
            (3) MEXC top-5 book imbalance same sign (microstructure confirms)
        Survives high latency because triple-confirmation implies a real
        directional regime that lasts more than a few seconds."""
        s = self.cfg.strategy

        fv = st.fair_velocity_bps_per_sec
        ofi = st.ofi
        imb = st.mexc_book_imbalance

        if fv is None or ofi is None or imb is None:
            st.blocked_reason = "missing_signal"
            return st

        min_v = float(getattr(s, "confluence_min_velocity_bps", 3.0))
        max_v = float(getattr(s, "confluence_max_velocity_bps", 20.0))
        min_ofi = float(getattr(s, "confluence_min_ofi_usdt", 3000))
        min_imb = float(getattr(s, "confluence_min_imbalance_log", 0.5))

        # Strength
        if abs(fv) < min_v:
            st.blocked_reason = f"flat_velocity={fv:.2f}"
            return st
        if abs(fv) > max_v:
            st.blocked_reason = f"crash_velocity={fv:.2f}"
            return st
        if abs(ofi) < min_ofi:
            st.blocked_reason = f"low_ofi={ofi:+.0f}"
            return st
        if abs(imb) < min_imb:
            st.blocked_reason = f"flat_book={imb:+.2f}"
            return st

        # All three must point the SAME way
        sig_v = 1 if fv > 0 else -1
        sig_o = 1 if ofi > 0 else -1
        sig_i = 1 if imb > 0 else -1
        if not (sig_v == sig_o == sig_i):
            st.blocked_reason = f"mixed signals v={sig_v} o={sig_o} i={sig_i}"
            return st

        side = "LONG" if sig_v > 0 else "SHORT"

        depth = st.mexc_book_top10_notional or 0.0
        if depth < float(s.min_book_depth_usdt):
            st.blocked_reason = f"thin_book={depth:.0f}"
            return st

        # Score: product of normalized strengths × liquidity
        v_norm = abs(fv) / max(1.0, min_v)
        o_norm = abs(ofi) / max(1.0, min_ofi)
        i_norm = abs(imb) / max(1e-6, min_imb)
        liq_factor = min(1.0, depth / max(1.0, float(s.min_book_depth_usdt) * 5.0))
        st.score = (v_norm * o_norm * i_norm) ** (1.0 / 3.0) * liq_factor
        st.side_hint = side
        return st

    # ------------------------ bb_revert (Bollinger revert on MEXC own price) ------------------------
    def _eval_bb_revert(self, symbol: str, st: SymbolStats) -> SymbolStats:
        """Classic textbook mean reversion on MEXC's OWN price:
            - Track MEXC mid over 60s, compute mean and stdev
            - If current > mean + 2σ → SHORT (revert to mean)
            - If current < mean - 2σ → LONG (revert to mean)
        Independent of Binance lag — pure single-venue microstructure.
        """
        s = self.cfg.strategy

        z = st.mexc_mid_z_60s
        if z is None:
            st.blocked_reason = "warming_up_bb"
            return st

        z_entry = float(getattr(s, "bb_revert_z_entry", 2.0))
        z_max = float(getattr(s, "bb_revert_z_max", 4.0))

        if abs(z) < z_entry:
            st.blocked_reason = f"low_bb_z={z:.2f}"
            return st
        if abs(z) > z_max:
            st.blocked_reason = f"extreme_bb_z={z:.2f}"
            return st

        # Fade the deviation: z>0 (price high) → SHORT, z<0 (price low) → LONG
        side = "SHORT" if z > 0 else "LONG"

        # Don't fight a strong Binance trend in the SAME direction as our deviation
        # (e.g., MEXC is high AND Binance is racing higher → fade is suicide).
        fv30 = st.fair_velocity_30s_bps or 0.0
        if (z > 0 and fv30 > 5.0) or (z < 0 and fv30 < -5.0):
            st.blocked_reason = f"trend_against fv30={fv30:.2f}"
            return st

        depth = st.mexc_book_top10_notional or 0.0
        if depth < float(s.min_book_depth_usdt):
            st.blocked_reason = f"thin_book={depth:.0f}"
            return st

        st.score = abs(z) / max(0.01, z_entry)
        st.side_hint = side
        return st

    def make_opportunity(self, symbol: str, st: SymbolStats) -> Optional[Opportunity]:
        if not st.side_hint or st.score <= 0 or st.blocked_reason:
            return None
        return Opportunity(
            symbol=symbol,
            side=st.side_hint,
            score=st.score,
            entry_price=st.mexc_mid or 0.0,
            fair=st.fair or 0.0,
            sigma=st.sigma_spread or 0.0,
            z=st.z_score or 0.0,
        )

    def rank(self, stats: Dict[str, SymbolStats]) -> List[Dict[str, object]]:
        out = []
        for sym, st in stats.items():
            out.append({
                "symbol": sym,
                "side": st.side_hint,
                "score": st.score,
                "z": st.z_score,
                "spread_bps": st.spread_bps,
                "fair": st.fair,
                "mexc": st.mexc_mid,
                "depth": st.mexc_book_top10_notional,
                "blocked": st.blocked_reason,
            })
        out.sort(key=lambda x: (x["score"] if x["side"] else -1), reverse=True)
        return out
