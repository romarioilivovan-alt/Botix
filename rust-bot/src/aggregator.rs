use crate::config::AppConfig;
use crate::state::{OrderBook, SymbolStats, now_ts};
use std::collections::{HashMap, VecDeque};

const STOCK_SYMBOLS: &[&str] = &[
    "NVIDIA_USDT",
    "MSTRSTOCK_USDT",
    "TSLA_USDT",
    "INTC_USDT",
    "NVDA_USDT",
    "MSTR_USDT",
];

fn book_imbalance_log(book: &OrderBook, levels: usize, contract_size: f64) -> Option<f64> {
    let cs = contract_size.max(1e-18);
    let bid_n: f64 = book
        .bids
        .iter()
        .take(levels)
        .map(|lvl| (lvl[0] * lvl[1] * cs).max(0.0))
        .sum();
    let ask_n: f64 = book
        .asks
        .iter()
        .take(levels)
        .map(|lvl| (lvl[0] * lvl[1] * cs).max(0.0))
        .sum();
    (bid_n > 0.0 && ask_n > 0.0).then_some((bid_n / ask_n).ln())
}

fn max_burst_in_window(
    samples: &VecDeque<(f64, f64)>,
    now: f64,
    window_sec: f64,
    bucket_sec: f64,
) -> Option<f64> {
    let cutoff = now - window_sec;
    let mut relevant: Vec<(f64, f64)> = samples
        .iter()
        .copied()
        .filter(|(t, _)| *t >= cutoff)
        .collect();
    if relevant.is_empty() {
        return None;
    }
    relevant.sort_by(|a, b| a.0.total_cmp(&b.0));
    let mut best: f64 = 0.0;
    let mut bucket = 0.0;
    let mut i = 0;
    for j in 0..relevant.len() {
        bucket += relevant[j].1;
        while i < j && relevant[j].0 - relevant[i].0 > bucket_sec {
            bucket -= relevant[i].1;
            i += 1;
        }
        if bucket.abs() > best.abs() {
            best = bucket;
        }
    }
    Some(best)
}

#[derive(Debug, Clone, Default)]
struct SymbolAgg {
    mexc_book: OrderBook,
    binance_book: OrderBook,
    spread_samples: VecDeque<(f64, f64)>,
    fair_samples: VecDeque<(f64, f64)>,
    mexc_mid_samples: VecDeque<(f64, f64)>,
    trade_samples: VecDeque<(f64, f64)>,
    mexc_spread_samples: VecDeque<(f64, f64)>,
}

pub struct Aggregator {
    cfg: AppConfig,
    symbols: HashMap<String, SymbolAgg>,
    binance_to_mexc: HashMap<String, String>,
    price_factors: HashMap<String, f64>,
    contract_sizes: HashMap<String, f64>,
}

impl Aggregator {
    pub fn new(cfg: AppConfig) -> Self {
        Self {
            cfg,
            symbols: HashMap::new(),
            binance_to_mexc: HashMap::new(),
            price_factors: HashMap::new(),
            contract_sizes: HashMap::new(),
        }
    }

    pub fn update_config(&mut self, cfg: AppConfig) {
        self.cfg = cfg;
    }

    pub fn configure_symbols(
        &mut self,
        mexc_to_binance: &HashMap<String, Option<String>>,
        price_factors: &HashMap<String, f64>,
        contract_sizes: &HashMap<String, f64>,
    ) {
        self.binance_to_mexc = mexc_to_binance
            .iter()
            .filter_map(|(m, b)| b.as_ref().map(|b| (b.clone(), m.clone())))
            .collect();
        self.price_factors = price_factors.clone();
        self.contract_sizes = contract_sizes.clone();
        for mexc in mexc_to_binance.keys() {
            self.symbols.entry(mexc.clone()).or_default();
        }
        self.symbols.retain(|k, _| mexc_to_binance.contains_key(k));
    }

    pub fn symbols(&self) -> Vec<String> {
        self.symbols.keys().cloned().collect()
    }

    pub fn get_book(&self, mexc_symbol: &str) -> Option<OrderBook> {
        self.symbols.get(mexc_symbol).map(|a| a.mexc_book.clone())
    }

