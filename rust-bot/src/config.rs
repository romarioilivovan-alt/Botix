use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

pub const LEGACY_NVDA: &str = "NVDA_USDT";
pub const LEGACY_MSTR: &str = "MSTR_USDT";

pub fn normalize_symbol_name(symbol: &str) -> String {
    let mut s = symbol.trim().to_ascii_uppercase();
    if s.is_empty() {
        return s;
    }
    if !s.ends_with("_USDT") && !s.contains('_') {
        s = format!("{s}_USDT");
    }
    match s.as_str() {
        LEGACY_NVDA => "NVIDIA_USDT".to_string(),
        LEGACY_MSTR => "MSTRSTOCK_USDT".to_string(),
        _ => s,
    }
}

pub fn default_binance_symbol_overrides() -> HashMap<String, String> {
    [
        ("PEPE_USDT", "1000PEPEUSDT"),
        ("NVIDIA_USDT", "NVDAUSDT"),
        ("MSTRSTOCK_USDT", "MSTRUSDT"),
        ("TSLA_USDT", "TSLAUSDT"),
        ("INTC_USDT", "INTCUSDT"),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_string(), v.to_string()))
    .collect()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct MexcWebConfig {
    pub web_uid: String,
    pub device_id: String,
    pub mhash: String,
    pub proxy: Option<String>,
}

