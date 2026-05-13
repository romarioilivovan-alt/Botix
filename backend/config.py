from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH: Optional[Path] = None

LEGACY_SYMBOL_ALIASES = {
    "NVDA_USDT": "NVIDIA_USDT",
    "MSTR_USDT": "MSTRSTOCK_USDT",
}

DEFAULT_BINANCE_SYMBOL_OVERRIDES = {
    "PEPE_USDT": "1000PEPEUSDT",
    "NVIDIA_USDT": "NVDAUSDT",
    "MSTRSTOCK_USDT": "MSTRUSDT",
    "TSLA_USDT": "TSLAUSDT",
    "INTC_USDT": "INTCUSDT",
}


def normalize_symbol_name(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    if not s:
        return s
    if not s.endswith("_USDT") and "_" not in s:
        s = f"{s}_USDT"
    return LEGACY_SYMBOL_ALIASES.get(s, s)


@dataclass
class MexcWebConfig:
    web_uid: str = ""
    device_id: str = ""
    mhash: str = ""
    proxy: Optional[str] = None
    order_submit_path: str = "legacy_submit"


@dataclass
class UniverseConfig:
    """0-fee symbol universe filters."""

    refresh_sec: int = 300
    max_symbols: int = 60
    require_binance_ref: bool = True
    min_book_notional_usdt: float = 5_000

    exclude: List[str] = field(default_factory=list)
    include_only: List[str] = field(default_factory=list)
    force_include_symbols: List[str] = field(default_factory=list)


@dataclass
class StrategyConfig:
    algorithm: str = "meanrev"

    invert: bool = False

    entry_z: float = 1.8
    max_entry_z: float = 4.0
    cancel_z: float = 0.5
    meanrev_min_spread_bps: float = 0.0
    meanrev_require_book_alignment: bool = False
    meanrev_min_imbalance_log: float = 0.0

    momentum_min_velocity_bps_per_sec: float = 1.0
    momentum_max_velocity_bps_per_sec: float = 8.0
    momentum_require_lag: bool = True

    ofi_min_usdt: float = 5_000
    ofi_require_lag: bool = False
    ofi_min_lag_bps: float = 0.0
    ofi_max_chase_bps: float = 0.0
    ofi_require_book_alignment: bool = False
    ofi_min_imbalance_log: float = 0.0

    imbalance_min_log: float = 0.7
    imbalance_max_log: float = 3.0
    imbalance_require_lag: bool = False
    imbalance_min_lag_bps: float = 0.0
    imbalance_max_chase_bps: float = 0.0

    sweep_min_usdt_1s: float = 25_000

    wide_spread_ratio: float = 2.5
    wide_spread_min_bps: float = 5.0
    wide_spread_min_imbalance: float = 0.3

    raw_momentum_min_bps: float = 2.0
    raw_momentum_max_bps: float = 30.0
    raw_momentum_window_sec: float = 1.0
    raw_momentum_require_5s_agree: bool = True
    raw_momentum_anti_fade_30s_bps: float = 3.0
    raw_momentum_require_lag: bool = False
    raw_momentum_min_lag_bps: float = 0.0
    raw_momentum_max_chase_bps: float = 0.0
    raw_momentum_require_ofi_alignment: bool = False
    raw_momentum_min_ofi_usdt: float = 0.0
    raw_momentum_require_book_alignment: bool = False
    raw_momentum_min_imbalance_log: float = 0.0

    book_lean_min_ratio: float = 2.0

    bb_revert_z_entry: float = 2.0
    bb_revert_z_exit: float = 0.3
    bb_revert_z_max: float = 4.0

    confluence_min_velocity_bps: float = 3.0
    confluence_max_velocity_bps: float = 20.0
    confluence_min_ofi_usdt: float = 3000
    confluence_min_imbalance_log: float = 0.5
    confluence_require_lag: bool = False
    confluence_min_lag_bps: float = 0.0
    confluence_max_chase_bps: float = 0.0

    taker_entry: bool = False

    entry_latency_ms: int = 200
    signal_max_age_ms: int = 0
    pre_submit_max_spread_drift_bps: float = 0.0
    micro_levels: int = 0
    micro_path_hole_max_points: float = 0.0
    micro_support_ratio_min: float = 0.0
    micro_support_shape_min: float = 0.0
    micro_path_shape_min: float = 0.0
    micro_back_hole_max_points: float = 0.0
    exit_latency_ms: int = 200
    paper_exchange_sl: bool = False
    grid_log_candidates: bool = True
    grid_skip_idle_equity: bool = False
    grid_emit_interval_sec: float = 0.5
    equity_log_sec: float = 5.0
    paper_tick_sec: float = 0.2
    taker_fee_bps: float = 0.0
    maker_fee_bps: float = 0.0
    taker_order_mode: str = ""
    taker_ioc_simulation: bool = False
    taker_ioc_price_buffer_bps: float = 2.0
    taker_ioc_min_fill_ratio: float = 0.2
    taker_ioc_adverse_fill_bps: float = 0.0
    taker_market_min_fill_ratio: float = 0.98
    taker_market_adverse_fill_bps: float = 0.0
    quote_timeout_sec: float = 5.0
    quote_offset_ticks: int = 1
    min_spread_samples: int = 60
    min_sigma_bps: float = 0.3

    max_fair_velocity_bps_per_sec: float = 5.0
    min_book_depth_usdt: float = 2_000
    require_ofi_alignment: bool = True

    sigma_spread_window_sec: float = 30.0
    ofi_window_sec: float = 0.5
    fair_velocity_window_sec: float = 1.0
    fair_price_mode: str = "mid"
    fair_ema_alpha: float = 0.2
    mexc_fair_poll_sec: float = 1.0

    hard_sl_margin_pct: float = 0.01
    hard_sl_pct: float = 0.0

    breakeven_at_sigma: float = 0.5
    trail_dist_sigma: float = 0.5
    sl_update_throttle_sec: float = 0.2

    use_r_trail: bool = False
    trail_breakeven_R: float = 1.0
    trail_lock_R: float = 2.0
    trail_dist_R: float = 1.0

    signal_flip_exit: bool = False
    imbalance_exit_log: float = 0.0

    use_fair_tp: bool = False

    scalp_take_profit_bps: float = 0.0
    scratch_exit_sec: float = 0.0
    scratch_exit_bps: float = 0.0
    profit_protect_arm_bps: float = 0.0
    profit_giveback_bps: float = 0.0
    fast_profit_arm_bps: float = 0.0
    fast_profit_giveback_bps: float = 0.0
    profit_protect_min_bps: float = 0.0
    edge_collapse_exit_bps: float = 0.0
    edge_loss_after_sec: float = 0.0
    edge_loss_exit_bps: float = 0.0
    settled_profit_sec: float = 0.0
    settled_profit_min_bps: float = 0.0
    settled_profit_max_drift_bps: float = 0.0
    settled_profit_edge_bps: float = 0.0
    dead_trade_after_sec: float = 0.0
    dead_trade_max_bps: float = 0.0
    bad_entry_guard_sec: float = 0.0
    bad_entry_min_age_sec: float = 0.0
    bad_entry_spread_bps: float = 0.0
    bad_entry_exit_bps: float = 0.0
    late_impulse_reject_enabled: bool = False
    late_impulse_min_edge_bps: float = 0.0
    late_impulse_max_chase_bps: float = 0.0
    late_impulse_max_fair_age_ms: float = 0.0

    # Multi-strategy mode: ANY | BEST | CONSENSUS
    algo_mode: str = "ANY"
    # Backstop SL by asset type (price fraction, not margin fraction)
    sl_pct_crypto: float = 0.0025
    sl_pct_stocks: float = 0.0010

    max_hold_sec: float = 300.0

    cooldown_min_sec: float = 3.0
    cooldown_max_sec: float = 8.0


@dataclass
class RiskConfig:
    max_concurrent_positions: int = 5
    margin_pct_per_slot: float = 0.18
    account_share_pct: float = 1.0
    leverage_mode: str = "max"
    fixed_leverage: int = 20
    book_depth_consume_pct: float = 0.05

    daily_loss_pct_kill: float = 0.30
    max_drawdown_pct_kill: float = 0.50
    session_loss_usdt_kill: float = 0.0
    session_loss_pct_kill: float = 0.0
    consecutive_losses_kill: int = 0
    max_runtime_sec: float = 0.0
    max_trades_per_session: int = 0
    max_open_loss_per_position_usdt: float = 0.0
    stale_data_kill_sec: float = 0.0
    stale_book_age_ms_kill: float = 0.0
    auth_error_kill_count: int = 0
    private_api_error_kill_count: int = 0
    emergency_close_on_stop: bool = True
    emergency_close_retries: int = 3

    min_balance_usdt: float = 5.0
    min_trade_margin_usdt: float = 1.0


@dataclass
class SymbolOverride:
    symbol: str
    enabled: bool = True
    leverage: Optional[int] = None
    margin_pct: Optional[float] = None
    book_depth_consume_pct: Optional[float] = None
    max_notional_usdt: Optional[float] = None
    sl_pct: Optional[float] = None
    max_hold_sec: Optional[float] = None
    cooldown_min_sec: Optional[float] = None
    cooldown_max_sec: Optional[float] = None
    allow_long: Optional[bool] = None
    allow_short: Optional[bool] = None
    min_entry_score: Optional[float] = None
    min_lag_bps: Optional[float] = None
    max_chase_bps: Optional[float] = None
    anti_fade_30s_bps: Optional[float] = None
    min_abs_spread_bps: Optional[float] = None
    entry_latency_ms: Optional[int] = None
    exit_latency_ms: Optional[int] = None
    signal_max_age_ms: Optional[int] = None
    pre_submit_max_spread_drift_bps: Optional[float] = None
    micro_levels: Optional[int] = None
    micro_path_hole_max_points: Optional[float] = None
    micro_support_ratio_min: Optional[float] = None
    micro_support_shape_min: Optional[float] = None
    micro_path_shape_min: Optional[float] = None
    micro_back_hole_max_points: Optional[float] = None
    taker_order_mode: Optional[str] = None
    taker_ioc_price_buffer_bps: Optional[float] = None
    taker_ioc_min_fill_ratio: Optional[float] = None
    taker_ioc_adverse_fill_bps: Optional[float] = None
    taker_market_min_fill_ratio: Optional[float] = None
    taker_market_adverse_fill_bps: Optional[float] = None
    scalp_take_profit_bps: Optional[float] = None
    scratch_exit_sec: Optional[float] = None
    scratch_exit_bps: Optional[float] = None
    use_fair_tp: Optional[bool] = None
    profit_protect_arm_bps: Optional[float] = None
    profit_giveback_bps: Optional[float] = None
    fast_profit_arm_bps: Optional[float] = None
    fast_profit_giveback_bps: Optional[float] = None
    profit_protect_min_bps: Optional[float] = None
    edge_collapse_exit_bps: Optional[float] = None
    edge_loss_after_sec: Optional[float] = None
    edge_loss_exit_bps: Optional[float] = None
    settled_profit_sec: Optional[float] = None
    settled_profit_min_bps: Optional[float] = None
    settled_profit_max_drift_bps: Optional[float] = None
    settled_profit_edge_bps: Optional[float] = None
    dead_trade_after_sec: Optional[float] = None
    dead_trade_max_bps: Optional[float] = None
    bad_entry_guard_sec: Optional[float] = None
    bad_entry_min_age_sec: Optional[float] = None
    bad_entry_spread_bps: Optional[float] = None
    bad_entry_exit_bps: Optional[float] = None
    late_impulse_reject_enabled: Optional[bool] = None
    late_impulse_min_edge_bps: Optional[float] = None
    late_impulse_max_chase_bps: Optional[float] = None
    late_impulse_max_fair_age_ms: Optional[float] = None
    algorithms: Optional[List[str]] = None  # None = use global
    algo_mode: Optional[str] = None         # None = use global
    raw_momentum_min_bps: Optional[float] = None
    raw_momentum_max_bps: Optional[float] = None
    raw_momentum_require_5s_agree: Optional[bool] = None
    raw_momentum_anti_fade_30s_bps: Optional[float] = None
    raw_momentum_require_lag: Optional[bool] = None
    raw_momentum_min_lag_bps: Optional[float] = None
    raw_momentum_max_chase_bps: Optional[float] = None
    raw_momentum_require_book_alignment: Optional[bool] = None
    raw_momentum_min_imbalance_log: Optional[float] = None
    raw_momentum_require_ofi_alignment: Optional[bool] = None
    raw_momentum_min_ofi_usdt: Optional[float] = None
    imbalance_min_log: Optional[float] = None
    imbalance_max_log: Optional[float] = None
    imbalance_require_lag: Optional[bool] = None
    imbalance_min_lag_bps: Optional[float] = None
    imbalance_max_chase_bps: Optional[float] = None
    confluence_min_velocity_bps: Optional[float] = None
    confluence_max_velocity_bps: Optional[float] = None
    confluence_min_ofi_usdt: Optional[float] = None
    confluence_min_imbalance_log: Optional[float] = None
    confluence_require_lag: Optional[bool] = None
    confluence_min_lag_bps: Optional[float] = None
    confluence_max_chase_bps: Optional[float] = None
    book_lean_min_ratio: Optional[float] = None
    bb_revert_z_entry: Optional[float] = None
    bb_revert_z_max: Optional[float] = None
    entry_z: Optional[float] = None
    meanrev_min_spread_bps: Optional[float] = None
    meanrev_require_book_alignment: Optional[bool] = None
    meanrev_min_imbalance_log: Optional[float] = None


@dataclass
class AppConfig:
    mode: str = "paper"
    autostart: bool = False

    mexc_web: MexcWebConfig = field(default_factory=MexcWebConfig)

    universe: UniverseConfig = field(default_factory=UniverseConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    reference_exchanges: List[str] = field(default_factory=lambda: ["binance"])

    paper_starting_balance: float = 1000.0

    zero_fee_symbols: List[str] = field(default_factory=list)
    binance_symbol_overrides: Dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_BINANCE_SYMBOL_OVERRIDES)
    )

    # Per-symbol configuration overrides
    symbol_overrides: List[SymbolOverride] = field(default_factory=list)
    # Server binding
    host: str = "0.0.0.0"
    port: int = 8080


