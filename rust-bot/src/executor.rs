use crate::aggregator::Aggregator;
use crate::allocator::CapitalAllocator;
use crate::config::{AppConfig, SymbolOverride};
use crate::mexc::{MexcClient, extract_order_id, num_as_f64};
use crate::opportunity::Opportunity;
use crate::state::{AppState, ManagedPosition, OrderBook, now_ts};
use crate::store::Store;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio::time::{Duration, sleep};

const STOCK_SYMBOLS: &[&str] = &[
    "NVIDIA_USDT",
    "MSTRSTOCK_USDT",
    "TSLA_USDT",
    "INTC_USDT",
    "NVDA_USDT",
    "MSTR_USDT",
];

#[derive(Debug, Clone)]
struct Quote {
    symbol: String,
    side: String,
    price: f64,
    qty: f64,
    contract_size: f64,
    notional: f64,
    margin: f64,
    leverage: i64,
    placed_ts: f64,
    fair_at_quote: f64,
    sigma_at_quote: f64,
    taker_open_at: Option<f64>,
    signal_ts: f64,
    entry_algo: Option<String>,
    entry_score: f64,
    spread_bps_at_quote: Option<f64>,
    fill_ratio: Option<f64>,
    levels_eaten: Option<i64>,
    real_order_id: Option<i64>,
    real_submitted: bool,
}

#[derive(Debug, Clone)]
struct CloseResolution {
    exit_price: f64,
    realized_pnl: f64,
    close_ts: f64,
    price_source: &'static str,
    position_id: Option<i64>,
}

#[derive(Clone)]
pub struct Executor {
    cfg: Arc<Mutex<AppConfig>>,
    state: Arc<AppState>,
    agg: Arc<Mutex<Aggregator>>,
    alloc: Arc<Mutex<CapitalAllocator>>,
    store: Store,
    trader: MexcClient,
    quotes: Arc<Mutex<HashMap<String, Quote>>>,
    max_lev_cache: Arc<Mutex<HashMap<String, i64>>>,
    contract_size_cache: Arc<Mutex<HashMap<String, f64>>>,
    last_real_sync_ts: Arc<Mutex<f64>>,
    last_balance_refresh_ts: Arc<Mutex<f64>>,
}

impl Executor {
    pub fn new(
        cfg: Arc<Mutex<AppConfig>>,
        state: Arc<AppState>,
        agg: Arc<Mutex<Aggregator>>,
        alloc: Arc<Mutex<CapitalAllocator>>,
        store: Store,
        trader: MexcClient,
    ) -> Self {
        Self {
            cfg,
            state,
            agg,
            alloc,
            store,
            trader,
            quotes: Arc::new(Mutex::new(HashMap::new())),
            max_lev_cache: Arc::new(Mutex::new(HashMap::new())),
            contract_size_cache: Arc::new(Mutex::new(HashMap::new())),
            last_real_sync_ts: Arc::new(Mutex::new(0.0)),
            last_balance_refresh_ts: Arc::new(Mutex::new(0.0)),
        }
    }

    pub async fn init_balance(&self) {
        let cfg = self.cfg.lock().await.clone();
        {
            let mut state = self.state.write().await;
            if cfg.mode == "real" {
                let (available, equity) = self.trader.get_usdt_balance_snapshot().await;
                state.balance = equity;
                state.available_balance = available;
                state.session_starting_balance = equity;
                state.session_peak_balance = equity;
                state.strategy_session_starting_balance = equity;
                state.strategy_session_peak_balance = equity;
            } else {
                state.balance = cfg.paper_starting_balance;
                state.available_balance = cfg.paper_starting_balance;
                state.session_starting_balance = cfg.paper_starting_balance;
                state.session_peak_balance = cfg.paper_starting_balance;
                state.strategy_session_starting_balance = cfg.paper_starting_balance;
                state.strategy_session_peak_balance = cfg.paper_starting_balance;
            }
            state.day_start_ts = now_ts();
            state.day_start_balance = state.balance;
        }
        if cfg.mode == "real" {
            if let Err(e) = self.sync_real_positions(true).await {
                self.state
                    .add_log("warn", format!("initial real position sync failed: {e}"))
                    .await;
            }
        }
    }

    pub async fn loop_forever(&self) {
        self.init_balance().await;
        loop {
            let tick_sec = self.cfg.lock().await.strategy.paper_tick_sec.max(0.05);
            if self.state.read().await.engine_running {
                if let Err(e) = self.tick().await {
                    self.state
                        .add_log("error", format!("executor tick: {e}"))
                        .await;
                }
            }
            sleep(Duration::from_secs_f64(tick_sec)).await;
        }
    }

    pub async fn on_signal(&self, opp: Opportunity) {
        if let Err(e) = self.maybe_place_quote(opp).await {
            self.state
                .add_log("warn", format!("signal rejected: {e}"))
                .await;
        }
    }

    async fn tick(&self) -> anyhow::Result<()> {
        self.process_quotes().await?;
        self.process_positions().await?;
        self.log_equity_periodically().await?;
        self.check_kill_switch().await?;
        Ok(())
    }

