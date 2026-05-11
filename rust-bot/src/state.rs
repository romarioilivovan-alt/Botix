use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{HashMap, VecDeque};
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::RwLock;

pub fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct OrderBook {
    pub bids: Vec<[f64; 2]>,
    pub asks: Vec<[f64; 2]>,
    pub ts: f64,
}

impl OrderBook {
    pub fn best_bid(&self) -> Option<f64> {
        self.bids.first().map(|x| x[0]).filter(|v| *v > 0.0)
    }

    pub fn best_ask(&self) -> Option<f64> {
        self.asks.first().map(|x| x[0]).filter(|v| *v > 0.0)
    }

    pub fn mid(&self) -> Option<f64> {
        let bid = self.best_bid()?;
        let ask = self.best_ask()?;
        Some((bid + ask) / 2.0)
    }

    pub fn top_notional(&self, levels: usize, contract_size: f64) -> f64 {
        let cs = contract_size.max(1e-18);
        self.bids
            .iter()
            .take(levels)
            .chain(self.asks.iter().take(levels))
            .filter_map(|lvl| {
                let p = lvl[0];
                let q = lvl[1];
                (p > 0.0 && q > 0.0).then_some(p * q * cs)
            })
            .sum()
    }

    pub fn side_levels(&self, side: &str) -> &Vec<[f64; 2]> {
        if side.eq_ignore_ascii_case("LONG") {
            &self.asks
        } else {
            &self.bids
        }
    }

    pub fn available_qty(&self, side: &str, levels: usize) -> f64 {
        self.side_levels(side)
            .iter()
            .take(levels.max(1))
            .map(|lvl| lvl[1].max(0.0))
            .sum()
    }

    pub fn inferred_tick_size(&self, levels: usize) -> f64 {
        let depth = levels.max(2);
        let mut gaps = Vec::new();
        for rows in [&self.bids, &self.asks] {
            for pair in rows.iter().take(depth).collect::<Vec<_>>().windows(2) {
                let gap = (pair[0][0] - pair[1][0]).abs();
                if gap > 0.0 {
                    gaps.push(gap);
                }
            }
        }
        gaps.into_iter()
            .fold(0.0, |acc, v| if acc == 0.0 { v } else { acc.min(v) })
    }

    pub fn path_hole_points(&self, side: &str, levels: usize, point_size: f64) -> f64 {
        let rows = self.side_levels(side);
        if rows.len() < 2 {
            return 0.0;
        }
        let tick = if point_size > 0.0 {
            point_size
        } else {
            self.inferred_tick_size(levels)
        };
        if tick <= 0.0 {
            return 0.0;
        }
        let long_side = side.eq_ignore_ascii_case("LONG");
        let mut worst: f64 = 0.0;
        for pair in rows
            .iter()
            .take(levels.max(2))
            .collect::<Vec<_>>()
            .windows(2)
        {
            let p1 = pair[0][0];
            let p2 = pair[1][0];
            let gap = if long_side { p2 - p1 } else { p1 - p2 };
            worst = worst.max((gap / tick).max(0.0));
        }
        worst
    }

    pub fn level_shape_ratio(&self, side: &str, levels: usize) -> f64 {
        let rows = self.side_levels(side);
        let Some(top) = rows.first() else {
            return 0.0;
        };
        let top_qty = top[1].max(1e-18);
        let total: f64 = rows
            .iter()
            .take(levels.max(1))
            .map(|lvl| lvl[1].max(0.0))
            .sum();
        total / top_qty
    }

