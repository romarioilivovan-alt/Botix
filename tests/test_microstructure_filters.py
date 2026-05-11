import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.config import AppConfig, SymbolOverride
from backend.opportunity import OpportunityEngine
from backend.state import OrderBook, SymbolStats


def test_orderbook_microstructure_helpers():
    book = OrderBook(
        bids=[[99.0, 12.0], [98.0, 6.0], [97.0, 3.0]],
        asks=[[101.0, 10.0], [102.0, 4.0], [104.0, 2.0]],
    )

    assert book.inferred_tick_size(levels=3) == 1.0
    assert book.path_hole_points("LONG", levels=3) == 2.0
    assert book.path_hole_points("SHORT", levels=3) == 1.0
    assert abs(book.level_shape_ratio("LONG", levels=3) - 1.6) < 1e-9
    assert abs(book.level_shape_ratio("SHORT", levels=3) - 1.75) < 1e-9
    assert abs(book.support_ratio("LONG", levels=3) - (21.0 / 16.0)) < 1e-9
    assert abs(book.support_ratio("SHORT", levels=3) - (16.0 / 21.0)) < 1e-9


def test_global_microstructure_filter_blocks_weak_long_book():
    cfg = AppConfig()
    cfg.strategy.micro_levels = 3
    cfg.strategy.micro_path_hole_max_points = 1.5
    cfg.strategy.micro_support_ratio_min = 1.20
    cfg.strategy.micro_support_shape_min = 1.70
    cfg.strategy.micro_path_shape_min = 1.50
    cfg.strategy.micro_back_hole_max_points = 1.5
    eng = OpportunityEngine(cfg)

    st = SymbolStats(
        fair=100.0,
        mexc_mid=99.9,
        spread_bps=-1.0,
        score=3.0,
        side_hint="LONG",
        long_path_hole_points=2.0,
        long_support_ratio=1.10,
        long_support_shape=1.60,
        long_path_shape=1.40,
        long_back_hole_points=2.0,
    )

    out = eng._apply_symbol_override_filters(st, None)

    assert out.side_hint is None
    assert out.score == 0.0
    assert out.blocked_reason == "micro_path_hole=2.00"


def test_symbol_override_microstructure_filter_can_be_tighter_than_global():
    cfg = AppConfig()
    cfg.strategy.micro_levels = 3
    cfg.strategy.micro_path_hole_max_points = 3.0
    eng = OpportunityEngine(cfg)

    override = SymbolOverride(
        symbol="PEPE_USDT",
        micro_levels=3,
        micro_path_hole_max_points=1.0,
    )
    st = SymbolStats(
        fair=100.0,
        mexc_mid=99.9,
        spread_bps=-1.0,
        score=2.0,
        side_hint="LONG",
        long_path_hole_points=1.25,
        long_support_ratio=2.0,
        long_support_shape=2.0,
        long_path_shape=2.0,
        long_back_hole_points=0.0,
    )

    out = eng._apply_symbol_override_filters(st, override)

    assert out.side_hint is None
    assert out.blocked_reason == "micro_path_hole=1.25"