    pub fn contract_size_for(&self, mexc_symbol: &str) -> f64 {
        self.contract_sizes
            .get(mexc_symbol)
            .copied()
            .unwrap_or(1.0)
            .max(1e-18)
    }

    fn push_capped(buf: &mut VecDeque<(f64, f64)>, item: (f64, f64), cap: usize) {
        if buf.len() >= cap {
            buf.pop_front();
        }
        buf.push_back(item);
    }

    pub fn on_binance_depth(
        &mut self,
        binance_symbol: &str,
        bids: Vec<[f64; 2]>,
        asks: Vec<[f64; 2]>,
        ts: f64,
    ) {
        let Some(mexc) = self
            .binance_to_mexc
            .get(&binance_symbol.to_ascii_uppercase())
            .cloned()
        else {
            return;
        };
        let factor = self.price_factors.get(&mexc).copied().unwrap_or(1.0);
        let Some(agg) = self.symbols.get_mut(&mexc) else {
            return;
        };
        let b = bids
            .into_iter()
            .filter(|x| x[1] > 0.0)
            .take(20)
            .map(|x| [x[0] * factor, x[1]])
            .collect();
        let a = asks
            .into_iter()
            .filter(|x| x[1] > 0.0)
            .take(20)
            .map(|x| [x[0] * factor, x[1]])
            .collect();
        agg.binance_book = OrderBook {
            bids: b,
            asks: a,
            ts,
        };
        if let Some(mid) = agg.binance_book.mid() {
            Self::push_capped(&mut agg.fair_samples, (ts, mid), 400);
            self.update_spread_sample(&mexc, ts);
        }
    }

    pub fn on_binance_trade(
        &mut self,
        binance_symbol: &str,
        price: f64,
        qty: f64,
        buyer_is_maker: bool,
        ts: f64,
    ) {
        let Some(mexc) = self
            .binance_to_mexc
            .get(&binance_symbol.to_ascii_uppercase())
        else {
            return;
        };
        let Some(agg) = self.symbols.get_mut(mexc) else {
            return;
        };
        let sign = if buyer_is_maker { -1.0 } else { 1.0 };
        Self::push_capped(&mut agg.trade_samples, (ts, sign * price * qty), 2_000);
    }

    pub fn on_mexc_depth(
        &mut self,
        mexc_symbol: &str,
        bids: Vec<[f64; 2]>,
        asks: Vec<[f64; 2]>,
        ts: f64,
    ) {
        let key = mexc_symbol.to_ascii_uppercase();
        let mut b: Vec<[f64; 2]> = bids
            .into_iter()
            .filter(|x| x[0] > 0.0 && x[1] > 0.0)
            .collect();
        let mut a: Vec<[f64; 2]> = asks
            .into_iter()
            .filter(|x| x[0] > 0.0 && x[1] > 0.0)
            .collect();
        b.sort_by(|x, y| y[0].total_cmp(&x[0]));
        a.sort_by(|x, y| x[0].total_cmp(&y[0]));
        b.truncate(50);
        a.truncate(50);
        {
            let Some(agg) = self.symbols.get_mut(&key) else {
                return;
            };
            agg.mexc_book = OrderBook {
                bids: b,
                asks: a,
                ts,
            };
            if let Some(mid) = agg.mexc_book.mid().filter(|m| *m > 0.0) {
                Self::push_capped(&mut agg.mexc_mid_samples, (ts, mid), 2_000);
            }
        }
        self.update_spread_sample(&key, ts);
        if let Some(agg) = self.symbols.get_mut(&key) {
            let Some(bb) = agg.mexc_book.best_bid() else {
                return;
            };
            let Some(ba) = agg.mexc_book.best_ask() else {
                return;
            };
            if ba > bb && bb > 0.0 {
                let mid = (bb + ba) / 2.0;
                Self::push_capped(
                    &mut agg.mexc_spread_samples,
                    (ts, (ba - bb) / mid * 1e4),
                    400,
                );
            }
        }
    }

    fn update_spread_sample(&mut self, mexc_symbol: &str, ts: f64) {
        let Some(agg) = self.symbols.get_mut(mexc_symbol) else {
            return;
        };
        let Some(m_mid) = agg.mexc_book.mid() else {
            return;
        };
        let Some(b_mid) = agg.binance_book.mid() else {
            return;
        };
        Self::push_capped(&mut agg.spread_samples, (ts, m_mid - b_mid), 4_000);
    }