    fn override_for<'a>(cfg: &'a AppConfig, symbol: &str) -> Option<&'a SymbolOverride> {
        cfg.symbol_overrides.iter().find(|o| o.symbol == symbol)
    }

    fn float_setting(cfg: &AppConfig, symbol: &str, field: &str, default: f64) -> f64 {
        let ov = Self::override_for(cfg, symbol);
        match field {
            "scalp_take_profit_bps" => ov
                .and_then(|o| o.scalp_take_profit_bps)
                .unwrap_or(cfg.strategy.scalp_take_profit_bps),
            "scratch_exit_sec" => ov
                .and_then(|o| o.scratch_exit_sec)
                .unwrap_or(cfg.strategy.scratch_exit_sec),
            "scratch_exit_bps" => ov
                .and_then(|o| o.scratch_exit_bps)
                .unwrap_or(cfg.strategy.scratch_exit_bps),
            "pre_submit_max_spread_drift_bps" => ov
                .and_then(|o| o.pre_submit_max_spread_drift_bps)
                .unwrap_or(cfg.strategy.pre_submit_max_spread_drift_bps),
            "taker_ioc_price_buffer_bps" => ov
                .and_then(|o| o.taker_ioc_price_buffer_bps)
                .unwrap_or(cfg.strategy.taker_ioc_price_buffer_bps),
            "taker_ioc_min_fill_ratio" => ov
                .and_then(|o| o.taker_ioc_min_fill_ratio)
                .unwrap_or(cfg.strategy.taker_ioc_min_fill_ratio),
            "profit_protect_arm_bps" => ov
                .and_then(|o| o.profit_protect_arm_bps)
                .unwrap_or(cfg.strategy.profit_protect_arm_bps),
            "profit_giveback_bps" => ov
                .and_then(|o| o.profit_giveback_bps)
                .unwrap_or(cfg.strategy.profit_giveback_bps),
            "fast_profit_arm_bps" => ov
                .and_then(|o| o.fast_profit_arm_bps)
                .unwrap_or(cfg.strategy.fast_profit_arm_bps),
            "fast_profit_giveback_bps" => ov
                .and_then(|o| o.fast_profit_giveback_bps)
                .unwrap_or(cfg.strategy.fast_profit_giveback_bps),
            "profit_protect_min_bps" => ov
                .and_then(|o| o.profit_protect_min_bps)
                .unwrap_or(cfg.strategy.profit_protect_min_bps),
            "edge_collapse_exit_bps" => ov
                .and_then(|o| o.edge_collapse_exit_bps)
                .unwrap_or(cfg.strategy.edge_collapse_exit_bps),
            "edge_loss_after_sec" => ov
                .and_then(|o| o.edge_loss_after_sec)
                .unwrap_or(cfg.strategy.edge_loss_after_sec),
            "edge_loss_exit_bps" => ov
                .and_then(|o| o.edge_loss_exit_bps)
                .unwrap_or(cfg.strategy.edge_loss_exit_bps),
            "settled_profit_sec" => ov
                .and_then(|o| o.settled_profit_sec)
                .unwrap_or(cfg.strategy.settled_profit_sec),
            "settled_profit_min_bps" => ov
                .and_then(|o| o.settled_profit_min_bps)
                .unwrap_or(cfg.strategy.settled_profit_min_bps),
            "settled_profit_max_drift_bps" => ov
                .and_then(|o| o.settled_profit_max_drift_bps)
                .unwrap_or(cfg.strategy.settled_profit_max_drift_bps),
            "settled_profit_edge_bps" => ov
                .and_then(|o| o.settled_profit_edge_bps)
                .unwrap_or(cfg.strategy.settled_profit_edge_bps),
            "dead_trade_after_sec" => ov
                .and_then(|o| o.dead_trade_after_sec)
                .unwrap_or(cfg.strategy.dead_trade_after_sec),
            "dead_trade_max_bps" => ov
                .and_then(|o| o.dead_trade_max_bps)
                .unwrap_or(cfg.strategy.dead_trade_max_bps),
            "bad_entry_guard_sec" => ov
                .and_then(|o| o.bad_entry_guard_sec)
                .unwrap_or(cfg.strategy.bad_entry_guard_sec),
            "bad_entry_min_age_sec" => ov
                .and_then(|o| o.bad_entry_min_age_sec)
                .unwrap_or(cfg.strategy.bad_entry_min_age_sec),
            "bad_entry_spread_bps" => ov
                .and_then(|o| o.bad_entry_spread_bps)
                .unwrap_or(cfg.strategy.bad_entry_spread_bps),
            "bad_entry_exit_bps" => ov
                .and_then(|o| o.bad_entry_exit_bps)
                .unwrap_or(cfg.strategy.bad_entry_exit_bps),
            _ => default,
        }
    }

    fn bool_setting(cfg: &AppConfig, symbol: &str, field: &str, default: bool) -> bool {
        let ov = Self::override_for(cfg, symbol);
        match field {
            "use_fair_tp" => ov
                .and_then(|o| o.use_fair_tp)
                .unwrap_or(cfg.strategy.use_fair_tp),
            _ => default,
        }
    }

    fn sl_pct_for(cfg: &AppConfig, symbol: &str) -> f64 {
        if let Some(ov) = Self::override_for(cfg, symbol).and_then(|o| o.sl_pct) {
            return ov;
        }
        if STOCK_SYMBOLS.contains(&symbol) {
            cfg.strategy.sl_pct_stocks
        } else {
            cfg.strategy.sl_pct_crypto
        }
    }

    fn max_hold_for(cfg: &AppConfig, symbol: &str) -> f64 {
        Self::override_for(cfg, symbol)
            .and_then(|o| o.max_hold_sec)
            .unwrap_or(cfg.strategy.max_hold_sec)
    }

    fn cooldown_min_for(cfg: &AppConfig, symbol: &str) -> f64 {
        Self::override_for(cfg, symbol)
            .and_then(|o| o.cooldown_min_sec)
            .unwrap_or(cfg.strategy.cooldown_min_sec)
    }

    fn cooldown_max_for(cfg: &AppConfig, symbol: &str) -> f64 {
        Self::override_for(cfg, symbol)
            .and_then(|o| o.cooldown_max_sec)
            .unwrap_or(cfg.strategy.cooldown_max_sec)
    }

    fn entry_latency_ms_for(cfg: &AppConfig, symbol: &str) -> u64 {
        Self::override_for(cfg, symbol)
            .and_then(|o| o.entry_latency_ms)
            .unwrap_or(cfg.strategy.entry_latency_ms)
    }

    fn signal_max_age_ms_for(cfg: &AppConfig, symbol: &str) -> u64 {
        Self::override_for(cfg, symbol)
            .and_then(|o| o.signal_max_age_ms)
            .unwrap_or(cfg.strategy.signal_max_age_ms)
    }

    fn min_entry_score_for(cfg: &AppConfig, symbol: &str) -> f64 {
        Self::override_for(cfg, symbol)
            .and_then(|o| o.min_entry_score)
            .unwrap_or(0.0)
    }

    fn max_chase_bps_for(cfg: &AppConfig, symbol: &str, algo: Option<&str>) -> f64 {
        if let Some(v) = Self::override_for(cfg, symbol).and_then(|o| o.max_chase_bps) {
            return v.max(0.0);
        }
        match algo
            .unwrap_or(&cfg.strategy.algorithm)
            .to_ascii_lowercase()
            .as_str()
        {
            "raw_momentum" => cfg.strategy.raw_momentum_max_chase_bps.max(0.0),
            "confluence" => cfg.strategy.confluence_max_chase_bps.max(0.0),
            "ofi" => cfg.strategy.ofi_max_chase_bps.max(0.0),
            "imbalance" => cfg.strategy.imbalance_max_chase_bps.max(0.0),
            _ => 0.0,
        }
    }

    fn fill_reject_reason(
        cfg: &AppConfig,
        symbol: &str,
        side: &str,
        algo: Option<&str>,
        fill_price: f64,
        fair: f64,
    ) -> Option<String> {
        if fair <= 0.0 || fill_price <= 0.0 {
            return None;
        }
        let spread_bps = (fill_price - fair) / fair * 1e4;
        let max_chase = Self::max_chase_bps_for(cfg, symbol, algo);
        if side == "LONG" && spread_bps > max_chase {
            return Some(format!("fill_chasing_long={spread_bps:.2}bps"));
        }
        if side == "SHORT" && spread_bps < -max_chase {
            return Some(format!("fill_chasing_short={spread_bps:.2}bps"));
        }
        None
    }

    fn signal_invalid_reason(
        cfg: &AppConfig,
        q: &Quote,
        st: Option<&crate::state::SymbolStats>,
        now: f64,
    ) -> Option<String> {
        let Some(st) = st else {
            return None;
        };
        if let Some(side) = st.side_hint.as_deref() {
            if side != q.side {
                return Some(format!("side_flip={side}"));
            }
        }

        let max_age_ms = Self::signal_max_age_ms_for(cfg, &q.symbol) as f64;
        if max_age_ms > 0.0 && q.signal_ts > 0.0 {
            let age_ms = (now - q.signal_ts).max(0.0) * 1000.0;
            let tick_ms = cfg.strategy.paper_tick_sec.max(0.05) * 1000.0;
            let entry_latency_ms = Self::entry_latency_ms_for(cfg, &q.symbol) as f64;
            let grace_ms = (tick_ms + entry_latency_ms + 25.0).clamp(75.0, 250.0);
            let fresh_books = st.mexc_book_age_ms.is_none_or(|v| v <= 200.0)
                && st.binance_book_age_ms.is_none_or(|v| v <= 200.0);
            if age_ms > max_age_ms && !(age_ms <= max_age_ms + grace_ms && fresh_books) {
                return Some(format!("signal_age={age_ms:.0}ms"));
            }
        }

        let drift_limit =
            Self::float_setting(cfg, &q.symbol, "pre_submit_max_spread_drift_bps", 0.0);
        if drift_limit > 0.0 {
            if let (Some(at_quote), Some(now_spread)) = (q.spread_bps_at_quote, st.spread_bps) {
                let drift = if q.side == "LONG" {
                    now_spread - at_quote
                } else {
                    at_quote - now_spread
                };
                if drift > drift_limit {
                    return Some(format!("spread_drift={drift:.2}bps"));
                }
            }
        }
        None
    }

    async fn max_leverage(&self, symbol: &str, cfg: &AppConfig) -> i64 {
        if let Some(lev) = self.max_lev_cache.lock().await.get(symbol).copied() {
            return lev;
        }
        let lev = self
            .trader
            .get_max_leverage(symbol)
            .await
            .unwrap_or(cfg.risk.fixed_leverage)
            .max(1);
        self.max_lev_cache
            .lock()
            .await
            .insert(symbol.to_string(), lev);
        lev
    }

    async fn contract_size(&self, symbol: &str) -> f64 {
        if let Some(v) = self.contract_size_cache.lock().await.get(symbol).copied() {
            return v;
        }
        let from_agg = self.agg.lock().await.contract_size_for(symbol);
        if from_agg > 0.0 && (from_agg - 1.0).abs() > f64::EPSILON {
            self.contract_size_cache
                .lock()
                .await
                .insert(symbol.to_string(), from_agg);
            return from_agg;
        }
        let v = self
            .trader
            .get_contract_detail(symbol, Duration::from_secs(3600))
            .await
            .and_then(|d| d.get("contractSize").and_then(num_as_f64))
            .unwrap_or(1.0)
            .max(1e-18);
        self.contract_size_cache
            .lock()
            .await
            .insert(symbol.to_string(), v);
        v
    }

    async fn maybe_place_quote(&self, opp: Opportunity) -> anyhow::Result<()> {
        let cfg = self.cfg.lock().await.clone();
        if cfg.mode == "logger" {
            return Ok(());
        }
        let blocked = {
            let st = self.state.read().await;
            if st.kill_switch {
                Some("kill_switch")
            } else if st.positions.contains_key(&opp.symbol) {
                Some("already_open")
            } else {
                None
            }
        };
        if let Some(reason) = blocked {
            self.record_candidate_reject(&opp, reason).await;
            return Ok(());
        }
        if opp.score < Self::min_entry_score_for(&cfg, &opp.symbol) {
            self.record_candidate_reject(&opp, "low_score").await;
            return Ok(());
        }
        if cfg.mode == "real"
            && cfg.strategy.real_require_zero_fee
            && !self.trader.is_zero_fee_symbol(&opp.symbol).await
        {
            self.record_candidate_reject(&opp, "not_zero_fee_now").await;
            return Ok(());
        }

        let st = self.agg.lock().await.compute_stats(&opp.symbol);
        let book = self.agg.lock().await.get_book(&opp.symbol);
        let Some(book) = book else {
            self.record_candidate_reject(&opp, "no_book").await;
            return Ok(());
        };
        if book.best_bid().is_none() || book.best_ask().is_none() {
            self.record_candidate_reject(&opp, "empty_book").await;
            return Ok(());
        }
        let drift_limit =
            Self::float_setting(&cfg, &opp.symbol, "pre_submit_max_spread_drift_bps", 0.0);
        if drift_limit > 0.0 {
            if let (Some(now_spread), Some(quote_spread)) = (st.spread_bps, Some(opp.z)) {
                let _ = quote_spread;
                if now_spread.abs() > 0.0 && st.spread_bps.zip(Some(opp.entry_price)).is_some() {
                    // The Python code compares spread at signal vs submit.
                    // Here we only have the current stats in the opportunity payload, so keep a conservative age/book gate.
                }
            }
        }
        let balance_free = self.free_balance().await;
        let max_lev = self.max_leverage(&opp.symbol, &cfg).await;
        let ov = Self::override_for(&cfg, &opp.symbol);
        let alloc = {
            let state = self.state.read().await;
            self.alloc.lock().await.decide(
                &opp,
                &state,
                balance_free,
                Some(max_lev),
                st.mexc_book_top10_notional.unwrap_or(0.0),
                ov.and_then(|o| o.margin_pct),
                ov.and_then(|o| o.leverage),
                ov.and_then(|o| o.max_notional_usdt),
            )
        };
        if !alloc.accept {
            self.record_candidate_reject(&opp, &alloc.reason).await;
            return Ok(());
        }
        if self.quotes.lock().await.contains_key(&opp.symbol) {
            return Ok(());
        }

        let contract_size = self.contract_size(&opp.symbol).await;
        let tick = book.inferred_tick_size(5).max(0.0);
        let price = if cfg.strategy.taker_entry {
            let buffer =
                Self::float_setting(&cfg, &opp.symbol, "taker_ioc_price_buffer_bps", 0.0) / 1e4;
            if opp.side == "LONG" {
                book.best_ask().unwrap() * (1.0 + buffer)
            } else {
                book.best_bid().unwrap() * (1.0 - buffer)
            }
        } else if opp.side == "LONG" {
            (book.best_bid().unwrap() - tick * cfg.strategy.quote_offset_ticks.max(0) as f64)
                .max(0.0)
        } else {
            book.best_ask().unwrap() + tick * cfg.strategy.quote_offset_ticks.max(0) as f64
        };
        let qty = if price > 0.0 && contract_size > 0.0 {
            alloc.notional_usdt / (price * contract_size)
        } else {
            0.0
        };
        if qty <= 0.0 {
            self.record_candidate_reject(&opp, "zero_qty").await;
            return Ok(());
        }
        let fair_for_open = st.fair.unwrap_or(opp.fair);
        if let Some(reason) = Self::fill_reject_reason(
            &cfg,
            &opp.symbol,
            &opp.side,
            Some(&opp.algorithm),
            price,
            fair_for_open,
        ) {
            self.record_candidate_reject(&opp, &reason).await;
            return Ok(());
        }
        let latency = Self::entry_latency_ms_for(&cfg, &opp.symbol) as f64 / 1000.0;
        let quote = Quote {
            symbol: opp.symbol.clone(),
            side: opp.side.clone(),
            price,
            qty,
            contract_size,
            notional: alloc.notional_usdt,
            margin: alloc.margin_usdt,
            leverage: alloc.leverage,
            placed_ts: now_ts(),
            fair_at_quote: fair_for_open,
            sigma_at_quote: opp.sigma,
            taker_open_at: cfg.strategy.taker_entry.then_some(now_ts() + latency),
            signal_ts: opp.signal_ts,
            entry_algo: Some(opp.algorithm.clone()),
            entry_score: opp.score,
            spread_bps_at_quote: st.spread_bps,
            fill_ratio: None,
            levels_eaten: None,
            real_order_id: None,
            real_submitted: false,
        };
        self.quotes.lock().await.insert(opp.symbol.clone(), quote);
        self.state
            .add_log(
                "info",
                format!(
                    "quote {} {} @ {:.8} score={:.2}",
                    opp.symbol, opp.side, price, opp.score
                ),
            )
            .await;
        Ok(())
    }

    async fn record_candidate_reject(&self, opp: &Opportunity, reason: &str) {
        let _ = self
            .store
            .insert_candidate(
                now_ts(),
                &opp.symbol,
                Some(&opp.side),
                opp.score,
                Some(opp.z),
                None,
                Some(opp.fair),
                Some(opp.entry_price),
                None,
                Some(reason),
                false,
            )
            .await;
    }

    async fn process_quotes(&self) -> anyhow::Result<()> {
        let cfg = self.cfg.lock().await.clone();
        if self.state.read().await.kill_switch {
            let quotes: Vec<Quote> = self.quotes.lock().await.values().cloned().collect();
            if !quotes.is_empty() {
                self.quotes.lock().await.clear();
                if cfg.mode == "real" {
                    for q in &quotes {
                        let _ = self.trader.cancel_all_for(&q.symbol).await;
                    }
                }
                self.state
                    .add_log(
                        "warn",
                        format!("cleared {} pending quotes: kill_switch", quotes.len()),
                    )
                    .await;
            }
            return Ok(());
        }
        let symbols: Vec<String> = self.quotes.lock().await.keys().cloned().collect();
        for sym in symbols {
            let quote = {
                let quotes = self.quotes.lock().await;
                quotes.get(&sym).cloned()
            };
            let Some(mut q) = quote else {
                continue;
            };
            if cfg.mode == "real" {
                self.process_real_quote(&mut q, &cfg).await?;
                continue;
            }
            let now = now_ts();
            let book = self.agg.lock().await.get_book(&q.symbol);
            let Some(book) = book else {
                continue;
            };
            if now - q.placed_ts > cfg.strategy.quote_timeout_sec {
                self.quotes.lock().await.remove(&q.symbol);
                self.state
                    .add_log("info", format!("quote timeout {}", q.symbol))
                    .await;
                continue;
            }
            let st = self.agg.lock().await.compute_stats(&q.symbol);
            if st.z_score.is_some_and(|z| z.abs() < cfg.strategy.cancel_z)
                && q.taker_open_at.is_none()
            {
                self.quotes.lock().await.remove(&q.symbol);
                self.state
                    .add_log("info", format!("quote cancel_z {}", q.symbol))
                    .await;
                continue;
            }
            if let Some(open_at) = q.taker_open_at {
                if now < open_at {
                    continue;
                }
                let levels = if q.side == "LONG" {
                    &book.asks
                } else {
                    &book.bids
                };
                let buffer =
                    Self::float_setting(&cfg, &q.symbol, "taker_ioc_price_buffer_bps", 0.0) / 1e4;
                let cap = if q.side == "LONG" {
                    q.price * (1.0 + buffer)
                } else {
                    q.price * (1.0 - buffer)
                };
                let (vwap, qty, notional, eaten) = if cfg.strategy.taker_ioc_simulation {
                    vwap_by_notional_capped(levels, q.notional, &q.side, cap, q.contract_size)
                } else {
                    vwap_by_notional(levels, q.notional, q.contract_size)
                };
                let min_fill =
                    Self::float_setting(&cfg, &q.symbol, "taker_ioc_min_fill_ratio", 0.2);
                if let Some(fill_price) = vwap {
                    let ratio = (notional / q.notional).clamp(0.0, 1.0);
                    if ratio >= min_fill {
                        q.qty = qty;
                        q.notional = notional;
                        q.margin = notional / q.leverage as f64;
                        q.fill_ratio = Some(ratio);
                        q.levels_eaten = Some(eaten as i64);
                        self.quotes.lock().await.remove(&q.symbol);
                        self.open_position(q, fill_price).await?;
                    } else {
                        self.quotes.lock().await.remove(&q.symbol);
                        self.state
                            .add_log("info", format!("IOC tiny fill {}", sym))
                            .await;
                    }
                } else {
                    self.quotes.lock().await.remove(&q.symbol);
                }
                continue;
            }
            let fill = if q.side == "LONG" {
                book.best_ask().is_some_and(|ask| ask <= q.price)
            } else {
                book.best_bid().is_some_and(|bid| bid >= q.price)
            };
            if fill {
                self.quotes.lock().await.remove(&q.symbol);
                self.open_position(q.clone(), q.price).await?;
            }
        }
        Ok(())
    }

    async fn process_real_quote(&self, q: &mut Quote, cfg: &AppConfig) -> anyhow::Result<()> {
        let now = now_ts();
        if now - q.placed_ts > cfg.strategy.quote_timeout_sec && q.real_submitted {
            let _ = self.trader.cancel_all_for(&q.symbol).await;
            let mut order_note = String::new();
            if let Some(order_id) = q.real_order_id {
                let res = self.trader.query_order(order_id).await;
                let rows = match res.get("data") {
                    Some(Value::Array(rows)) => rows.clone(),
                    Some(Value::Object(_)) => vec![res.get("data").cloned().unwrap_or(Value::Null)],
                    _ => Vec::new(),
                };
                if let Some(row) = rows.first() {
                    let state = row.get("state").and_then(num_as_f64).unwrap_or(-1.0) as i64;
                    let deal_vol = row.get("dealVol").and_then(num_as_f64).unwrap_or(0.0);
                    order_note = format!(" state={state} dealVol={deal_vol:.4}");
                } else if res.get("success").and_then(Value::as_bool) != Some(true) {
                    order_note = format!(" order_query={res}");
                }
            }
            let mut synced = self.sync_real_positions(true).await.unwrap_or(0);
            if synced == 0 && q.real_order_id.is_some() {
                sleep(Duration::from_millis(150)).await;
                synced += self.sync_real_positions(true).await.unwrap_or(0);
            }
            {
                let mut st = self.state.write().await;
                st.cooldown_until.insert(
                    q.symbol.clone(),
                    now + Self::cooldown_min_for(cfg, &q.symbol).max(1.0),
                );
            }
            self.quotes.lock().await.remove(&q.symbol);
            self.state
                .add_log(
                    "info",
                    format!(
                        "real quote timeout {}; synced_positions={synced}{order_note}",
                        q.symbol
                    ),
                )
                .await;
            return Ok(());
        }
        if q.taker_open_at.is_some_and(|t| now < t) {
            return Ok(());
        }
        if !q.real_submitted {
            let st_now = self.agg.lock().await.compute_stats(&q.symbol);
            let evaluated_now = self.state.read().await.stats.get(&q.symbol).cloned();
            if let Some(reason) = Self::signal_invalid_reason(cfg, q, evaluated_now.as_ref(), now) {
                self.quotes.lock().await.remove(&q.symbol);
                self.state
                    .add_log(
                        "debug",
                        format!("[real] skip {} {}: {reason}", q.symbol, q.side),
                    )
                    .await;
                return Ok(());
            }
            if cfg.strategy.taker_entry {
                if let Some(book) = self.agg.lock().await.get_book(&q.symbol) {
                    if let (Some(bid), Some(ask)) = (book.best_bid(), book.best_ask()) {
                        let buffer =
                            Self::float_setting(cfg, &q.symbol, "taker_ioc_price_buffer_bps", 0.0)
                                / 1e4;
                        q.price = if q.side == "LONG" {
                            ask * (1.0 + buffer)
                        } else {
                            bid * (1.0 - buffer)
                        };
                    }
                }
            }
            let fair_now = st_now.fair.unwrap_or(q.fair_at_quote);
            if let Some(reason) = Self::fill_reject_reason(
                cfg,
                &q.symbol,
                &q.side,
                q.entry_algo.as_deref(),
                q.price,
                fair_now,
            ) {
                self.quotes.lock().await.remove(&q.symbol);
                self.state
                    .add_log(
                        "debug",
                        format!("[real] skip {} {}: {reason}", q.symbol, q.side),
                    )
                    .await;
                return Ok(());
            }
            q.fair_at_quote = fair_now;
            q.sigma_at_quote = st_now.sigma_spread.unwrap_or(q.sigma_at_quote);
            let resp = if cfg.strategy.taker_entry {
                self.trader
                    .open_ioc(&q.symbol, &q.side, q.notional, q.leverage, q.price)
                    .await
            } else {
                self.trader
                    .open_limit(&q.symbol, &q.side, q.notional, q.leverage, q.price)
                    .await
            };
            q.real_order_id = extract_order_id(&resp);
            q.real_submitted = true;
            self.quotes.lock().await.insert(q.symbol.clone(), q.clone());
            if resp.get("success").and_then(Value::as_bool) != Some(true) {
                self.quotes.lock().await.remove(&q.symbol);
                let code = resp.get("code").and_then(num_as_f64).unwrap_or(0.0) as i64;
                let cooldown_sec = if code == 510 { 10.0 } else { 2.0 };
                self.state
                    .write()
                    .await
                    .cooldown_until
                    .insert(q.symbol.clone(), now_ts() + cooldown_sec);
                self.state
                    .add_log("warn", format!("real open failed {}: {}", q.symbol, resp))
                    .await;
            } else if cfg.strategy.taker_entry {
                self.state
                    .add_log(
                        "info",
                        format!(
                            "real ioc submitted {} {} @ {:.8} notional={:.2} lev={} oid={}",
                            q.symbol,
                            q.side,
                            q.price,
                            q.notional,
                            q.leverage,
                            q.real_order_id
                                .map(|id| id.to_string())
                                .unwrap_or_else(|| "-".to_string())
                        ),
                    )
                    .await;
                let _ = self.sync_real_positions(true).await;
                if self.state.read().await.positions.contains_key(&q.symbol) {
                    self.quotes.lock().await.remove(&q.symbol);
                }
            }
            return Ok(());
        }
        let Some(order_id) = q.real_order_id else {
            return Ok(());
        };
        let res = self.trader.query_order(order_id).await;
        let rows = match res.get("data") {
            Some(Value::Array(rows)) => rows.clone(),
            Some(Value::Object(_)) => vec![res.get("data").cloned().unwrap_or(Value::Null)],
            _ => Vec::new(),
        };
        let filled = rows.iter().any(|row| {
            let state = row.get("state").and_then(num_as_f64).unwrap_or(0.0) as i64;
            let deal_vol = row.get("dealVol").and_then(num_as_f64).unwrap_or(0.0);
            state == 3 || deal_vol > 0.0
        });
        if filled {
            let fill_price = rows
                .first()
                .and_then(|r| {
                    ["priceAvg", "avgPrice", "dealAvgPrice", "price"]
                        .iter()
                        .find_map(|k| r.get(*k).and_then(num_as_f64))
                })
                .unwrap_or(q.price);
            let _ = self.sync_real_positions(true).await;
            if self.state.read().await.positions.contains_key(&q.symbol) {
                self.quotes.lock().await.remove(&q.symbol);
            } else {
                self.state
                    .add_log(
                        "warn",
                        format!(
                            "real fill detected {} @ {:.8}, but exchange position not visible yet",
                            q.symbol, fill_price
                        ),
                    )
                    .await;
            }
        }
        Ok(())
    }

    async fn open_position(&self, q: Quote, fill_price: f64) -> anyhow::Result<()> {
        let cfg = self.cfg.lock().await.clone();
        let sl_pct = Self::sl_pct_for(&cfg, &q.symbol);
        let initial_sl_distance = fill_price * sl_pct.max(0.0);
        let stop = if q.side == "LONG" {
            Some(fill_price - initial_sl_distance)
        } else {
            Some(fill_price + initial_sl_distance)
        };
        let pos = ManagedPosition {
            symbol: q.symbol.clone(),
            side: q.side.clone(),
            entry_price: fill_price,
            notional_usdt: q.notional,
            margin_usdt: q.margin,
            leverage: q.leverage as f64,
            qty: q.qty,
            open_ts: now_ts(),
            fair_at_open: q.fair_at_quote,
            sigma_at_open: q.sigma_at_quote,
            contract_size: q.contract_size,
            quote_ts: q.placed_ts,
            signal_ts: q.signal_ts,
            entry_latency_ms: (now_ts() - q.signal_ts).max(0.0) * 1000.0,
            entry_algo: q.entry_algo.clone(),
            entry_score: q.entry_score,
            max_hold_sec: Self::max_hold_for(&cfg, &q.symbol),
            entry_fill_ratio: q.fill_ratio,
            entry_levels_eaten: q.levels_eaten,
            entry_spread_bps: q.spread_bps_at_quote,
            entry_ofi: None,
            entry_imbalance: None,
            entry_fv1: None,
            entry_fv5: None,
            entry_fv30: None,
            entry_mexc_book_age_ms: None,
            entry_binance_book_age_ms: None,
            stop_price: stop,
            tp_price: Self::bool_setting(&cfg, &q.symbol, "use_fair_tp", cfg.strategy.use_fair_tp)
                .then_some(q.fair_at_quote),
            best_excursion: Some(fill_price),
            best_realized_bps: 0.0,
            last_sl_update_ts: 0.0,
            initial_sl_distance: Some(initial_sl_distance),
            last_pnl_usdt: 0.0,
            last_pnl_pct: 0.0,
            mexc_position_id: None,
            mexc_stop_plan_id: None,
            mexc_entry_order_id: q.real_order_id,
            closed: false,
            close_reason: None,
            close_ts: 0.0,
            close_price: None,
            realized_pnl: 0.0,
            exit_signal_ts: 0.0,
            exit_latency_ms: 0.0,
            settled_profit_since: 0.0,
            settled_profit_anchor_bps: 0.0,
        };
        {
            let mut st = self.state.write().await;
            st.available_balance = (st.available_balance - pos.margin_usdt).max(0.0);
            st.positions.insert(pos.symbol.clone(), pos.clone());
        }
        let _ = self
            .store
            .upsert_managed_position(&cfg.mode, &pos.symbol, &serde_json::to_value(&pos)?)
            .await;
        self.state
            .add_log(
                "info",
                format!("OPEN {} {} @ {:.8}", pos.symbol, pos.side, pos.entry_price),
            )
            .await;
        Ok(())
    }

    async fn process_positions(&self) -> anyhow::Result<()> {
        let cfg = self.cfg.lock().await.clone();
        if cfg.mode == "real" {
            self.refresh_real_balance().await;
            self.sync_real_positions(false).await?;
        }
        let positions: Vec<ManagedPosition> = self
            .state
            .read()
            .await
            .positions
            .values()
            .cloned()
            .collect();
        for mut pos in positions {
            let book = self.agg.lock().await.get_book(&pos.symbol);
            let Some(book) = book else {
                continue;
            };
            let exit_price = realisable_exit_price(&pos, &book);
            let current_bps = realized_bps_at_price(&pos, exit_price);
            let pnl = current_bps / 1e4 * pos.notional_usdt;
            pos.last_pnl_usdt = pnl;
            pos.last_pnl_pct = if pos.margin_usdt > 0.0 {
                pnl / pos.margin_usdt * 100.0
            } else {
                0.0
            };
            pos.best_realized_bps = pos.best_realized_bps.max(current_bps);
            if pos.side == "LONG" {
                pos.best_excursion = Some(
                    pos.best_excursion
                        .unwrap_or(pos.entry_price)
                        .max(exit_price),
                );
            } else {
                pos.best_excursion = Some(
                    pos.best_excursion
                        .unwrap_or(pos.entry_price)
                        .min(exit_price),
                );
            }
            let now = now_ts();
            let age = now - pos.open_ts;
            let stats = self.state.read().await.stats.get(&pos.symbol).cloned();
            let current_fair = stats.as_ref().and_then(|s| s.fair);
            let current_imbalance = stats.as_ref().and_then(|s| s.mexc_book_imbalance);
            let residual_edge = residual_edge_bps(&pos, current_fair, exit_price);
            let edge_collapse =
                Self::float_setting(&cfg, &pos.symbol, "edge_collapse_exit_bps", 0.0);
            let mut close_reason = None;

            let min_fill = Self::float_setting(&cfg, &pos.symbol, "taker_ioc_min_fill_ratio", 0.0);
            if let Some(fill_ratio) = pos.entry_fill_ratio {
                if min_fill > 0.0 && fill_ratio > 0.0 && fill_ratio < min_fill {
                    close_reason = Some("bad_fill_ratio".to_string());
                }
            }

            if let Some(tp) = pos.tp_price {
                if pos.side == "LONG" && exit_price >= tp {
                    close_reason = Some("tp".to_string());
                }
                if pos.side == "SHORT" && exit_price <= tp {
                    close_reason = Some("tp".to_string());
                }
            }
            let scalp = Self::float_setting(&cfg, &pos.symbol, "scalp_take_profit_bps", 0.0);
            if close_reason.is_none() && scalp > 0.0 && current_bps >= scalp {
                close_reason = Some("scalp_tp".to_string());
            }

            let profit_protect_arm =
                Self::float_setting(&cfg, &pos.symbol, "profit_protect_arm_bps", 0.0);
            let profit_giveback =
                Self::float_setting(&cfg, &pos.symbol, "profit_giveback_bps", 0.0);
            let fast_profit_arm =
                Self::float_setting(&cfg, &pos.symbol, "fast_profit_arm_bps", 0.0);
            let fast_profit_giveback =
                Self::float_setting(&cfg, &pos.symbol, "fast_profit_giveback_bps", 0.0);
            let profit_protect_min =
                Self::float_setting(&cfg, &pos.symbol, "profit_protect_min_bps", 0.0);
            if close_reason.is_none()
                && should_profit_protect_exit(
                    current_bps,
                    pos.best_realized_bps,
                    residual_edge,
                    profit_protect_arm,
                    profit_giveback,
                    fast_profit_arm,
                    fast_profit_giveback,
                    profit_protect_min,
                    edge_collapse,
                )
            {
                close_reason = Some("profit_protect".to_string());
            }

            if close_reason.is_none() {
                let settled_sec = Self::float_setting(&cfg, &pos.symbol, "settled_profit_sec", 0.0);
                let settled_min =
                    Self::float_setting(&cfg, &pos.symbol, "settled_profit_min_bps", 0.0);
                let settled_drift =
                    Self::float_setting(&cfg, &pos.symbol, "settled_profit_max_drift_bps", 0.0);
                let settled_edge =
                    Self::float_setting(&cfg, &pos.symbol, "settled_profit_edge_bps", 0.0);
                if update_settled_profit_state(
                    &mut pos,
                    now,
                    current_bps,
                    residual_edge,
                    settled_sec,
                    settled_min,
                    settled_drift,
                    settled_edge,
                ) {
                    close_reason = Some("settled_profit".to_string());
                }
            }

            if close_reason.is_none()
                && should_bad_entry_exit(
                    &pos,
                    age,
                    current_bps,
                    residual_edge,
                    Self::float_setting(&cfg, &pos.symbol, "bad_entry_guard_sec", 0.0),
                    Self::float_setting(&cfg, &pos.symbol, "bad_entry_min_age_sec", 0.0),
                    Self::float_setting(&cfg, &pos.symbol, "bad_entry_spread_bps", 0.0),
                    Self::float_setting(&cfg, &pos.symbol, "bad_entry_exit_bps", 0.0),
                    edge_collapse,
                )
            {
                close_reason = Some("bad_entry".to_string());
            }

            let edge_loss_after =
                Self::float_setting(&cfg, &pos.symbol, "edge_loss_after_sec", 0.0);
            let edge_loss_exit = Self::float_setting(&cfg, &pos.symbol, "edge_loss_exit_bps", 0.0);
            if close_reason.is_none()
                && edge_loss_after > 0.0
                && age >= edge_loss_after
                && current_bps <= edge_loss_exit
                && residual_edge.is_some_and(|edge| edge <= edge_collapse)
            {
                close_reason = Some("edge_loss".to_string());
            }

            let dead_trade_after =
                Self::float_setting(&cfg, &pos.symbol, "dead_trade_after_sec", 0.0);
            let dead_trade_max = Self::float_setting(&cfg, &pos.symbol, "dead_trade_max_bps", 0.0);
            if close_reason.is_none()
                && dead_trade_after > 0.0
                && age >= dead_trade_after
                && current_bps <= dead_trade_max
                && residual_edge.is_some_and(|edge| edge <= edge_collapse)
            {
                close_reason = Some("dead_trade".to_string());
            }

            let scratch_sec = Self::float_setting(&cfg, &pos.symbol, "scratch_exit_sec", 0.0);
            let scratch_bps = Self::float_setting(&cfg, &pos.symbol, "scratch_exit_bps", 0.0);
            if close_reason.is_none()
                && scratch_sec > 0.0
                && age >= scratch_sec
                && current_bps <= scratch_bps
            {
                close_reason = Some("scratch".to_string());
            }

            if close_reason.is_none() && cfg.strategy.signal_flip_exit {
                if let Some(imb) = current_imbalance {
                    let threshold = cfg.strategy.imbalance_exit_log.max(0.0);
                    let flipped = (pos.side == "LONG" && imb < -threshold)
                        || (pos.side == "SHORT" && imb > threshold);
                    if flipped {
                        close_reason = Some("signal_flip".to_string());
                    }
                }
            }

            if close_reason.is_none()
                && now - pos.last_sl_update_ts >= cfg.strategy.sl_update_throttle_sec
            {
                self.update_trailing_stop(&cfg, &mut pos);
                pos.last_sl_update_ts = now;
            }

            if close_reason.is_none() {
                if let Some(stop) = pos.stop_price {
                    if pos.side == "LONG" && exit_price <= stop {
                        close_reason = Some("sl".to_string());
                    }
                    if pos.side == "SHORT" && exit_price >= stop {
                        close_reason = Some("sl".to_string());
                    }
                }
            }

            if close_reason.is_none() && age >= pos.max_hold_sec {
                close_reason = Some("time".to_string());
            }
            if let Some(reason) = close_reason {
                pos.exit_signal_ts = now;
                self.close_position(pos, exit_price, &reason).await?;
            } else {
                self.state
                    .write()
                    .await
                    .positions
                    .insert(pos.symbol.clone(), pos);
            }
        }
        Ok(())
    }

    async fn sync_real_positions(&self, force: bool) -> anyhow::Result<usize> {
        let cfg = self.cfg.lock().await.clone();
        if cfg.mode != "real" {
            return Ok(0);
        }
        let now = now_ts();
        {
            let mut last = self.last_real_sync_ts.lock().await;
            if !force && now - *last < 1.25 {
                return Ok(0);
            }
            *last = now;
        }
        let Some(rows) = self.trader.get_positions_raw_checked().await else {
            self.state
                .add_log("warn", "real position sync failed")
                .await;
            return Ok(0);
        };
        let mut seen = HashSet::new();
        let mut synced = 0_usize;
        for row in rows {
            let Some(symbol) = row
                .get("symbol")
                .and_then(Value::as_str)
                .map(|s| s.to_ascii_uppercase())
                .filter(|s| !s.is_empty())
            else {
                continue;
            };
            let existing = self.state.read().await.positions.get(&symbol).cloned();
            let pending = self.quotes.lock().await.get(&symbol).cloned();
            let is_new = existing.is_none();
            let Some(pos) = self
                .managed_from_exchange_position(&cfg, &row, existing, pending)
                .await
            else {
                continue;
            };
            seen.insert(symbol.clone());
            {
                let mut st = self.state.write().await;
                st.positions.insert(symbol.clone(), pos.clone());
            }
            self.quotes.lock().await.remove(&symbol);
            let _ = self
                .store
                .upsert_managed_position(&cfg.mode, &symbol, &serde_json::to_value(&pos)?)
                .await;
            if is_new {
                self.state
                    .add_log(
                        "info",
                        format!(
                            "SYNC OPEN {} {} @ {:.8} qty={:.4}",
                            pos.symbol, pos.side, pos.entry_price, pos.qty
                        ),
                    )
                    .await;
            }
            synced += 1;
        }

        let stale: Vec<String> = self
            .state
            .read()
            .await
            .positions
            .keys()
            .filter(|sym| !seen.contains(*sym))
            .cloned()
            .collect();
        for symbol in stale {
            self.state.write().await.positions.remove(&symbol);
            self.quotes.lock().await.remove(&symbol);
            let _ = self.store.delete_managed_position(&cfg.mode, &symbol).await;
            self.state
                .add_log("info", format!("SYNC CLOSED {symbol} on exchange"))
                .await;
        }
        Ok(synced)
    }

    async fn managed_from_exchange_position(
        &self,
        cfg: &AppConfig,
        row: &Value,
        existing: Option<ManagedPosition>,
        pending: Option<Quote>,
    ) -> Option<ManagedPosition> {
        let symbol = row.get("symbol")?.as_str()?.to_ascii_uppercase();
        let side = match first_num(
            row,
            &["positionType", "position_type", "positionSide", "side"],
        )
        .unwrap_or(0.0) as i64
        {
            1 => "LONG".to_string(),
            2 => "SHORT".to_string(),
            _ => return None,
        };
        let qty = first_num(
            row,
            &[
                "holdVol",
                "holdVolFullyScale",
                "positionVol",
                "vol",
                "volume",
            ],
        )
        .unwrap_or(0.0)
        .abs();
        if qty <= 0.0 {
            return None;
        }
        let entry_price = first_num(
            row,
            &[
                "holdAvgPriceFullyScale",
                "holdAvgPrice",
                "openAvgPriceFullyScale",
                "openAvgPrice",
                "newOpenAvgPrice",
            ],
        )
        .filter(|v| *v > 0.0)?;
        let leverage = first_num(row, &["leverage", "lever"])
            .unwrap_or(1.0)
            .max(1.0);
        let contract_size = existing
            .as_ref()
            .map(|p| p.contract_size)
            .filter(|v| *v > 0.0)
            .unwrap_or_else(|| 0.0);
        let contract_size = if contract_size > 0.0 {
            contract_size
        } else {
            self.contract_size(&symbol).await
        };
        let notional = first_num(
            row,
            &[
                "holdValue",
                "positionValue",
                "openValue",
                "value",
                "notional",
            ],
        )
        .filter(|v| *v > 0.0)
        .unwrap_or_else(|| qty * entry_price * contract_size);
        let margin = first_num(
            row,
            &[
                "im",
                "oim",
                "positionMargin",
                "holdMargin",
                "margin",
                "isolatedMargin",
            ],
        )
        .filter(|v| *v > 0.0)
        .unwrap_or_else(|| notional / leverage.max(1.0));
        let now = now_ts();
        let open_ts = time_sec(first_num(
            row,
            &["createTime", "openTime", "openTs", "holdTime"],
        ))
        .or_else(|| existing.as_ref().map(|p| p.open_ts))
        .unwrap_or(now);
        let stats = self.state.read().await.stats.get(&symbol).cloned();
        let book_mid = self
            .agg
            .lock()
            .await
            .get_book(&symbol)
            .and_then(|b| b.mid());
        let fair = existing
            .as_ref()
            .map(|p| p.fair_at_open)
            .filter(|v| *v > 0.0)
            .or_else(|| {
                pending
                    .as_ref()
                    .map(|q| q.fair_at_quote)
                    .filter(|v| *v > 0.0)
            })
            .or_else(|| stats.as_ref().and_then(|s| s.fair))
            .or(book_mid)
            .unwrap_or(entry_price);
        let sigma = existing
            .as_ref()
            .map(|p| p.sigma_at_open)
            .filter(|v| *v > 0.0)
            .or_else(|| {
                pending
                    .as_ref()
                    .map(|q| q.sigma_at_quote)
                    .filter(|v| *v > 0.0)
            })
            .or_else(|| stats.as_ref().and_then(|s| s.sigma_spread))
            .unwrap_or(0.0);
        let initial_sl_distance = existing
            .as_ref()
            .and_then(|p| p.initial_sl_distance)
            .filter(|v| *v > 0.0)
            .unwrap_or_else(|| entry_price * Self::sl_pct_for(cfg, &symbol).max(0.0));
        let stop_price = existing.as_ref().and_then(|p| p.stop_price).or_else(|| {
            if side == "LONG" {
                Some(entry_price - initial_sl_distance)
            } else {
                Some(entry_price + initial_sl_distance)
            }
        });
        let tp_price = existing
            .as_ref()
            .and_then(|p| p.tp_price)
            .or_else(|| cfg.strategy.use_fair_tp.then_some(fair));
        let position_id = first_num(row, &["positionId", "id"]).map(|v| v as i64);

        Some(ManagedPosition {
            symbol: symbol.clone(),
            side: side.clone(),
            entry_price,
            notional_usdt: notional,
            margin_usdt: margin,
            leverage,
            qty,
            open_ts,
            fair_at_open: fair,
            sigma_at_open: sigma,
            contract_size,
            quote_ts: existing
                .as_ref()
                .map(|p| p.quote_ts)
                .or_else(|| pending.as_ref().map(|q| q.placed_ts))
                .unwrap_or(open_ts),
            signal_ts: existing
                .as_ref()
                .map(|p| p.signal_ts)
                .or_else(|| pending.as_ref().map(|q| q.signal_ts))
                .unwrap_or(open_ts),
            entry_latency_ms: existing
                .as_ref()
                .map(|p| p.entry_latency_ms)
                .or_else(|| {
                    pending
                        .as_ref()
                        .map(|q| (now - q.signal_ts).max(0.0) * 1000.0)
                })
                .unwrap_or(0.0),
            entry_algo: existing
                .as_ref()
                .and_then(|p| p.entry_algo.clone())
                .or_else(|| pending.as_ref().and_then(|q| q.entry_algo.clone()))
                .or_else(|| Some("mexc_sync".to_string())),
            entry_score: existing
                .as_ref()
                .map(|p| p.entry_score)
                .or_else(|| pending.as_ref().map(|q| q.entry_score))
                .unwrap_or(0.0),
            max_hold_sec: existing
                .as_ref()
                .map(|p| p.max_hold_sec)
                .filter(|v| *v > 0.0)
                .unwrap_or_else(|| Self::max_hold_for(cfg, &symbol)),
            entry_fill_ratio: existing
                .as_ref()
                .and_then(|p| p.entry_fill_ratio)
                .or_else(|| {
                    pending.as_ref().and_then(|q| {
                        if q.qty > 0.0 {
                            Some((qty / q.qty).clamp(0.0, 1.0))
                        } else {
                            q.fill_ratio
                        }
                    })
                }),
            entry_levels_eaten: existing
                .as_ref()
                .and_then(|p| p.entry_levels_eaten)
                .or_else(|| pending.as_ref().and_then(|q| q.levels_eaten)),
            entry_spread_bps: existing
                .as_ref()
                .and_then(|p| p.entry_spread_bps)
                .or_else(|| {
                    (fair > 0.0).then(|| {
                        if side == "LONG" {
                            (entry_price - fair) / fair * 1e4
                        } else {
                            (fair - entry_price) / fair * 1e4
                        }
                    })
                })
                .or_else(|| pending.as_ref().and_then(|q| q.spread_bps_at_quote)),
            entry_ofi: existing.as_ref().and_then(|p| p.entry_ofi),
            entry_imbalance: existing.as_ref().and_then(|p| p.entry_imbalance),
            entry_fv1: existing.as_ref().and_then(|p| p.entry_fv1),
            entry_fv5: existing.as_ref().and_then(|p| p.entry_fv5),
            entry_fv30: existing.as_ref().and_then(|p| p.entry_fv30),
            entry_mexc_book_age_ms: existing.as_ref().and_then(|p| p.entry_mexc_book_age_ms),
            entry_binance_book_age_ms: existing.as_ref().and_then(|p| p.entry_binance_book_age_ms),
            stop_price,
            tp_price,
            best_excursion: existing
                .as_ref()
                .and_then(|p| p.best_excursion)
                .or(Some(entry_price)),
            best_realized_bps: existing
                .as_ref()
                .map(|p| p.best_realized_bps)
                .unwrap_or(0.0),
            last_sl_update_ts: existing
                .as_ref()
                .map(|p| p.last_sl_update_ts)
                .unwrap_or(0.0),
            initial_sl_distance: Some(initial_sl_distance),
            last_pnl_usdt: existing.as_ref().map(|p| p.last_pnl_usdt).unwrap_or(0.0),
            last_pnl_pct: existing.as_ref().map(|p| p.last_pnl_pct).unwrap_or(0.0),
            mexc_position_id: position_id
                .or_else(|| existing.as_ref().and_then(|p| p.mexc_position_id)),
            mexc_stop_plan_id: existing.as_ref().and_then(|p| p.mexc_stop_plan_id),
            mexc_entry_order_id: existing
                .as_ref()
                .and_then(|p| p.mexc_entry_order_id)
                .or_else(|| pending.as_ref().and_then(|q| q.real_order_id)),
            closed: false,
            close_reason: None,
            close_ts: 0.0,
            close_price: None,
            realized_pnl: 0.0,
            exit_signal_ts: existing.as_ref().map(|p| p.exit_signal_ts).unwrap_or(0.0),
            exit_latency_ms: existing.as_ref().map(|p| p.exit_latency_ms).unwrap_or(0.0),
            settled_profit_since: existing
                .as_ref()
                .map(|p| p.settled_profit_since)
                .unwrap_or(0.0),
            settled_profit_anchor_bps: existing
                .as_ref()
                .map(|p| p.settled_profit_anchor_bps)
                .unwrap_or(0.0),
        })
    }

    fn update_trailing_stop(&self, cfg: &AppConfig, pos: &mut ManagedPosition) {
        let best = pos.best_excursion.unwrap_or(pos.entry_price);
        let hard_sl = hard_sl_price_fraction(cfg, pos.leverage);
        if cfg.strategy.use_r_trail {
            let r = pos.initial_sl_distance.unwrap_or(0.0);
            if pos.side == "LONG" {
                pos.stop_price = Some(r_trail_long(
                    pos.entry_price,
                    best,
                    r,
                    cfg.strategy.trail_breakeven_r,
                    cfg.strategy.trail_lock_r,
                    cfg.strategy.trail_dist_r,
                    pos.stop_price,
                ));
            } else {
                pos.stop_price = Some(r_trail_short(
                    pos.entry_price,
                    best,
                    r,
                    cfg.strategy.trail_breakeven_r,
                    cfg.strategy.trail_lock_r,
                    cfg.strategy.trail_dist_r,
                    pos.stop_price,
                ));
            }
        } else if pos.side == "LONG" {
            pos.stop_price = Some(trail_long(
                pos.entry_price,
                best,
                pos.sigma_at_open,
                hard_sl,
                cfg.strategy.breakeven_at_sigma,
                cfg.strategy.trail_dist_sigma,
                pos.stop_price,
            ));
        } else {
            pos.stop_price = Some(trail_short(
                pos.entry_price,
                best,
                pos.sigma_at_open,
                hard_sl,
                cfg.strategy.breakeven_at_sigma,
                cfg.strategy.trail_dist_sigma,
                pos.stop_price,
            ));
        }
    }

    async fn resolve_history_close(
        &self,
        pos: &ManagedPosition,
        fallback_exit_price: f64,
    ) -> Option<CloseResolution> {
        let now = now_ts();
        let start_ms = ((pos.open_ts - 300.0).max(0.0) * 1000.0) as i64;
        let end_ms = ((now + 30.0) * 1000.0) as i64;
        let mut best: Option<(f64, Value)> = None;

        for attempt in 0..4 {
            if attempt > 0 {
                sleep(Duration::from_millis(120)).await;
            }
            let rows = self
                .trader
                .get_history_positions_window(Some(&pos.symbol), Some(start_ms), Some(end_ms), 100)
                .await;
            for row in rows {
                let Some(score) = history_match_score(pos, &row, now) else {
                    continue;
                };
                if best
                    .as_ref()
                    .is_none_or(|(best_score, _)| score < *best_score)
                {
                    best = Some((score, row));
                }
            }
            if best.is_some() {
                break;
            }
        }

        let (_, row) = best?;
        let realized = history_realized(&row).unwrap_or_else(|| {
            let price = history_price(&row, &["closeAvgPrice", "dealAvgPrice"])
                .unwrap_or(fallback_exit_price);
            realized_usdt_at_price(pos, price)
        });
        let exit_price = history_price(&row, &["closeAvgPrice", "dealAvgPrice"])
            .or_else(|| realized_to_exit_price(pos, realized))
            .filter(|v| *v > 0.0)
            .unwrap_or(fallback_exit_price);
        let close_ts = time_sec(first_num(&row, &["updateTime", "closeTime"])).unwrap_or(now);
        Some(CloseResolution {
            exit_price,
            realized_pnl: realized,
            close_ts,
            price_source: "exchange_history",
            position_id: position_id(&row).or(pos.mexc_position_id),
        })
    }

    async fn close_position(
        &self,
        mut pos: ManagedPosition,
        exit_price: f64,
        reason: &str,
    ) -> anyhow::Result<()> {
        let cfg = self.cfg.lock().await.clone();
        let mut actual_exit_price = exit_price;
        let mut close_order_id = None;
        let mut price_source: &'static str = if cfg.mode == "real" {
            "exchange_fallback"
        } else {
            "paper_book"
        };
        let mut history_position_id = pos.mexc_position_id;
        if cfg.mode == "real" {
            let mut resp = Value::Null;
            for attempt in 0..3 {
                resp = self
                    .trader
                    .close_market(&pos.symbol, &pos.side, pos.qty, pos.leverage as i64)
                    .await;
                if resp.get("success").and_then(Value::as_bool) == Some(true) {
                    break;
                }
                if attempt >= 2 || !is_retryable_close_reject(&resp) {
                    break;
                }
                let backoff = 120 * (attempt + 1);
                self.state
                    .add_log(
                        "warn",
                        format!(
                            "real close retry {}/2 {}: {}",
                            attempt + 1,
                            pos.symbol,
                            resp
                        ),
                    )
                    .await;
                sleep(Duration::from_millis(backoff as u64)).await;
            }
            if resp.get("success").and_then(Value::as_bool) != Some(true) {
                let code = resp.get("code").and_then(num_as_f64).unwrap_or(0.0) as i64;
                {
                    let mut st = self.state.write().await;
                    st.positions.insert(pos.symbol.clone(), pos.clone());
                    st.cooldown_until.insert(
                        pos.symbol.clone(),
                        now_ts() + if code == 510 { 5.0 } else { 1.0 },
                    );
                }
                let _ = self
                    .store
                    .upsert_managed_position(&cfg.mode, &pos.symbol, &serde_json::to_value(&pos)?)
                    .await;
                self.state
                    .add_log(
                        "warn",
                        format!("real close failed {}: {}", pos.symbol, resp),
                    )
                    .await;
                return Ok(());
            }
            close_order_id = extract_order_id(&resp);
            if let Some(order_id) = close_order_id {
                for _ in 0..4 {
                    let query = self.trader.query_order(order_id).await;
                    if let Some(avg) = order_avg_price(&query) {
                        actual_exit_price = avg;
                        price_source = "order_avg";
                        break;
                    }
                    sleep(Duration::from_millis(50)).await;
                }
            }
            if price_source == "exchange_fallback" {
                price_source = "book_fallback";
            }
        }
        let mut close_ts = now_ts();
        let mut pnl = realized_usdt_at_price(&pos, actual_exit_price);
        if cfg.mode == "real" {
            if let Some(resolved) = self.resolve_history_close(&pos, actual_exit_price).await {
                actual_exit_price = resolved.exit_price;
                pnl = resolved.realized_pnl;
                close_ts = resolved.close_ts;
                price_source = resolved.price_source;
                history_position_id = resolved.position_id;
            }
        }
        if pos.exit_signal_ts > 0.0 {
            pos.exit_latency_ms = (close_ts - pos.exit_signal_ts).max(0.0) * 1000.0;
        }
        pos.closed = true;
        pos.close_reason = Some(reason.to_string());
        pos.close_ts = close_ts;
        pos.close_price = Some(actual_exit_price);
        pos.realized_pnl = pnl;
        pos.last_pnl_usdt = pnl;
        pos.last_pnl_pct = if pos.margin_usdt > 0.0 {
            pnl / pos.margin_usdt * 100.0
        } else {
            0.0
        };
        {
            let mut st = self.state.write().await;
            st.positions.remove(&pos.symbol);
            if cfg.mode == "real" {
                st.available_balance += pos.margin_usdt.max(0.0);
            } else {
                st.balance += pnl;
                st.available_balance += pos.margin_usdt + pnl;
                st.session_peak_balance = st.session_peak_balance.max(st.balance);
            }
            st.strategy_realized_pnl += pnl;
            st.strategy_session_peak_balance = st
                .strategy_session_peak_balance
                .max(st.strategy_session_starting_balance + st.strategy_realized_pnl);
            let cd_min = Self::cooldown_min_for(&cfg, &pos.symbol);
            let cd_max = Self::cooldown_max_for(&cfg, &pos.symbol);
            let cd = if cd_max > cd_min {
                (cd_min + cd_max) / 2.0
            } else {
                cd_min
            };
            st.cooldown_until
                .insert(pos.symbol.clone(), close_ts + cd.max(0.0));
            let item = json!({
                "ts": close_ts,
                "symbol": pos.symbol.clone(),
                "side": pos.side.clone(),
                "entry": pos.entry_price,
                "exit": actual_exit_price,
                "pnl": pnl,
                "pnl_pct": pos.last_pnl_pct,
                "reason": reason,
                "duration": close_ts - pos.open_ts,
                "entry_latency_ms": pos.entry_latency_ms,
                "exit_latency_ms": pos.exit_latency_ms,
                "entry_algo": pos.entry_algo.clone(),
                "entry_score": pos.entry_score,
                "entry_fill_ratio": pos.entry_fill_ratio,
                "entry_spread_bps": pos.entry_spread_bps,
                "price_source": price_source,
            });
            if st.recent_trades.len() >= 200 {
                st.recent_trades.pop_front();
            }
            st.recent_trades.push_back(item);
        }
        let trade_extra = json!({
            "entry_latency_ms": pos.entry_latency_ms,
            "exit_latency_ms": pos.exit_latency_ms,
            "entry_algo": pos.entry_algo.clone(),
            "entry_score": pos.entry_score,
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
            "best_excursion": pos.best_excursion,
            "best_excursion_bps": best_excursion_bps(&pos),
            "realized_bps": realized_bps_at_price(&pos, actual_exit_price),
            "price_source": price_source,
            "close_order_id": close_order_id,
            "history_position_id": history_position_id,
        });
        let trade = json!({
            "ts": close_ts,
            "mode": cfg.mode.clone(),
            "symbol": pos.symbol.clone(),
            "side": pos.side.clone(),
            "entry": pos.entry_price,
            "exit": actual_exit_price,
            "qty": pos.qty,
            "notional": pos.notional_usdt,
            "margin": pos.margin_usdt,
            "leverage": pos.leverage,
            "open_ts": pos.open_ts,
            "close_ts": close_ts,
            "duration_sec": close_ts - pos.open_ts,
            "pnl_usdt": pnl,
            "pnl_pct": pos.last_pnl_pct,
            "fair_at_open": pos.fair_at_open,
            "sigma_at_open": pos.sigma_at_open,
            "z_at_open": 0.0,
            "close_reason": reason,
            "entry_latency_sec": pos.entry_latency_ms / 1000.0,
            "extra": trade_extra,
        });
        self.store.insert_trade(&trade).await?;
        let _ = self
            .store
            .delete_managed_position(&cfg.mode, &pos.symbol)
            .await;
        self.state
            .add_log(
                if pnl >= 0.0 { "info" } else { "warn" },
                format!(
                    "CLOSE {} {} pnl={:.4} reason={} source={}",
                    pos.symbol, pos.side, pnl, reason, price_source
                ),
            )
            .await;
        if cfg.mode == "real" {
            self.refresh_real_balance_force().await;
        }
        Ok(())
    }

    async fn free_balance(&self) -> f64 {
        let is_real = self.cfg.lock().await.mode == "real";
        let st = self.state.read().await;
        if is_real {
            return st.available_balance.max(0.0);
        }
        let locked: f64 = st.positions.values().map(|p| p.margin_usdt).sum();
        (st.available_balance - locked).max(0.0)
    }

    async fn log_equity_periodically(&self) -> anyhow::Result<()> {
        let now = now_ts();
        let cfg = self.cfg.lock().await.clone();
        let mut st = self.state.write().await;
        let last = st
            .equity_history
            .back()
            .and_then(|v| v.get("ts"))
            .and_then(Value::as_f64)
            .unwrap_or(0.0);
        if now - last < 2.0 {
            return Ok(());
        }
        let open_pnl: f64 = st.positions.values().map(|p| p.last_pnl_usdt).sum();
        let equity = st.balance + open_pnl;
        let item = json!({"ts": now, "balance": st.balance, "equity": equity, "open_positions": st.positions.len()});
        if st.equity_history.len() >= 2000 {
            st.equity_history.pop_front();
        }
        st.equity_history.push_back(item.clone());
        if st.strategy_equity_history.len() >= 2000 {
            st.strategy_equity_history.pop_front();
        }
        st.strategy_equity_history.push_back(item.clone());
        drop(st);
        self.store
            .insert_equity(
                now,
                &cfg.mode,
                item["balance"].as_f64().unwrap_or(0.0),
                item["equity"].as_f64().unwrap_or(0.0),
                item["open_positions"].as_u64().unwrap_or(0) as usize,
            )
            .await?;
        Ok(())
    }

    async fn check_kill_switch(&self) -> anyhow::Result<()> {
        let cfg = self.cfg.lock().await.clone();
        let mut st = self.state.write().await;
        if st.kill_switch || st.day_start_balance <= 0.0 || st.session_peak_balance <= 0.0 {
            return Ok(());
        }
        let daily_dd = (st.day_start_balance - st.balance) / st.day_start_balance;
        if daily_dd >= cfg.risk.daily_loss_pct_kill {
            st.kill_switch = true;
            st.last_kill_reason = format!("daily_loss {:.2}%", daily_dd * 100.0);
        }
        let peak_dd = (st.session_peak_balance - st.balance) / st.session_peak_balance;
        if peak_dd >= cfg.risk.max_drawdown_pct_kill {
            st.kill_switch = true;
            st.last_kill_reason = format!("max_drawdown {:.2}%", peak_dd * 100.0);
        }
        Ok(())
    }

    async fn refresh_real_balance(&self) {
        let now = now_ts();
        {
            let mut last = self.last_balance_refresh_ts.lock().await;
            if now - *last < 2.0 {
                return;
            }
            *last = now;
        }
        self.refresh_real_balance_force().await;
    }

    async fn refresh_real_balance_force(&self) {
        let (available, equity) = self.trader.get_usdt_balance_snapshot().await;
        if equity > 0.0 || available > 0.0 {
            let mut st = self.state.write().await;
            st.available_balance = available;
            st.balance = equity;
            st.session_peak_balance = st.session_peak_balance.max(equity);
        }
    }

    pub async fn kill_all(&self) -> Value {
        {
            let mut st = self.state.write().await;
            st.engine_running = false;
            st.kill_switch = true;
            st.last_kill_reason = "manual".to_string();
        }
        let cfg = self.cfg.lock().await.clone();
        self.quotes.lock().await.clear();
        if cfg.mode == "real" {
            let positions = self.trader.get_positions_raw().await;
            let mut close_sent = 0_i64;
            let mut close_failed = 0_i64;
            for p in positions {
                let sym = p
                    .get("symbol")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                let side = match p.get("positionType").and_then(num_as_f64).unwrap_or(0.0) as i64 {
                    1 => "LONG",
                    2 => "SHORT",
                    _ => "",
                };
                let hold = p.get("holdVol").and_then(num_as_f64).unwrap_or(0.0);
                let lev = p.get("leverage").and_then(num_as_f64).unwrap_or(1.0) as i64;
                if !sym.is_empty() && !side.is_empty() && hold > 0.0 {
                    let resp = self.trader.close_market(&sym, side, hold, lev).await;
                    if resp.get("success").and_then(Value::as_bool) == Some(true) {
                        close_sent += 1;
                    } else {
                        close_failed += 1;
                        self.state
                            .add_log("warn", format!("kill close failed {sym}: {resp}"))
                            .await;
                    }
                    let _ = self.trader.cancel_all_for(&sym).await;
                }
            }
            sleep(Duration::from_millis(700)).await;
            let _ = self.sync_real_positions(true).await;
            self.refresh_real_balance_force().await;
            self.state
                .add_log(
                    "warn",
                    format!(
                        "KILL ALL invoked; exchange_positions={} close_sent={} failed={}",
                        close_sent + close_failed,
                        close_sent,
                        close_failed
                    ),
                )
                .await;
        } else {
            let positions: Vec<_> = self
                .state
                .read()
                .await
                .positions
                .values()
                .cloned()
                .collect();
            for pos in positions {
                if let Some(book) = self.agg.lock().await.get_book(&pos.symbol) {
                    let price = realisable_exit_price(&pos, &book);
                    let _ = self.close_position(pos, price, "manual_kill").await;
                }
            }
            self.state.add_log("warn", "KILL ALL invoked").await;
        }
        json!({"success": true})
    }
}

