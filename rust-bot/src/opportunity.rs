use crate::config::{AppConfig, SymbolOverride};
use crate::state::{SymbolStats, now_ts};
use serde::{Deserialize, Serialize};
use serde_json::json;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Opportunity {
    pub symbol: String,
    pub side: String,
    pub score: f64,
    pub entry_price: f64,
    pub fair: f64,
    pub sigma: f64,
    pub z: f64,
    pub algorithm: String,
    pub signal_ts: f64,
}

#[derive(Debug, Clone)]
pub struct OpportunityEngine {
    cfg: AppConfig,
}

impl OpportunityEngine {
    pub fn new(cfg: AppConfig) -> Self {
        Self { cfg }
    }

    pub fn update_config(&mut self, cfg: AppConfig) {
        self.cfg = cfg;
    }

    fn metric_for_side(st: &SymbolStats, side: &str, suffix: &str) -> Option<f64> {
        let long = side.eq_ignore_ascii_case("LONG");
        match (long, suffix) {
            (true, "path_hole_points") => st.long_path_hole_points,
            (false, "path_hole_points") => st.short_path_hole_points,
            (true, "support_ratio") => st.long_support_ratio,
            (false, "support_ratio") => st.short_support_ratio,
            (true, "support_shape") => st.long_support_shape,
            (false, "support_shape") => st.short_support_shape,
            (true, "path_shape") => st.long_path_shape,
            (false, "path_shape") => st.short_path_shape,
            (true, "back_hole_points") => st.long_back_hole_points,
            (false, "back_hole_points") => st.short_back_hole_points,
            _ => None,
        }
    }

    fn micro_i64(&self, ov: Option<&SymbolOverride>, name: &str, default: i64) -> i64 {
        match name {
            "micro_levels" => ov
                .and_then(|o| o.micro_levels)
                .unwrap_or(self.cfg.strategy.micro_levels),
            _ => default,
        }
    }

    fn micro_f64(&self, ov: Option<&SymbolOverride>, name: &str, default: f64) -> f64 {
        match name {
            "micro_path_hole_max_points" => ov
                .and_then(|o| o.micro_path_hole_max_points)
                .unwrap_or(self.cfg.strategy.micro_path_hole_max_points),
            "micro_support_ratio_min" => ov
                .and_then(|o| o.micro_support_ratio_min)
                .unwrap_or(self.cfg.strategy.micro_support_ratio_min),
            "micro_support_shape_min" => ov
                .and_then(|o| o.micro_support_shape_min)
                .unwrap_or(self.cfg.strategy.micro_support_shape_min),
            "micro_path_shape_min" => ov
                .and_then(|o| o.micro_path_shape_min)
                .unwrap_or(self.cfg.strategy.micro_path_shape_min),
            "micro_back_hole_max_points" => ov
                .and_then(|o| o.micro_back_hole_max_points)
                .unwrap_or(self.cfg.strategy.micro_back_hole_max_points),
            _ => default,
        }
    }