    pub fn compute_stats(&self, mexc_symbol: &str) -> SymbolStats {
        let Some(agg) = self.symbols.get(mexc_symbol) else {
            return SymbolStats::default();
        };
        let contract_size = self.contract_size_for(mexc_symbol);
        let now = now_ts();
        let mut st = SymbolStats {
            last_update_ts: now,
            mexc_book_age_ms: (agg.mexc_book.ts > 0.0)
                .then_some(((now - agg.mexc_book.ts) * 1000.0).max(0.0)),
            binance_book_age_ms: (agg.binance_book.ts > 0.0)
                .then_some(((now - agg.binance_book.ts) * 1000.0).max(0.0)),
            mexc_mid: agg.mexc_book.mid(),
            ..SymbolStats::default()
        };
        let is_stock = STOCK_SYMBOLS.contains(&mexc_symbol);
        st.fair = if is_stock && agg.binance_book.mid().is_none() {
            st.mexc_mid
        } else {
            agg.binance_book.mid()
        };
        let (Some(fair), Some(mexc_mid)) = (st.fair, st.mexc_mid) else {
            return st;
        };

        let spread = if is_stock && agg.binance_book.mid().is_none() {
            0.0
        } else {
            mexc_mid - fair
        };
        st.spread = Some(spread);
        st.spread_bps = (fair > 0.0).then_some(spread / fair * 1e4);

        let win = self.cfg.strategy.sigma_spread_window_sec;
        let cutoff = now - win;
        let samples: Vec<f64> = agg
            .spread_samples
            .iter()
            .filter_map(|(t, v)| (*t >= cutoff).then_some(*v))
            .collect();
        if samples.len() >= self.cfg.strategy.min_spread_samples {
            let mean = samples.iter().sum::<f64>() / samples.len() as f64;
            let var =
                samples.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / samples.len() as f64;
            let sigma = var.max(0.0).sqrt();
            st.sigma_spread = Some(sigma);
            let min_sigma = fair * (self.cfg.strategy.min_sigma_bps / 1e4);
            if sigma > 0.0 && sigma >= min_sigma {
                st.z_score = Some(spread / sigma);
            }
        }

        let ofi_cut = now - self.cfg.strategy.ofi_window_sec;
        st.ofi = Some(
            agg.trade_samples
                .iter()
                .filter(|(t, _)| *t >= ofi_cut)
                .map(|(_, v)| *v)
                .sum(),
        );

        self.set_velocity(
            &mut st,
            agg,
            now,
            self.cfg.strategy.fair_velocity_window_sec,
            "1s",
        );
        self.set_velocity(&mut st, agg, now, 5.0, "5s");
        self.set_velocity(&mut st, agg, now, 30.0, "30s");

        st.mexc_book_top10_notional = Some(agg.mexc_book.top_notional(10, contract_size));
        st.mexc_book_imbalance = book_imbalance_log(&agg.mexc_book, 5, contract_size);
        let micro_levels = 3;
        st.long_path_hole_points = Some(agg.mexc_book.path_hole_points("LONG", micro_levels, 0.0));
        st.short_path_hole_points =
            Some(agg.mexc_book.path_hole_points("SHORT", micro_levels, 0.0));
        st.long_path_shape = Some(agg.mexc_book.level_shape_ratio("LONG", micro_levels));
        st.short_path_shape = Some(agg.mexc_book.level_shape_ratio("SHORT", micro_levels));
        st.long_support_ratio = Some(agg.mexc_book.support_ratio("LONG", micro_levels));
        st.short_support_ratio = Some(agg.mexc_book.support_ratio("SHORT", micro_levels));
        st.long_support_shape = Some(agg.mexc_book.level_shape_ratio("SHORT", micro_levels));
        st.short_support_shape = Some(agg.mexc_book.level_shape_ratio("LONG", micro_levels));
        st.long_back_hole_points = Some(agg.mexc_book.path_hole_points("SHORT", micro_levels, 0.0));
        st.short_back_hole_points = Some(agg.mexc_book.path_hole_points("LONG", micro_levels, 0.0));

        if let (Some(bb), Some(ba)) = (agg.mexc_book.best_bid(), agg.mexc_book.best_ask()) {
            if ba > bb && bb > 0.0 {
                let mid = (bb + ba) / 2.0;
                st.mexc_spread_bps = Some((ba - bb) / mid * 1e4);
                let avg_cut = now - 30.0;
                let vals: Vec<f64> = agg
                    .mexc_spread_samples
                    .iter()
                    .filter_map(|(t, v)| (*t >= avg_cut).then_some(*v))
                    .collect();
                if !vals.is_empty() {
                    st.mexc_spread_bps_avg = Some(vals.iter().sum::<f64>() / vals.len() as f64);
                }
            }
        }

        st.binance_burst_usdt_1s = max_burst_in_window(&agg.trade_samples, now, 5.0, 1.0);

        let bb_cut = now - 60.0;
        let bb_samples: Vec<f64> = agg
            .mexc_mid_samples
            .iter()
            .filter_map(|(t, p)| (*t >= bb_cut).then_some(*p))
            .collect();
        if bb_samples.len() >= 30 {
            let mean = bb_samples.iter().sum::<f64>() / bb_samples.len() as f64;
            let var = bb_samples.iter().map(|x| (x - mean).powi(2)).sum::<f64>()
                / bb_samples.len() as f64;
            let std = var.max(0.0).sqrt();
            st.mexc_mid_mean_60s = Some(mean);
            st.mexc_mid_std_60s = Some(std);
            if std > 0.0 {
                st.mexc_mid_z_60s = Some((mexc_mid - mean) / std);
            }
        }
        st
    }