fn first_num(row: &Value, keys: &[&str]) -> Option<f64> {
    keys.iter()
        .find_map(|key| row.get(*key).and_then(num_as_f64))
}

fn time_sec(value: Option<f64>) -> Option<f64> {
    value.map(|v| if v > 10_000_000_000.0 { v / 1000.0 } else { v })
}

fn position_side(row: &Value) -> Option<&'static str> {
    match first_num(
        row,
        &["positionType", "position_type", "positionSide", "side"],
    )
    .unwrap_or(0.0) as i64
    {
        1 => Some("LONG"),
        2 => Some("SHORT"),
        _ => None,
    }
}

fn position_id(row: &Value) -> Option<i64> {
    first_num(row, &["positionId", "positionID", "position_id", "id"]).map(|v| v as i64)
}

fn history_realized(row: &Value) -> Option<f64> {
    first_num(row, &["realised", "realized", "profit", "pnl"])
}

fn history_price(row: &Value, keys: &[&str]) -> Option<f64> {
    first_num(row, keys).filter(|v| *v > 0.0)
}

fn realized_to_exit_price(pos: &ManagedPosition, realized: f64) -> Option<f64> {
    let denom = pos.qty * pos.contract_size;
    if pos.entry_price <= 0.0 || denom <= 0.0 {
        return None;
    }
    if pos.side == "LONG" {
        Some(pos.entry_price + realized / denom)
    } else {
        Some(pos.entry_price - realized / denom)
    }
}