    fn apply_symbol_override_filters(&self, st: &mut SymbolStats, ov: Option<&SymbolOverride>) {
        if st.side_hint.is_none() || st.blocked_reason.is_some() {
            return;
        }
        let side = st.side_hint.clone().unwrap_or_default();
        let spread_bps = st.spread_bps;
        if let Some(ov) = ov {
            if side == "LONG" && ov.allow_long == Some(false) {
                Self::block(st, "long_disabled");
                return;
            }
            if side == "SHORT" && ov.allow_short == Some(false) {
                Self::block(st, "short_disabled");
                return;
            }
            let min_abs = ov.min_abs_spread_bps.unwrap_or(0.0);
            if min_abs > 0.0 && spread_bps.is_some_and(|s| s.abs() < min_abs) {
                Self::block(
                    st,
                    format!("tiny_spread={:.2}bps", spread_bps.unwrap_or(0.0)),
                );
                return;
            }
            let min_lag = ov.min_lag_bps.unwrap_or(0.0);
            let max_chase = ov.max_chase_bps.unwrap_or(0.0);
            if let Some(spread) = spread_bps {
                if side == "LONG" {
                    if spread > max_chase {
                        Self::block(st, format!("chasing_long={spread:.2}bps"));
                        return;
                    }
                    if min_lag > 0.0 && spread > -min_lag {
                        Self::block(st, format!("tiny_lag_long={spread:.2}bps"));
                        return;
                    }
                } else {
                    if spread < -max_chase {
                        Self::block(st, format!("chasing_short={spread:.2}bps"));
                        return;
                    }
                    if min_lag > 0.0 && spread < min_lag {
                        Self::block(st, format!("tiny_lag_short={spread:.2}bps"));
                        return;
                    }
                }
            }
            let anti = ov.anti_fade_30s_bps.unwrap_or(0.0);
            if anti > 0.0 {
                if let Some(fv30) = st.fair_velocity_30s_bps {
                    if side == "LONG" && fv30 < -anti {
                        Self::block(st, format!("anti_fade_30s={fv30:.2}"));
                        return;
                    }
                    if side == "SHORT" && fv30 > anti {
                        Self::block(st, format!("anti_fade_30s={fv30:.2}"));
                        return;
                    }
                }
            }
        }

        let micro_levels = self.micro_i64(ov, "micro_levels", 0);
        if micro_levels <= 0 {
            return;
        }
        let path_hole_max = self.micro_f64(ov, "micro_path_hole_max_points", 0.0);
        let support_ratio_min = self.micro_f64(ov, "micro_support_ratio_min", 0.0);
        let support_shape_min = self.micro_f64(ov, "micro_support_shape_min", 0.0);
        let path_shape_min = self.micro_f64(ov, "micro_path_shape_min", 0.0);
        let back_hole_max = self.micro_f64(ov, "micro_back_hole_max_points", 0.0);
        if [
            path_hole_max,
            support_ratio_min,
            support_shape_min,
            path_shape_min,
            back_hole_max,
        ]
        .iter()
        .all(|v| *v <= 0.0)
        {
            return;
        }
        let path_hole = Self::metric_for_side(st, &side, "path_hole_points");
        let support_ratio = Self::metric_for_side(st, &side, "support_ratio");
        let support_shape = Self::metric_for_side(st, &side, "support_shape");
        let path_shape = Self::metric_for_side(st, &side, "path_shape");
        let back_hole = Self::metric_for_side(st, &side, "back_hole_points");
        if path_hole_max > 0.0 && path_hole.is_some_and(|v| v > path_hole_max) {
            Self::block(st, format!("micro_path_hole={:.2}", path_hole.unwrap()));
        } else if support_ratio_min > 0.0 && support_ratio.is_some_and(|v| v < support_ratio_min) {
            Self::block(
                st,
                format!("micro_support_ratio={:.2}", support_ratio.unwrap()),
            );
        } else if support_shape_min > 0.0 && support_shape.is_some_and(|v| v < support_shape_min) {
            Self::block(
                st,
                format!("micro_support_shape={:.2}", support_shape.unwrap()),
            );
        } else if path_shape_min > 0.0 && path_shape.is_some_and(|v| v < path_shape_min) {
            Self::block(st, format!("micro_path_shape={:.2}", path_shape.unwrap()));
        } else if back_hole_max > 0.0 && back_hole.is_some_and(|v| v > back_hole_max) {
            Self::block(st, format!("micro_back_hole={:.2}", back_hole.unwrap()));
        }
    }

    fn block(st: &mut SymbolStats, reason: impl Into<String>) {
        st.score = 0.0;
        st.side_hint = None;
        st.blocked_reason = Some(reason.into());
    }

    pub fn evaluate(&self, symbol: &str, st: &mut SymbolStats) {
        st.score = 0.0;
        st.side_hint = None;
        st.blocked_reason = None;
        st.selected_algorithm = None;
        if st.fair.is_none() || st.mexc_mid.is_none() {
            st.blocked_reason = Some("no_books".to_string());
            return;
        }
        let algo = self.cfg.strategy.algorithm.to_ascii_lowercase();
        st.selected_algorithm = Some(algo.clone());
        self.evaluate_named(symbol, st, &algo);
        if self.cfg.strategy.invert && st.side_hint.is_some() {
            st.side_hint = Some(if st.side_hint.as_deref() == Some("LONG") {
                "SHORT".to_string()
            } else {
                "LONG".to_string()
            });
        }
        self.apply_symbol_override_filters(st, None);
    }

    fn evaluate_single(&self, symbol: &str, st: &SymbolStats, algo: &str) -> SymbolStats {
        let mut s = st.clone();
        s.score = 0.0;
        s.side_hint = None;
        s.blocked_reason = None;
        s.selected_algorithm = Some(algo.to_ascii_lowercase());
        if s.fair.is_none() || s.mexc_mid.is_none() {
            s.blocked_reason = Some("no_books".to_string());
            return s;
        }
        self.evaluate_named(symbol, &mut s, &algo.to_ascii_lowercase());
        if self.cfg.strategy.invert && s.side_hint.is_some() {
            s.side_hint = Some(if s.side_hint.as_deref() == Some("LONG") {
                "SHORT".to_string()
            } else {
                "LONG".to_string()
            });
        }
        s
    }