    pub fn support_ratio(&self, side: &str, levels: usize) -> f64 {
        let path_qty = self.available_qty(side, levels).max(1e-18);
        let support_side = if side.eq_ignore_ascii_case("LONG") {
            "SHORT"
        } else {
            "LONG"
        };
        self.available_qty(support_side, levels) / path_qty
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct SymbolStats {
    pub fair: Option<f64>,
    pub mexc_mid: Option<f64>,
    pub spread: Option<f64>,
    pub spread_bps: Option<f64>,
    pub z_score: Option<f64>,
    pub sigma_spread: Option<f64>,
    pub ofi: Option<f64>,
    pub fair_velocity_bps_per_sec: Option<f64>,
    pub mexc_book_top10_notional: Option<f64>,
    pub mexc_book_age_ms: Option<f64>,
    pub binance_book_age_ms: Option<f64>,
    pub mexc_book_imbalance: Option<f64>,
    pub mexc_spread_bps: Option<f64>,
    pub mexc_spread_bps_avg: Option<f64>,
    pub long_path_hole_points: Option<f64>,
    pub short_path_hole_points: Option<f64>,
    pub long_path_shape: Option<f64>,
    pub short_path_shape: Option<f64>,
    pub long_support_ratio: Option<f64>,
    pub short_support_ratio: Option<f64>,
    pub long_support_shape: Option<f64>,
    pub short_support_shape: Option<f64>,
    pub long_back_hole_points: Option<f64>,
    pub short_back_hole_points: Option<f64>,
    pub binance_burst_usdt_1s: Option<f64>,
    pub fair_velocity_5s_bps: Option<f64>,
    pub fair_velocity_30s_bps: Option<f64>,
    pub mexc_mid_mean_60s: Option<f64>,
    pub mexc_mid_std_60s: Option<f64>,
    pub mexc_mid_z_60s: Option<f64>,
    pub score: f64,
    pub side_hint: Option<String>,
    pub blocked_reason: Option<String>,
    pub selected_algorithm: Option<String>,
    pub last_update_ts: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManagedPosition {
    pub symbol: String,
    pub side: String,
    pub entry_price: f64,
    pub notional_usdt: f64,
    pub margin_usdt: f64,
    pub leverage: f64,
    pub qty: f64,
    pub open_ts: f64,
    pub fair_at_open: f64,
    pub sigma_at_open: f64,
    pub contract_size: f64,
    pub quote_ts: f64,
    pub signal_ts: f64,
    pub entry_latency_ms: f64,
    pub entry_algo: Option<String>,
    pub entry_score: f64,
    pub max_hold_sec: f64,
    pub entry_fill_ratio: Option<f64>,
    pub entry_levels_eaten: Option<i64>,
    pub entry_spread_bps: Option<f64>,
    pub entry_ofi: Option<f64>,
    pub entry_imbalance: Option<f64>,
    pub entry_fv1: Option<f64>,
    pub entry_fv5: Option<f64>,
    pub entry_fv30: Option<f64>,
    pub entry_mexc_book_age_ms: Option<f64>,
    pub entry_binance_book_age_ms: Option<f64>,
    pub stop_price: Option<f64>,
    pub tp_price: Option<f64>,
    pub best_excursion: Option<f64>,
    pub best_realized_bps: f64,
    pub last_sl_update_ts: f64,
    pub initial_sl_distance: Option<f64>,
    pub last_pnl_usdt: f64,
    pub last_pnl_pct: f64,
    pub mexc_position_id: Option<i64>,
    pub mexc_stop_plan_id: Option<i64>,
    pub mexc_entry_order_id: Option<i64>,
    pub closed: bool,
    pub close_reason: Option<String>,
    pub close_ts: f64,
    pub close_price: Option<f64>,
    pub realized_pnl: f64,
    pub exit_signal_ts: f64,
    pub exit_latency_ms: f64,
    pub settled_profit_since: f64,
    pub settled_profit_anchor_bps: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TradeLogEntry {
    pub t: f64,
    pub level: String,
    pub msg: String,
}

#[derive(Debug, Default)]
pub struct StateInner {
    pub stats: HashMap<String, SymbolStats>,
    pub universe: Vec<String>,
    pub universe_refs: HashMap<String, String>,
    pub candidates: Vec<Value>,
    pub positions: HashMap<String, ManagedPosition>,
    pub cooldown_until: HashMap<String, f64>,
    pub balance: f64,
    pub available_balance: f64,
    pub equity_history: VecDeque<Value>,
    pub session_starting_balance: f64,
    pub session_peak_balance: f64,
    pub strategy_realized_pnl: f64,
    pub strategy_equity_history: VecDeque<Value>,
    pub strategy_session_starting_balance: f64,
    pub strategy_session_peak_balance: f64,
    pub day_start_ts: f64,
    pub day_start_balance: f64,
    pub recent_trades: VecDeque<Value>,
    pub engine_running: bool,
    pub engine_mode: String,
    pub kill_switch: bool,
    pub last_kill_reason: String,
    pub binance_ws_ok: bool,
    pub mexc_ws_ok: bool,
    pub mexc_auth_ok: Option<bool>,
    pub mexc_auth_msg: String,
    pub logs: VecDeque<TradeLogEntry>,
}

pub struct AppState {
    inner: RwLock<StateInner>,
}

impl AppState {
    pub fn new(mode: &str) -> Self {
        let mut inner = StateInner::default();
        inner.engine_mode = mode.to_string();
        Self {
            inner: RwLock::new(inner),
        }
    }

    pub async fn read(&self) -> tokio::sync::RwLockReadGuard<'_, StateInner> {
        self.inner.read().await
    }

    pub async fn write(&self) -> tokio::sync::RwLockWriteGuard<'_, StateInner> {
        self.inner.write().await
    }

    pub async fn add_log(&self, level: &str, msg: impl Into<String>) {
        let mut st = self.inner.write().await;
        if st.logs.len() >= 500 {
            st.logs.pop_front();
        }
        st.logs.push_back(TradeLogEntry {
            t: now_ts(),
            level: level.to_string(),
            msg: msg.into(),
        });
    }

    pub async fn snapshot(&self) -> Value {
        let st = self.inner.read().await;
        let positions: Vec<Value> = st
            .positions
            .values()
            .map(|p| {
                json!({
                    "symbol": p.symbol,
                    "side": p.side,
                    "entry": p.entry_price,
                    "qty": p.qty,
                    "notional": p.notional_usdt,
                    "margin": p.margin_usdt,
                    "lev": p.leverage,
                    "stop": p.stop_price,
                    "tp": p.tp_price,
                    "open_ts": p.open_ts,
                    "pnl": p.last_pnl_usdt,
                    "pnl_pct": p.last_pnl_pct,
                    "entry_latency_ms": p.entry_latency_ms,
                    "entry_algo": p.entry_algo,
                    "entry_score": p.entry_score,
                })
            })
            .collect();

        let stats_summary: serde_json::Map<String, Value> = st
            .stats
            .iter()
            .map(|(sym, s)| {
                (
                    sym.clone(),
                    json!({
                        "fair": s.fair,
                        "mexc_mid": s.mexc_mid,
                        "spread_bps": s.spread_bps,
                        "z": s.z_score,
                        "sigma": s.sigma_spread,
                        "ofi": s.ofi,
                        "fv": s.fair_velocity_bps_per_sec,
                        "fv5": s.fair_velocity_5s_bps,
                        "fv30": s.fair_velocity_30s_bps,
                        "depth": s.mexc_book_top10_notional,
                        "imbalance": s.mexc_book_imbalance,
                        "score": s.score,
                        "side": s.side_hint,
                        "blocked": s.blocked_reason,
                    }),
                )
            })
            .collect();

        let strategy_start = if st.strategy_session_starting_balance > 0.0 {
            st.strategy_session_starting_balance
        } else {
            st.session_starting_balance
        };
        let strategy_open_pnl: f64 = st.positions.values().map(|p| p.last_pnl_usdt).sum();
        let strategy_equity = strategy_start + st.strategy_realized_pnl + strategy_open_pnl;
        json!({
            "engine": {
                "running": st.engine_running,
                "mode": st.engine_mode,
                "kill": st.kill_switch,
                "kill_reason": st.last_kill_reason,
                "binance_ok": st.binance_ws_ok,
                "mexc_ok": st.mexc_ws_ok,
                "mexc_auth_ok": st.mexc_auth_ok,
                "mexc_auth_msg": st.mexc_auth_msg,
            },
            "balance": st.balance,
            "available_balance": st.available_balance,
            "session_starting_balance": st.session_starting_balance,
            "session_peak_balance": st.session_peak_balance,
            "account": {
                "equity": st.balance,
                "available_balance": st.available_balance,
                "session_starting_balance": st.session_starting_balance,
                "session_peak_balance": st.session_peak_balance,
            },
            "strategy": {
                "realized_pnl": st.strategy_realized_pnl,
                "open_pnl": strategy_open_pnl,
                "equity": strategy_equity,
                "session_starting_balance": strategy_start,
                "session_peak_balance": if st.strategy_session_peak_balance > 0.0 { st.strategy_session_peak_balance } else { strategy_start },
            },
            "universe_size": st.universe.len(),
            "candidates": st.candidates.iter().take(20).cloned().collect::<Vec<_>>(),
            "stats_summary": Value::Object(stats_summary),
            "positions": positions,
            "equity": st.equity_history.iter().rev().take(300).cloned().collect::<Vec<_>>().into_iter().rev().collect::<Vec<_>>(),
            "strategy_equity": st.strategy_equity_history.iter().rev().take(300).cloned().collect::<Vec<_>>().into_iter().rev().collect::<Vec<_>>(),
            "recent_trades": st.recent_trades.iter().rev().take(50).cloned().collect::<Vec<_>>().into_iter().rev().collect::<Vec<_>>(),
            "logs": st.logs.iter().rev().take(100).cloned().collect::<Vec<_>>().into_iter().rev().collect::<Vec<_>>(),
        })
    }
}
