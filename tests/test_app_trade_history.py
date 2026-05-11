import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.app import _merge_recent_trades, _normalize_exchange_history_row


def test_normalize_exchange_history_row():
    row = {
        "symbol": "PEPE_USDT",
        "positionType": 1,
        "openAvgPrice": 0.00000418,
        "closeAvgPrice": 0.00000421,
        "realised": 1.25,
        "im": 100.0,
        "createTime": 1_700_000_000_000,
        "updateTime": 1_700_000_002_000,
        "state": 3,
    }
    item = _normalize_exchange_history_row(row)
    assert item["symbol"] == "PEPE_USDT"
    assert item["side"] == "LONG"
    assert item["entry"] == 0.00000418
    assert item["exit"] == 0.00000421
    assert item["pnl"] == 1.25
    assert item["pnl_pct"] == 1.25
    assert item["reason"] == "exchange_history"
    assert item["price_source"] == "exchange_history"
    assert item["duration"] == 2.0


def test_merge_recent_trades_prefers_non_empty_fields():
    snapshot_items = [{
        "ts": 1700000002.0,
        "symbol": "PEPE_USDT",
        "side": "LONG",
        "entry": 0.1,
        "exit": 0.2,
        "pnl": 1.0,
        "reason": "scratch",
        "price_source": None,
    }]
    exchange_items = [{
        "ts": 1700000002.0,
        "symbol": "PEPE_USDT",
        "side": "LONG",
        "entry": 0.1,
        "exit": 0.2,
        "pnl": 1.0,
        "reason": "exchange_history",
        "price_source": "exchange_history",
    }]
    merged = _merge_recent_trades(snapshot_items, exchange_items, limit=10)
    assert len(merged) == 1
    assert merged[0]["price_source"] == "exchange_history"