    pub fn evaluate_multi(&self, symbol: &str, st: &mut SymbolStats, ov: &SymbolOverride) {
        let Some(algos) = ov.algorithms.as_ref().filter(|a| !a.is_empty()) else {
            self.evaluate(symbol, st);
            return;
        };
        let mode = ov
            .algo_mode
            .as_deref()
            .unwrap_or("ANY")
            .to_ascii_uppercase();
        let mut results = Vec::new();
        let mut signals = Vec::new();
        for algo in algos {
            let res = self.evaluate_single(symbol, st, algo);
            if res.score > 0.0 && res.side_hint.is_some() && res.blocked_reason.is_none() {
                signals.push(res.clone());
            }
            results.push(res);
        }
        if signals.is_empty() {
            let best = results.iter().max_by(|a, b| a.score.total_cmp(&b.score));
            st.score = 0.0;
            st.side_hint = None;
            st.blocked_reason = Some(
                best.and_then(|b| b.blocked_reason.clone())
                    .unwrap_or_else(|| "no_algo_fired".to_string()),
            );
            st.selected_algorithm = best.and_then(|b| b.selected_algorithm.clone());
            return;
        }
        if mode == "CONSENSUS" {
            let side = signals[0].side_hint.clone().unwrap();
            if signals
                .iter()
                .any(|s| s.side_hint.as_deref() != Some(&side))
            {
                Self::block(st, "consensus_conflict");
                st.selected_algorithm = None;
                return;
            }
            let product = signals.iter().map(|s| s.score.max(1e-12)).product::<f64>();
            let geo = product.powf(1.0 / signals.len() as f64);
            let best = signals
                .iter()
                .max_by(|a, b| a.score.total_cmp(&b.score))
                .unwrap();
            st.score = geo;
            st.side_hint = Some(side);
            st.blocked_reason = None;
            st.selected_algorithm = best.selected_algorithm.clone();
            self.apply_symbol_override_filters(st, Some(ov));
            return;
        }
        let best = signals
            .iter()
            .max_by(|a, b| a.score.total_cmp(&b.score))
            .unwrap();
        if mode == "BEST" && best.score < 1.2 {
            Self::block(st, "best_below_threshold");
            st.selected_algorithm = None;
            return;
        }
        st.score = best.score;
        st.side_hint = best.side_hint.clone();
        st.blocked_reason = None;
        st.z_score = best.z_score;
        st.sigma_spread = best.sigma_spread;
        st.selected_algorithm = best.selected_algorithm.clone();
        self.apply_symbol_override_filters(st, Some(ov));
    }

    fn evaluate_named(&self, symbol: &str, st: &mut SymbolStats, algo: &str) {
        match algo {
            "momentum" => self.eval_momentum(st),
            "ofi" => self.eval_ofi(st),
            "imbalance" => self.eval_imbalance(st),
            "sweep" => self.eval_sweep(st),
            "wide_spread" => self.eval_wide_spread(st),
            "raw_momentum" => self.eval_raw_momentum(st),
            "book_lean" => self.eval_book_lean(st),
            "confluence" => self.eval_confluence(st),
            "bb_revert" => self.eval_bb_revert(st),
            _ => self.eval_meanrev(symbol, st),
        }
    }

    fn depth_gate(&self, st: &mut SymbolStats) -> Option<f64> {
        let depth = st.mexc_book_top10_notional.unwrap_or(0.0);
        if depth < self.cfg.strategy.min_book_depth_usdt {
            st.blocked_reason = Some(format!("thin_book={depth:.0}"));
            return None;
        }
        Some((depth / (self.cfg.strategy.min_book_depth_usdt * 5.0).max(1.0)).min(1.0))
    }

