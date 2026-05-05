import json
import logging
import hashlib
import time
import aiohttp
import random
import string
import math
import asyncio
from typing import Dict, Optional, Any
import re
from urllib.parse import urlparse
from decimal import Decimal
from .models import UserAccount
try:
    from aiohttp_socks import ProxyConnector
except ImportError:
    ProxyConnector = None  # для SOCKS-прокси нужно будет поставить aiohttp_socks


logger = logging.getLogger(__name__)

_CONTRACT_LIMIT_RE = re.compile(
    r"maximum number of contracts\s+(\d+)", re.IGNORECASE
)


def parse_contracts_limit_from_message(message: str) -> Optional[int]:
    if not message:
        return None
    m = _CONTRACT_LIMIT_RE.search(message)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


CONTRACT_CACHE_TTL = 3600.0
FAIR_PRICE_CACHE_TTL = 0.7
LAST_PRICE_CACHE_TTL = 0.7
TICKER_CACHE_TTL = 0.7

PRICE_SAFETY_K = 1.01  # небольшой запас по цене (1%)


_CONTRACT_CACHE: Dict[str, tuple[dict, float]] = {}
_FAIR_PRICE_CACHE: Dict[str, tuple[float, float]] = {}
_LAST_PRICE_CACHE: Dict[str, tuple[float, float]] = {}
_TICKER_CACHE: Dict[str, tuple[dict, float]] = {}


class MexcSignatureGenerator:
    def __init__(self, authorization_token: str):
        self.authorization = authorization_token

    def generate_signature(self, method: str, endpoint: str, body_data: Any) -> tuple[int, str]:
        timestamp = int(time.time() * 1000)
        first_md5_input = f"{self.authorization}{timestamp}"
        first_md5 = hashlib.md5(first_md5_input.encode()).hexdigest()[7:]
        if body_data and method in ("POST", "PUT"):
            body_string = json.dumps(body_data, separators=(",", ":"))
        else:
            body_string = ""
        final_input = f"{timestamp}{body_string}{first_md5}"
        signature = hashlib.md5(final_input.encode()).hexdigest()
        return timestamp, signature


