import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _bare_executor(cls, cfg):
    obj = cls.__new__(cls)
    obj.cfg = cfg
    return obj


def test_symbol_override_max_chase_applies_to_fill_checks():
    from backend.config import AppConfig, SymbolOverride
    from backend.paper import PaperExecutor
    from backend.real import RealExecutor

    cfg = AppConfig()
    cfg.strategy.raw_momentum_max_chase_bps = 0.0
    cfg.symbol_overrides = [
        SymbolOverride(symbol="TAO_USDT", max_chase_bps=3.5),
    ]

    paper = _bare_executor(PaperExecutor, cfg)
    real = _bare_executor(RealExecutor, cfg)

    fair = 100.0
    fill_price = 99.98  # 2 bps below fair: should pass for TAO override, fail for default.

    assert paper._fill_respects_fair("TAO_USDT", "SHORT", "raw_momentum", fill_price, fair)[0]
    assert real._fill_respects_fair("TAO_USDT", "SHORT", "raw_momentum", fill_price, fair)[0]

    assert not paper._fill_respects_fair("BCH_USDT", "SHORT", "raw_momentum", fill_price, fair)[0]
    assert not real._fill_respects_fair("BCH_USDT", "SHORT", "raw_momentum", fill_price, fair)[0]