    fn eval_meanrev(&self, _symbol: &str, st: &mut SymbolStats) {
        let s = &self.cfg.strategy;
        let Some(sigma) = st.sigma_spread.filter(|v| *v > 0.0) else {
            st.blocked_reason = Some("warming_up".to_string());
            return;
        };
        let Some(z) = st.z_score else {
            st.blocked_reason = Some("no_z".to_string());
            return;
        };
        let side = if z > 0.0 { "SHORT" } else { "LONG" };
        if s.meanrev_min_spread_bps > 0.0
            && st
                .spread_bps
                .is_some_and(|v| v.abs() < s.meanrev_min_spread_bps)
        {
            st.blocked_reason = Some(format!(
                "tiny_spread={:.2}bps",
                st.spread_bps.unwrap_or(0.0)
            ));
            return;
        }
        if z.abs() < s.entry_z {
            st.blocked_reason = Some("low_z".to_string());
            return;
        }
        if s.max_entry_z > 0.0 && z.abs() > s.max_entry_z {
            st.blocked_reason = Some(format!("extreme_z={z:.1}"));
            return;
        }
        let fv = st.fair_velocity_bps_per_sec.unwrap_or(0.0);
        if fv.abs() > s.max_fair_velocity_bps_per_sec
            && ((z > 0.0 && fv > 0.0) || (z < 0.0 && fv < 0.0))
        {
            st.blocked_reason = Some(format!("fast_fair_vel={fv:.1}bps/s"));
            return;
        }
        if s.require_ofi_alignment {
            let ofi = st.ofi.unwrap_or(0.0);
            let min_ofi = s.ofi_min_usdt;
            if z > 0.0 && ofi > 0.0 && ofi.abs() > min_ofi {
                st.blocked_reason = Some(format!("ofi_with_dev={ofi:+.0}"));
                return;
            }
            if z < 0.0 && ofi < 0.0 && ofi.abs() > min_ofi {
                st.blocked_reason = Some(format!("ofi_with_dev={ofi:+.0}"));
                return;
            }
        }
        if s.meanrev_require_book_alignment {
            if let Some(imb) = st.mexc_book_imbalance {
                if side == "LONG" && imb < -s.meanrev_min_imbalance_log {
                    st.blocked_reason = Some(format!("book_against={imb:+.2}"));
                    return;
                }
                if side == "SHORT" && imb > s.meanrev_min_imbalance_log {
                    st.blocked_reason = Some(format!("book_against={imb:+.2}"));
                    return;
                }
            }
        }
        let Some(liq) = self.depth_gate(st) else {
            return;
        };
        let regime = if fv.abs() > 0.0 {
            (1.0 - fv.abs() / (s.max_fair_velocity_bps_per_sec * 2.0).max(1.0)).max(0.5)
        } else {
            1.0
        };
        st.score = (z.abs() * liq * regime).max(0.0);
        st.side_hint = Some(side.to_string());
        st.sigma_spread = Some(sigma);
    }

    fn eval_momentum(&self, st: &mut SymbolStats) {
        let s = &self.cfg.strategy;
        let Some(fv) = st.fair_velocity_bps_per_sec else {
            st.blocked_reason = Some("no_velocity".to_string());
            return;
        };
        if fv.abs() < s.momentum_min_velocity_bps_per_sec {
            st.blocked_reason = Some(format!("flat_fair={fv:.2}bps/s"));
            return;
        }
        if fv.abs() > s.momentum_max_velocity_bps_per_sec {
            st.blocked_reason = Some(format!("crash_fair={fv:.2}bps/s"));
            return;
        }
        let side = if fv > 0.0 { "LONG" } else { "SHORT" };
        if s.momentum_require_lag {
            if let Some(spread) = st.spread {
                if side == "LONG" && spread > 0.0 {
                    st.blocked_reason = Some("no_lag_long".to_string());
                    return;
                }
                if side == "SHORT" && spread < 0.0 {
                    st.blocked_reason = Some("no_lag_short".to_string());
                    return;
                }
            }
        }
        let Some(liq) = self.depth_gate(st) else {
            return;
        };
        let ofi = st.ofi.unwrap_or(0.0);
        if s.require_ofi_alignment {
            if side == "LONG" && ofi < 0.0 && ofi.abs() > 5_000.0 {
                st.blocked_reason = Some(format!("ofi_against={ofi:+.0}"));
                return;
            }
            if side == "SHORT" && ofi > 0.0 && ofi.abs() > 5_000.0 {
                st.blocked_reason = Some(format!("ofi_against={ofi:+.0}"));
                return;
            }
        }
        let ofi_bonus = if (side == "LONG" && ofi > 0.0) || (side == "SHORT" && ofi < 0.0) {
            1.2
        } else {
            1.0
        };
        st.score = fv.abs() / s.momentum_min_velocity_bps_per_sec.max(1.0) * liq * ofi_bonus;
        st.side_hint = Some(side.to_string());
    }