class MexcFuturesAPI:
    CONTRACT_BASE_URL = "https://contract.mexc.com"

    def __init__(self, account: UserAccount, proxy: Optional[str] = None):
        self.base_url = "https://www.mexc.com/api/platform/futures/api/v1"
        self.account = account
        self.sg = MexcSignatureGenerator(account.uid)
        self.session: Optional[aiohttp.ClientSession] = None

        # Derive a stable per-account mhash when not provided.
        # MEXC web uses `mhash` as a query/body param for some private endpoints.
        try:
            if not (getattr(self.account, "mhash", "") or "").strip():
                base = f"{getattr(self.account, 'uid', '')}{getattr(self.account, 'device_id', '')}".encode("utf-8")
                if base:
                    self.account.mhash = hashlib.md5(base).hexdigest()
        except Exception:
            pass

        # строка прокси (как хранится в БД)
        self.proxy: Optional[str] = proxy if proxy is not None else getattr(
            account,
            "proxy",
            None,
        )
        # что реально уходит в aiohttp.request(proxy=...)
        self._session_proxy: Optional[str] = None

    async def _ensure_session(self) -> None:
        if self.session is not None and not self.session.closed:
            # Rarely, the connector can be closed while session is still set,
            # which leads to "Connector is closed" / "NoneType...connect".
            conn = getattr(self.session, "_connector", None)
            if conn is not None and not getattr(conn, "closed", False):
                return
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = None

        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=10)

            connector: aiohttp.BaseConnector
            proxy_for_request: Optional[str] = None

            if self.proxy:
                parsed = urlparse(self.proxy)
                scheme = (parsed.scheme or "").lower()

                # SOCKS-прокси: socks4/socks5/socks5h
                if scheme in ("socks4", "socks5", "socks5h"):
                    if ProxyConnector is None:
                        raise RuntimeError(
                            "Для использования SOCKS-прокси установи пакет "
                            "`aiohttp_socks` (pip install aiohttp_socks)."
                        )
                    connector = ProxyConnector.from_url(self.proxy, ssl=False)
                    proxy_for_request = None
                else:
                    # обычный HTTP(S)-прокси
                    connector = aiohttp.TCPConnector(ssl=False, resolver=aiohttp.ThreadedResolver())
                    proxy_for_request = self.proxy
            else:
                connector = aiohttp.TCPConnector(ssl=False, resolver=aiohttp.ThreadedResolver())
                proxy_for_request = None

            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            )
            self._session_proxy = proxy_for_request

    async def __aenter__(self) -> "MexcFuturesAPI":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def auth_ping(self) -> dict:
        """Lightweight auth check for current MEXC web credentials."""
        return await self._request("GET", "private/account/assets")

    def _normalize_list_response(self, raw: Dict, what: str) -> Dict:
        if not raw.get("success"):
            return {
                "success": False,
                "message": raw.get("message", "Unknown error"),
                "data": [],
            }
        data = raw.get("data")
        if data is None:
            return {"success": True, "message": "", "data": []}
        if isinstance(data, list):
            return {"success": True, "message": "", "data": data}
        if isinstance(data, dict) and "list" in data and isinstance(data["list"], list):
            return {"success": True, "message": "", "data": data["list"]}
        logger.warning("Unexpected %s data format: %s - %r", what, type(data), data)
        return {"success": True, "message": "", "data": []}

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Any = None,
        params: Optional[Dict[str, Any]] = None,
        need_captcha: bool = False,
    ) -> Dict:
        await self._ensure_session()
        assert self.session is not None
        url = f"{self.base_url}/{endpoint}"
        timestamp, signature = self.sg.generate_signature(method, endpoint, data)
        headers = {
            "authorization": self.account.uid,
            "x-mxc-nonce": str(timestamp),
            "x-mxc-sign": signature,
            "content-type": "application/json",
            "x-language": "en-US",
            "language": "English",
            "platform": "H5-web",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "origin": "https://www.mexc.com",
            "referer": "https://www.mexc.com/",
        }
        # Some MEXC deployments rely on a client fingerprint header.
        try:
            did = getattr(self.account, "device_id", "") or ""
            if isinstance(did, str) and did.strip():
                did = did.strip()
                headers["device-id"] = did
                # Empirically, MEXC web futures endpoints often expect an mtoken header.
                # The web client uses a fingerprint visitor id; using the same value keeps requests consistent.
                headers["mtoken"] = did
        except Exception:
            pass
        # If no device-id was provided, still send a stable-looking mtoken.
        # Some endpoints return 4xx when mtoken header is missing.
        if "mtoken" not in headers:
            try:
                # deterministic-ish: hash of uid
                h = hashlib.md5(self.account.uid.encode()).hexdigest()
                headers["mtoken"] = h
            except Exception:
                headers["mtoken"] = "".join(random.choice("0123456789abcdef") for _ in range(32))
        body = json.dumps(data, separators=(",", ":")) if data is not None else None
        try:
            logger.info(
                "MEXC _request %s %s (proxy=%r, account=%s)",
                method,
                url,
                self.proxy,
                self.account.uid,
            )
            async with self.session.request(
                method=method,
                url=url,
                headers=headers,
                data=body,
                params=params,
                proxy=self._session_proxy,
            ) as response:
                text = await response.text()
                try:
                    result = json.loads(text)
                except Exception:
                    logger.error(
                        "Non-JSON response from MEXC %s %s: %s",
                        method,
                        url,
                        text[:500],
                    )
                    return {"success": False, "message": text}
                return result
        except Exception as e:
            logger.error("Request error: %s", e)
            return {"success": False, "message": str(e)}

    async def _request_market(self, path: str) -> Dict:
        await self._ensure_session()
        assert self.session is not None
        url = f"{self.CONTRACT_BASE_URL}/{path.lstrip('/')}"
        try:
            logger.info(
                "MEXC _request_market %s (proxy=%r, account=%s)",
                url,
                self.proxy,
                self.account.uid,
            )
            async with self.session.get(
                url,
                proxy=self._session_proxy,
                ssl=False,
                timeout=10,
            ) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except Exception:
                    logger.error(
                        "Non-JSON market response from MEXC %s: %s",
                        url,
                        text[:500],
                    )
                    return {"success": False, "message": text}
                return data
        except Exception as e:
            logger.error("Market request error: %s", e)
            return {"success": False, "message": str(e)}

    # ---------- Маркет-данные и кэши ----------

    async def get_contract_info(self, symbol: str) -> dict | None:
        full_symbol = symbol if "_" in symbol else f"{symbol}_USDT"
        data = await self._request_market(
            f"api/v1/contract/detail?symbol={full_symbol}"
        )
        if not data.get("success"):
            logger.warning("get_contract_info failed for %s: %s", full_symbol, data)
            return None
        raw = data.get("data")
        if not raw:
            logger.warning("get_contract_info: empty data for %s: %s", full_symbol, data)
            return None
        if isinstance(raw, list):
            info = raw[0]
        elif isinstance(raw, dict):
            info = raw
        else:
            logger.warning(
                "get_contract_info: unexpected data type for %s: %r (%s)",
                full_symbol,
                type(raw),
                raw,
            )
            return None
        try:
            return {
                "symbol": info["symbol"],
                "contractSize": float(info.get("contractSize", 1)),
                "volScale": int(info.get("volScale", 0)),
                "volUnit": float(info.get("volUnit", 1)),
                "minVol": float(info.get("minVol", 1)),
                # price tick info (used to snap stop-loss)
                "priceScale": int(info.get("priceScale", 0)),
                "priceUnit": float(info.get("priceUnit", 0) or 0),
            }
        except Exception as e:
            logger.warning(
                "get_contract_info parse error for %s: %s | raw=%r",
                full_symbol,
                e,
                info,
            )
            return None

    async def get_contract_info_cached(
        self,
        symbol: str,
        ttl: float = CONTRACT_CACHE_TTL,
    ) -> dict | None:
        full_symbol = symbol if "_" in symbol else f"{symbol}_USDT"
        now = time.monotonic()
        cached = _CONTRACT_CACHE.get(full_symbol)
        if cached is not None:
            info, ts = cached
            if now - ts < ttl:
                return info
        info = await self.get_contract_info(full_symbol)
        if info:
            _CONTRACT_CACHE[full_symbol] = (info, now)
        return info

    async def get_fair_price(self, symbol: str) -> float | None:
        full_symbol = symbol if "_" in symbol else f"{symbol}_USDT"
        data = await self._request_market(f"api/v1/contract/fair_price/{full_symbol}")
        if not data.get("success"):
            logger.warning("get_fair_price failed: %s", data)
            return None
        fp = data.get("data", {}).get("fairPrice")
        return float(fp) if fp is not None else None

    async def get_fair_price_cached(
        self,
        symbol: str,
        ttl: float = FAIR_PRICE_CACHE_TTL,
    ) -> float | None:
        full_symbol = symbol if "_" in symbol else f"{symbol}_USDT"
        now = time.monotonic()
        cached = _FAIR_PRICE_CACHE.get(full_symbol)
        if cached is not None:
            price, ts = cached
            if now - ts < ttl:
                return price
        price = await self.get_fair_price(full_symbol)
        if price is not None:
            _FAIR_PRICE_CACHE[full_symbol] = (price, now)
        return price

    async def get_ticker_cached(
        self,
        symbol: str,
        ttl: float = TICKER_CACHE_TTL,
    ) -> Optional[dict]:
        """
        Кэшированный тикер /contract/ticker: lastPrice, bid1, ask1, indexPrice, fairPrice и т.п.
        """
        full_symbol = symbol if "_" in symbol else f"{symbol}_USDT"
        now = time.monotonic()
        cached = _TICKER_CACHE.get(full_symbol)
        if cached is not None:
            data, ts = cached
            if now - ts < ttl:
                return data

        data = await self._request_market(
            f"api/v1/contract/ticker?symbol={full_symbol}"
        )
        if not data.get("success"):
            logger.warning("get_ticker_cached failed for %s: %s", full_symbol, data)
            return None

        raw = data.get("data") or {}
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
        if not isinstance(raw, dict):
            logger.warning("get_ticker_cached: unexpected data type for %s: %r", full_symbol, raw)
            return None

        _TICKER_CACHE[full_symbol] = (raw, now)
        return raw

    async def get_last_price_cached(
        self,
        symbol: str,
        ttl: float = LAST_PRICE_CACHE_TTL,
    ) -> Optional[float]:
        """
        Кэшированная lastPrice (по /contract/ticker).
        """
        full_symbol = symbol if "_" in symbol else f"{symbol}_USDT"
        now = time.monotonic()
        cached = _LAST_PRICE_CACHE.get(full_symbol)
        if cached is not None:
            price, ts = cached
            if now - ts < ttl:
                return price

        ticker = await self.get_ticker_cached(full_symbol, ttl=max(ttl, TICKER_CACHE_TTL))
        if not ticker:
            return None

        val = ticker.get("lastPrice")
        try:
            price = float(val)
        except (TypeError, ValueError):
            price = None

        if price is not None and price > 0:
            _LAST_PRICE_CACHE[full_symbol] = (price, now)
            return price
        return None

    async def get_price_for_side(
        self,
        symbol: str,
        side: Optional[int],
        price_override: Optional[float] = None,
    ) -> Optional[float]:
        """
        Выбирает референс-цену для расчёта объёма / риск-лимита:
        - если передан price_override > 0 — используем его (лимитный ордер);
        - иначе берём bid1/ask1/lastPrice/indexPrice/fairPrice из тикера в зависимости от стороны;
        - применяется небольшой множитель PRICE_SAFETY_K, чтобы учесть слиппедж.
        """
        full_symbol = symbol if "_" in symbol else f"{symbol}_USDT"

        if price_override is not None and price_override > 0:
            return price_override

        ticker = await self.get_ticker_cached(full_symbol)
        last_price = bid1 = ask1 = index_price = fair_price = 0.0

        if ticker:
            def _to_f(x: Any) -> float:
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return 0.0

            last_price = _to_f(ticker.get("lastPrice"))
            bid1 = _to_f(ticker.get("bid1"))
            ask1 = _to_f(ticker.get("ask1"))
            index_price = _to_f(ticker.get("indexPrice"))
            fair_price = _to_f(ticker.get("fairPrice"))

        basis = 0.0
        if side in (1, 4):  # buy side (open long / close short)
            basis = ask1 or last_price or index_price or fair_price
        elif side in (2, 3):  # sell side (open short / close long)
            basis = bid1 or last_price or index_price or fair_price
        else:
            basis = last_price or index_price or fair_price

        if basis <= 0:
            # fallback — пробуем lastPrice отдельно
            basis = await self.get_last_price_cached(full_symbol) or 0.0
        if basis <= 0:
            # ещё один fallback — fairPrice
            basis = await self.get_fair_price_cached(full_symbol) or 0.0

        if basis <= 0:
            return None

        return basis * PRICE_SAFETY_K

    # ---------- Торговля (ордеры, позиции, баланс) ----------

    async def create_order(
        self,
        symbol: str,
        side: int,
        order_type: str,
        price: Optional[float] = None,
        vol: Optional[float] = None,
        leverage: Optional[int] = None,
        margin_mode: Optional[int] = None,
    ) -> Dict:
        if vol is None:
            vol = self.account.default_size
        if leverage is None:
            leverage = self.account.default_leverage
        if margin_mode is None:
            margin_mode = getattr(self.account, "margin_mode", 1)
        if margin_mode not in (1, 2):
            margin_mode = 1

        timestamp = int(time.time() * 1000)
        p0 = self._generate_security_param()
        k0 = self._generate_security_param()
        symbol_full = symbol.upper() + "_USDT"

        payload: Dict[str, Any] = {
            "symbol": symbol_full,
            "side": side,
            "openType": margin_mode,
            "type": order_type,
            "vol": vol,
            "leverage": leverage,
            "marketCeiling": False,
            "priceProtect": "0",
            "p0": p0,
            "k0": k0,
            "chash": getattr(self.account, "chash", "d6c64d28e362f314071b3f9d78ff7494d9cd7177ae0465e772d1840e9f7905d8"),
            "ts": timestamp,
            "mhash": (getattr(self.account, "mhash", "") or "").strip(),
            "mtoken": getattr(self.account, "device_id", ""),
        }

        # для close-стороны включаем flashClose
        if side in (2, 4):
            payload["flashClose"] = True

        if price is not None and order_type == "2":  # LIMIT
            payload["price"] = str(price)

        log_payload = {
            "symbol": payload["symbol"],
            "side": payload["side"],
            "type": payload["type"],
            "openType": payload["openType"],
            "vol": payload["vol"],
            "leverage": payload["leverage"],
        }
        if "price" in payload:
            log_payload["price"] = payload["price"]
        logger.info("Creating order: %s", log_payload)

        return await self._request("POST", "private/order/create", data=payload)

    async def _postprocess_market_order_response(
        self,
        resp: Dict,
        *,
        symbol: str,
        side: int,
        order_type: str,
        vol: Optional[float],
    ) -> Dict:
        """
        Для MARKET-ордеров: после create проверяем статус через batch_query.
        Если ордер отменён с dealVol=0 (в т.ч. errorCode=6 — deviation / fair price),
        возвращаем success=False с адекватным message.
        Если биржа вернула success, но пустой data — помечаем ответ флагом
        для повторной попытки (ретрая) выше.
        """
        # нас интересуют только успешные ответы и только MARKET (type="5")
        if not resp.get("success"):
            return resp
        if str(order_type) != "5":
            return resp

        data = resp.get("data") or {}
        order_id = data.get("orderId")
        if not order_id:
            return resp

        # небольшая пауза, чтобы ордер успел проматчиться
        await asyncio.sleep(0.01)

        detail_raw = await self._request(
            method="GET",
            endpoint="private/order/batch_query",
            params={"order_ids": str(order_id)},
        )
        logger.info("Order batch_query response (%s): %s", order_id, detail_raw)

        if not detail_raw.get("success"):
            return resp

        items = detail_raw.get("data") or []
        if not items:
            resp = dict(resp)
            resp["_need_retry_empty_batch_query"] = True
            resp["_order_id"] = order_id
            logger.warning(
                "Market order %s for %s: empty data in batch_query, will retry once",
                order_id,
                symbol,
            )
            return resp

        od = items[0]
        try:
            state = int(od.get("state") or 0)
        except Exception:
            state = None
        try:
            deal_vol = float(od.get("dealVol") or 0)
        except Exception:
            deal_vol = 0.0

        error_code = od.get("errorCode") or od.get("error_code")

        # state: 1 pending, 2 unfilled, 3 filled, 4 canceled, 5 invalid
        if state in (4, 5) and deal_vol <= 0:
            if str(error_code) == "6":
                msg = (
                    "рыночный ордер был отменён биржей, потому что цена сделки "
                    "слишком сильно отличается от индексной (fair price). "
                    "Позиция *не была открыта*."
                )
            else:
                msg = f"ордер был отменён биржей, errorCode={error_code}"

            return {
                "success": False,
                "code": int(error_code) if error_code is not None else 0,
                "message": msg,
                "_order_state": state,
                "_order_deal_vol": deal_vol,
                "_order_id": order_id,
            }

        # Частично/полностью исполнен — считаем ордер успешным.
        if deal_vol > 0 and "_final_vol" not in resp:
            resp["_final_vol"] = deal_vol

        return resp

    async def create_order_with_limit(
        self,
        symbol: str,
        side: int,
        order_type: str,
        price: Optional[float] = None,
        vol: Optional[float] = None,
        leverage: Optional[int] = None,
    ) -> Dict:
        # первый заход
        resp = await self.create_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
            vol=vol,
            leverage=leverage,
        )
        resp = await self._postprocess_market_order_response(
            resp,
            symbol=symbol,
            side=side,
            order_type=order_type,
            vol=vol,
        )

        # Спецкейс: MEXC подтвердила create, но batch_query не вернул по ордеру данных.
        if resp.get("_need_retry_empty_batch_query"):
            logger.warning(
                "create_order_with_limit: empty batch_query data for market order "
                "(symbol=%s, side=%s) — retrying order once",
                symbol,
                side,
            )
            resp_retry = await self.create_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                price=price,
                vol=vol,
                leverage=leverage,
            )
            resp_retry = await self._postprocess_market_order_response(
                resp_retry,
                symbol=symbol,
                side=side,
                order_type=order_type,
                vol=vol,
            )

            if resp_retry.get("_need_retry_empty_batch_query"):
                msg = (
                    "биржа MEXC дважды подтвердила создание рыночного ордера, "
                    "но статус ордера по-прежнему недоступен. "
                    "Скорее всего, позиция *не была открыта*. "
                    "Проверь, пожалуйста, вручную открытые позиции по этому инструменту."
                )
                return {
                    "success": False,
                    "code": 0,
                    "message": msg,
                    "_order_id": resp_retry.get("_order_id") or resp.get("_order_id"),
                }

            resp = resp_retry

        if resp.get("success"):
            return resp

        # если это не наш кейс с лимитом контрактов — просто возвращаем ошибку
        limit = parse_contracts_limit_from_message(resp.get("message") or "")
        if not limit:
            return resp

        if vol is None or vol <= limit:
            return resp

        new_vol = float(limit)
        logger.warning(
            "create_order_with_limit: exceeded contracts limit "
            "(requested=%s, limit=%s, symbol=%s). Retry with vol=%s",
            vol,
            limit,
            symbol,
            new_vol,
        )

        # повтор с урезанным объёмом
        resp2 = await self.create_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
            vol=new_vol,
            leverage=leverage,
        )
        resp2 = await self._postprocess_market_order_response(
            resp2,
            symbol=symbol,
            side=side,
            order_type=order_type,
            vol=new_vol,
        )
        resp2["_reduced_from_vol"] = vol
        resp2["_contracts_limit"] = limit
        resp2["_final_vol"] = resp2.get("_final_vol", new_vol)
        return resp2

    def _norm_symbol(self, symbol: str) -> str:
        """Normalize symbol to MEXC web futures format.

        Accepts: "BCH", "BCH_USDT", "BCHUSDT".
        Returns: "BCH_USDT".
        """

        s = (symbol or "").upper().strip()
        if not s:
            return s
        if s.endswith("_USDT"):
            return s
        if s.endswith("USDT") and "_" not in s:
            return f"{s[:-4]}_USDT"
        if "_" in s:
            # Unknown quote; keep as-is.
            return s
        return f"{s}_USDT"

    async def cancel_orders(self, symbol: str) -> Dict:
        payload = {"symbol": self._norm_symbol(symbol)}
        return await self._request("POST", "private/order/cancel_all", data=payload)

    async def get_open_orders(self, symbol: Optional[str] = None) -> Dict:
        params = {"symbol": self._norm_symbol(symbol)} if symbol else None
        raw = await self._request(
            "GET",
            "private/order/list/open_orders",
            data=None,
            params=params,
        )
        logger.info("Orders API response: %s", raw)
        return self._normalize_list_response(raw, what="orders")

    async def get_open_stop_orders(self, symbol: Optional[str] = None) -> Dict:
        params = {"symbol": self._norm_symbol(symbol)} if symbol else None
        raw = await self._request(
            "GET",
            "private/stoporder/open_orders",
            data=None,
            params=params,
        )
        logger.info("Stop orders API response: %s", raw)
        return self._normalize_list_response(raw, what="stop-orders")

    def _plain_decimal_str(self, v: float | str) -> str:
        """Format numbers without scientific notation (MEXC may reject `2e-06`)."""
        if isinstance(v, str):
            s = v.strip()
            # If user passed '2e-06', normalize to plain decimal
            try:
                d = Decimal(s)
                s = format(d, "f")
            except Exception:
                pass
        else:
            d = Decimal(str(v))
            s = format(d, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s

    def _plain_decimal_number(self, v: float | str) -> float:
        """Return a JSON-safe float number (tries to avoid float noise).

        Note: Python's float may still use scientific notation for very tiny values.
        For the coins we target here (e.g. 0.007.. and above), this produces the
        same numeric representation as the web client.
        """
        s = self._plain_decimal_str(v)
        try:
            d = Decimal(s)
            # Keep a reasonable amount of decimals to prevent `519.5500000000001`.
            if "." in s:
                dp = len(s.split(".", 1)[1])
                return round(float(d), min(dp, 16))
            return float(d)
        except Exception:
            try:
                return float(v)  # type: ignore[arg-type]
            except Exception:
                return 0.0

    async def add_stop_order_by_position(
        self,
        position_id: int,
        *,
        take_profit_price: Optional[float | str] = None,
        stop_loss_price: Optional[float | str] = None,
        profit_trend: Optional[int] = None,
        loss_trend: Optional[int] = None,
        profit_loss_vol_type: str = "SAME",
        vol_type: int = 2,
        take_profit_reverse: int = 2,
        stop_loss_reverse: int = 2,
        price_protect: str = "0",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        if take_profit_price is None and stop_loss_price is None:
            raise ValueError("Need at least one of take_profit_price or stop_loss_price")

        timestamp = int(time.time() * 1000)
        p0 = self._generate_security_param()
        k0 = self._generate_security_param()

        mhash = (getattr(self.account, "mhash", "") or "").strip()
        if not mhash:
            # Last-ditch fallback (should be set in __init__)
            try:
                base = f"{getattr(self.account,'uid','')}{getattr(self.account,'device_id','')}".encode("utf-8")
                mhash = hashlib.md5(base).hexdigest() if base else ""
            except Exception:
                mhash = ""

        payload: Dict[str, Any] = {
            "positionId": int(position_id),
            "profitLossVolType": profit_loss_vol_type,
            "volType": vol_type,
            "takeProfitReverse": take_profit_reverse,
            "stopLossReverse": stop_loss_reverse,
            "priceProtect": price_protect,
            "p0": p0,
            "k0": k0,
            "chash": getattr(self.account, "chash", "d6c64d28e362f314071b3f9d78ff7494d9cd7177ae0465e772d1840e9f7905d8"),
            "ts": timestamp,
            "mhash": mhash,
            # Some endpoints expect mtoken in body (same as device-id)
            "mtoken": getattr(self.account, "device_id", ""),
        }

        if take_profit_price is not None:
            payload["takeProfitPrice"] = self._plain_decimal_number(take_profit_price)
            if profit_trend is not None:
                payload["profitTrend"] = str(int(profit_trend))
        if stop_loss_price is not None:
            payload["stopLossPrice"] = self._plain_decimal_number(stop_loss_price)
            if loss_trend is not None:
                # Web sends these as strings
                payload["lossTrend"] = str(int(loss_trend))
        if extra:
            payload.update(extra)

        log_payload = {
            "positionId": payload["positionId"],
        }
        if "takeProfitPrice" in payload:
            log_payload["takeProfitPrice"] = payload["takeProfitPrice"]
        if "stopLossPrice" in payload:
            log_payload["stopLossPrice"] = payload["stopLossPrice"]
        if "profitTrend" in payload:
            log_payload["profitTrend"] = payload["profitTrend"]
        if "lossTrend" in payload:
            log_payload["lossTrend"] = payload["lossTrend"]
        logger.info("Creating TP/SL stop-order: %s", log_payload)

        # Web client passes mhash as a query param too.
        params = {"mhash": mhash} if mhash else None
        return await self._request("POST", "private/stoporder/place/v2", data=payload, params=params)

    async def change_stop_order_plan_price(
        self,
        stop_plan_order_id: int,
        *,
        take_profit_price: Optional[float | str] = None,
        stop_loss_price: Optional[float | str] = None,
        profit_trend: Optional[int] = None,
        loss_trend: Optional[int] = None,
        take_profit_reverse: Optional[int] = None,
        stop_loss_reverse: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        timestamp = int(time.time() * 1000)
        p0 = self._generate_security_param()
        k0 = self._generate_security_param()

        mhash = (getattr(self.account, "mhash", "") or "").strip()
        payload: Dict[str, Any] = {
            "stopPlanOrderId": int(stop_plan_order_id),
            "p0": p0,
            "k0": k0,
            "chash": getattr(self.account, "chash", "d6c64d28e362f314071b3f9d78ff7494d9cd7177ae0465e772d1840e9f7905d8"),
            "ts": timestamp,
            "mhash": mhash,
            "mtoken": getattr(self.account, "device_id", ""),
        }
        if take_profit_price is not None:
            payload["takeProfitPrice"] = self._plain_decimal_number(take_profit_price)
        if stop_loss_price is not None:
            payload["stopLossPrice"] = self._plain_decimal_number(stop_loss_price)
        if profit_trend is not None:
            payload["profitTrend"] = str(int(profit_trend))
        if loss_trend is not None:
            payload["lossTrend"] = str(int(loss_trend))
        if take_profit_reverse is not None:
            payload["takeProfitReverse"] = int(take_profit_reverse)
        if stop_loss_reverse is not None:
            payload["stopLossReverse"] = int(stop_loss_reverse)
        if extra:
            payload.update(extra)

        log_payload = {
            k: v
            for k, v in payload.items()
            if k
            in {
                "stopPlanOrderId",
                "takeProfitPrice",
                "stopLossPrice",
                "profitTrend",
                "lossTrend",
                "takeProfitReverse",
                "stopLossReverse",
            }
        }
        logger.info("Changing stop-plan price: %s", log_payload)

        params = {"mhash": mhash} if mhash else None
        return await self._request(
            "POST",
            "private/stoporder/change_plan_price",
            data=payload,
            params=params,
        )

    async def get_positions(self) -> Dict:
        raw = await self._request("GET", "private/position/open_positions")
        logger.info("Positions API response: %s", raw)
        return self._normalize_list_response(raw, what="positions")

    async def get_available_usdt(self) -> Optional[float]:
        """
        Возвращает максимально доступный объём USDT для открытия новых позиций.

        Берём максимум из всех релевантных полей (availableBalance, marginAvailable,
        maxAvailable, balance и т.п.), потому что на разных аккаунтах MEXC
        используют разные поля для отображения "свободного" баланса.
        """
        info = await self.get_account_info()
        if not info.get("success"):
            return None

        data = info.get("data") or []
        if not isinstance(data, list):
            logger.warning(
                "Unexpected account data format in get_available_usdt: %r", data
            )
            return None

        best = 0.0
        for item in data:
            if not isinstance(item, dict):
                continue
            cur = str(item.get("currency") or item.get("asset") or "").upper()
            if cur != "USDT":
                continue

            candidate_keys = [
                "availableBalance",
                "available",
                "availableMargin",
                "marginAvailable",
                "maxAvailable",
                "positionMarginFree",
                "balance",
            ]

            for key in candidate_keys:
                if key in item and item[key] is not None:
                    try:
                        val = float(item[key])
                    except Exception:
                        continue
                    if val > best:
                        best = val
            # нашли строку USDT — дальше не идём
            break

        if best <= 0:
            return 0.0
        return best

    async def get_account_info(self) -> Dict:
        raw = await self._request("GET", "private/account/assets")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Account API response: %s", raw)
        else:
            if raw.get("success") and isinstance(raw.get("data"), list):
                data = raw["data"]
                usdt = next(
                    (
                        item
                        for item in data
                        if isinstance(item, dict)
                        and str(item.get("currency") or item.get("asset") or "").upper()
                        == "USDT"
                    ),
                    None,
                )
                if usdt:
                    avail = (
                        usdt.get("availableBalance")
                        or usdt.get("availableCash")
                        or usdt.get("availableOpen")
                        or usdt.get("available")
                        or usdt.get("availableMargin")
                        or usdt.get("marginAvailable")
                        or usdt.get("balance")
                    )
                    equity = usdt.get("equity")
                    logger.info(
                        "Account API: USDT available=%s equity=%s (currencies=%d)",
                        avail,
                        equity,
                        len(data),
                    )
                else:
                    logger.info(
                        "Account API: success, currencies=%d (USDT not found)",
                        len(data),
                    )
            else:
                logger.info(
                    "Account API: error success=%r code=%r message=%r",
                    raw.get("success"),
                    raw.get("code"),
                    raw.get("message"),
                )
        return self._normalize_list_response(raw, what="account")

    async def calc_volume_from_usdt(
        self,
        symbol: str,
        notional_usdt: float,
        price_override: float | None = None,
        side: Optional[int] = None,
    ) -> float | None:
        """
        Переводит требуемый нотионал в USDT в объём в контрактах с учётом:
        - текущей рыночной цены (через тикер / get_price_for_side);
        - размеров контракта, шага объёма и minVol.
        """
        if notional_usdt <= 0:
            return None
        symbol_full = symbol if "_" in symbol else f"{symbol}_USDT"
        info = await self.get_contract_info_cached(symbol_full)
        if not info:
            return None
        try:
            contract_size = float(info.get("contractSize", 1.0) or 1.0)
            vol_unit = float(info.get("volUnit", 0.0) or 0.0)
            min_vol = float(info.get("minVol", 0.0) or 0.0)
            vol_scale = int(info.get("volScale", 0) or 0)
        except Exception:
            return None
        if contract_size <= 0:
            return None

        if price_override is not None and price_override > 0:
            price = price_override
        else:
            price = await self.get_price_for_side(symbol_full, side, None)

        if price is None or price <= 0:
            return None

        raw_vol = notional_usdt / (price * contract_size)

        if vol_unit > 0:
            steps = max(1, math.floor(raw_vol / vol_unit))
            vol = steps * vol_unit
        else:
            vol = raw_vol
        if min_vol > 0 and vol < min_vol:
            vol = min_vol
        if vol_scale >= 0:
            factor = 10 ** vol_scale
            vol = math.floor(vol * factor) / factor
        return vol

    async def check_proxy_health(self) -> dict:
        """
        Быстрая проверка доступности контрактного API через прокси —
        берём тикер BTC_USDT.
        """
        await self._ensure_session()
        assert self.session is not None
        test_symbol = "BTC_USDT"
        start = time.monotonic()
        data = await self._request_market(
            f"api/v1/contract/ticker?symbol={test_symbol}"
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        ok = bool(data) and data.get("success") is True
        return {
            "ok": ok,
            "latency_ms": elapsed_ms,
            "raw": data,
        }

    async def get_symbol_max_notional(
        self,
        symbol: str,
        side: int,
        leverage: int,
    ) -> Optional[float]:
        """
        Максимальный дополнительный нотионал по инструменту и стороне с учётом:
        - риск-лимита по maxVol из /private/position/leverage;
        - уже открытого объёма по этой паре и стороне;
        - текущей рыночной цены (get_price_for_side).
        """
        full_symbol = symbol if "_" in symbol else f"{symbol}_USDT"
        resp = await self._request(
            method="GET",
            endpoint="private/position/leverage",
            params={"symbol": full_symbol},
        )
        logger.info("symbol leverage info [%s]: %s", full_symbol, resp)
        if not resp.get("success"):
            return None
        data = resp.get("data") or []
        if not isinstance(data, list):
            logger.warning("Unexpected leverage data format: %r", data)
            return None

        pos_type = 1 if side in (1, 4) else 2
        row: Optional[dict] = None

        for item in data:
            try:
                item_pos_type = int(
                    item.get("positionType")
                    or item.get("position_type")
                    or 0
                )
            except Exception:
                continue
            if item_pos_type != pos_type:
                continue
            try:
                item_leverage = int(
                    item.get("leverage")
                    or item.get("lever")
                    or 0
                )
            except Exception:
                item_leverage = 0
            if item_leverage == leverage:
                row = item
                break
            if row is None:
                row = item

        if not row:
            return None

        try:
            max_vol = float(
                row.get("maxVol")
                or row.get("maxOpenVol")
                or 0.0
            )
        except Exception:
            max_vol = 0.0
        if max_vol <= 0:
            return 0.0

        # Вычитаем уже открытый объём по этой паре и стороне
        current_vol = 0.0
        try:
            positions = await self.get_positions()
            if positions.get("success"):
                for p in positions.get("data", []):
                    try:
                        psym = str(p.get("symbol") or "").upper()
                        ptype = int(p.get("positionType") or p.get("position_type") or 0)
                        hold_vol = float(p.get("holdVol") or 0.0)
                    except Exception:
                        continue
                    if psym == full_symbol and ptype == pos_type and hold_vol > 0:
                        current_vol += hold_vol
        except Exception as exc:
            logger.warning("get_symbol_max_notional: failed to read positions: %s", exc)

        remaining_vol = max(0.0, max_vol - current_vol)
        if remaining_vol <= 0:
            return 0.0

        info = await self.get_contract_info_cached(full_symbol)
        contract_size = float(info.get("contractSize", 1.0) or 1.0) if info else 1.0

        price = await self.get_price_for_side(full_symbol, side, None)
        if price is None or price <= 0:
            return None

        max_notional = remaining_vol * contract_size * price
        return max_notional

    async def get_account_max_notional(self, leverage: int) -> Optional[float]:
        available = await self.get_available_usdt()
        if available is None:
            return None
        if available <= 0:
            return 0.0
        try:
            lev = float(leverage)
        except Exception:
            lev = 1.0
        return available * lev

    async def get_history_positions(
        self,
        symbol: Optional[str] = None,
        position_type: Optional[int] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        page_num: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "page_num": page_num,
            "page_size": page_size,
        }
        if symbol:
            params["symbol"] = symbol
        if position_type is not None:
            params["position_type"] = position_type
        if start_time:
            params["start_time"] = int(start_time)
        if end_time:
            params["end_time"] = int(end_time)

        resp = await self._request(
            method="GET",
            endpoint="private/position/list/history_positions",
            params=params,
            need_captcha=True,
        )
        if logger.isEnabledFor(logging.DEBUG):
            data = (resp or {}).get("data") or []
            logger.debug(
                "history_positions: success=%s code=%s rows=%d",
                resp.get("success"),
                resp.get("code"),
                len(data),
            )
        return resp

    def _generate_security_param(self, length: int = 400) -> str:
        chars = string.ascii_letters + string.digits + "+/="
        return "".join(random.choices(chars, k=length))

    def humanize_mexc_error(
            self,
            code: Optional[int],
            message: Optional[str],
            *,
            contracts_limit: Optional[int] = None,
    ) -> str:
        """
        Преобразует сырое сообщение об ошибке MEXC в понятный текст для юзера.
        """
        msg = (message or "").strip()
        lower = msg.lower()

        # 0) Слишком частые запросы (rate limit)
        if code == 510 or "too frequent" in lower or "too many requests" in lower:
            return (
                "слишком частые запросы к MEXC. Биржа временно отклонила запрос "
                "(rate limit).\n"
                "Подожди несколько секунд и повтори действие."
            )

        # 0.1) Ордер отменён биржей (типовой код 21)
        if code == 21 or "order canceled by exchange" in lower:
            return (
                "ордер был *отменён биржей* (код 21). "
                "Обычно такое происходит из-за внутренних ограничений биржи, "
                "изменения риск-лимитов или резкого движения цены.\n"
                "Проверь открытые позиции по инструменту и при необходимости "
                "попробуй выставить ордер ещё раз."
            )

        # 1) Специальный кейс: рыночный ордер отменён из-за большой разницы
        # между рыночной ценой и индексной (fair price).
        if (
                "deviation between market price and index price" in lower
                or (
                "market order" in lower
                and "index price" in lower
                and "deviation" in lower
        )
                or (
                "рыночный ордер" in lower
                and "индексн" in lower
                and ("отменен" in lower or "отклонен" in lower)
        )
        ):
            return (
                "рыночный ордер был *отменён биржей*, потому что цена сделки "
                "слишком сильно отличается от индексной (fair price). "
                "Позиция *не была открыта*.\n"
                "Попробуй выставить *лимитный* ордер с ценой ближе к текущей "
                "рыночной."
            )

        # 2) Превышен лимит контрактов по этой паре/плечу
        if "exceeded the maximum number of contracts" in lower:
            if contracts_limit:
                return (
                    f"превышен лимит контрактов на этом плече — максимум {contracts_limit} "
                    f"контрактов по этой паре. Уменьши размер позиции или плечо."
                )
            return (
                "превышен лимит контрактов на этом плече по этой паре. "
                "Уменьши размер позиции или плечо."
            )

        # 3) Только закрытие позиций (risk control, open запрещён)
        if "risk controls" in lower and "only closing is allowed" in lower:
            return (
                "по этой паре сейчас можно только *закрывать* позиции — "
                "открытие новых временно запрещено риск-контролем MEXC."
            )

        # 4) Конфликт cross/isolated в одну и ту же сторону
        if "cross and isolated position of the same direction are alternative" in lower:
            return (
                "по этой паре уже есть позиция в другом режиме маржи (cross/isolated) "
                "в ту же сторону. Закрой существующую позицию или поменяй режим маржи."
            )

        # 5) Позиция не существует / уже закрыта
        if "position is nonexistent or closed" in lower:
            return (
                "позиция уже закрыта или не существует. "
                "Возможно, её успели закрыть вручную или другим ордером."
            )

        # 6) Не хватает маржи / баланса
        if "insufficient margin" in lower or "balance insufficient" in lower:
            return (
                "недостаточно маржи/баланса для открытия такой позиции. "
                "Попробуй уменьшить объём или плечо, либо пополни баланс."
            )

        # 7) Общий случай
        if code is not None:
            return f"ошибка MEXC (код {code}): {msg or 'неизвестная ошибка'}"
        return f"ошибка MEXC: {msg or 'неизвестная ошибка'}"

