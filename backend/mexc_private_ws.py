"""MEXC private WebSocket position/order feed.

This is used only as a fast confirmation layer. REST remains the fallback and
source of truth for recovery/emergency paths.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Iterable, Optional, Set, Tuple

import websockets


logger = logging.getLogger(__name__)

MEXC_PRIVATE_WS_ENDPOINT = "wss://contract.mexc.com/edge"


class MexcPrivateWS:
    def __init__(self, token: str) -> None:
        self.token = str(token or "").strip()
        self._desired: Set[str] = set()
        self._subscribed_orders: Set[str] = set()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_lock = asyncio.Lock()
        self._data_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._connected = False
        self._last_msg_ts = 0.0
        self._positions: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._position_events: Dict[Tuple[str, int], asyncio.Event] = {}
        self._orders: Dict[int, Dict[str, Any]] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_msg_ts(self) -> float:
        return self._last_msg_ts

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
                await self._sub_order(sym)
            for sym in to_remove:
                await self._unsub_order(sym)
        except Exception as e:
            logger.debug("MEXC private update_symbols error: %s", e)

    async def find_position(
        self,
        symbol: str,
        side: str,
        *,
        max_age_sec: float = 2.0,
    ) -> Optional[Dict[str, Any]]:
        key = self._key(symbol, side)
        now = time.time()
        async with self._data_lock:
            pos = self._positions.get(key)
            if not pos:
                return None
            seen = float(pos.get("__ws_seen_ts") or 0.0)
            if seen > 0 and max_age_sec > 0 and now - seen > max_age_sec:
                return None
            return dict(pos)

    async def wait_for_position(
        self,
        symbol: str,
        side: str,
        *,
        timeout: float = 0.35,
        max_age_sec: float = 2.0,
    ) -> Optional[Dict[str, Any]]:
        existing = await self.find_position(symbol, side, max_age_sec=max_age_sec)
        if existing is not None:
            return existing
        key = self._key(symbol, side)
        async with self._data_lock:
            ev = self._position_events.setdefault(key, asyncio.Event())
            ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), timeout=max(0.01, float(timeout)))
        except asyncio.TimeoutError:
            return await self.find_position(symbol, side, max_age_sec=max_age_sec)
        return await self.find_position(symbol, side, max_age_sec=max_age_sec)

    async def find_order(self, order_id: int) -> Optional[Dict[str, Any]]:
        try:
            oid = int(order_id)
        except Exception:
            return None
        async with self._data_lock:
            item = self._orders.get(oid)
            return dict(item) if item else None

    async def run(self, initial: Iterable[str]) -> None:
        if not self.token:
            logger.info("MEXC private WS disabled: no token")
            return
        self._desired = set(s for s in initial if s)
        self._stop.clear()
        backoff = 1.0
        while not self._stop.is_set():
            ping_task: Optional[asyncio.Task] = None
            try:
                async with websockets.connect(
                    MEXC_PRIVATE_WS_ENDPOINT,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=4_000_000,
                    compression=None,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._last_msg_ts = time.time()
                    self._subscribed_orders.clear()
                    backoff = 1.0
                    await self._send_json({"method": "login", "param": {"token": self.token}})
                    await self._send_json({"method": "sub.personal.position", "param": {}})
                    for sym in list(self._desired):
                        await self._sub_order(sym)
                    ping_task = asyncio.create_task(self._ping_loop(), name="mexc_private_ping")
                    logger.info("MEXC private WS connected")

                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        self._last_msg_ts = time.time()
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        await self._handle_message(msg)
            except Exception as e:
                logger.info("MEXC private WS reconnect: %s", e)
            finally:
                if ping_task is not None:
                    ping_task.cancel()
                self._connected = False
                self._ws = None

            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 10.0)

    async def _ping_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(15.0)
            try:
                await self._send_json({"method": "ping"})
            except Exception:
                return

    async def _sub_order(self, sym: str) -> None:
        async with self._ws_lock:
            if not self._ws or sym in self._subscribed_orders:
                return
            await self._send_json({"method": "sub.personal.order", "param": {"symbol": sym}})
            self._subscribed_orders.add(sym)

    async def _unsub_order(self, sym: str) -> None:
        async with self._ws_lock:
            if not self._ws:
                return
            try:
                await self._send_json({"method": "unsub.personal.order", "param": {"symbol": sym}})
            except Exception:
                pass
            self._subscribed_orders.discard(sym)

    async def _send_json(self, payload: Dict[str, Any]) -> None:
        if not self._ws:
            return
        await self._ws.send(json.dumps(payload, separators=(",", ":")))

    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        channel = str(msg.get("channel") or "")
        data = msg.get("data")
        if not data:
            return
        items = data if isinstance(data, list) else [data]
        if channel == "push.personal.position":
            for item in items:
                if isinstance(item, dict):
                    await self._handle_position(item)
        elif channel in {"push.personal.order", "push.personal.order.deal"}:
            async with self._data_lock:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    oid = item.get("orderId") or item.get("order_id") or item.get("id")
                    try:
                        if oid is not None:
                            item = dict(item)
                            item["__ws_seen_ts"] = time.time()
                            self._orders[int(oid)] = item
                    except Exception:
                        continue

    async def _handle_position(self, pos: Dict[str, Any]) -> None:
        sym = str(pos.get("symbol") or "").upper()
        if not sym:
            return
        try:
            pos_type = int(pos.get("positionType") or 0)
        except Exception:
            pos_type = 0
        if pos_type not in (1, 2):
            return
        try:
            hold_vol = float(pos.get("holdVol") or 0.0)
        except Exception:
            hold_vol = 0.0
        try:
            state = int(pos.get("state") or 0)
        except Exception:
            state = 0

        key = (sym, pos_type)
        async with self._data_lock:
            if state == 3 or hold_vol <= 0:
                self._positions.pop(key, None)
                return
            item = dict(pos)
            item["__ws_seen_ts"] = time.time()
            self._positions[key] = item
            ev = self._position_events.setdefault(key, asyncio.Event())
            ev.set()

    def _key(self, symbol: str, side: str) -> Tuple[str, int]:
        sym = str(symbol or "").upper()
        pos_type = 1 if str(side or "").upper() == "LONG" else 2
        return (sym, pos_type)