    fn eval_ofi(&self, st: &mut SymbolStats) {
        let s = &self.cfg.strategy;
        let Some(ofi) = st.ofi else {
            st.blocked_reason = Some("no_ofi".to_string());
            return;
        };
        if ofi.abs() < s.ofi_min_usdt {
            st.blocked_reason = Some(format!("low_ofi={ofi:+.0}"));
            return;
        }
        let side = if ofi > 0.0 { "LONG" } else { "SHORT" };
        if s.ofi_require_lag {
            if let Some(spread) = st.spread_bps {
                if side == "LONG" && spread > s.ofi_max_chase_bps {
                    st.blocked_reason = Some(format!("chasing_long={spread:.2}bps"));
                    return;
                }
                if side == "SHORT" && spread < -s.ofi_max_chase_bps {
                    st.blocked_reason = Some(format!("chasing_short={spread:.2}bps"));
                    return;
                }
                if side == "LONG" && spread > -s.ofi_min_lag_bps {
                    st.blocked_reason = Some(format!("tiny_lag_long={spread:.2}bps"));
                    return;
                }
                if side == "SHORT" && spread < s.ofi_min_lag_bps {
                    st.blocked_reason = Some(format!("tiny_lag_short={spread:.2}bps"));
                    return;
                }
            }
        }
        let fv = st.fair_velocity_bps_per_sec.unwrap_or(0.0);
        if fv.abs() > s.momentum_max_velocity_bps_per_sec {
            st.blocked_reason = Some(format!("crash_fair={fv:.2}bps/s"));
            return;
        }
        let imb = st.mexc_book_imbalance;
        if s.ofi_require_book_alignment {
            let Some(imb_v) = imb else {
                st.blocked_reason = Some("no_imbalance".to_string());
                return;
            };
            if imb_v.abs() < s.ofi_min_imbalance_log {
                st.blocked_reason = Some(format!("flat_book={imb_v:+.2}"));
                return;
            }
            if (side == "LONG" && imb_v <= 0.0) || (side == "SHORT" && imb_v >= 0.0) {
                st.blocked_reason = Some(format!("book_against={imb_v:+.2}"));
                return;
            }
        }
        let Some(liq) = self.depth_gate(st) else {
            return;
        };
        let flow_bonus =
            if imb.is_some_and(|v| (side == "LONG" && v > 0.0) || (side == "SHORT" && v < 0.0)) {
                1.10
            } else {
                1.0
            };
        st.score = ofi.abs() / s.ofi_min_usdt.max(1.0) * liq * flow_bonus;
        st.side_hint = Some(side.to_string());
    }

    fn eval_imbalance(&self, st: &mut SymbolStats) {
        let s = &self.cfg.strategy;
        let Some(imb) = st.mexc_book_imbalance else {
            st.blocked_reason = Some("no_imbalance".to_string());
            return;
        };
        if imb.abs() < s.imbalance_min_log {
            st.blocked_reason = Some(format!("low_imb={imb:+.2}"));
            return;
        }
        if imb.abs() > s.imbalance_max_log {
            st.blocked_reason = Some(format!("extreme_imb={imb:+.2}"));
            return;
        }
        let side = if imb > 0.0 { "LONG" } else { "SHORT" };
        if s.imbalance_require_lag {
            if let Some(spread) = st.spread_bps {
                if side == "LONG" && spread > s.imbalance_max_chase_bps {
                    st.blocked_reason = Some(format!("chasing_long={spread:.2}bps"));
                    return;
                }
                if side == "SHORT" && spread < -s.imbalance_max_chase_bps {
                    st.blocked_reason = Some(format!("chasing_short={spread:.2}bps"));
                    return;
                }
                if side == "LONG" && spread > -s.imbalance_min_lag_bps {
                    st.blocked_reason = Some(format!("tiny_lag_long={spread:.2}bps"));
                    return;
                }
                if side == "SHORT" && spread < s.imbalance_min_lag_bps {
                    st.blocked_reason = Some(format!("tiny_lag_short={spread:.2}bps"));
                    return;
                }
            }
        }
        let fv = st.fair_velocity_bps_per_sec.unwrap_or(0.0);
        if fv.abs() > s.momentum_max_velocity_bps_per_sec {
            st.blocked_reason = Some(format!("crash_fair={fv:.2}bps/s"));
            return;
        }
        if (side == "LONG" && fv < -2.0) || (side == "SHORT" && fv > 2.0) {
            st.blocked_reason = Some(format!("fv_against={fv:+.2}"));
            return;
        }
        let Some(liq) = self.depth_gate(st) else {
            return;
        };
        st.score = imb.abs() / s.imbalance_min_log.max(1e-6) * liq;
        st.side_hint = Some(side.to_string());
    }

