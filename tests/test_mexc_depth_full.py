import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.aggregator import Aggregator


def test_mexc_depth_full_snapshot_replaces_old_levels():
    agg = Aggregator(cfg=None)
    agg.configure_symbols({"PEPE_USDT": "PEPEUSDT"})

    agg.on_mexc_depth(
        "PEPE_USDT",
        bids=[[100.0, 10.0], [99.0, 5.0]],
        asks=[[101.0, 8.0], [102.0, 4.0]],
        ts=1.0,
    )

    first = agg.get_book("PEPE_USDT")
    assert first is not None
    assert first.bids == [[100.0, 10.0], [99.0, 5.0]]
    assert first.asks == [[101.0, 8.0], [102.0, 4.0]]

    agg.on_mexc_depth(
        "PEPE_USDT",
        bids=[[100.5, 7.0]],
        asks=[[101.5, 6.0]],
        ts=2.0,
    )

    second = agg.get_book("PEPE_USDT")
    assert second is not None
    assert second.bids == [[100.5, 7.0]]
    assert second.asks == [[101.5, 6.0]]
    assert [p for p, _q in second.bids] == [100.5]
    assert [p for p, _q in second.asks] == [101.5]
    assert second.ts == 2.0


def test_mexc_depth_full_snapshot_drops_zero_qty_rows():
    agg = Aggregator(cfg=None)
    agg.configure_symbols({"PEPE_USDT": "PEPEUSDT"})

    agg.on_mexc_depth(
        "PEPE_USDT",
        bids=[[100.0, 10.0], [99.0, 0.0]],
        asks=[[101.0, 8.0], [102.0, 0.0]],
        ts=1.0,
    )

    book = agg.get_book("PEPE_USDT")
    assert book is not None
    assert book.bids == [[100.0, 10.0]]
    assert book.asks == [[101.0, 8.0]]
