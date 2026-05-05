"""Multi-symbol MEXC Futures WebSocket client.

One connection multiplexes depth subscriptions for all symbols in the working
set. We deliberately do NOT use OpenAPI keys — trading via the OpenAPI strips
0-fee status, and the public depth feed is sufficient for the strategy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Iterable, Optional, Set

import websockets


logger = logging.getLogger(__name__)


MEXC_WS_ENDPOINT = "wss://contract.mexc.com/edge"


DepthCallback = Callable[[str, list, list, float], Awaitable[None]]


class MexcMultiWS:
    def __init__(self, on_depth: DepthCallback) -> None:
        self.on_depth = on_depth
        self._desired: Set[str] = set()
        self._subscribed: Set[str] = set()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def stop(self) -> None:
        self._stop.set()

    async def update_symbols(self, symbols: Iterable[str]) -> None:
        new = set(s for s in symbols if s)
        if new == self._desired:
            return
        to_add = new - self._desired
        to_remove = self._desired - new
        self._desired = new

        if not self._ws:
            return

        try:
            for sym in to_add:
                await self._sub(sym)
            for sym in to_remove:
                await self._unsub(sym)
        except Exception as e:
            logger.warning("MEXC update_symbols error: %s", e)

    async def _sub(self, sym: str) -> None:
        async with self._ws_lock:
            if not self._ws:
                return
            try:
                await self._ws.send(json.dumps({
                    "method": "sub.depth",
                    "param": {"symbol": sym},
                    "gzip": False,
                }))
                self._subscribed.add(sym)
            except Exception as e:
                logger.warning("MEXC sub %s error: %s", sym, e)

    async def _unsub(self, sym: str) -> None:
        async with self._ws_lock:
            if not self._ws:
                return
            try:
                await self._ws.send(json.dumps({
                    "method": "unsub.depth",
                    "param": {"symbol": sym},
                }))
                self._subscribed.discard(sym)
            except Exception:
                pass

    async def run(self, initial: Iterable[str]) -> None:
        self._desired = set(initial)
        self._stop.clear()

        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    MEXC_WS_ENDPOINT,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=4_000_000,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._subscribed.clear()
                    backoff = 1.0
                    # subscribe to all desired
                    for sym in list(self._desired):
                        await self._sub(sym)

                    last_ping = time.time()
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        now = time.time()
                        if now - last_ping > 15:
                            last_ping = now
                            try:
                                await ws.send(json.dumps({"method": "ping"}))
                            except Exception:
                                pass
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if msg.get("channel") == "push.depth":
                            payload = msg.get("data") or {}
                            sym = msg.get("symbol") or payload.get("symbol")
                            bids = payload.get("bids") or payload.get("b") or []
                            asks = payload.get("asks") or payload.get("a") or []
                            try:
                                await self.on_depth(sym, bids, asks, now)
                            except Exception as e:
                                logger.warning("mexc on_depth %s error: %s", sym, e)
            except Exception as e:
                logger.info("MEXC WS reconnect: %s", e)
            finally:
                self._connected = False
                self._ws = None

            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 10.0)