    fn eval_sweep(&self, st: &mut SymbolStats) {
        let s = &self.cfg.strategy;
        let Some(burst) = st.binance_burst_usdt_1s else {
            st.blocked_reason = Some("no_burst_data".to_string());
            return;
        };
        if burst.abs() < s.sweep_min_usdt_1s {
            st.blocked_reason = Some(format!("no_burst={burst:+.0}"));
            return;
        }
        let fv = st.fair_velocity_bps_per_sec.unwrap_or(0.0);
        if fv.abs() > s.momentum_max_velocity_bps_per_sec {
            st.blocked_reason = Some(format!("crash_fair={fv:.2}bps/s"));
            return;
        }
        let side = if burst > 0.0 { "LONG" } else { "SHORT" };
        let Some(liq) = self.depth_gate(st) else {
            return;
        };
        st.score = burst.abs() / s.sweep_min_usdt_1s.max(1.0) * liq;
        st.side_hint = Some(side.to_string());
    }

    fn eval_wide_spread(&self, st: &mut SymbolStats) {
        let s = &self.cfg.strategy;
        let (Some(cur), Some(avg)) = (st.mexc_spread_bps, st.mexc_spread_bps_avg) else {
            st.blocked_reason = Some("no_spread_baseline".to_string());
            return;
        };
        if avg <= 0.0 {
            st.blocked_reason = Some("no_spread_baseline".to_string());
            return;
        }
        let ratio = cur / avg;
        if ratio < s.wide_spread_ratio {
            st.blocked_reason = Some(format!("narrow_spread={ratio:.2}"));
            return;
        }
        if cur < s.wide_spread_min_bps {
            st.blocked_reason = Some(format!("trivial_bps={cur:.1}"));
            return;
        }
        let Some(imb) = st.mexc_book_imbalance else {
            st.blocked_reason = Some("no_imb_for_dir".to_string());
            return;
        };
        if imb.abs() < s.wide_spread_min_imbalance {
            st.blocked_reason = Some(format!("flat_book={imb:+.2}"));
            return;
        }
        let side = if imb > 0.0 { "LONG" } else { "SHORT" };
        let fv = st.fair_velocity_bps_per_sec.unwrap_or(0.0);
        if (side == "LONG" && fv < -2.0) || (side == "SHORT" && fv > 2.0) {
            st.blocked_reason = Some(format!("fv_against={fv:+.2}"));
            return;
        }
        if self.depth_gate(st).is_none() {
            return;
        }
        st.score = ratio * (cur / 10.0) * imb.abs();
        st.side_hint = Some(side.to_string());
    }

