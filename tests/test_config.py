import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

def test_symbol_override_roundtrip(tmp_path, monkeypatch):
    import backend.config as m
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(m, "CONFIG_PATH", cfg_path)

    data = {
        "strategy": {
            "sl_pct_crypto": 0.0025,
            "sl_pct_stocks": 0.0010,
            "algo_mode": "CONSENSUS",
            "profit_protect_arm_bps": 1.2,
            "settled_profit_min_bps": 1.8,
            "signal_max_age_ms": 220,
            "pre_submit_max_spread_drift_bps": 0.9,
        },
        "symbol_overrides": [
            {"symbol": "ENA_USDT", "enabled": True, "margin_pct": 0.30,
             "algorithms": ["meanrev", "raw_momentum"], "algo_mode": "ANY",
             "profit_protect_arm_bps": 1.5, "signal_max_age_ms": 140,
             "pre_submit_max_spread_drift_bps": 0.6,
             "micro_levels": 3, "micro_path_hole_max_points": 1.2},
            {"symbol": "PENGU_USDT", "enabled": False, "leverage": 50}
        ]
    }
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    cfg = m.load_config()

    assert cfg.strategy.sl_pct_crypto == 0.0025
    assert cfg.strategy.sl_pct_stocks == 0.0010
    assert cfg.strategy.algo_mode == "CONSENSUS"
    assert cfg.strategy.profit_protect_arm_bps == 1.2
    assert cfg.strategy.settled_profit_min_bps == 1.8
    assert cfg.strategy.signal_max_age_ms == 220
    assert cfg.strategy.pre_submit_max_spread_drift_bps == 0.9
    assert len(cfg.symbol_overrides) == 2

    ena = cfg.symbol_overrides[0]
    assert isinstance(ena, m.SymbolOverride)
    assert ena.symbol == "ENA_USDT"
    assert ena.enabled is True
    assert ena.margin_pct == 0.30
    assert ena.algorithms == ["meanrev", "raw_momentum"]
    assert ena.algo_mode == "ANY"
    assert ena.profit_protect_arm_bps == 1.5
    assert ena.signal_max_age_ms == 140
    assert ena.pre_submit_max_spread_drift_bps == 0.6
    assert ena.micro_levels == 3
    assert ena.micro_path_hole_max_points == 1.2

    pengu = cfg.symbol_overrides[1]
    assert pengu.symbol == "PENGU_USDT"
    assert pengu.enabled is False
    assert pengu.leverage == 50
    assert pengu.algorithms is None


def test_host_port_defaults():
    import backend.config as m
    cfg = m.AppConfig()
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 8080


def test_save_load_roundtrip(tmp_path, monkeypatch):
    import backend.config as m
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(m, "CONFIG_PATH", cfg_path)

    cfg = m.AppConfig()
    cfg.symbol_overrides = [
        m.SymbolOverride(symbol="ENA_USDT", enabled=True, margin_pct=0.30,
                         algorithms=["meanrev"], algo_mode="ANY",
                         settled_profit_min_bps=1.8,
                         signal_max_age_ms=150,
                         pre_submit_max_spread_drift_bps=0.7,
                         micro_levels=3,
                         micro_support_ratio_min=1.1)
    ]
    cfg.strategy.profit_protect_arm_bps = 1.0
    cfg.strategy.signal_max_age_ms = 200
    cfg.strategy.pre_submit_max_spread_drift_bps = 0.8
    cfg.strategy.micro_path_hole_max_points = 1.5
    m.save_config(cfg)

    cfg2 = m.load_config()
    assert cfg2.strategy.profit_protect_arm_bps == 1.0
    assert cfg2.strategy.signal_max_age_ms == 200
    assert cfg2.strategy.pre_submit_max_spread_drift_bps == 0.8
    assert cfg2.strategy.micro_path_hole_max_points == 1.5
    assert len(cfg2.symbol_overrides) == 1
    assert cfg2.symbol_overrides[0].symbol == "ENA_USDT"
    assert cfg2.symbol_overrides[0].margin_pct == 0.30
    assert cfg2.symbol_overrides[0].settled_profit_min_bps == 1.8
    assert cfg2.symbol_overrides[0].signal_max_age_ms == 150
    assert cfg2.symbol_overrides[0].pre_submit_max_spread_drift_bps == 0.7
    assert cfg2.symbol_overrides[0].micro_levels == 3
    assert cfg2.symbol_overrides[0].micro_support_ratio_min == 1.1