fn realized_usdt_at_price(pos: &ManagedPosition, exit_price: f64) -> f64 {
    if pos.side == "LONG" {
        (exit_price - pos.entry_price) * pos.qty * pos.contract_size
    } else {
        (pos.entry_price - exit_price) * pos.qty * pos.contract_size
    }
}

fn best_excursion_bps(pos: &ManagedPosition) -> Option<f64> {
    let best = pos.best_excursion?;
    if pos.entry_price <= 0.0 {
        return None;
    }
    if pos.side == "LONG" {
        Some((best - pos.entry_price) / pos.entry_price * 1e4)
    } else {
        Some((pos.entry_price - best) / pos.entry_price * 1e4)
    }
}

fn history_match_score(pos: &ManagedPosition, row: &Value, now: f64) -> Option<f64> {
    let row_symbol = row
        .get("symbol")
        .and_then(Value::as_str)
        .map(|s| s.to_ascii_uppercase())?;
    if row_symbol != pos.symbol {
        return None;
    }
    if position_side(row)? != pos.side {
        return None;
    }
    let row_state = first_num(row, &["state"]).unwrap_or(0.0) as i64;
    if row_state != 0 && row_state != 3 {
        return None;
    }

    let mut score = 0.0;
    if let (Some(pos_id), Some(row_id)) = (pos.mexc_position_id, position_id(row)) {
        if pos_id == row_id {
            score -= 1_000_000.0;
        } else {
            score += 1_000.0;
        }
    }
    if let Some(close_ts) = time_sec(first_num(row, &["updateTime", "closeTime"])) {
        score += (close_ts - now).abs().min(600.0);
    }
    if let Some(open_ts) = time_sec(first_num(row, &["createTime", "openTime"])) {
        if pos.open_ts > 0.0 {
            score += (open_ts - pos.open_ts).abs().min(300.0) * 0.1;
        }
    }
    if let Some(row_entry) = history_price(row, &["openAvgPrice", "holdAvgPrice"]) {
        if pos.entry_price > 0.0 {
            score += ((row_entry - pos.entry_price).abs() / pos.entry_price * 10_000.0).min(500.0);
        }
    }
    if let Some(row_vol) = first_num(row, &["closeVol", "holdVol"]) {
        if row_vol > 0.0 && pos.qty > 0.0 {
            score += ((row_vol - pos.qty).abs() / pos.qty * 100.0).min(200.0);
        }
    }
    Some(score)
}

