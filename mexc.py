"""MEXC adapter (spot V3 + contract futures).

Official docs:
- Spot V3:  https://www.mexc.com/api-docs/spot-v3/introduction
  * GET /api/v3/exchangeInfo
  * GET /api/v3/ticker/bookTicker
- Spot WS: https://www.mexc.com/api-docs/spot-v3/websocket-market-streams
  * wss://wbs-api.mexc.com/ws
  * binary Protobuf frames (PushDataV3ApiWrapper)
  * subscribe: {"method":"SUBSCRIPTION","params":["<channel>",...]}
  * channel example: spot@public.aggre.bookTicker.v3.api.pb@100ms@BTCUSDT
  * ping: {"method":"PING"}  (server replies with a PONG frame)
  * 30 subs per connection max; 24h connection lifetime; 60s no-data cutoff
- Futures (contract):
  https://www.mexc.com/api-docs/futures/market-endpoints
  * GET /api/v1/contract/detail
  * GET /api/v1/contract/ticker
  * GET /api/v1/contract/funding_rate/{symbol}
- Futures WS: https://www.mexc.com/api-docs/futures/websocket-api
  * wss://contract.mexc.com/edge
  * subscribe: {"method":"sub.ticker","param":{"symbol":"BTC_USDT"}}
  * push fields (verified 2026-04-19 live): bid1, ask1, lastPrice,
    fairPrice, indexPrice, fundingRate, timestamp

MEXC futures uses a separate hostname (`contract.mexc.com`).
MEXC spot WS is additive: REST remains authoritative until sustained
Protobuf traffic is verified (ws_covers_spot stays False).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx

from ..core.logging_setup import get_logger
from ..core.models import FundingSnapshot, FuturesQuote, InstrumentRef, MarketType, SpotQuote
from ..core.symbols import make_canonical
from .base import BaseAdapter
from .mexc_spot_pb import parse_push
from .ws import websocket_loop

log = get_logger("adapter.mexc")


class MexcAdapter(BaseAdapter):
    venue = "mexc"
    spot_base = "https://api.mexc.com"
    spot_ws_url = "wss://wbs-api.mexc.com/ws"
    futures_base = "https://contract.mexc.com"
    futures_ws_url = "wss://contract.mexc.com/edge"
    # Spot WS stays additive until Protobuf flow is verified in production.
    ws_covers_spot = False
    ws_covers_futures = True
    ws_covers_funding = True

    # MEXC caps one spot WS connection at 30 subscriptions.
    _MAX_SPOT_SUBS_PER_CONN = 30

    async def refresh_asset_ops(self, client):
        from ..core.asset_ops import AssetOps, NetworkStatus
        from ._auth import _creds, mexc_signed
        creds = _creds("MEXC")
        if not creds:
            return {}
        url, headers, params = mexc_signed(
            self.spot_base, "/api/v3/capital/config/getall",
            creds["api_key"], creds["api_secret"],
        )
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        out: dict[str, AssetOps] = {}
        for row in r.json() or []:
            sym = (row.get("coin") or "").upper()
            if not sym:
                continue
            networks = row.get("networkList") or []
            # MEXC (Binance-forked) exposes coin-level `depositAllEnable`
            # and `withdrawAllEnable`. These are the authoritative
            # "is the wallet operational" flags — when MEXC suspends a
            # coin (maintenance, delisting), depositAllEnable flips to
            # false while per-network `depositEnable` can stay true.
            # Require BOTH the top-level flag AND at least one live
            # network so we don't falsely mark suspended coins as open.
            dep_all = row.get("depositAllEnable")
            wd_all = row.get("withdrawAllEnable")
            any_dep_net = any(n.get("depositEnable") for n in networks)
            any_wd_net = any(n.get("withdrawEnable") for n in networks)
            # If MEXC omits the top-level flag, fall back to per-network
            # aggregation (legacy behaviour).
            dep = (bool(dep_all) and any_dep_net) if dep_all is not None else any_dep_net
            wd = (bool(wd_all) and any_wd_net) if wd_all is not None else any_wd_net
            # Per-chain breakdown: MEXC networkList entries carry
            # `network` (ERC20/TRC20/BSC…) with per-side toggles. Pass
            # through so alerts can show which chains actually work.
            net_rows = []
            for n in networks:
                nname = (n.get("network") or n.get("netWork") or n.get("name") or "").upper()
                if not nname:
                    continue
                net_rows.append(NetworkStatus(
                    name=nname,
                    deposit_enabled=bool(n.get("depositEnable")) if n.get("depositEnable") is not None else None,
                    withdraw_enabled=bool(n.get("withdrawEnable")) if n.get("withdrawEnable") is not None else None,
                ))
            out[sym] = AssetOps(
                deposit_enabled=dep, withdraw_enabled=wd,
                networks=tuple(net_rows) if net_rows else None,
            )
        return out

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._spot: dict[str, InstrumentRef] = {}
        self._fut: dict[str, InstrumentRef] = {}
        self._funding_interval: dict[str, float] = {}

    async def load_instruments(self, client: httpx.AsyncClient) -> list[InstrumentRef]:
        refs: list[InstrumentRef] = []

        r = await client.get(f"{self.spot_base}/api/v3/exchangeInfo")
        r.raise_for_status()
        for s in r.json().get("symbols", []) or []:
            if s.get("status") not in ("1", "ENABLED", "TRADING"):
                # MEXC reports either numeric or string status depending on endpoint version
                continue
            base = s.get("baseAsset")
            quote = s.get("quoteAsset")
            canonical = make_canonical(base, quote)
            ref = InstrumentRef(
                venue=self.venue,
                market_type=MarketType.SPOT,
                symbol_native=s["symbol"],
                canonical=canonical,
                base=canonical.symbol,
                quote=canonical.quote,
            )
            refs.append(ref)
            if self.asset_filter.accepts(canonical):
                self._spot[s["symbol"]] = ref

        r = await client.get(f"{self.futures_base}/api/v1/contract/detail")
        r.raise_for_status()
        for s in r.json().get("data", []) or []:
            if s.get("state") not in (0, "0", "ENABLED"):
                # 0 == ENABLED per MEXC contract docs
                continue
            base = s.get("baseCoin")
            quote = s.get("quoteCoin")
            settle = s.get("settleCoin", quote)
            canonical = make_canonical(base, quote, settle)
            ref = InstrumentRef(
                venue=self.venue,
                market_type=MarketType.FUTURES,
                symbol_native=s["symbol"],
                canonical=canonical,
                base=canonical.symbol,
                quote=canonical.quote,
                settle_asset=canonical.settle,
                tags={"contract_type": "linear_perp"},
            )
            refs.append(ref)
            if self.asset_filter.accepts(canonical):
                self._fut[s["symbol"]] = ref

        return refs

    async def poll_spot(self, client: httpx.AsyncClient) -> None:
        if not self._spot:
            return
        r = await client.get(f"{self.spot_base}/api/v3/ticker/bookTicker")
        r.raise_for_status()
        now = datetime.now(timezone.utc)
        for row in r.json():
            sym = row.get("symbol")
            ref = self._spot.get(sym)
            if ref is None:
                continue
            await self.emit_spot(SpotQuote(
                venue=self.venue,
                symbol_native=sym,
                canonical=ref.canonical,
                base=ref.base,
                quote=ref.quote,
                bid=float(row["bidPrice"]) if row.get("bidPrice") else None,
                ask=float(row["askPrice"]) if row.get("askPrice") else None,
                ts_exchange=now,
            ))

    async def poll_futures(self, client: httpx.AsyncClient) -> None:
        if not self._fut:
            return
        r = await client.get(f"{self.futures_base}/api/v1/contract/ticker")
        r.raise_for_status()
        now = datetime.now(timezone.utc)
        for row in r.json().get("data", []) or []:
            sym = row.get("symbol")
            ref = self._fut.get(sym)
            if ref is None:
                continue
            await self.emit_futures(FuturesQuote(
                venue=self.venue,
                symbol_native=sym,
                canonical=ref.canonical,
                base=ref.base,
                quote=ref.quote,
                settle_asset=ref.settle_asset,
                bid=float(row["bid1"]) if row.get("bid1") else None,
                ask=float(row["ask1"]) if row.get("ask1") else None,
                last=float(row["lastPrice"]) if row.get("lastPrice") else None,
                mark_price=float(row["fairPrice"]) if row.get("fairPrice") else None,
                index_price=float(row["indexPrice"]) if row.get("indexPrice") else None,
                ts_exchange=now,
            ))

    async def fetch_funding_history_24h(self, client, symbol_native: str):
        try:
            r = await client.get(
                "https://contract.mexc.com/api/v1/contract/funding_rate/history",
                params={"symbol": symbol_native, "page_size": 24},
            )
            r.raise_for_status()
        except httpx.HTTPError:
            return None
        rows = (r.json().get("data") or {}).get("resultList") or []
        import time as _t
        cutoff_ms = int((_t.time() - 86400) * 1000)
        total = 0.0
        for row in rows:
            ts = row.get("settleTime")
            if ts and int(ts) >= cutoff_ms:
                try:
                    total += float(row.get("fundingRate") or 0)
                except (TypeError, ValueError):
                    pass
        return total

    async def poll_funding(self, client: httpx.AsyncClient) -> None:
        if not self._fut:
            return
        # `/contract/funding_rate` (no symbol) returns ALL perps in one
        # shot with both `fundingRate` AND `collectCycle` per symbol.
        # This replaces the startup prefetch loop — each poll picks up
        # any mid-run interval changes (MEXC flips coins 8h ↔ 4h live
        # without a separate notice endpoint).
        r = await client.get(f"{self.futures_base}/api/v1/contract/funding_rate")
        r.raise_for_status()
        now = datetime.now(timezone.utc)
        for row in r.json().get("data", []) or []:
            sym = row.get("symbol")
            ref = self._fut.get(sym)
            if ref is None:
                continue
            rate = row.get("fundingRate")
            if rate in (None, ""):
                continue
            cycle = row.get("collectCycle")
            try:
                interval = float(cycle) if cycle not in (None, 0, "0") else 8.0
            except (TypeError, ValueError):
                interval = 8.0
            self._funding_interval[sym] = interval
            next_ms = row.get("nextSettleTime")
            next_time = None
            if next_ms:
                try:
                    next_time = datetime.fromtimestamp(int(next_ms) / 1000, tz=timezone.utc)
                except (TypeError, ValueError):
                    pass
            await self.emit_funding(FundingSnapshot(
                venue=self.venue,
                symbol_native=sym,
                canonical=ref.canonical,
                funding_rate_current=float(rate),
                funding_interval_hours=interval,
                next_funding_time=next_time,
                ts_exchange=now,
            ))

    # ------------------------------------------------------------------
    # WebSocket hot path — futures JSON + spot Protobuf
    # ------------------------------------------------------------------

    async def run_ws(self, client: httpx.AsyncClient) -> None:
        loops = []
        if self._fut:
            loops.append(self._run_futures_ws())
        if self._spot:
            loops.extend(self._spot_ws_loops())
        if not loops:
            return
        await asyncio.gather(*loops)

    # ------- spot: Protobuf binary bookTicker stream, 30 subs per connection --

    def _spot_ws_loops(self) -> list:
        symbols = list(self._spot.keys())
        shards = [
            symbols[i:i + self._MAX_SPOT_SUBS_PER_CONN]
            for i in range(0, len(symbols), self._MAX_SPOT_SUBS_PER_CONN)
        ]
        return [self._run_spot_ws(i, shard) for i, shard in enumerate(shards)]

    async def _run_spot_ws(self, shard_idx: int, symbols: list[str]):
        # MEXC requires UPPERCASE symbols in the channel.
        channels = [
            f"spot@public.aggre.bookTicker.v3.api.pb@100ms@{sym.upper()}"
            for sym in symbols
        ]
        ref_by_symbol_upper = {sym.upper(): self._spot[sym] for sym in symbols}

        async def keepalive(ws):
            try:
                while not self._stop.is_set():
                    await asyncio.sleep(20)
                    try:
                        await ws.send(json.dumps({"method": "PING"}))
                    except Exception:
                        return
            except asyncio.CancelledError:
                return

        async def on_open(ws):
            # Subscribe all channels for this shard in one request (≤30).
            await ws.send(json.dumps({
                "method": "SUBSCRIPTION",
                "params": channels,
            }))
            asyncio.create_task(keepalive(ws))

        async def on_message(ws, raw):
            if isinstance(raw, str):
                # Text frames: subscribe ACKs ("code":0,"msg":"..."), PONG, errors.
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    return
                code = msg.get("code")
                if code not in (None, 0):
                    log.warning("mexc.spot_ws_error", msg=msg)
                return
            if not isinstance(raw, (bytes, bytearray)):
                return
            ticker = parse_push(bytes(raw))
            if ticker is None:
                return
            sym_native = None
            if ticker.symbol:
                sym_native = ticker.symbol.upper()
            elif ticker.channel:
                # Fallback — channel ends with @SYMBOL
                tail = ticker.channel.rsplit("@", 1)[-1]
                if tail:
                    sym_native = tail.upper()
            if not sym_native:
                return
            ref = ref_by_symbol_upper.get(sym_native)
            if ref is None:
                return
            await self.emit_spot(SpotQuote(
                venue=self.venue,
                symbol_native=ref.symbol_native,
                canonical=ref.canonical,
                base=ref.base,
                quote=ref.quote,
                bid=ticker.bid_price,
                ask=ticker.ask_price,
                ts_exchange=datetime.now(timezone.utc),
                source="ws",
            ))
            self.tracker.mark_message()

        await websocket_loop(
            self.spot_ws_url,
            on_open=on_open,
            on_message=on_message,
            on_connect=self._mark_ws_connected,
            on_disconnect=self._mark_ws_disconnected,
            stop_event=self._stop,
            venue=f"{self.venue}:spot[{shard_idx}]",
            ping_interval=None,  # app-level PING handled in keepalive()
        )

    # ------- futures: JSON ticker stream (existing) --------------------------

    async def _run_futures_ws(self):
        symbols = list(self._fut.keys())

        async def keepalive(ws):
            try:
                while not self._stop.is_set():
                    await asyncio.sleep(15)
                    try:
                        await ws.send(json.dumps({"method": "ping"}))
                    except Exception:
                        return
            except asyncio.CancelledError:
                return

        async def on_open(ws):
            # MEXC accepts one symbol per sub message. Queue them.
            for sym in symbols:
                await ws.send(json.dumps({"method": "sub.ticker", "param": {"symbol": sym}}))
            asyncio.create_task(keepalive(ws))

        async def on_message(ws, raw):
            msg = json.loads(raw)
            channel = msg.get("channel") or msg.get("symbol")  # push messages vary
            if msg.get("channel") == "rs.sub.ticker":
                return  # subscribe ack
            if msg.get("channel") == "pong":
                return
            data = msg.get("data") or {}
            sym = data.get("symbol") or msg.get("symbol")
            ref = self._fut.get(sym)
            if ref is None:
                return
            ts_ms = data.get("timestamp") or msg.get("ts")
            ts_ex = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc) if ts_ms else datetime.now(timezone.utc)
            await self.emit_futures(FuturesQuote(
                venue=self.venue,
                symbol_native=sym,
                canonical=ref.canonical,
                base=ref.base,
                quote=ref.quote,
                settle_asset=ref.settle_asset,
                bid=float(data["bid1"]) if data.get("bid1") is not None else None,
                ask=float(data["ask1"]) if data.get("ask1") is not None else None,
                last=float(data["lastPrice"]) if data.get("lastPrice") is not None else None,
                mark_price=float(data["fairPrice"]) if data.get("fairPrice") is not None else None,
                index_price=float(data["indexPrice"]) if data.get("indexPrice") is not None else None,
                ts_exchange=ts_ex,
                source="ws",
            ))
            rate = data.get("fundingRate")
            if rate not in (None, ""):
                interval = self._funding_interval.get(sym, 8.0)
                await self.emit_funding(FundingSnapshot(
                    venue=self.venue,
                    symbol_native=sym,
                    canonical=ref.canonical,
                    funding_rate_current=float(rate),
                    funding_interval_hours=interval,
                    ts_exchange=ts_ex,
                    source="ws",
                ))
            self.tracker.mark_message()

        await websocket_loop(
            self.futures_ws_url,
            on_open=on_open,
            on_message=on_message,
            on_connect=self._mark_ws_connected,
            on_disconnect=self._mark_ws_disconnected,
            stop_event=self._stop,
            venue=self.venue + ":fut",
            ping_interval=None,  # app-level ping via method:ping
        )