impl Default for MexcWebConfig {
    fn default() -> Self {
        Self {
            web_uid: String::new(),
            device_id: String::new(),
            mhash: String::new(),
            proxy: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct UniverseConfig {
    pub refresh_sec: u64,
    pub max_symbols: usize,
    pub require_binance_ref: bool,
    pub min_book_notional_usdt: f64,
    pub exclude: Vec<String>,
    pub include_only: Vec<String>,
    pub force_include_symbols: Vec<String>,
}

impl Default for UniverseConfig {
    fn default() -> Self {
        Self {
            refresh_sec: 300,
            max_symbols: 60,
            require_binance_ref: true,
            min_book_notional_usdt: 5_000.0,
            exclude: Vec::new(),
            include_only: Vec::new(),
            force_include_symbols: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct StrategyConfig {
    pub algorithm: String,
    pub invert: bool,
    pub entry_z: f64,
    pub max_entry_z: f64,
    pub cancel_z: f64,
    pub meanrev_min_spread_bps: f64,
    pub meanrev_require_book_alignment: bool,
    pub meanrev_min_imbalance_log: f64,
    pub momentum_min_velocity_bps_per_sec: f64,
    pub momentum_max_velocity_bps_per_sec: f64,
    pub momentum_require_lag: bool,
    pub ofi_min_usdt: f64,
    pub ofi_require_lag: bool,
    pub ofi_min_lag_bps: f64,
    pub ofi_max_chase_bps: f64,
    pub ofi_require_book_alignment: bool,
    pub ofi_min_imbalance_log: f64,
    pub imbalance_min_log: f64,
    pub imbalance_max_log: f64,
    pub imbalance_require_lag: bool,
    pub imbalance_min_lag_bps: f64,
    pub imbalance_max_chase_bps: f64,
    pub sweep_min_usdt_1s: f64,
    pub wide_spread_ratio: f64,
    pub wide_spread_min_bps: f64,
    pub wide_spread_min_imbalance: f64,
    pub raw_momentum_min_bps: f64,
    pub raw_momentum_max_bps: f64,
    pub raw_momentum_window_sec: f64,
    pub raw_momentum_require_5s_agree: bool,
    pub raw_momentum_anti_fade_30s_bps: f64,
    pub raw_momentum_require_lag: bool,
    pub raw_momentum_min_lag_bps: f64,
    pub raw_momentum_max_chase_bps: f64,
    pub raw_momentum_require_ofi_alignment: bool,
    pub raw_momentum_min_ofi_usdt: f64,
    pub raw_momentum_require_book_alignment: bool,
    pub raw_momentum_min_imbalance_log: f64,
    pub book_lean_min_ratio: f64,
    pub bb_revert_z_entry: f64,
    pub bb_revert_z_exit: f64,
    pub bb_revert_z_max: f64,
    pub confluence_min_velocity_bps: f64,
    pub confluence_max_velocity_bps: f64,
    pub confluence_min_ofi_usdt: f64,
    pub confluence_min_imbalance_log: f64,
    pub confluence_require_lag: bool,
    pub confluence_min_lag_bps: f64,
    pub confluence_max_chase_bps: f64,
    pub taker_entry: bool,
    pub real_require_zero_fee: bool,
    pub entry_latency_ms: u64,
    pub signal_max_age_ms: u64,
    pub pre_submit_max_spread_drift_bps: f64,
    pub micro_levels: i64,
    pub micro_path_hole_max_points: f64,
    pub micro_support_ratio_min: f64,
    pub micro_support_shape_min: f64,
    pub micro_path_shape_min: f64,
    pub micro_back_hole_max_points: f64,
    pub exit_latency_ms: u64,
    pub paper_tick_sec: f64,
    pub taker_fee_bps: f64,
    pub maker_fee_bps: f64,
    pub taker_ioc_simulation: bool,
    pub taker_ioc_price_buffer_bps: f64,
    pub taker_ioc_min_fill_ratio: f64,
    pub quote_timeout_sec: f64,
    pub quote_offset_ticks: i64,
    pub min_spread_samples: usize,
    pub min_sigma_bps: f64,
    pub max_fair_velocity_bps_per_sec: f64,
    pub min_book_depth_usdt: f64,
    pub require_ofi_alignment: bool,
    pub sigma_spread_window_sec: f64,
    pub ofi_window_sec: f64,
    pub fair_velocity_window_sec: f64,
    pub hard_sl_margin_pct: f64,
    pub hard_sl_pct: f64,
    pub breakeven_at_sigma: f64,
    pub trail_dist_sigma: f64,
    pub sl_update_throttle_sec: f64,
    pub use_r_trail: bool,
    #[serde(alias = "trail_breakeven_R")]
    pub trail_breakeven_r: f64,
    #[serde(alias = "trail_lock_R")]
    pub trail_lock_r: f64,
    #[serde(alias = "trail_dist_R")]
    pub trail_dist_r: f64,
    pub signal_flip_exit: bool,
    pub imbalance_exit_log: f64,
    pub use_fair_tp: bool,
    pub scalp_take_profit_bps: f64,
    pub scratch_exit_sec: f64,
    pub scratch_exit_bps: f64,
    pub profit_protect_arm_bps: f64,
    pub profit_giveback_bps: f64,
    pub fast_profit_arm_bps: f64,
    pub fast_profit_giveback_bps: f64,
    pub profit_protect_min_bps: f64,
    pub edge_collapse_exit_bps: f64,
    pub edge_loss_after_sec: f64,
    pub edge_loss_exit_bps: f64,
    pub settled_profit_sec: f64,
    pub settled_profit_min_bps: f64,
    pub settled_profit_max_drift_bps: f64,
    pub settled_profit_edge_bps: f64,
    pub dead_trade_after_sec: f64,
    pub dead_trade_max_bps: f64,
    pub bad_entry_guard_sec: f64,
    pub bad_entry_min_age_sec: f64,
    pub bad_entry_spread_bps: f64,
    pub bad_entry_exit_bps: f64,
    pub algo_mode: String,
    pub sl_pct_crypto: f64,
    pub sl_pct_stocks: f64,
    pub max_hold_sec: f64,
    pub cooldown_min_sec: f64,
    pub cooldown_max_sec: f64,
}

impl Default for StrategyConfig {
    fn default() -> Self {
        Self {
            algorithm: "meanrev".to_string(),
            invert: false,
            entry_z: 1.8,
            max_entry_z: 4.0,
            cancel_z: 0.5,
            meanrev_min_spread_bps: 0.0,
            meanrev_require_book_alignment: false,
            meanrev_min_imbalance_log: 0.0,
            momentum_min_velocity_bps_per_sec: 1.0,
            momentum_max_velocity_bps_per_sec: 8.0,
            momentum_require_lag: true,
            ofi_min_usdt: 5_000.0,
            ofi_require_lag: false,
            ofi_min_lag_bps: 0.0,
            ofi_max_chase_bps: 0.0,
            ofi_require_book_alignment: false,
            ofi_min_imbalance_log: 0.0,
            imbalance_min_log: 0.7,
            imbalance_max_log: 3.0,
            imbalance_require_lag: false,
            imbalance_min_lag_bps: 0.0,
            imbalance_max_chase_bps: 0.0,
            sweep_min_usdt_1s: 25_000.0,
            wide_spread_ratio: 2.5,
            wide_spread_min_bps: 5.0,
            wide_spread_min_imbalance: 0.3,
            raw_momentum_min_bps: 2.0,
            raw_momentum_max_bps: 30.0,
            raw_momentum_window_sec: 1.0,
            raw_momentum_require_5s_agree: true,
            raw_momentum_anti_fade_30s_bps: 3.0,
            raw_momentum_require_lag: false,
            raw_momentum_min_lag_bps: 0.0,
            raw_momentum_max_chase_bps: 0.0,
            raw_momentum_require_ofi_alignment: false,
            raw_momentum_min_ofi_usdt: 0.0,
            raw_momentum_require_book_alignment: false,
            raw_momentum_min_imbalance_log: 0.0,
            book_lean_min_ratio: 2.0,
            bb_revert_z_entry: 2.0,
            bb_revert_z_exit: 0.3,
            bb_revert_z_max: 4.0,
            confluence_min_velocity_bps: 3.0,
            confluence_max_velocity_bps: 20.0,
            confluence_min_ofi_usdt: 3_000.0,
            confluence_min_imbalance_log: 0.5,
            confluence_require_lag: false,
            confluence_min_lag_bps: 0.0,
            confluence_max_chase_bps: 0.0,
            taker_entry: false,
            real_require_zero_fee: false,
            entry_latency_ms: 200,
            signal_max_age_ms: 0,
            pre_submit_max_spread_drift_bps: 0.0,
            micro_levels: 0,
            micro_path_hole_max_points: 0.0,
            micro_support_ratio_min: 0.0,
            micro_support_shape_min: 0.0,
            micro_path_shape_min: 0.0,
            micro_back_hole_max_points: 0.0,
            exit_latency_ms: 200,
            paper_tick_sec: 0.2,
            taker_fee_bps: 0.0,
            maker_fee_bps: 0.0,
            taker_ioc_simulation: false,
            taker_ioc_price_buffer_bps: 2.0,
            taker_ioc_min_fill_ratio: 0.2,
            quote_timeout_sec: 5.0,
            quote_offset_ticks: 1,
            min_spread_samples: 60,
            min_sigma_bps: 0.3,
            max_fair_velocity_bps_per_sec: 5.0,
            min_book_depth_usdt: 2_000.0,
            require_ofi_alignment: true,
            sigma_spread_window_sec: 30.0,
            ofi_window_sec: 0.5,
            fair_velocity_window_sec: 1.0,
            hard_sl_margin_pct: 0.01,
            hard_sl_pct: 0.0,
            breakeven_at_sigma: 0.5,
            trail_dist_sigma: 0.5,
            sl_update_throttle_sec: 0.2,
            use_r_trail: false,
            trail_breakeven_r: 1.0,
            trail_lock_r: 2.0,
            trail_dist_r: 1.0,
            signal_flip_exit: false,
            imbalance_exit_log: 0.0,
            use_fair_tp: false,
            scalp_take_profit_bps: 0.0,
            scratch_exit_sec: 0.0,
            scratch_exit_bps: 0.0,
            profit_protect_arm_bps: 0.0,
            profit_giveback_bps: 0.0,
            fast_profit_arm_bps: 0.0,
            fast_profit_giveback_bps: 0.0,
            profit_protect_min_bps: 0.0,
            edge_collapse_exit_bps: 0.0,
            edge_loss_after_sec: 0.0,
            edge_loss_exit_bps: 0.0,
            settled_profit_sec: 0.0,
            settled_profit_min_bps: 0.0,
            settled_profit_max_drift_bps: 0.0,
            settled_profit_edge_bps: 0.0,
            dead_trade_after_sec: 0.0,
            dead_trade_max_bps: 0.0,
            bad_entry_guard_sec: 0.0,
            bad_entry_min_age_sec: 0.0,
            bad_entry_spread_bps: 0.0,
            bad_entry_exit_bps: 0.0,
            algo_mode: "ANY".to_string(),
            sl_pct_crypto: 0.0025,
            sl_pct_stocks: 0.0010,
            max_hold_sec: 300.0,
            cooldown_min_sec: 3.0,
            cooldown_max_sec: 8.0,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct RiskConfig {
    pub max_concurrent_positions: usize,
    pub margin_pct_per_slot: f64,
    pub account_share_pct: f64,
    pub leverage_mode: String,
    pub fixed_leverage: i64,
    pub book_depth_consume_pct: f64,
    pub daily_loss_pct_kill: f64,
    pub max_drawdown_pct_kill: f64,
    pub min_balance_usdt: f64,
    pub min_trade_margin_usdt: f64,
    pub max_notional_usdt: f64,
}

impl Default for RiskConfig {
    fn default() -> Self {
        Self {
            max_concurrent_positions: 5,
            margin_pct_per_slot: 0.18,
            account_share_pct: 1.0,
            leverage_mode: "max".to_string(),
            fixed_leverage: 20,
            book_depth_consume_pct: 0.05,
            daily_loss_pct_kill: 0.30,
            max_drawdown_pct_kill: 0.50,
            min_balance_usdt: 5.0,
            min_trade_margin_usdt: 1.0,
            max_notional_usdt: 0.0,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct SymbolOverride {
    pub symbol: String,
    pub enabled: bool,
    pub leverage: Option<i64>,
    pub margin_pct: Option<f64>,
    pub max_notional_usdt: Option<f64>,
    pub sl_pct: Option<f64>,
    pub max_hold_sec: Option<f64>,
    pub cooldown_min_sec: Option<f64>,
    pub cooldown_max_sec: Option<f64>,
    pub allow_long: Option<bool>,
    pub allow_short: Option<bool>,
    pub min_entry_score: Option<f64>,
    pub min_lag_bps: Option<f64>,
    pub max_chase_bps: Option<f64>,
    pub anti_fade_30s_bps: Option<f64>,
    pub min_abs_spread_bps: Option<f64>,
    pub entry_latency_ms: Option<u64>,
    pub signal_max_age_ms: Option<u64>,
    pub pre_submit_max_spread_drift_bps: Option<f64>,
    pub micro_levels: Option<i64>,
    pub micro_path_hole_max_points: Option<f64>,
    pub micro_support_ratio_min: Option<f64>,
    pub micro_support_shape_min: Option<f64>,
    pub micro_path_shape_min: Option<f64>,
    pub micro_back_hole_max_points: Option<f64>,
    pub taker_ioc_price_buffer_bps: Option<f64>,
    pub taker_ioc_min_fill_ratio: Option<f64>,
    pub scalp_take_profit_bps: Option<f64>,
    pub scratch_exit_sec: Option<f64>,
    pub scratch_exit_bps: Option<f64>,
    pub use_fair_tp: Option<bool>,
    pub profit_protect_arm_bps: Option<f64>,
    pub profit_giveback_bps: Option<f64>,
    pub fast_profit_arm_bps: Option<f64>,
    pub fast_profit_giveback_bps: Option<f64>,
    pub profit_protect_min_bps: Option<f64>,
    pub edge_collapse_exit_bps: Option<f64>,
    pub edge_loss_after_sec: Option<f64>,
    pub edge_loss_exit_bps: Option<f64>,
    pub settled_profit_sec: Option<f64>,
    pub settled_profit_min_bps: Option<f64>,
    pub settled_profit_max_drift_bps: Option<f64>,
    pub settled_profit_edge_bps: Option<f64>,
    pub dead_trade_after_sec: Option<f64>,
    pub dead_trade_max_bps: Option<f64>,
    pub bad_entry_guard_sec: Option<f64>,
    pub bad_entry_min_age_sec: Option<f64>,
    pub bad_entry_spread_bps: Option<f64>,
    pub bad_entry_exit_bps: Option<f64>,
    pub algorithms: Option<Vec<String>>,
    pub algo_mode: Option<String>,
}

impl Default for SymbolOverride {
    fn default() -> Self {
        Self {
            symbol: String::new(),
            enabled: true,
            leverage: None,
            margin_pct: None,
            max_notional_usdt: None,
            sl_pct: None,
            max_hold_sec: None,
            cooldown_min_sec: None,
            cooldown_max_sec: None,
            allow_long: None,
            allow_short: None,
            min_entry_score: None,
            min_lag_bps: None,
            max_chase_bps: None,
            anti_fade_30s_bps: None,
            min_abs_spread_bps: None,
            entry_latency_ms: None,
            signal_max_age_ms: None,
            pre_submit_max_spread_drift_bps: None,
            micro_levels: None,
            micro_path_hole_max_points: None,
            micro_support_ratio_min: None,
            micro_support_shape_min: None,
            micro_path_shape_min: None,
            micro_back_hole_max_points: None,
            taker_ioc_price_buffer_bps: None,
            taker_ioc_min_fill_ratio: None,
            scalp_take_profit_bps: None,
            scratch_exit_sec: None,
            scratch_exit_bps: None,
            use_fair_tp: None,
            profit_protect_arm_bps: None,
            profit_giveback_bps: None,
            fast_profit_arm_bps: None,
            fast_profit_giveback_bps: None,
            profit_protect_min_bps: None,
            edge_collapse_exit_bps: None,
            edge_loss_after_sec: None,
            edge_loss_exit_bps: None,
            settled_profit_sec: None,
            settled_profit_min_bps: None,
            settled_profit_max_drift_bps: None,
            settled_profit_edge_bps: None,
            dead_trade_after_sec: None,
            dead_trade_max_bps: None,
            bad_entry_guard_sec: None,
            bad_entry_min_age_sec: None,
            bad_entry_spread_bps: None,
            bad_entry_exit_bps: None,
            algorithms: None,
            algo_mode: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct AppConfig {
    pub mode: String,
    pub autostart: bool,
    pub mexc_web: MexcWebConfig,
    pub universe: UniverseConfig,
    pub strategy: StrategyConfig,
    pub risk: RiskConfig,
    pub reference_exchanges: Vec<String>,
    pub paper_starting_balance: f64,
    pub zero_fee_symbols: Vec<String>,
    pub binance_symbol_overrides: HashMap<String, String>,
    pub symbol_overrides: Vec<SymbolOverride>,
    pub host: String,
    pub port: u16,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            mode: "paper".to_string(),
            autostart: false,
            mexc_web: MexcWebConfig::default(),
            universe: UniverseConfig::default(),
            strategy: StrategyConfig::default(),
            risk: RiskConfig::default(),
            reference_exchanges: vec!["binance".to_string()],
            paper_starting_balance: 1000.0,
            zero_fee_symbols: Vec::new(),
            binance_symbol_overrides: default_binance_symbol_overrides(),
            symbol_overrides: Vec::new(),
            host: "0.0.0.0".to_string(),
            port: 8080,
        }
    }
}

impl AppConfig {
    pub fn normalize(mut self) -> Self {
        self.mode = self.mode.trim().to_ascii_lowercase();
        if !matches!(self.mode.as_str(), "paper" | "real" | "logger") {
            self.mode = "paper".to_string();
        }
        self.universe.exclude = self
            .universe
            .exclude
            .iter()
            .map(|s| normalize_symbol_name(s))
            .filter(|s| !s.is_empty())
            .collect();
        self.universe.include_only = self
            .universe
            .include_only
            .iter()
            .map(|s| normalize_symbol_name(s))
            .filter(|s| !s.is_empty())
            .collect();
        self.universe.force_include_symbols = self
            .universe
            .force_include_symbols
            .iter()
            .map(|s| normalize_symbol_name(s))
            .filter(|s| !s.is_empty())
            .collect();
        self.zero_fee_symbols = self
            .zero_fee_symbols
            .iter()
            .map(|s| normalize_symbol_name(s))
            .filter(|s| !s.is_empty())
            .collect();

        let mut aliases = default_binance_symbol_overrides();
        for (mexc, binance) in &self.binance_symbol_overrides {
            let norm = normalize_symbol_name(mexc);
            let b = binance.trim().to_ascii_uppercase();
            if !norm.is_empty() && !b.is_empty() {
                aliases.insert(norm, b);
            }
        }
        self.binance_symbol_overrides = aliases;

        self.symbol_overrides = self
            .symbol_overrides
            .into_iter()
            .filter_map(|mut ov| {
                ov.symbol = normalize_symbol_name(&ov.symbol);
                if ov.symbol.is_empty() {
                    return None;
                }
                Some(ov)
            })
            .collect();
        self
    }
}

pub fn default_root() -> Result<PathBuf> {
    let cwd = env::current_dir().context("cannot read current directory")?;
    if cwd.file_name().and_then(|s| s.to_str()) == Some("rust-bot") {
        Ok(cwd.parent().unwrap_or(&cwd).to_path_buf())
    } else {
        Ok(cwd)
    }
}

pub fn default_config_path(root: &Path) -> PathBuf {
    env::var_os("ZFEE_CONFIG_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("config.json"))
}

pub fn load_config(path: &Path) -> Result<AppConfig> {
    if !path.exists() {
        return Ok(AppConfig::default().normalize());
    }
    let raw = fs::read_to_string(path)
        .with_context(|| format!("failed to read config {}", path.display()))?;
    let raw = raw.trim_start_matches('\u{feff}');
    let cfg: AppConfig = serde_json::from_str(raw)
        .with_context(|| format!("failed to parse config {}", path.display()))?;
    Ok(cfg.normalize())
}

pub fn save_config(path: &Path, cfg: &AppConfig) -> Result<()> {
    let data = serde_json::to_string_pretty(cfg)?;
    fs::write(path, data).with_context(|| format!("failed to write config {}", path.display()))
}
