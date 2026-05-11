$root = 'C:\Users\Administrator\Desktop\VOLODYA\mexc0feesflipper_friendfix1'
$path = Join-Path $root 'config.json'
$cfg = Get-Content -Raw -Path $path | ConvertFrom-Json

$cfg.mode = 'real'
$cfg.autostart = $false
$cfg.host = '0.0.0.0'
$cfg.port = 8080

$cfg.universe.max_symbols = 3
$cfg.universe.require_binance_ref = $true
$cfg.universe.min_book_notional_usdt = 5000.0
$cfg.universe.include_only = @('TAO_USDT', 'PEPE_USDT', 'BCH_USDT')
$cfg.universe.force_include_symbols = @('TAO_USDT', 'PEPE_USDT', 'BCH_USDT')
$cfg.universe.exclude = @('UNI_USDT', 'MSTRSTOCK_USDT', 'NVIDIA_USDT', 'TSLA_USDT', 'INTC_USDT')
$cfg.zero_fee_symbols = @('TAO_USDT', 'PEPE_USDT', 'BCH_USDT')

$cfg.risk.max_concurrent_positions = 1
$cfg.risk.account_share_pct = 0.30
$cfg.risk.margin_pct_per_slot = 0.12
$cfg.risk.book_depth_consume_pct = 0.12
$cfg.risk.daily_loss_pct_kill = 0.02
$cfg.risk.max_drawdown_pct_kill = 0.03
$cfg.risk.min_balance_usdt = 20.0
$cfg.risk.min_trade_margin_usdt = 1.0
$cfg.risk.max_notional_usdt = 650.0

$cfg.strategy.algorithm = 'raw_momentum'
$cfg.strategy.algo_mode = 'ANY'
$cfg.strategy.taker_entry = $true
$cfg.strategy.real_require_zero_fee = $true
$cfg.strategy.entry_latency_ms = 20
$cfg.strategy.exit_latency_ms = 80
$cfg.strategy.signal_max_age_ms = 260
$cfg.strategy.taker_ioc_price_buffer_bps = 0.45
$cfg.strategy.taker_ioc_min_fill_ratio = 0.55
$cfg.strategy.quote_timeout_sec = 0.9
$cfg.strategy.raw_momentum_max_chase_bps = 0.8
$cfg.strategy.confluence_max_chase_bps = 0.7
$cfg.strategy.ofi_max_chase_bps = 0.6
$cfg.strategy.imbalance_max_chase_bps = 0.6
$cfg.strategy.max_hold_sec = 4.0
$cfg.strategy.cooldown_min_sec = 2.0
$cfg.strategy.cooldown_max_sec = 6.0
$cfg.strategy.scalp_take_profit_bps = 0.7
$cfg.strategy.scratch_exit_sec = 1.0
$cfg.strategy.scratch_exit_bps = -0.08
$cfg.strategy.profit_protect_arm_bps = 0.9
$cfg.strategy.profit_giveback_bps = 0.45
$cfg.strategy.fast_profit_arm_bps = 2.0
$cfg.strategy.fast_profit_giveback_bps = 0.7
$cfg.strategy.profit_protect_min_bps = 0.7
$cfg.strategy.edge_collapse_exit_bps = 0.35
$cfg.strategy.edge_loss_after_sec = 0.8
$cfg.strategy.edge_loss_exit_bps = -0.75
$cfg.strategy.settled_profit_sec = 0.7
$cfg.strategy.settled_profit_min_bps = 1.5
$cfg.strategy.settled_profit_max_drift_bps = 0.3
$cfg.strategy.settled_profit_edge_bps = 0.35
$cfg.strategy.dead_trade_after_sec = 4.5
$cfg.strategy.dead_trade_max_bps = 0.35
$cfg.strategy.bad_entry_guard_sec = 3.0
$cfg.strategy.bad_entry_min_age_sec = 0.35
$cfg.strategy.bad_entry_spread_bps = 0.45
$cfg.strategy.bad_entry_exit_bps = -0.05

