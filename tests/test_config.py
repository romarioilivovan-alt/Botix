import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

def test_symbol_override_roundtrip(tmp_path, monkeypatch):
    import backend.config as m
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(m, "CONFIG_PATH", cfg_path)

    data = {
        "strategy": {"sl_pct_crypto": 0.0025, "sl_pct_stocks": 0.0010, "algo_mode": "CONSENSUS"},
        "symbol_overrides": [
            {"symbol": "ENA_USDT", "enabled": True, "margin_pct": 0.30,
             "algorithms": ["meanrev", "raw_momentum"], "algo_mode": "ANY"},
            {"symbol": "PENGU_USDT", "enabled": False, "leverage": 50}
        ]
    }
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    cfg = m.load_config()

    assert cfg.strategy.sl_pct_crypto == 0.0025
    assert cfg.strategy.sl_pct_stocks == 0.0010
    assert cfg.strategy.algo_mode == "CONSENSUS"
    assert len(cfg.symbol_overrides) == 2

    ena = cfg.symbol_overrides[0]
    assert isinstance(ena, m.SymbolOverride)
    assert ena.symbol == "ENA_USDT"
    assert ena.enabled is True
    assert ena.margin_pct == 0.30
    assert ena.algorithms == ["meanrev", "raw_momentum"]
    assert ena.algo_mode == "ANY"

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
                         algorithms=["meanrev"], algo_mode="ANY")
    ]
    m.save_config(cfg)

    cfg2 = m.load_config()
    assert len(cfg2.symbol_overrides) == 1
    assert cfg2.symbol_overrides[0].symbol == "ENA_USDT"
    assert cfg2.symbol_overrides[0].margin_pct == 0.30
