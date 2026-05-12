"""Multi-symbol Binance Futures WebSocket client.

Subscribes to a combined stream of `<symbol>@depth20@100ms` and `<symbol>@trade`
for all symbols in the working set. Automatically resubscribes when the set
changes. Re-uses a single connection.

Why `trade` and not `aggTrade`?
Binance Futures currently emits live per-trade events for our symbols on
`@trade`, while `@aggTrade` can stay silent. Our OFI / burst signals need the
actual flow, so we subscribe to `trade` and still accept `aggTrade` on the
handler side for compatibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Set

import websockets


logger = logging.getLogger(__name__)


BINANCE_FUTURES_WS_BASE = "wss://fstream.binance.com/stream"


DepthCallback = Callable[[str, list, list, float], Awaitable[None]]
TradeCallback = Callable[[str, float, float, bool, float], Awaitable[None]]


class BinanceMultiWS:
    def __init__(
        self,
        on_depth: DepthCallback,
        on_trade: TradeCallback,
    ) -> None:
        self.on_depth = on_depth
        self.on_trade = on_trade

        self._desired: Set[str] = set()
        self._stop = asyncio.Event()
        self._ws_lock = asyncio.Lock()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._reqid = 0
        self._connected = False
        self._task: Optional[asyncio.Task] = None
        self._last_msg_ts: float = 0.0
        self._watchdog_task: Optional[asyncio.Task] = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def stop(self) -> None:
        self._stop.set()

    async def update_symbols(self, symbols: Iterable[str]) -> None:
        new = set(s.upper() for s in symbols)
        if new == self._desired:
            return
        to_add = new - self._desired
        to_remove = self._desired - new
        self._desired = new
        if not self._ws:
            return
        try:
            if to_add:
                await self._send_subscribe(to_add, subscribe=True)
            if to_remove:
                await self._send_subscribe(to_remove, subscribe=False)
        except Exception as e:
            logger.warning("Binance update_symbols error: %s", e)

    async def _send_subscribe(self, syms: Set[str], *, subscribe: bool) -> None:
        if not syms:
            return
        async with self._ws_lock:
            if not self._ws:
                return
            params: List[str] = []
            for s in syms:
                ls = s.lower()
                params.append(f"{ls}@depth20@100ms")
                params.append(f"{ls}@trade")
            self._reqid += 1
            method = "SUBSCRIBE" if subscribe else "UNSUBSCRIBE"
            try:
                await self._ws.send(json.dumps({
                    "method": method,
                    "params": params,
                    "id": self._reqid,
                }))
            except Exception as e:
                logger.warning("Binance %s send failed: %s", method, e)

    async def run(self, initial: Iterable[str]) -> None:
        logger.info(f"BinanceMultiWS.run() started with {len(list(initial))} symbols")
        self._desired = set(s.upper() for s in initial)
        self._stop.clear()
        logger.info(f"BinanceMultiWS desired symbols: {self._desired}")

        # Start watchdog task
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._stall_watchdog())

        backoff = 1.0
        logger.info("BinanceMultiWS entering main loop")
        while not self._stop.is_set():
            try:
                # Build the URL with the initial subset baked in (avoids race on first subscribe).
                streams = []
                for s in self._desired:
                    ls = s.lower()
                    streams.append(f"{ls}@depth20@100ms")
                    streams.append(f"{ls}@trade")
                # Binance limits combined-stream URL length; cap to ~200 streams (100 symbols × 2).
                # Use SUBSCRIBE for the rest after connect.
                first_chunk = streams[:200] if streams else []
                rest = streams[200:]

                if first_chunk:
                    url = f"{BINANCE_FUTURES_WS_BASE}?streams=" + "/".join(first_chunk)
                else:
                    # No symbols yet — open empty wrapper to enable later SUBSCRIBE
                    url = f"{BINANCE_FUTURES_WS_BASE}?streams=btcusdt@trade"

                async with websockets.connect(
                    url, ping_interval=15, ping_timeout=15, max_size=4_000_000
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._last_msg_ts = time.time()
                    backoff = 1.0
                    if rest:
                        await self._send_subscribe(
                            set(rest_to_symbols(rest)), subscribe=True
                        )

                    async for raw in ws:
                        self._last_msg_ts = time.time()
                        if self._stop.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        await self._handle(msg)

            except Exception as e:
                logger.info("Binance WS reconnect: %s", e)
            finally:
                self._connected = False
                self._ws = None

            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 10.0)

    async def _handle(self, msg: dict) -> None:
        # Combined stream wraps payload as {"stream": ..., "data": ...}
        stream = msg.get("stream")
        data = msg.get("data") or msg
        if not stream:
            return

        # Format: "<symbol>@<type>"
        try:
            sym, kind = stream.split("@", 1)
        except ValueError:
            return
        sym_u = sym.upper()

        if kind.startswith("depth"):
            bids = data.get("b") or []
            asks = data.get("a") or []
            ts = time.time()
            try:
                await self.on_depth(sym_u, bids, asks, ts)
            except Exception as e:
                logger.warning("on_depth %s error: %s", sym_u, e)

        elif kind in ("trade", "aggTrade"):
            try:
                price = float(data.get("p") or 0.0)
                qty = float(data.get("q") or 0.0)
                # m=true means buyer is the market maker → seller was aggressor
                buyer_is_maker = bool(data.get("m"))
                ts = float(data.get("T") or time.time() * 1000) / 1000.0
            except Exception:
                return
            try:
                await self.on_trade(sym_u, price, qty, buyer_is_maker, ts)
            except Exception as e:
                logger.warning("on_trade %s error: %s", sym_u, e)

    async def _stall_watchdog(self) -> None:
        """Monitor for stalled connection and force reconnect if no messages for 10s."""
        while not self._stop.is_set():
            await asyncio.sleep(5.0)
            if self._ws is not None and self._last_msg_ts > 0:
                silence = time.time() - self._last_msg_ts
                if silence > 10.0:
                    logger.warning("Binance WS stalled (%.1fs no msg), forcing reconnect", silence)
                    try:
                        await self._ws.close()
                    except Exception:
                        pass


def rest_to_symbols(streams: List[str]) -> List[str]:
    """Extract unique symbols from a list of streams like 'btcusdt@aggTrade'."""
    out: Set[str] = set()
    for s in streams:
        out.add(s.split("@", 1)[0].upper())
    return list(out)
