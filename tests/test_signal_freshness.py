import pathlib
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.config import AppConfig
from backend.paper import PaperExecutor
from backend.real import RealExecutor
from backend.state import AppState, OrderBook, SymbolStats


class _FlatAgg:
    def __init__(self, *, mexc_book_age_ms: float = 25.0, binance_book_age_ms: float = 25.0):
        self.mexc_book_age_ms = mexc_book_age_ms
        self.binance_book_age_ms = binance_book_age_ms

    def compute_stats(self, symbol):
        return SymbolStats(
            fair=10.0,
            mexc_mid=9.99,
            spread_bps=-1.0,
            sigma_spread=0.1,
            score=0.0,
            mexc_book_age_ms=self.mexc_book_age_ms,
            binance_book_age_ms=self.binance_book_age_ms,
        )

    def contract_size_for(self, symbol):
        return 1.0

    def get_book(self, symbol):
        return OrderBook(
            bids=[[9.99, 1000.0]],
            asks=[[10.01, 1000.0]],
            ts=time.time(),
        )


class _FlatOpp:
    def evaluate(self, symbol, st):
        st.score = 0.0
        st.side_hint = None
        st.blocked_reason = "flat_velocity=0.00"

    def evaluate_multi(self, symbol, st, ov):
        self.evaluate(symbol, st)


class _FlipOpp:
    def evaluate(self, symbol, st):
        st.score = 3.0
        st.side_hint = "SHORT"
        st.blocked_reason = None

    def evaluate_multi(self, symbol, st, ov):
        self.evaluate(symbol, st)


class _Alloc:
    def decide(self, *args, **kwargs):
        return SimpleNamespace(accept=True)


class _Store:
    async def insert_trade(self, trade):
        return None


class _Trader:
    api = SimpleNamespace(get_contract_info_cached=None)


def _paper_cfg():
    cfg = AppConfig()
    cfg.strategy.signal_max_age_ms = 500
    cfg.strategy.pre_submit_max_spread_drift_bps = 2.0
    return cfg


def _real_cfg():
    cfg = AppConfig()
    cfg.strategy.signal_max_age_ms = 500
    cfg.strategy.pre_submit_max_spread_drift_bps = 2.0
    return cfg


def test_paper_fill_recheck_tolerates_flattened_signal_if_age_and_drift_are_ok():
    executor = PaperExecutor(
        _paper_cfg(),
        AppState(),
        _FlatAgg(),
        _FlatOpp(),
        _Alloc(),
        _Store(),
    )
    quote = SimpleNamespace(
        signal_ts=time.time(),
        spread_bps_at_quote=-1.0,
    )

    ok, why = executor._signal_valid_for_fill("PEPE_USDT", "LONG", quote)

    assert ok is True
    assert why == ""


def test_paper_fill_recheck_still_rejects_explicit_side_flip():
    executor = PaperExecutor(
        _paper_cfg(),
        AppState(),
        _FlatAgg(),
        _FlipOpp(),
        _Alloc(),
        _Store(),
    )
    quote = SimpleNamespace(
        signal_ts=time.time(),
        spread_bps_at_quote=-1.0,
    )

    ok, why = executor._signal_valid_for_fill("PEPE_USDT", "LONG", quote)

    assert ok is False
    assert why == "side_flip=SHORT"


def test_paper_fill_recheck_allows_small_signal_age_overrun_when_books_are_fresh():
    cfg = _paper_cfg()
    cfg.strategy.signal_max_age_ms = 100
    executor = PaperExecutor(
        cfg,
        AppState(),
        _FlatAgg(mexc_book_age_ms=40.0, binance_book_age_ms=55.0),
        _FlatOpp(),
        _Alloc(),
        _Store(),
    )
    quote = SimpleNamespace(
        signal_ts=time.time() - 0.14,
        spread_bps_at_quote=-1.0,
    )

    ok, why = executor._signal_valid_for_fill("PEPE_USDT", "LONG", quote)

    assert ok is True
    assert why == ""


def test_paper_fill_recheck_rejects_small_signal_age_overrun_when_books_are_stale():
    cfg = _paper_cfg()
    cfg.strategy.signal_max_age_ms = 100
    executor = PaperExecutor(
        cfg,
        AppState(),
        _FlatAgg(mexc_book_age_ms=260.0, binance_book_age_ms=55.0),
        _FlatOpp(),
        _Alloc(),
        _Store(),
    )
    quote = SimpleNamespace(
        signal_ts=time.time() - 0.14,
        spread_bps_at_quote=-1.0,
    )

    ok, why = executor._signal_valid_for_fill("PEPE_USDT", "LONG", quote)

    assert ok is False
    assert why.startswith("signal_age=")


def test_real_submit_recheck_tolerates_flattened_signal_if_age_and_drift_are_ok():
    executor = RealExecutor(
        _real_cfg(),
        AppState(),
        _FlatAgg(),
        _FlatOpp(),
        _Alloc(),
        _Store(),
        mexc_trader=_Trader(),
    )

    ok, why = executor._signal_valid_now(
        "PEPE_USDT",
        "LONG",
        signal_ts=time.time(),
        spread_bps_at_quote=-1.0,
    )

    assert ok is True
    assert why == ""


def test_real_submit_recheck_still_rejects_explicit_side_flip():
    executor = RealExecutor(
        _real_cfg(),
        AppState(),
        _FlatAgg(),
        _FlipOpp(),
        _Alloc(),
        _Store(),
        mexc_trader=_Trader(),
    )

    ok, why = executor._signal_valid_now(
        "PEPE_USDT",
        "LONG",
        signal_ts=time.time(),
        spread_bps_at_quote=-1.0,
    )

    assert ok is False
    assert why == "side_flip=SHORT"
