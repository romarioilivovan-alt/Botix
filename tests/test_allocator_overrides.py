import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from backend.config import AppConfig, RiskConfig
from backend.allocator import CapitalAllocator
from backend.opportunity import Opportunity
from backend.state import AppState


def _opp():
    return Opportunity(symbol="ENA_USDT", side="LONG", score=2.0,
                       entry_price=1.0, fair=1.0, sigma=0.0001, z=2.0)


def _state():
    s = AppState()
    s.balance = 100.0
    return s


def test_global_margin_and_leverage():
    cfg = AppConfig()
    cfg.risk.margin_pct_per_slot = 0.25
    cfg.risk.leverage_mode = "fixed"
    cfg.risk.fixed_leverage = 100
    cfg.risk.max_concurrent_positions = 3
    alloc = CapitalAllocator(cfg)

    dec = alloc.decide(_opp(), _state(), balance_free=100.0,
                       max_leverage_for_symbol=None, book_top_notional=1_000_000)
    assert dec.accept
    assert abs(dec.margin_usdt - 25.0) < 0.01
    assert dec.leverage == 100


def test_margin_pct_override():
    cfg = AppConfig()
    cfg.risk.margin_pct_per_slot = 0.25
    cfg.risk.leverage_mode = "fixed"
    cfg.risk.fixed_leverage = 100
    cfg.risk.max_concurrent_positions = 3
    alloc = CapitalAllocator(cfg)

    dec = alloc.decide(_opp(), _state(), balance_free=100.0,
                       max_leverage_for_symbol=None, book_top_notional=1_000_000,
                       margin_pct_override=0.30)
    assert dec.accept
    assert abs(dec.margin_usdt - 30.0) < 0.01


def test_leverage_override():
    cfg = AppConfig()
    cfg.risk.margin_pct_per_slot = 0.25
    cfg.risk.leverage_mode = "fixed"
    cfg.risk.fixed_leverage = 20
    cfg.risk.max_concurrent_positions = 3
    alloc = CapitalAllocator(cfg)

    dec = alloc.decide(_opp(), _state(), balance_free=100.0,
                       max_leverage_for_symbol=None, book_top_notional=1_000_000,
                       leverage_override=50)
    assert dec.accept
    assert dec.leverage == 50


def test_both_overrides():
    cfg = AppConfig()
    cfg.risk.margin_pct_per_slot = 0.25
    cfg.risk.leverage_mode = "fixed"
    cfg.risk.fixed_leverage = 20
    cfg.risk.max_concurrent_positions = 3
    alloc = CapitalAllocator(cfg)

    dec = alloc.decide(_opp(), _state(), balance_free=100.0,
                       max_leverage_for_symbol=None, book_top_notional=1_000_000,
                       margin_pct_override=0.20, leverage_override=100)
    assert dec.accept
    assert abs(dec.margin_usdt - 20.0) < 0.01
    assert dec.leverage == 100
    assert abs(dec.notional_usdt - 2000.0) < 1.0
