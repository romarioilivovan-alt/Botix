use crate::config::AppConfig;
use crate::opportunity::Opportunity;
use crate::state::{StateInner, now_ts};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AllocationDecision {
    pub accept: bool,
    pub reason: String,
    pub notional_usdt: f64,
    pub margin_usdt: f64,
    pub leverage: i64,
}

#[derive(Debug, Clone)]
pub struct CapitalAllocator {
    cfg: AppConfig,
}

impl CapitalAllocator {
    pub fn new(cfg: AppConfig) -> Self {
        Self { cfg }
    }

    pub fn update_config(&mut self, cfg: AppConfig) {
        self.cfg = cfg;
    }

    #[allow(clippy::too_many_arguments)]
    pub fn decide(
        &self,
        opp: &Opportunity,
        state: &StateInner,
        balance_free: f64,
        max_leverage_for_symbol: Option<i64>,
        book_top_notional: f64,
        margin_pct_override: Option<f64>,
        leverage_override: Option<i64>,
        max_notional_override: Option<f64>,
    ) -> AllocationDecision {
        if state.kill_switch {
            return Self::reject("kill_switch");
        }
        if state.positions.contains_key(&opp.symbol) {
            return Self::reject("already_open");
        }
        let until = state
            .cooldown_until
            .get(&opp.symbol)
            .copied()
            .unwrap_or(0.0);
        if now_ts() < until {
            return Self::reject(format!("cooldown {:.1}s", until - now_ts()));
        }
        if state.positions.len() >= self.cfg.risk.max_concurrent_positions {
            return Self::reject("no_slots");
        }
        if balance_free < self.cfg.risk.min_balance_usdt {
            return Self::reject("low_balance");
        }

        let share_pct = self.cfg.risk.account_share_pct.clamp(0.0, 1.0);
        let usable_balance = balance_free * share_pct;
        let margin_pct = margin_pct_override.unwrap_or(self.cfg.risk.margin_pct_per_slot);
        let mut margin = usable_balance * margin_pct;
        if margin < self.cfg.risk.min_trade_margin_usdt {
            return Self::reject("tiny_margin");
        }

        let mut lev = if let Some(lev) = leverage_override {
            lev
        } else if self.cfg.risk.leverage_mode.eq_ignore_ascii_case("max") {
            max_leverage_for_symbol.unwrap_or(self.cfg.risk.fixed_leverage)
        } else {
            self.cfg.risk.fixed_leverage
        };
        if lev <= 0 {
            lev = 1;
        }

        let mut notional = margin * lev as f64;
        let depth_cap = self.cfg.risk.book_depth_consume_pct * book_top_notional.max(0.0);
        if depth_cap > 0.0 && notional > depth_cap {
            notional = depth_cap;
            margin = notional / lev as f64;
        }
        let global_cap = self.cfg.risk.max_notional_usdt.max(0.0);
        let symbol_cap = max_notional_override.unwrap_or(0.0).max(0.0);
        let notional_cap = match (global_cap > 0.0, symbol_cap > 0.0) {
            (true, true) => global_cap.min(symbol_cap),
            (true, false) => global_cap,
            (false, true) => symbol_cap,
            (false, false) => 0.0,
        };
        if notional_cap > 0.0 && notional > notional_cap {
            notional = notional_cap;
            margin = notional / lev as f64;
        }
        if notional <= 0.0 || margin <= 0.0 {
            return Self::reject("zero_size");
        }
        AllocationDecision {
            accept: true,
            reason: String::new(),
            notional_usdt: notional,
            margin_usdt: margin,
            leverage: lev,
        }
    }

    fn reject(reason: impl Into<String>) -> AllocationDecision {
        AllocationDecision {
            accept: false,
            reason: reason.into(),
            notional_usdt: 0.0,
            margin_usdt: 0.0,
            leverage: 1,
        }
    }
}