fn is_retryable_close_reject(res: &Value) -> bool {
    let code = res.get("code").and_then(num_as_f64).unwrap_or(0.0) as i64;
    if [429, 500, 502, 503, 504, 510].contains(&code) {
        return true;
    }
    let msg = res
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    msg.contains("too frequent")
        || msg.contains("too many requests")
        || msg.contains("temporarily unavailable")
        || msg.contains("timeout")
        || msg.contains("timed out")
}

fn residual_edge_bps(pos: &ManagedPosition, fair: Option<f64>, exit_price: f64) -> Option<f64> {
    let fair = fair?;
    if fair <= 0.0 || exit_price <= 0.0 {
        return None;
    }
    if pos.side == "LONG" {
        Some((fair - exit_price) / fair * 1e4)
    } else {
        Some((exit_price - fair) / fair * 1e4)
    }
}

fn should_profit_protect_exit(
    current_bps: f64,
    best_bps: f64,
    residual_edge_bps: Option<f64>,
    arm_bps: f64,
    giveback_bps: f64,
    fast_arm_bps: f64,
    fast_giveback_bps: f64,
    min_profit_bps: f64,
    edge_collapse_bps: f64,
) -> bool {
    let Some(edge) = residual_edge_bps else {
        return false;
    };
    if arm_bps <= 0.0
        || giveback_bps <= 0.0
        || best_bps < arm_bps
        || current_bps < min_profit_bps
        || edge > edge_collapse_bps
    {
        return false;
    }
    let active_giveback =
        if fast_arm_bps > 0.0 && best_bps >= fast_arm_bps && fast_giveback_bps > 0.0 {
            fast_giveback_bps
        } else {
            giveback_bps
        };
    let floor_bps = min_profit_bps.max(best_bps - active_giveback);
    current_bps <= floor_bps
}