    fn eval_raw_momentum(&self, st: &mut SymbolStats) {
        let s = &self.cfg.strategy;
        let Some(fv) = st.fair_velocity_bps_per_sec else {
            st.blocked_reason = Some("no_velocity".to_string());
            return;
        };
        if fv.abs() < s.raw_momentum_min_bps {
            st.blocked_reason = Some(format!("flat={fv:.2}bps/s"));
            return;
        }
        if fv.abs() > s.raw_momentum_max_bps {
            st.blocked_reason = Some(format!("crash={fv:.2}bps/s"));
            return;
        }
        let side = if fv > 0.0 { "LONG" } else { "SHORT" };
        let sig_v = if fv > 0.0 { 1 } else { -1 };
        if s.raw_momentum_require_lag {
            if let Some(spread) = st.spread_bps {
                if side == "LONG" && spread > s.raw_momentum_max_chase_bps {
                    st.blocked_reason = Some(format!("chasing_long={spread:.2}bps"));
                    return;
                }
                if side == "SHORT" && spread < -s.raw_momentum_max_chase_bps {
                    st.blocked_reason = Some(format!("chasing_short={spread:.2}bps"));
                    return;
                }
                if side == "LONG" && spread > -s.raw_momentum_min_lag_bps {
                    st.blocked_reason = Some(format!("tiny_lag_long={spread:.2}bps"));
                    return;
                }
                if side == "SHORT" && spread < s.raw_momentum_min_lag_bps {
                    st.blocked_reason = Some(format!("tiny_lag_short={spread:.2}bps"));
                    return;
                }
            }
        }
        if s.raw_momentum_require_5s_agree {
            if let Some(fv5) = st.fair_velocity_5s_bps {
                let sig5 = if fv5 > 0.0 {
                    1
                } else if fv5 < 0.0 {
                    -1
                } else {
                    0
                };
                if sig5 != 0 && sig5 != sig_v {
                    st.blocked_reason = Some(format!("5s_disagrees fv1={fv:.2} fv5={fv5:.2}"));
                    return;
                }
            }
        }
        if s.raw_momentum_anti_fade_30s_bps > 0.0 {
            if let Some(fv30) = st.fair_velocity_30s_bps {
                if fv30.abs() > s.raw_momentum_anti_fade_30s_bps {
                    let sig30 = if fv30 > 0.0 { 1 } else { -1 };
                    if sig30 != sig_v {
                        st.blocked_reason =
                            Some(format!("against_30s_trend fv1={fv:.2} fv30={fv30:.2}"));
                        return;
                    }
                }
            }
        }
        let ofi = st.ofi.unwrap_or(0.0);
        if s.raw_momentum_require_ofi_alignment {
            if ofi.abs() < s.raw_momentum_min_ofi_usdt {
                st.blocked_reason = Some(format!("low_ofi={ofi:+.0}"));
                return;
            }
            if (side == "LONG" && ofi <= 0.0) || (side == "SHORT" && ofi >= 0.0) {
                st.blocked_reason = Some(format!("ofi_against={ofi:+.0}"));
                return;
            }
        }
        let imb = st.mexc_book_imbalance;
        if s.raw_momentum_require_book_alignment {
            let Some(imb_v) = imb else {
                st.blocked_reason = Some("no_imbalance".to_string());
                return;
            };
            if imb_v.abs() < s.raw_momentum_min_imbalance_log {
                st.blocked_reason = Some(format!("flat_book={imb_v:+.2}"));
                return;
            }
            if (side == "LONG" && imb_v <= 0.0) || (side == "SHORT" && imb_v >= 0.0) {
                st.blocked_reason = Some(format!("book_against={imb_v:+.2}"));
                return;
            }
        }
        let Some(liq) = self.depth_gate(st) else {
            return;
        };
        let mut flow_bonus = 1.0;
        if (side == "LONG" && ofi > 0.0) || (side == "SHORT" && ofi < 0.0) {
            flow_bonus += 0.10;
        }
        if imb.is_some_and(|v| (side == "LONG" && v > 0.0) || (side == "SHORT" && v < 0.0)) {
            flow_bonus += 0.10;
        }
        st.score = fv.abs() / s.raw_momentum_min_bps.max(1.0) * liq * flow_bonus;
        st.side_hint = Some(side.to_string());
    }

    fn eval_book_lean(&self, st: &mut SymbolStats) {
        let s = &self.cfg.strategy;
        let Some(imb) = st.mexc_book_imbalance else {
            st.blocked_reason = Some("no_imbalance".to_string());
            return;
        };
        let min_ratio = s.book_lean_min_ratio.max(1.01);
        let min_log = min_ratio.ln();
        if imb.abs() < min_log {
            st.blocked_reason = Some(format!("flat_book={imb:+.2}"));
            return;
        }
        let side = if imb > 0.0 { "LONG" } else { "SHORT" };
        let fv = st.fair_velocity_bps_per_sec.unwrap_or(0.0);
        if (side == "LONG" && fv < -3.0) || (side == "SHORT" && fv > 3.0) {
            st.blocked_reason = Some(format!("fv_against={fv:+.2}"));
            return;
        }
        if self.depth_gate(st).is_none() {
            return;
        }
        st.score = imb.abs() / min_log.max(1e-6);
        st.side_hint = Some(side.to_string());
    }

