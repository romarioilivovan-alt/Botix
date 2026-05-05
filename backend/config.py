from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("ZFEE_CONFIG_PATH") or _PROJECT_ROOT / "config.json")


@dataclass
class MexcWebConfig:
    web_uid: str = ""
    device_id: str = ""
    mhash: str = ""
    proxy: Optional[str] = None


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

    momentum_min_velocity_bps_per_sec: float = 1.0
    momentum_max_velocity_bps_per_sec: float = 8.0
    momentum_require_lag: bool = True

    ofi_min_usdt: float = 5_000

    imbalance_min_log: float = 0.7
    imbalance_max_log: float = 3.0

    sweep_min_usdt_1s: float = 25_000

    wide_spread_ratio: float = 2.5
    wide_spread_min_bps: float = 5.0
    wide_spread_min_imbalance: float = 0.3

    raw_momentum_min_bps: float = 2.0
    raw_momentum_max_bps: float = 30.0
    raw_momentum_window_sec: float = 1.0
    raw_momentum_require_5s_agree: bool = True
    raw_momentum_anti_fade_30s_bps: float = 3.0

    book_lean_min_ratio: float = 2.0

    bb_revert_z_entry: float = 2.0
    bb_revert_z_exit: float = 0.3
    bb_revert_z_max: float = 4.0

    confluence_min_velocity_bps: float = 3.0
    confluence_max_velocity_bps: float = 20.0
    confluence_min_ofi_usdt: float = 3000
    confluence_min_imbalance_log: float = 0.5

    taker_entry: bool = False

    entry_latency_ms: int = 200
    exit_latency_ms: int = 200
    taker_fee_bps: float = 0.0
    maker_fee_bps: float = 0.0
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
    leverage_mode: str = "max"
    fixed_leverage: int = 20
    book_depth_consume_pct: float = 0.05

    daily_loss_pct_kill: float = 0.30
    max_drawdown_pct_kill: float = 0.50

    min_balance_usdt: float = 5.0


@dataclass
class SymbolOverride:
    symbol: str
    enabled: bool = True
    leverage: Optional[int] = None
    margin_pct: Optional[float] = None
    sl_pct: Optional[float] = None
    max_hold_sec: Optional[float] = None
    algorithms: Optional[List[str]] = None  # None = use global
    algo_mode: Optional[str] = None         # None = use global


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


def load_config() -> AppConfig:
    cfg = AppConfig()
    if not CONFIG_PATH.exists():
        return cfg
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
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
            sym = str(item.get("symbol") or "")
            if not sym:
                continue
            ov = SymbolOverride(symbol=sym)
            for fname in ("enabled", "leverage", "margin_pct", "sl_pct",
                          "max_hold_sec", "algorithms", "algo_mode"):
                if fname in item:
                    setattr(ov, fname, item[fname])
            parsed.append(ov)
        cfg.symbol_overrides = parsed

    return cfg


def save_config(cfg: AppConfig) -> None:
    CONFIG_PATH.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