fn update_settled_profit_state(
    pos: &mut ManagedPosition,
    now: f64,
    current_bps: f64,
    residual_edge_bps: Option<f64>,
    hold_sec: f64,
    min_bps: f64,
    max_drift_bps: f64,
    edge_bps: f64,
) -> bool {
    let Some(edge) = residual_edge_bps else {
        pos.settled_profit_since = 0.0;
        pos.settled_profit_anchor_bps = 0.0;
        return false;
    };
    if hold_sec <= 0.0
        || min_bps <= 0.0
        || max_drift_bps < 0.0
        || edge_bps <= 0.0
        || current_bps < min_bps
        || edge > edge_bps
    {
        pos.settled_profit_since = 0.0;
        pos.settled_profit_anchor_bps = 0.0;
        return false;
    }
    if pos.settled_profit_since <= 0.0
        || (current_bps - pos.settled_profit_anchor_bps).abs() > max_drift_bps
    {
        pos.settled_profit_since = now;
        pos.settled_profit_anchor_bps = current_bps;
        return false;
    }
    now - pos.settled_profit_since >= hold_sec
}

fn should_bad_entry_exit(
    pos: &ManagedPosition,
    age_sec: f64,
    current_bps: f64,
    residual_edge_bps: Option<f64>,
    guard_sec: f64,
    min_age_sec: f64,
    bad_entry_spread_bps: f64,
    exit_bps: f64,
    edge_collapse_bps: f64,
) -> bool {
    let Some(entry_spread) = pos.entry_spread_bps else {
        return false;
    };
    let Some(edge) = residual_edge_bps else {
        return false;
    };
    if guard_sec <= 0.0
        || bad_entry_spread_bps <= 0.0
        || entry_spread < bad_entry_spread_bps
        || edge > edge_collapse_bps
        || age_sec < min_age_sec.max(0.0)
        || age_sec > guard_sec
    {
        return false;
    }
    current_bps <= exit_bps
}

