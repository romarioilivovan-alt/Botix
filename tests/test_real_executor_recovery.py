import asyncio
import pathlib
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.config import AppConfig
from backend.engine import Engine
from backend.opportunity import Opportunity
from backend.persistence import Store
from backend.real import RealExecutor, _Quote
from backend.state import AppState, ManagedPosition, OrderBook, SymbolStats


class _Agg:
    def compute_stats(self, symbol):
        return SymbolStats(fair=10.0, sigma_spread=0.1, score=1.0)

    def contract_size_for(self, symbol):
        return 1.0


class _SignalAgg:
    def __init__(
        self,
        *,
        spread_bps: float = -2.0,
        mexc_book_age_ms: float = 25.0,
        binance_book_age_ms: float = 25.0,
    ):
        self.spread_bps = spread_bps
        self.mexc_book_age_ms = mexc_book_age_ms
        self.binance_book_age_ms = binance_book_age_ms

    def compute_stats(self, symbol):
        fair = 10.0
        mexc_mid = fair * (1.0 + self.spread_bps / 1e4)
        return SymbolStats(
            fair=fair,
            mexc_mid=mexc_mid,
            spread_bps=self.spread_bps,
            sigma_spread=0.1,
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


class _Opp:
    def evaluate(self, symbol, st):
        st.score = 2.0
        st.side_hint = "LONG"
        st.blocked_reason = None


class _Trader:
    def __init__(self):
        self.api = SimpleNamespace(get_contract_info_cached=self._get_contract_info_cached)
        self.closed = []
        self.canceled = []

    async def _get_contract_info_cached(self, symbol):
        return {"priceUnit": 0.01, "contractSize": 1.0}

    async def position_metrics(self, pos):
        return {
            "open_price": float(pos["open_price"]),
            "hold_vol": float(pos["holdVol"]),
            "leverage": float(pos.get("leverage", 100)),
            "margin": 10.0,
            "notional": 1000.0,
            "pnl": 0.0,
            "pnl_pct": 0.0,
        }

    async def get_positions_raw(self):
        return [
            {"symbol": "TAO_USDT", "positionType": 1, "holdVol": 2.0, "leverage": 100},
            {"symbol": "PEPE_USDT", "positionType": 1, "holdVol": 5.0, "leverage": 100},
        ]

    async def close_market(self, symbol, side, hold, lev):
        self.closed.append((symbol, side, hold, lev))
        return {"success": True}

    async def cancel_all_for(self, symbol):
        self.canceled.append(symbol)
        return {"success": True}

    async def get_history_positions(self, **kwargs):
        return []

    async def get_usdt_balance_snapshot(self):
        return {"equity": 123.0, "available": 120.0}

    async def is_zero_fee_symbol(self, symbol):
        return True

    async def get_max_leverage(self, symbol):
        return 100


class _FillTrader(_Trader):
    async def get_positions_raw(self):
        return [
            {
                "symbol": "TAO_USDT",
                "positionType": 1,
                "holdVol": 2.0,
                "leverage": 100,
                "openAvgPrice": 10.0,
            }
        ]


class _Alloc:
    def decide(self, *args, **kwargs):
        return SimpleNamespace(
            accept=True,
            notional_usdt=100.0,
            margin_usdt=10.0,
            leverage=10,
            reason="ok",
        )


class _MemoryStore:
    def __init__(self, payloads=None):
        self.payloads = dict(payloads or {})
        self.upserts = []
        self.deletes = []

    async def get_managed_position(self, mode, symbol):
        return self.payloads.get((mode, symbol))

    async def upsert_managed_position(self, mode, symbol, payload):
        self.payloads[(mode, symbol)] = dict(payload)
        self.upserts.append((mode, symbol))

    async def delete_managed_position(self, mode, symbol):
        self.payloads.pop((mode, symbol), None)
        self.deletes.append((mode, symbol))


def _managed_position(*, side="LONG", entry=100.0, qty=2.0) -> ManagedPosition:
    return ManagedPosition(
        symbol="TAO_USDT",
        side=side,
        entry_price=entry,
        notional_usdt=200.0,
        margin_usdt=20.0,
        leverage=10.0,
        qty=qty,
        open_ts=1_700_000_000.0,
        fair_at_open=entry,
        sigma_at_open=0.1,
        contract_size=1.0,
    )


def test_recover_untracked_positions_respects_line_universe():
    cfg = AppConfig()
    cfg.universe.include_only = ["TAO_USDT"]

    state = AppState()
    state.universe = ["TAO_USDT"]
    executor = RealExecutor(
        cfg,
        state,
        _SignalAgg(),
        opp=SimpleNamespace(),
        alloc=SimpleNamespace(),
        store=SimpleNamespace(),
        mexc_trader=_Trader(),
    )

    raw_by_sym = {
        "TAO_USDT": {
            "symbol": "TAO_USDT",
            "positionType": 1,
            "holdVol": 2.0,
            "open_price": 10.0,
            "leverage": 100,
            "createTime": 1_700_000_000_000,
        },
        "PEPE_USDT": {
            "symbol": "PEPE_USDT",
            "positionType": 1,
            "holdVol": 5.0,
            "open_price": 0.00001,
            "leverage": 100,
            "createTime": 1_700_000_000_000,
        },
    }

    asyncio.run(executor._recover_untracked_positions(raw_by_sym))

    assert set(state.positions) == {"TAO_USDT"}
    assert "PEPE_USDT" not in state.positions
    assert any("ignoring foreign account position PEPE_USDT" in log.msg for log in state.logs)


def test_recover_untracked_positions_restores_persisted_context():
    cfg = AppConfig()
    state = AppState()
    payload = _managed_position(entry=10.0).__dict__.copy()
    payload.update(
        {
            "entry_algo": "raw_momentum",
            "entry_score": 3.25,
            "entry_latency_ms": 187.0,
            "quote_ts": 1_700_000_000.0,
            "signal_ts": 1_700_000_000.0,
            "mexc_position_id": 77,
        }
    )
    store = _MemoryStore({("real", "TAO_USDT"): payload})
    executor = RealExecutor(
        cfg,
        state,
        _Agg(),
        opp=SimpleNamespace(),
        alloc=SimpleNamespace(),
        store=store,
        mexc_trader=_Trader(),
    )

    raw_by_sym = {
        "TAO_USDT": {
            "symbol": "TAO_USDT",
            "positionType": 1,
            "holdVol": 2.0,
            "open_price": 10.0,
            "leverage": 100,
            "createTime": 1_700_000_000_000,
            "positionId": 77,
        }
    }

    asyncio.run(executor._recover_untracked_positions(raw_by_sym))

    pos = state.positions["TAO_USDT"]
    assert pos.entry_algo == "raw_momentum"
    assert pos.entry_score == 3.25
    assert pos.entry_latency_ms == 187.0
    assert ("real", "TAO_USDT") in store.upserts


def test_kill_all_scopes_to_line_symbols():
    cfg = AppConfig()
    state = AppState()
    state.universe = ["TAO_USDT"]
    trader = _Trader()
    executor = RealExecutor(
        cfg,
        state,
        _SignalAgg(),
        opp=SimpleNamespace(),
        alloc=SimpleNamespace(),
        store=SimpleNamespace(),
        mexc_trader=trader,
    )
    engine = Engine(cfg, state)
    engine.trader = trader
    engine.executor = executor

    asyncio.run(engine.kill_all())

    assert trader.closed == [("TAO_USDT", "LONG", 2.0, 100)]
    assert trader.canceled == ["TAO_USDT"]


def test_external_close_uses_exchange_history_when_position_disappears():
    class _HistoryTrader(_Trader):
        async def get_history_positions(self, **kwargs):
            return [{
                "positionId": 42,
                "symbol": "TAO_USDT",
                "positionType": 1,
                "state": 3,
                "holdVol": 2.0,
                "closeVol": 2.0,
                "openAvgPrice": 100.0,
                "closeAvgPrice": 101.5,
                "realised": 3.0,
                "createTime": 1_700_000_000_000,
                "updateTime": 1_700_000_010_000,
            }]

    cfg = AppConfig()
    state = AppState()
    trader = _HistoryTrader()
    executor = RealExecutor(
        cfg,
        state,
        _SignalAgg(),
        opp=SimpleNamespace(),
        alloc=SimpleNamespace(),
        store=SimpleNamespace(),
        mexc_trader=trader,
    )

    pos = _managed_position()
    pos.mexc_position_id = 42

    details = asyncio.run(executor._resolve_external_close_details(pos, fallback_exit_price=100.0))

    assert details["price_source"] == "exchange_history"
    assert details["exit_price"] == 101.5
    assert details["realized_pnl"] == 3.0
    assert details["close_ts"] == 1_700_000_010.0


def test_external_close_falls_back_to_last_unrealized_pnl_when_history_is_missing():
    cfg = AppConfig()
    state = AppState()
    trader = _Trader()
    executor = RealExecutor(
        cfg,
        state,
        _SignalAgg(),
        opp=SimpleNamespace(),
        alloc=SimpleNamespace(),
        store=SimpleNamespace(),
        mexc_trader=trader,
    )

    pos = _managed_position(side="SHORT", entry=10.0, qty=2.0)
    pos.last_pnl_usdt = -4.0

    details = asyncio.run(executor._resolve_external_close_details(pos, fallback_exit_price=10.0))

    assert details["price_source"] == "exchange_unrealized_fallback"
    assert details["realized_pnl"] == -4.0
    assert details["exit_price"] == 12.0


def test_mark_closed_externally_clears_persisted_position():
    cfg = AppConfig()
    state = AppState()
    store = _MemoryStore()
    executor = RealExecutor(
        cfg,
        state,
        _Agg(),
        opp=SimpleNamespace(),
        alloc=SimpleNamespace(),
        store=store,
        mexc_trader=_Trader(),
    )

    pos = _managed_position()
    state.positions[pos.symbol] = pos
    asyncio.run(executor._persist_managed_position(pos))
    asyncio.run(
        executor._mark_closed_externally(
            pos,
            exit_price=101.0,
            reason="tp",
            price_source="order_avg",
        )
    )

    assert pos.symbol not in state.positions
    assert ("real", pos.symbol) in store.deletes


def test_store_managed_positions_sidecar_roundtrip(tmp_path):
    store = Store(tmp_path / "run.sqlite")

    async def _roundtrip():
        await store.open()
        await store.upsert_managed_position("real", "TAO_USDT", {"entry_algo": "raw_momentum"})
        loaded = await store.get_managed_position("real", "TAO_USDT")
        assert loaded == {"entry_algo": "raw_momentum"}
        await store.delete_managed_position("real", "TAO_USDT")
        cleared = await store.get_managed_position("real", "TAO_USDT")
        assert cleared is None
        await store.close()

    asyncio.run(_roundtrip())
    assert (tmp_path / "managed_positions.json").exists()


def test_signal_valid_now_rejects_stale_signal_age():
    cfg = AppConfig()
    cfg.strategy.signal_max_age_ms = 100
    state = AppState()
    executor = RealExecutor(
        cfg,
        state,
        _SignalAgg(),
        opp=_Opp(),
        alloc=SimpleNamespace(),
        store=SimpleNamespace(),
        mexc_trader=_Trader(),
    )

    ok, why = executor._signal_valid_now(
        "TAO_USDT",
        "LONG",
        signal_ts=time.time() - 1.0,
        spread_bps_at_quote=-2.0,
    )

    assert ok is False
    assert why.startswith("signal_age=")


def test_signal_valid_now_allows_small_signal_age_overrun_when_books_are_fresh():
    cfg = AppConfig()
    cfg.strategy.signal_max_age_ms = 100
    state = AppState()
    executor = RealExecutor(
        cfg,
        state,
        _SignalAgg(mexc_book_age_ms=60.0, binance_book_age_ms=80.0),
        opp=_Opp(),
        alloc=SimpleNamespace(),
        store=SimpleNamespace(),
        mexc_trader=_Trader(),
    )

    ok, why = executor._signal_valid_now(
        "TAO_USDT",
        "LONG",
        signal_ts=time.time() - 0.22,
        spread_bps_at_quote=-2.0,
    )

    assert ok is True
    assert why == ""


def test_signal_valid_now_rejects_small_signal_age_overrun_when_books_are_stale():
    cfg = AppConfig()
    cfg.strategy.signal_max_age_ms = 100
    state = AppState()
    executor = RealExecutor(
        cfg,
        state,
        _SignalAgg(mexc_book_age_ms=260.0, binance_book_age_ms=80.0),
        opp=_Opp(),
        alloc=SimpleNamespace(),
        store=SimpleNamespace(),
        mexc_trader=_Trader(),
    )

    ok, why = executor._signal_valid_now(
        "TAO_USDT",
        "LONG",
        signal_ts=time.time() - 0.22,
        spread_bps_at_quote=-2.0,
    )

    assert ok is False
    assert why.startswith("signal_age=")


def test_signal_valid_now_rejects_spread_drift():
    cfg = AppConfig()
    cfg.strategy.pre_submit_max_spread_drift_bps = 0.8
    state = AppState()
    executor = RealExecutor(
        cfg,
        state,
        _SignalAgg(spread_bps=-0.2),
        opp=_Opp(),
        alloc=SimpleNamespace(),
        store=SimpleNamespace(),
        mexc_trader=_Trader(),
    )

    ok, why = executor._signal_valid_now(
        "TAO_USDT",
        "LONG",
        signal_ts=time.time(),
        spread_bps_at_quote=-2.0,
    )

    assert ok is False
    assert why.startswith("spread_drift=")


def test_maybe_place_quote_keeps_signal_timestamp_for_real_taker_entries():
    cfg = AppConfig()
    cfg.strategy.taker_entry = True
    state = AppState()
    executor = RealExecutor(
        cfg,
        state,
        _SignalAgg(spread_bps=-1.5),
        opp=_Opp(),
        alloc=_Alloc(),
        store=SimpleNamespace(),
        mexc_trader=_Trader(),
    )

    opp = Opportunity(
        symbol="TAO_USDT",
        side="LONG",
        score=2.0,
        entry_price=10.0,
        fair=10.0,
        sigma=0.1,
        z=0.0,
        algorithm="raw_momentum",
        signal_ts=1234.5,
    )

    asyncio.run(executor._maybe_place_quote(opp))

    quote = executor._quotes["TAO_USDT"]
    assert quote.signal_ts == 1234.5
    assert quote.spread_bps_at_quote == 0.0


def test_refresh_balance_periodically_updates_state_without_name_errors():
    cfg = AppConfig()
    state = AppState()
    executor = RealExecutor(
        cfg,
        state,
        _Agg(),
        opp=SimpleNamespace(),
        alloc=SimpleNamespace(),
        store=SimpleNamespace(),
        mexc_trader=_Trader(),
    )

    asyncio.run(executor._refresh_balance_periodically())

    assert state.balance == 123.0
    assert state.available_balance == 120.0


def test_close_market_retries_rate_limit_rejects():
    class _RetryTrader(_Trader):
        def __init__(self):
            super().__init__()
            self.close_calls = 0

        async def close_market(self, symbol, side, hold, lev, margin_mode=1):
            self.close_calls += 1
            if self.close_calls == 1:
                return {"success": False, "code": 510, "message": "Requests are too frequent, please try again later"}
            return {"success": True, "data": {"orderId": 123456}}

    cfg = AppConfig()
    state = AppState()
    trader = _RetryTrader()
    executor = RealExecutor(
        cfg,
        state,
        _SignalAgg(),
        opp=SimpleNamespace(),
        alloc=SimpleNamespace(),
        store=SimpleNamespace(),
        mexc_trader=trader,
    )

    closed = {}

    async def _fake_resolve_close_price(symbol, order_id, fallback_exit_price):
        return (99.5, False)

    async def _fake_mark_closed_externally(pos, *, exit_price, reason, price_source, order_id=None):
        closed["symbol"] = pos.symbol
        closed["reason"] = reason
        closed["price_source"] = price_source
        closed["order_id"] = order_id
        closed["exit_price"] = exit_price

    executor._resolve_close_price = _fake_resolve_close_price
    executor._mark_closed_externally = _fake_mark_closed_externally

    pos = ManagedPosition(
        symbol="TAO_USDT",
        side="LONG",
        entry_price=100.0,
        notional_usdt=1000.0,
        margin_usdt=10.0,
        leverage=100.0,
        qty=2.0,
        open_ts=time.time() - 1.0,
        fair_at_open=100.0,
        sigma_at_open=0.1,
    )

    asyncio.run(executor._close_market(pos, {"holdVol": 2.0, "leverage": 100}, reason="scratch"))

    assert trader.close_calls == 2
    assert closed["symbol"] == "TAO_USDT"
    assert closed["reason"] == "scratch"
    assert closed["price_source"] == "book_fallback"
    assert closed["order_id"] == 123456
    assert closed["exit_price"] == 99.5


def test_real_loop_uses_strategy_tick_sec():
    cfg = AppConfig()
    cfg.strategy.paper_tick_sec = 0.05
    executor = RealExecutor(
        cfg,
        AppState(),
        _SignalAgg(),
        opp=SimpleNamespace(),
        alloc=SimpleNamespace(),
        store=SimpleNamespace(),
        mexc_trader=_Trader(),
    )

    assert executor._loop_tick_sec() == 0.05


def test_materialize_filled_quote_opens_position_without_waiting_for_next_tick():
    cfg = AppConfig()
    state = AppState()
    trader = _FillTrader()
    executor = RealExecutor(
        cfg,
        state,
        _SignalAgg(),
        opp=SimpleNamespace(),
        alloc=SimpleNamespace(),
        store=SimpleNamespace(),
        mexc_trader=trader,
    )

    quote = _Quote(
        symbol="TAO_USDT",
        side="LONG",
        price=10.01,
        notional=100.0,
        margin=10.0,
        leverage=10,
        placed_ts=time.time(),
        fair_at_quote=10.0,
        sigma_at_quote=0.1,
        z_at_quote=1.0,
        signal_ts=time.time() - 0.05,
    )
    executor._quotes[quote.symbol] = quote

    opened = asyncio.run(
        executor._materialize_filled_quote(
            quote,
            fair=10.0,
            sigma=0.1,
            retry_delays=(0.0,),
        )
    )

    assert opened is True
    assert quote.symbol not in executor._quotes
    assert quote.symbol in state.positions