    fn eval_confluence(&self, st: &mut SymbolStats) {
        let s = &self.cfg.strategy;
        let (Some(fv), Some(ofi), Some(imb)) =
            (st.fair_velocity_bps_per_sec, st.ofi, st.mexc_book_imbalance)
        else {
            st.blocked_reason = Some("missing_signal".to_string());
            return;
        };
        if s.confluence_require_lag {
            if let Some(spread) = st.spread_bps {
                if fv > 0.0 && spread > s.confluence_max_chase_bps {
                    st.blocked_reason = Some(format!("chasing_long={spread:.2}bps"));
                    return;
                }
                if fv < 0.0 && spread < -s.confluence_max_chase_bps {
                    st.blocked_reason = Some(format!("chasing_short={spread:.2}bps"));
                    return;
                }
                if fv > 0.0 && spread > -s.confluence_min_lag_bps {
                    st.blocked_reason = Some(format!("tiny_lag_long={spread:.2}bps"));
                    return;
                }
                if fv < 0.0 && spread < s.confluence_min_lag_bps {
                    st.blocked_reason = Some(format!("tiny_lag_short={spread:.2}bps"));
                    return;
                }
            }
        }
        if fv.abs() < s.confluence_min_velocity_bps {
            st.blocked_reason = Some(format!("flat_velocity={fv:.2}"));
            return;
        }
        if fv.abs() > s.confluence_max_velocity_bps {
            st.blocked_reason = Some(format!("crash_velocity={fv:.2}"));
            return;
        }
        if ofi.abs() < s.confluence_min_ofi_usdt {
            st.blocked_reason = Some(format!("low_ofi={ofi:+.0}"));
            return;
        }
        if imb.abs() < s.confluence_min_imbalance_log {
            st.blocked_reason = Some(format!("flat_book={imb:+.2}"));
            return;
        }
        let sig_v = if fv > 0.0 { 1 } else { -1 };
        let sig_o = if ofi > 0.0 { 1 } else { -1 };
        let sig_i = if imb > 0.0 { 1 } else { -1 };
        if !(sig_v == sig_o && sig_o == sig_i) {
            st.blocked_reason = Some(format!("mixed signals v={sig_v} o={sig_o} i={sig_i}"));
            return;
        }
        let side = if sig_v > 0 { "LONG" } else { "SHORT" };
        let Some(liq) = self.depth_gate(st) else {
            return;
        };
        let v_norm = fv.abs() / s.confluence_min_velocity_bps.max(1.0);
        let o_norm = ofi.abs() / s.confluence_min_ofi_usdt.max(1.0);
        let i_norm = imb.abs() / s.confluence_min_imbalance_log.max(1e-6);
        st.score = (v_norm * o_norm * i_norm).powf(1.0 / 3.0) * liq;
        st.side_hint = Some(side.to_string());
    }

    fn eval_bb_revert(&self, st: &mut SymbolStats) {
        let s = &self.cfg.strategy;
        let Some(z) = st.mexc_mid_z_60s else {
            st.blocked_reason = Some("warming_up_bb".to_string());
            return;
        };
        if z.abs() < s.bb_revert_z_entry {
            st.blocked_reason = Some(format!("low_bb_z={z:.2}"));
            return;
        }
        if z.abs() > s.bb_revert_z_max {
            st.blocked_reason = Some(format!("extreme_bb_z={z:.2}"));
            return;
        }
        let side = if z > 0.0 { "SHORT" } else { "LONG" };
        if let Some(fv30) = st.fair_velocity_30s_bps {
            if (z > 0.0 && fv30 > 5.0) || (z < 0.0 && fv30 < -5.0) {
                st.blocked_reason = Some(format!("trend_against fv30={fv30:.2}"));
                return;
            }
        }
        if self.depth_gate(st).is_none() {
            return;
        }
        st.score = z.abs() / s.bb_revert_z_entry.max(0.01);
        st.side_hint = Some(side.to_string());
    }

    pub fn make_opportunity(&self, symbol: &str, st: &SymbolStats) -> Option<Opportunity> {
        if st.score <= 0.0 || st.side_hint.is_none() || st.blocked_reason.is_some() {
            return None;
        }
        Some(Opportunity {
            symbol: symbol.to_string(),
            side: st.side_hint.clone()?,
            score: st.score,
            entry_price: st.mexc_mid.unwrap_or(0.0),
            fair: st.fair.unwrap_or(0.0),
            sigma: st.sigma_spread.unwrap_or(0.0),
            z: st.z_score.unwrap_or(0.0),
            algorithm: st
                .selected_algorithm
                .clone()
                .unwrap_or_else(|| self.cfg.strategy.algorithm.to_ascii_lowercase()),
            signal_ts: now_ts(),
        })
    }

    pub fn rank(
        &self,
        stats: &std::collections::HashMap<String, SymbolStats>,
    ) -> Vec<serde_json::Value> {
        let mut out: Vec<_> = stats
            .iter()
            .map(|(sym, st)| {
                json!({
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
            })
            .collect();
        out.sort_by(|a, b| {
            let sa = if a.get("side").and_then(|v| v.as_str()).is_some() {
                a.get("score").and_then(|v| v.as_f64()).unwrap_or(0.0)
            } else {
                -1.0
            };
            let sb = if b.get("side").and_then(|v| v.as_str()).is_some() {
                b.get("score").and_then(|v| v.as_f64()).unwrap_or(0.0)
            } else {
                -1.0
            };
            sb.total_cmp(&sa)
        });
        out
    }
}
