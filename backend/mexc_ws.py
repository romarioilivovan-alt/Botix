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
        self._last_msg_ts: float = 0.0
        self._watchdog_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

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
                    "method": "sub.depth.full",
                    "param": {"symbol": sym, "limit": 20},
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
                    "method": "unsub.depth.full",
                    "param": {"symbol": sym, "limit": 20},
                }))
                self._subscribed.discard(sym)
            except Exception:
                pass

    async def run(self, initial: Iterable[str]) -> None:
        logger.info(f"MexcMultiWS.run() started with {len(list(initial))} symbols")
        self._desired = set(initial)
        self._stop.clear()
        logger.info(f"MexcMultiWS desired symbols: {self._desired}")

        # Start watchdog and heartbeat tasks
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._stall_watchdog())
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        backoff = 1.0
        logger.info("MexcMultiWS entering main loop")
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    MEXC_WS_ENDPOINT,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=4_000_000,
                    compression=None,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._subscribed.clear()
                    self._last_msg_ts = time.time()
                    backoff = 1.0
                    # subscribe to all desired
                    for sym in list(self._desired):
                        await self._sub(sym)

                    async for raw in ws:
                        self._last_msg_ts = time.time()
                        if self._stop.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if msg.get("channel") in {"push.depth", "push.depth.full"}:
                            payload = msg.get("data") or {}
                            sym = msg.get("symbol") or payload.get("symbol")
                            bids = payload.get("bids") or payload.get("b") or []
                            asks = payload.get("asks") or payload.get("a") or []
                            try:
                                await self.on_depth(sym, bids, asks, time.time())
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

    async def _stall_watchdog(self) -> None:
        """Monitor for stalled connection and force reconnect if no messages for 10s."""
        while not self._stop.is_set():
            await asyncio.sleep(5.0)
            if self._ws is not None and self._last_msg_ts > 0:
                silence = time.time() - self._last_msg_ts
                if silence > 10.0:
                    logger.warning("MEXC WS stalled (%.1fs no msg), forcing reconnect", silence)
                    try:
                        await self._ws.close()
                    except Exception:
                        pass

    async def _heartbeat_loop(self) -> None:
        """Send ping every 15s independently of incoming message flow."""
        while not self._stop.is_set():
            await asyncio.sleep(15.0)
            async with self._ws_lock:
                if self._ws is not None:
                    try:
                        await self._ws.send(json.dumps({"method": "ping"}))
                    except Exception:
                        pass
