"""0-fee symbol universe manager.

Periodically pulls the list of 0-fee MEXC futures symbols, resolves available
reference symbols on Binance Futures, and exposes the working set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

import aiohttp


logger = logging.getLogger(__name__)


BINANCE_FUTURES_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"


def to_binance(mexc: str) -> str:
    """BTC_USDT -> BTCUSDT."""
    return mexc.replace("_", "").upper()


def resolve_binance_symbol(mexc: str, binance_set: set) -> tuple:
    """Try multiple Binance symbol variants for a given MEXC symbol.

    Returns (binance_symbol, price_factor) where price_factor is the multiplier
    to convert Binance price → MEXC-equivalent price.
    e.g. 1000PEPEUSDT price × 0.001 = PEPE_USDT fair price.
    """
    base = to_binance(mexc)
    if base in binance_set:
        return base, 1.0
    # Try 1000-prefix variant (common for micro-cap coins)
    prefixed = f"1000{base}"
    if prefixed in binance_set:
        return prefixed, 0.001
    return None, 1.0


def to_mexc(binance: str) -> str:
    """BTCUSDT -> BTC_USDT (best-effort, USDT only)."""
    s = binance.upper()
    if s.endswith("USDT") and "_" not in s:
        return f"{s[:-4]}_USDT"
    return s


@dataclass
class UniverseEntry:
    mexc_symbol: str
    binance_symbol: Optional[str]
    has_ref: bool
    binance_price_factor: float = 1.0  # multiply Binance price by this to get MEXC-equivalent fair


class UniverseManager:
    def __init__(
        self,
        mexc_trader,
        cfg,
        cache_path: Optional[Path] = None,
    ) -> None:
        self.trader = mexc_trader
        self.cfg = cfg
        self.cache_path = cache_path or Path(
            os.environ.get("ZFEE_UNIVERSE_CACHE")
            or (Path(__file__).resolve().parents[1] / ".universe_cache.json")
        )

        self._binance_set: Set[str] = set()
        self._binance_set_ts: float = 0.0
        self._entries: Dict[str, UniverseEntry] = {}
        self._lock = asyncio.Lock()

    @property
    def entries(self) -> Dict[str, UniverseEntry]:
        return self._entries

    @property
    def working_set(self) -> List[str]:
        """MEXC symbols we should subscribe to."""
        out: List[str] = []
        excl = set(s.upper() for s in (self.cfg.universe.exclude or []))
        only = set(s.upper() for s in (self.cfg.universe.include_only or []))
        for sym, e in self._entries.items():
            if sym.upper() in excl:
                continue
            if only and sym.upper() not in only:
                continue
            if self.cfg.universe.require_binance_ref and not e.has_ref:
                continue
            out.append(sym)
            if len(out) >= self.cfg.universe.max_symbols:
                break
        return out

    @property
    def available_pool(self) -> List[str]:
        """All MEXC 0-fee symbols that also have a Binance reference,
        regardless of include_only / exclude / max_symbols."""
        out: List[str] = []
        for sym, e in self._entries.items():
            if self.cfg.universe.require_binance_ref and not e.has_ref:
                continue
            out.append(sym)
        return out

    def reference_for(self, mexc_symbol: str) -> Optional[str]:
        e = self._entries.get(mexc_symbol)
        return e.binance_symbol if e else None

    def price_factor_for(self, mexc_symbol: str) -> float:
        e = self._entries.get(mexc_symbol)
        return e.binance_price_factor if e else 1.0

    async def refresh(self) -> List[str]:
        """Pull 0-fee list + Binance availability. Returns the working set."""
        async with self._lock:
            zero_fee = await self._safe_zero_fee_list()
            binance_set = await self._safe_binance_set()

            entries: Dict[str, UniverseEntry] = {}

            # Force-include symbols come first so they're not pushed off by max_symbols cap.
            for sym in (self.cfg.universe.force_include_symbols or []):
                norm = str(sym).strip().upper()
                if not norm:
                    continue
                if not norm.endswith("_USDT") and "_" not in norm:
                    norm = f"{norm}_USDT"
                if norm in entries:
                    continue
                bsym, bfactor = resolve_binance_symbol(norm, binance_set)
                has_ref = bsym is not None
                entries[norm] = UniverseEntry(
                    mexc_symbol=norm,
                    binance_symbol=bsym,
                    has_ref=has_ref,
                    binance_price_factor=bfactor,
                )

            for sym in zero_fee:
                if sym in entries:
                    continue
                bsym, bfactor = resolve_binance_symbol(sym, binance_set)
                has_ref = bsym is not None
                entries[sym] = UniverseEntry(
                    mexc_symbol=sym,
                    binance_symbol=bsym,
                    has_ref=has_ref,
                    binance_price_factor=bfactor,
                )

            self._entries = entries
            try:
                self._save_cache()
            except Exception:
                pass
            return self.working_set

    async def loop(self, on_change=None) -> None:
        """Long-running refresh loop."""
        while True:
            try:
                prev = set(self.working_set)
                ws = await self.refresh()
                cur = set(ws)
                if prev != cur and on_change is not None:
                    try:
                        await on_change(ws)
                    except Exception as e:
                        logger.warning("universe on_change handler error: %s", e)
            except Exception as e:
                logger.warning("universe refresh error: %s", e)
            await asyncio.sleep(max(30, int(self.cfg.universe.refresh_sec)))

    # -------------------- internals --------------------

    async def _safe_zero_fee_list(self) -> List[str]:
        try:
            return await self.trader.list_zero_fee_symbols()
        except Exception as e:
            logger.warning("list_zero_fee_symbols failed: %s; using cache", e)
            return self._load_cache().get("symbols", [])

    async def _safe_binance_set(self) -> Set[str]:
        # Cache for 10 minutes
        now = time.time()
        if self._binance_set and (now - self._binance_set_ts) < 600:
            return self._binance_set

        try:
            connector = aiohttp.TCPConnector(ssl=False, resolver=aiohttp.ThreadedResolver())
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as s:
                async with s.get(BINANCE_FUTURES_INFO) as r:
                    data = await r.json()
            symbols = data.get("symbols") or []
            out = set()
            for it in symbols:
                if not isinstance(it, dict):
                    continue
                sym = str(it.get("symbol") or "").upper()
                contract_type = str(it.get("contractType") or "").upper()
                quote = str(it.get("quoteAsset") or "").upper()
                if quote != "USDT":
                    continue
                if contract_type and contract_type != "PERPETUAL":
                    continue
                out.add(sym)
            self._binance_set = out
            self._binance_set_ts = now
            return out
        except Exception as e:
            logger.warning("Binance exchangeInfo failed: %s", e)
            return self._binance_set or set()

    def _save_cache(self) -> None:
        data = {
            "ts": time.time(),
            "symbols": list(self._entries.keys()),
            "with_ref": [s for s, e in self._entries.items() if e.has_ref],
        }
        self.cache_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def _load_cache(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
