from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import UserAccount
from .mexc_api import MexcFuturesAPI


class MexcTrader:
    def __init__(self, account: UserAccount, proxy: Optional[str] = None):
        self.api = MexcFuturesAPI(account, proxy=proxy)

    async def close(self) -> None:
        await self.api.close()

    async def get_available_usdt(self) -> float:
        v = await self.api.get_available_usdt()
        return float(v or 0.0)

    async def get_positions_raw(self) -> List[Dict[str, Any]]:
        res = await self.api.get_positions()
        if res.get("success"):
            data = res.get("data") or []
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        return []

    async def get_contract_detail(self, symbol_full: str) -> Optional[Dict[str, Any]]:
        sym = symbol_full if "_" in symbol_full else f"{symbol_full}_USDT"
        data = await self.api._request_market(f"api/v1/contract/detail?symbol={sym}")
        if not data.get("success"):
            return None
        raw = data.get("data")
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict):
            return None
        return raw

    async def is_zero_fee_symbol(self, symbol_full: str) -> bool:
        """Live check: symbol must currently have 0 trading fee.

        The flag can disappear, so we check just-in-time before each open.
        """
        d = await self.get_contract_detail(symbol_full)
        if not d:
            return False
        flag = d.get("isZeroFeeSymbol")
        if flag is None:
            flag = d.get("zeroFee") or d.get("isZeroFee")
        try:
            if isinstance(flag, str):
                flag = flag.strip()
            return bool(int(flag)) if isinstance(flag, (int, str)) else bool(flag)
        except Exception:
            return bool(flag)

    async def get_max_leverage(self, symbol_full: str) -> Optional[int]:
        d = await self.get_contract_detail(symbol_full)
        if not d:
            return None
        for k in ("maxLeverage", "max_leverage", "maxLever", "maxLeveraged"):
            if k in d:
                try:
                    return int(float(d[k]))
                except Exception:
                    pass
        return None

    async def list_zero_fee_symbols(self) -> List[str]:
        """Best-effort list of 0-fee symbols (public endpoint)."""
        data = await self.api._request_market("api/v1/contract/detail")
        if not data.get("success"):
            return []
        raw = data.get("data")
        if isinstance(raw, dict) and isinstance(raw.get("data"), list):
            raw = raw.get("data")
        if not isinstance(raw, list):
            return []

        out: List[str] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").upper()
            if not sym.endswith("_USDT"):
                continue
            flag = item.get("isZeroFeeSymbol")
            if flag is None:
                flag = item.get("zeroFee") or item.get("isZeroFee")
            if flag is None:
                # Some contract snapshots don't include a boolean flag, but provide fee rates.
                # Treat as zero-fee when both maker and taker are 0 (or a generic feeRate is 0).
                for fk in (
                    "makerFeeRate",
                    "takerFeeRate",
                    "makerFee",
                    "takerFee",
                    "feeRate",
                    "fee_rate",
                    "openFeeRate",
                    "closeFeeRate",
                ):
                    if fk in item:
                        try:
                            v = float(item.get(fk) or 0.0)
                        except Exception:
                            v = None
                        # record back to dict for later check
                        item[f"__fee__{fk}"] = v

            try:
                is_zero = bool(int(flag)) if isinstance(flag, (int, str)) else bool(flag)
            except Exception:
                is_zero = bool(flag)

            if flag is None and not is_zero:
                maker = item.get("__fee__makerFeeRate")
                taker = item.get("__fee__takerFeeRate")
                fee = item.get("__fee__feeRate")
                # if both rates known and 0 => zero fee
                try:
                    if maker is not None and taker is not None and float(maker) == 0.0 and float(taker) == 0.0:
                        is_zero = True
                    elif fee is not None and float(fee) == 0.0:
                        is_zero = True
                except Exception:
                    pass
            if is_zero:
                out.append(sym)

        out = sorted(set(out))
        if "BTC_USDT" in out:
            out.remove("BTC_USDT")
            out.insert(0, "BTC_USDT")
        return out

    async def position_metrics(self, pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        symbol_full = str(pos.get("symbol") or "")
        if not symbol_full:
            return None

        # contracts (usually) or volume
        try:
            hold_vol = float(pos.get("holdVol", 0) or 0.0)
        except Exception:
            hold_vol = 0.0
        if hold_vol <= 0:
            return None

        # entry
        try:
            open_price = (
                float(pos.get("newOpenAvgPrice") or 0)
                or float(pos.get("holdAvgPrice") or 0)
                or float(pos.get("openAvgPrice") or 0)
            )
        except Exception:
            open_price = 0.0
        if open_price <= 0:
            return None

        info = await self.api.get_contract_info_cached(symbol_full)
        if not info:
            return None

        try:
            contract_size = float(info.get("contractSize") or 1.0)
        except Exception:
            contract_size = 1.0

        # base tokens amount
        tokens = hold_vol * contract_size
        notional = tokens * open_price

        # leverage and margin
        try:
            lev = float(pos.get("leverage", 1) or 1)
        except Exception:
            lev = 1.0
        margin = notional / lev if lev > 0 else 0.0
        try:
            im_val = float(pos.get("im", 0) or 0)
            if im_val > 0:
                margin = im_val
        except Exception:
            pass

        # mark price
        mark_price: float = 0.0
        try:
            mp = await self.api.get_fair_price_cached(symbol_full)
            if mp and mp > 0:
                mark_price = float(mp)
        except Exception:
            pass
        if mark_price <= 0:
            for key in ("markPrice", "fairPrice", "lastPrice"):
                val = pos.get(key)
                if val is None:
                    continue
                try:
                    mark_price = float(val)
                    break
                except Exception:
                    continue

        # liquidation
        liq_price = 0.0
        for key in ("liquidatePrice", "liqPrice", "liquidationPrice"):
            val = pos.get(key)
            if val is None:
                continue
            try:
                liq_price = float(val)
                break
            except Exception:
                continue

        pos_type = int(pos.get("positionType") or 0)
        direction = 1 if pos_type == 1 else -1 if pos_type == 2 else 0

        # prefer exchange-provided unrealized PnL if available
        pnl_usdt: float = 0.0
        for k in (
            "unrealizedPnl",
            "unrealizedProfit",
            "unRealizedProfit",
            "unrealisedProfit",
            "unrealizedPNL",
        ):
            if k in pos and pos[k] is not None:
                try:
                    pnl_usdt = float(pos[k])
                    break
                except Exception:
                    pass
        else:
            if direction != 0 and mark_price > 0 and open_price > 0 and tokens > 0:
                pnl_usdt = (mark_price - open_price) * tokens * direction

        pnl_pct = (pnl_usdt / margin * 100.0) if margin > 0 else 0.0

        return {
            "symbol": symbol_full,
            "hold_vol": hold_vol,
            "tokens": tokens,
            "open_price": open_price,
            "mark_price": mark_price,
            "liq_price": liq_price,
            "notional": notional,
            "margin": margin,
            "leverage": lev,
            "positionType": pos_type,
            "pnl": pnl_usdt,
            "pnl_pct": pnl_pct,
        }

    async def open_market(
        self,
        symbol_full: str,
        side: str,
        notional_usdt: float,
        leverage: int,
        margin_mode: int = 1,
    ) -> Dict[str, Any]:
        symbol = symbol_full.replace("_USDT", "")
        side_code = 1 if side.upper() == "LONG" else 3
        vol = await self.api.calc_volume_from_usdt(symbol, notional_usdt, side=side_code)
        if vol is None or vol <= 0:
            return {"success": False, "message": "Could not calc volume"}
        return await self.api.create_order(
            symbol,
            side=side_code,
            order_type="5",
            vol=float(vol),
            leverage=int(leverage),
            margin_mode=margin_mode,
        )

    async def open_limit(
        self,
        symbol_full: str,
        side: str,
        notional_usdt: float,
        leverage: int,
        price: float,
        margin_mode: int = 1,
    ) -> Dict[str, Any]:
        """Place a maker limit order to open. Returns the create_order response."""
        symbol = symbol_full.replace("_USDT", "")
        side_code = 1 if side.upper() == "LONG" else 3
        vol = await self.api.calc_volume_from_usdt(
            symbol, notional_usdt, price_override=price, side=side_code,
        )
        if vol is None or vol <= 0:
            return {"success": False, "message": "Could not calc volume"}
        return await self.api.create_order(
            symbol,
            side=side_code,
            order_type="2",
            price=float(price),
            vol=float(vol),
            leverage=int(leverage),
            margin_mode=margin_mode,
        )

    async def cancel_all_for(self, symbol_full: str) -> Dict[str, Any]:
        return await self.api.cancel_orders(symbol_full)

    async def query_order(self, order_id: int) -> Dict[str, Any]:
        """Best-effort order state lookup."""
        return await self.api._request(
            "GET",
            "private/order/batch_query",
            params={"order_ids": str(int(order_id))},
        )

    async def close_market(
        self,
        symbol_full: str,
        side: str,
        hold_vol: float,
        leverage: int,
        margin_mode: int = 1,
    ) -> Dict[str, Any]:
        symbol = symbol_full.replace("_USDT", "")
        # В web-private MEXC Futures коды стороны отличаются от OpenAPI:
        #   1 = open long (BUY)
        #   3 = open short (SELL)
        #   4 = close long (SELL)
        #   2 = close short (BUY)
        # Ранее тут было перепутано (LONG->2, SHORT->4), из-за чего биржа
        # отвечала: "Position is nonexistent or closed" при попытке закрытия.
        side_code = 4 if side.upper() == "LONG" else 2
        return await self.api.create_order(
            symbol,
            side=side_code,
            order_type="5",
            vol=float(hold_vol),
            leverage=int(leverage),
            margin_mode=margin_mode,
        )

    async def place_stop_by_position(
        self,
        position_id: int,
        *,
        stop_loss_price: Optional[float | str] = None,
        take_profit_price: Optional[float | str] = None,
        side: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create TP/SL stop-plan order for an existing position (web-private endpoint).

        NOTE: We only use stop-loss for now. take_profit_price is optional.
        """
        profit_trend = None
        loss_trend = None
        if side:
            s = side.upper()
            # MEXC web uses trend flags; empirically:
            #  - LONG: profit triggers on price going up, loss triggers on price going down
            #  - SHORT: profit triggers on price going down, loss triggers on price going up
            if s == "LONG":
                profit_trend = 2
                loss_trend = 1
            elif s == "SHORT":
                profit_trend = 1
                loss_trend = 2

        return await self.api.add_stop_order_by_position(
            int(position_id),
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            profit_trend=profit_trend,
            loss_trend=loss_trend,
        )

    async def change_stop_plan_price(
        self,
        stop_plan_order_id: int,
        *,
        stop_loss_price: Optional[float | str] = None,
        take_profit_price: Optional[float | str] = None,
        side: Optional[str] = None,
    ) -> Dict[str, Any]:
        profit_trend = None
        loss_trend = None
        if side:
            s = side.upper()
            if s == "LONG":
                profit_trend = 2
                loss_trend = 1
            elif s == "SHORT":
                profit_trend = 1
                loss_trend = 2

        return await self.api.change_stop_order_plan_price(
            int(stop_plan_order_id),
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            profit_trend=profit_trend,
            loss_trend=loss_trend,
        )

    async def get_open_stop_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        return await self.api.get_open_stop_orders(symbol)