    fn set_velocity(
        &self,
        st: &mut SymbolStats,
        agg: &SymbolAgg,
        now: f64,
        win: f64,
        target: &str,
    ) {
        let cut = now - win;
        let samples: Vec<(f64, f64)> = agg
            .fair_samples
            .iter()
            .copied()
            .filter(|(t, _)| *t >= cut)
            .collect();
        if samples.len() < 2 {
            return;
        }
        let (t0, p0) = samples[0];
        let (t1, p1) = samples[samples.len() - 1];
        if p0 <= 0.0 {
            return;
        }
        let dt = (t1 - t0).max(1e-3);
        let v = (p1 - p0) / p0 * 1e4 / dt;
        match target {
            "1s" => st.fair_velocity_bps_per_sec = Some(v),
            "5s" => st.fair_velocity_5s_bps = Some(v),
            "30s" => st.fair_velocity_30s_bps = Some(v),
            _ => {}
        }
    }

    pub fn cleanup_old_samples(&mut self) {
        let now = now_ts();
        let spread_cut = now
            - self
                .cfg
                .strategy
                .sigma_spread_window_sec
                .mul_add(2.0, 0.0)
                .max(60.0);
        let ofi_cut = now - self.cfg.strategy.ofi_window_sec.mul_add(4.0, 0.0).max(5.0);
        let fair_cut = now
            - self
                .cfg
                .strategy
                .fair_velocity_window_sec
                .mul_add(4.0, 0.0)
                .max(5.0);
        let mexc_spread_cut = now - 60.0;
        let mexc_mid_cut = now - 90.0;
        for agg in self.symbols.values_mut() {
            while agg
                .spread_samples
                .front()
                .is_some_and(|(t, _)| *t < spread_cut)
            {
                agg.spread_samples.pop_front();
            }
            while agg.trade_samples.front().is_some_and(|(t, _)| *t < ofi_cut) {
                agg.trade_samples.pop_front();
            }
            while agg.fair_samples.front().is_some_and(|(t, _)| *t < fair_cut) {
                agg.fair_samples.pop_front();
            }
            while agg
                .mexc_spread_samples
                .front()
                .is_some_and(|(t, _)| *t < mexc_spread_cut)
            {
                agg.mexc_spread_samples.pop_front();
            }
            while agg
                .mexc_mid_samples
                .front()
                .is_some_and(|(t, _)| *t < mexc_mid_cut)
            {
                agg.mexc_mid_samples.pop_front();
            }
        }
    }
}