fn hard_sl_price_fraction(cfg: &AppConfig, leverage: f64) -> f64 {
    if cfg.strategy.hard_sl_margin_pct > 0.0 {
        cfg.strategy.hard_sl_margin_pct / 100.0 / leverage.max(1.0)
    } else {
        cfg.strategy.hard_sl_pct.max(0.0)
    }
}

fn trail_long(
    entry: f64,
    best: f64,
    sigma: f64,
    hard_sl_pct: f64,
    breakeven_at_sigma: f64,
    trail_dist_sigma: f64,
    prev_stop: Option<f64>,
) -> f64 {
    let profit = (best - entry).max(0.0);
    let sl = if sigma <= 0.0 || profit < breakeven_at_sigma * sigma {
        prev_stop.unwrap_or(entry * (1.0 - hard_sl_pct.max(0.0)))
    } else {
        entry.max(best - trail_dist_sigma * sigma)
    };
    prev_stop.map(|p| sl.max(p)).unwrap_or(sl)
}

fn trail_short(
    entry: f64,
    best: f64,
    sigma: f64,
    hard_sl_pct: f64,
    breakeven_at_sigma: f64,
    trail_dist_sigma: f64,
    prev_stop: Option<f64>,
) -> f64 {
    let profit = (entry - best).max(0.0);
    let sl = if sigma <= 0.0 || profit < breakeven_at_sigma * sigma {
        prev_stop.unwrap_or(entry * (1.0 + hard_sl_pct.max(0.0)))
    } else {
        entry.min(best + trail_dist_sigma * sigma)
    };
    prev_stop.map(|p| sl.min(p)).unwrap_or(sl)
}