$overridesJson = @"
[
  {
    "symbol": "TAO_USDT", "enabled": true, "leverage": 150, "margin_pct": 0.12, "max_notional_usdt": 650,
    "sl_pct": 0.0010, "max_hold_sec": 4.0, "cooldown_min_sec": 2.0, "cooldown_max_sec": 5.0,
    "allow_long": true, "allow_short": true, "min_entry_score": 2.7, "min_lag_bps": 1.5, "max_chase_bps": 1.0,
    "anti_fade_30s_bps": 1.0, "entry_latency_ms": 20, "signal_max_age_ms": 240, "pre_submit_max_spread_drift_bps": 0.45,
    "taker_ioc_price_buffer_bps": 0.55, "taker_ioc_min_fill_ratio": 0.50,
    "scalp_take_profit_bps": 0.8, "scratch_exit_sec": 1.2, "scratch_exit_bps": -0.10,
    "profit_protect_arm_bps": 1.0, "profit_giveback_bps": 0.5, "fast_profit_arm_bps": 2.2, "fast_profit_giveback_bps": 0.8,
    "profit_protect_min_bps": 0.8, "edge_collapse_exit_bps": 0.4, "edge_loss_after_sec": 0.9, "edge_loss_exit_bps": -0.9,
    "settled_profit_sec": 0.75, "settled_profit_min_bps": 1.6, "settled_profit_max_drift_bps": 0.35, "settled_profit_edge_bps": 0.4,
    "dead_trade_after_sec": 4.5, "dead_trade_max_bps": 0.35, "bad_entry_guard_sec": 3.5, "bad_entry_min_age_sec": 0.4,
    "bad_entry_spread_bps": 0.55, "bad_entry_exit_bps": -0.05, "algorithms": ["raw_momentum"], "algo_mode": "ANY"
  },
  {
    "symbol": "PEPE_USDT", "enabled": true, "leverage": 120, "margin_pct": 0.12, "max_notional_usdt": 450,
    "sl_pct": 0.0016, "max_hold_sec": 1.5, "cooldown_min_sec": 2.5, "cooldown_max_sec": 6.0,
    "allow_long": true, "allow_short": true, "min_entry_score": 3.1, "min_lag_bps": 0.8, "max_chase_bps": 0.55,
    "anti_fade_30s_bps": 0.8, "entry_latency_ms": 20, "signal_max_age_ms": 180, "pre_submit_max_spread_drift_bps": 0.45,
    "taker_ioc_price_buffer_bps": 0.32, "taker_ioc_min_fill_ratio": 0.60,
    "scalp_take_profit_bps": 0.7, "scratch_exit_sec": 0.9, "scratch_exit_bps": -0.06,
    "profit_protect_arm_bps": 0.9, "profit_giveback_bps": 0.45, "fast_profit_arm_bps": 2.0, "fast_profit_giveback_bps": 0.7,
    "profit_protect_min_bps": 0.75, "edge_collapse_exit_bps": 0.35, "edge_loss_after_sec": 0.65, "edge_loss_exit_bps": -0.7,
    "settled_profit_sec": 0.55, "settled_profit_min_bps": 1.5, "settled_profit_max_drift_bps": 0.25, "settled_profit_edge_bps": 0.35,
    "dead_trade_after_sec": 3.2, "dead_trade_max_bps": 0.25, "bad_entry_guard_sec": 3.0, "bad_entry_min_age_sec": 0.35,
    "bad_entry_spread_bps": 0.40, "bad_entry_exit_bps": -0.03, "algorithms": ["confluence"], "algo_mode": "ANY"
  },
  {
    "symbol": "BCH_USDT", "enabled": true, "leverage": 100, "margin_pct": 0.12, "max_notional_usdt": 500,
    "sl_pct": 0.0011, "max_hold_sec": 2.8, "cooldown_min_sec": 2.0, "cooldown_max_sec": 5.0,
    "allow_long": true, "allow_short": false, "min_entry_score": 2.2, "min_lag_bps": 0.8, "max_chase_bps": 0.65,
    "anti_fade_30s_bps": 0.8, "entry_latency_ms": 25, "signal_max_age_ms": 300, "pre_submit_max_spread_drift_bps": 0.45,
    "taker_ioc_price_buffer_bps": 0.40, "taker_ioc_min_fill_ratio": 0.60,
    "scalp_take_profit_bps": 0.75, "scratch_exit_sec": 1.1, "scratch_exit_bps": -0.10,
    "profit_protect_arm_bps": 1.0, "profit_giveback_bps": 0.5, "fast_profit_arm_bps": 2.2, "fast_profit_giveback_bps": 0.8,
    "profit_protect_min_bps": 0.8, "edge_collapse_exit_bps": 0.4, "edge_loss_after_sec": 0.9, "edge_loss_exit_bps": -0.8,
    "settled_profit_sec": 0.7, "settled_profit_min_bps": 1.6, "settled_profit_max_drift_bps": 0.35, "settled_profit_edge_bps": 0.4,
    "dead_trade_after_sec": 4.0, "dead_trade_max_bps": 0.35, "bad_entry_guard_sec": 3.5, "bad_entry_min_age_sec": 0.4,
    "bad_entry_spread_bps": 0.5, "bad_entry_exit_bps": -0.04, "algorithms": ["raw_momentum"], "algo_mode": "ANY"
  }
]
"@
$overrides = (ConvertFrom-Json -InputObject $overridesJson) -as [object[]]
$cfg.PSObject.Properties.Remove('symbol_overrides')
$cfg | Add-Member -NotePropertyName symbol_overrides -NotePropertyValue $overrides

$json = $cfg | ConvertTo-Json -Depth 40
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($path, $json, $utf8)

[pscustomobject]@{
    written = $true
    universe = ($cfg.universe.include_only -join ',')
    maxpos = $cfg.risk.max_concurrent_positions
    zeroFee = $cfg.strategy.real_require_zero_fee
    buffer = $cfg.strategy.taker_ioc_price_buffer_bps
    cap = $cfg.risk.max_notional_usdt
    overrides = ($cfg.symbol_overrides | ForEach-Object {
        "$($_.symbol):score=$($_.min_entry_score),buf=$($_.taker_ioc_price_buffer_bps),chase=$($_.max_chase_bps),hold=$($_.max_hold_sec)"
    })
} | ConvertTo-Json -Depth 5
