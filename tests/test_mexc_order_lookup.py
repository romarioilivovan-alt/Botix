import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.mexc_api import MexcFuturesAPI
from backend.mexc_trader import MexcTrader
from backend.models import UserAccount


def test_normalize_order_lookup_wraps_single_object():
    api = MexcFuturesAPI(UserAccount(uid="WEBtest"))

    normalized = api._normalize_order_lookup_response(
        {
            "success": True,
            "code": 0,
            "data": {
                "orderId": 123,
                "state": 3,
                "dealVol": 10,
            },
        }
    )

    assert normalized["success"] is True
    assert normalized["data"] == [{"orderId": 123, "state": 3, "dealVol": 10}]


def test_query_order_uses_single_order_lookup_endpoint():
    class _API:
        def __init__(self):
            self.calls = []

        async def get_order(self, order_id: int):
            self.calls.append(order_id)
            return {
                "success": True,
                "data": [{"orderId": order_id, "state": 3, "dealVol": 7}],
            }

    trader = object.__new__(MexcTrader)
    trader.api = _API()

    result = asyncio.run(MexcTrader.query_order(trader, 987654321))

    assert trader.api.calls == [987654321]
    assert result["success"] is True
    assert result["data"][0]["orderId"] == 987654321


def test_hot_private_endpoint_routing_prefers_trade_lane():
    api = MexcFuturesAPI(UserAccount(uid="WEBtest"))

    assert api._is_hot_private_endpoint("private/order/create") is True
    assert api._is_hot_private_endpoint("private/order/get/123") is True
    assert api._is_hot_private_endpoint("private/position/open_positions") is True
    assert api._is_hot_private_endpoint("private/account/assets") is True

    assert api._is_hot_private_endpoint("private/position/list/history_positions") is False
    assert api._is_hot_private_endpoint("private/order/list/open_orders") is False