fn r_trail_long(
    entry: f64,
    best: f64,
    r: f64,
    breakeven_r: f64,
    lock_r: f64,
    trail_r: f64,
    prev_stop: Option<f64>,
) -> f64 {
    if r <= 0.0 {
        return prev_stop.unwrap_or(entry);
    }
    let p = (best - entry).max(0.0) / r;
    let sl = if p < breakeven_r {
        prev_stop.unwrap_or(entry - r)
    } else if p < lock_r {
        entry
    } else {
        (entry + 0.5 * r).max(best - trail_r * r)
    };
    prev_stop.map(|x| sl.max(x)).unwrap_or(sl)
}

fn r_trail_short(
    entry: f64,
    best: f64,
    r: f64,
    breakeven_r: f64,
    lock_r: f64,
    trail_r: f64,
    prev_stop: Option<f64>,
) -> f64 {
    if r <= 0.0 {
        return prev_stop.unwrap_or(entry);
    }
    let p = (entry - best).max(0.0) / r;
    let sl = if p < breakeven_r {
        prev_stop.unwrap_or(entry + r)
    } else if p < lock_r {
        entry
    } else {
        (entry - 0.5 * r).min(best + trail_r * r)
    };
    prev_stop.map(|x| sl.min(x)).unwrap_or(sl)
}

fn vwap_by_notional(
    levels: &[[f64; 2]],
    target: f64,
    contract_size: f64,
) -> (Option<f64>, f64, f64, usize) {
    if levels.is_empty() || target <= 0.0 {
        return (None, 0.0, 0.0, 0);
    }
    let mut total_value = 0.0;
    let mut total_qty = 0.0;
    let mut eaten = 0;
    for [p, q] in levels {
        if *p <= 0.0 || *q <= 0.0 {
            continue;
        }
        let level_notional = p * q * contract_size;
        if total_value + level_notional >= target {
            let remaining = target - total_value;
            let partial_qty = remaining / (p * contract_size);
            total_value += partial_qty * p * contract_size;
            total_qty += partial_qty;
            eaten += 1;
            return (
                Some(total_value / (total_qty * contract_size).max(1e-18)),
                total_qty,
                total_value,
                eaten,
            );
        }
        total_value += level_notional;
        total_qty += q;
        eaten += 1;
    }
    if total_qty <= 0.0 {
        (None, 0.0, 0.0, 0)
    } else {
        (
            Some(total_value / (total_qty * contract_size).max(1e-18)),
            total_qty,
            total_value,
            eaten,
        )
    }
}

fn vwap_by_notional_capped(
    levels: &[[f64; 2]],
    target: f64,
    side: &str,
    limit_price: f64,
    contract_size: f64,
) -> (Option<f64>, f64, f64, usize) {
    if levels.is_empty() || target <= 0.0 || limit_price <= 0.0 {
        return (None, 0.0, 0.0, 0);
    }
    let mut total_value = 0.0;
    let mut total_qty = 0.0;
    let mut eaten = 0;
    for [p, q] in levels {
        if *p <= 0.0 || *q <= 0.0 {
            continue;
        }
        if side == "LONG" && *p > limit_price {
            break;
        }
        if side == "SHORT" && *p < limit_price {
            break;
        }
        let level_notional = p * q * contract_size;
        if total_value + level_notional >= target {
            let remaining = target - total_value;
            let partial_qty = remaining / (p * contract_size);
            total_value += partial_qty * p * contract_size;
            total_qty += partial_qty;
            eaten += 1;
            return (
                Some(total_value / (total_qty * contract_size).max(1e-18)),
                total_qty,
                total_value,
                eaten,
            );
        }
        total_value += level_notional;
        total_qty += q;
        eaten += 1;
    }
    if total_qty <= 0.0 {
        (None, 0.0, 0.0, 0)
    } else {
        (
            Some(total_value / (total_qty * contract_size).max(1e-18)),
            total_qty,
            total_value,
            eaten,
        )
    }
}

fn vwap_by_qty(
    levels: &[[f64; 2]],
    qty_target: f64,
    contract_size: f64,
) -> (Option<f64>, f64, f64, usize) {
    if levels.is_empty() || qty_target <= 0.0 {
        return (None, 0.0, 0.0, 0);
    }
    let mut total_value = 0.0;
    let mut total_qty = 0.0;
    let mut eaten = 0;
    for [p, q] in levels {
        if *p <= 0.0 || *q <= 0.0 {
            continue;
        }
        if total_qty + q >= qty_target {
            let rem = qty_target - total_qty;
            total_value += rem * p * contract_size;
            total_qty += rem;
            eaten += 1;
            return (
                Some(total_value / (total_qty * contract_size).max(1e-18)),
                total_qty,
                total_value,
                eaten,
            );
        }
        total_value += p * q * contract_size;
        total_qty += q;
        eaten += 1;
    }
    if total_qty <= 0.0 {
        (None, 0.0, 0.0, 0)
    } else {
        (
            Some(total_value / (total_qty * contract_size).max(1e-18)),
            total_qty,
            total_value,
            eaten,
        )
    }
}

fn realisable_exit_price(pos: &ManagedPosition, book: &OrderBook) -> f64 {
    let levels = if pos.side == "LONG" {
        &book.bids
    } else {
        &book.asks
    };
    if let (Some(vwap), _, _, _) = vwap_by_qty(levels, pos.qty, pos.contract_size) {
        return vwap;
    }
    if pos.side == "LONG" {
        book.best_bid().unwrap_or(pos.entry_price)
    } else {
        book.best_ask().unwrap_or(pos.entry_price)
    }
}

fn realized_bps_at_price(pos: &ManagedPosition, exit_price: f64) -> f64 {
    if pos.entry_price <= 0.0 {
        return 0.0;
    }
    if pos.side == "LONG" {
        (exit_price - pos.entry_price) / pos.entry_price * 1e4
    } else {
        (pos.entry_price - exit_price) / pos.entry_price * 1e4
    }
}

fn order_avg_price(res: &Value) -> Option<f64> {
    let rows = match res.get("data") {
        Some(Value::Array(rows)) => rows.clone(),
        Some(Value::Object(_)) => vec![res.get("data").cloned().unwrap_or(Value::Null)],
        _ => Vec::new(),
    };
    rows.first().and_then(|row| {
        [
            "avgDealPrice",
            "dealAvgPrice",
            "avgPrice",
            "priceAvg",
            "dealPrice",
            "price",
        ]
        .iter()
        .find_map(|key| row.get(*key).and_then(num_as_f64).filter(|v| *v > 0.0))
    })
}