def _merge_dataclass(dc, data: dict) -> None:
    for k, v in data.items():
        if not hasattr(dc, k):
            continue
        cur = getattr(dc, k)
        if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
            _merge_dataclass(cur, v)
        else:
            try:
                setattr(dc, k, v)
            except Exception:
                pass


def _resolve_config_path() -> Path:
    if CONFIG_PATH is not None:
        return Path(CONFIG_PATH)
    env_path = os.environ.get("ZFEE_CONFIG_PATH")
    if env_path:
        return Path(env_path)
    return _PROJECT_ROOT / "config.json"


def load_config() -> AppConfig:
    cfg = AppConfig()
    config_path = _resolve_config_path()
    if not config_path.exists():
        return cfg
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return cfg
    if not isinstance(data, dict):
        return cfg

    # Extract symbol_overrides before generic merge (list-of-dicts, not a nested dataclass)
    raw_overrides = data.pop("symbol_overrides", None)
    _merge_dataclass(cfg, data)

    if isinstance(raw_overrides, list):
        parsed = []
        for item in raw_overrides:
            if not isinstance(item, dict):
                continue
            sym = normalize_symbol_name(item.get("symbol") or "")
            if not sym:
                continue
            ov = SymbolOverride(symbol=sym)
            for fname in (
                "enabled", "leverage", "margin_pct", "book_depth_consume_pct", "max_notional_usdt",
                "sl_pct", "max_hold_sec",
                "cooldown_min_sec", "cooldown_max_sec",
                "allow_long", "allow_short",
                "min_entry_score", "min_lag_bps", "max_chase_bps",
                "anti_fade_30s_bps", "min_abs_spread_bps",
                "entry_latency_ms", "exit_latency_ms", "signal_max_age_ms",
                "pre_submit_max_spread_drift_bps",
                "micro_levels", "micro_path_hole_max_points",
                "micro_support_ratio_min", "micro_support_shape_min",
                "micro_path_shape_min", "micro_back_hole_max_points",
                "taker_order_mode",
                "taker_ioc_price_buffer_bps", "taker_ioc_min_fill_ratio",
                "taker_ioc_adverse_fill_bps",
                "taker_market_min_fill_ratio", "taker_market_adverse_fill_bps",
                "scalp_take_profit_bps", "scratch_exit_sec", "scratch_exit_bps",
                "use_fair_tp",
                "profit_protect_arm_bps", "profit_giveback_bps",
                "fast_profit_arm_bps", "fast_profit_giveback_bps",
                "profit_protect_min_bps", "edge_collapse_exit_bps",
                "edge_loss_after_sec", "edge_loss_exit_bps",
                "settled_profit_sec", "settled_profit_min_bps",
                "settled_profit_max_drift_bps", "settled_profit_edge_bps",
                "dead_trade_after_sec", "dead_trade_max_bps",
                "bad_entry_guard_sec", "bad_entry_min_age_sec",
                "bad_entry_spread_bps", "bad_entry_exit_bps",
                "late_impulse_reject_enabled",
                "late_impulse_min_edge_bps",
                "late_impulse_max_chase_bps",
                "late_impulse_max_fair_age_ms",
                "algorithms", "algo_mode",
                "raw_momentum_min_bps", "raw_momentum_max_bps",
                "raw_momentum_require_5s_agree", "raw_momentum_anti_fade_30s_bps",
                "raw_momentum_require_lag", "raw_momentum_min_lag_bps",
                "raw_momentum_max_chase_bps", "raw_momentum_require_book_alignment",
                "raw_momentum_min_imbalance_log", "raw_momentum_require_ofi_alignment",
                "raw_momentum_min_ofi_usdt",
                "imbalance_min_log", "imbalance_max_log", "imbalance_require_lag",
                "imbalance_min_lag_bps", "imbalance_max_chase_bps",
                "confluence_min_velocity_bps", "confluence_max_velocity_bps",
                "confluence_min_ofi_usdt", "confluence_min_imbalance_log",
                "confluence_require_lag", "confluence_min_lag_bps",
                "confluence_max_chase_bps", "book_lean_min_ratio",
                "bb_revert_z_entry", "bb_revert_z_max",
                "entry_z", "meanrev_min_spread_bps",
                "meanrev_require_book_alignment", "meanrev_min_imbalance_log",
            ):
                if fname in item:
                    setattr(ov, fname, item[fname])
            parsed.append(ov)
        cfg.symbol_overrides = parsed

    cfg.universe.exclude = [normalize_symbol_name(s) for s in (cfg.universe.exclude or []) if str(s or "").strip()]
    cfg.universe.include_only = [normalize_symbol_name(s) for s in (cfg.universe.include_only or []) if str(s or "").strip()]
    cfg.universe.force_include_symbols = [normalize_symbol_name(s) for s in (cfg.universe.force_include_symbols or []) if str(s or "").strip()]
    cfg.zero_fee_symbols = [normalize_symbol_name(s) for s in (cfg.zero_fee_symbols or []) if str(s or "").strip()]

    normalized_aliases: Dict[str, str] = dict(DEFAULT_BINANCE_SYMBOL_OVERRIDES)
    for mexc_symbol, binance_symbol in (cfg.binance_symbol_overrides or {}).items():
        norm = normalize_symbol_name(mexc_symbol)
        if not norm:
            continue
        normalized_aliases[norm] = str(binance_symbol or "").upper().strip()
    cfg.binance_symbol_overrides = normalized_aliases

    return cfg


def save_config(cfg: AppConfig) -> None:
    _resolve_config_path().write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
