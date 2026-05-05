from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional


@dataclass
class OrderBook:
    bids: List[List[float]] = field(default_factory=list)
    asks: List[List[float]] = field(default_factory=list)
    ts: float = 0.0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    def top_notional(self, levels: int = 10) -> float:
        """Sum (price * qty) for top N levels both sides."""
        s = 0.0
        for p, q in self.bids[:levels]:
            try:
                s += float(p) * float(q)
            except Exception:
                pass
        for p, q in self.asks[:levels]:
            try:
                s += float(p) * float(q)
            except Exception:
                pass
        return s


@dataclass
class SymbolStats:
    """Rolling stats for a single MEXC 0-fee symbol."""

    # Last fair value & spread reading
    fair: Optional[float] = None
    mexc_mid: Optional[float] = None
    spread: Optional[float] = None      # MEXC_mid - F (in price units)
    spread_bps: Optional[float] = None  # spread / F * 1e4
    z_score: Optional[float] = None
    sigma_spread: Optional[float] = None  # std of spread (price units)

    # Order flow imbalance (Binance trades) — positive = aggressive buys
    ofi: Optional[float] = None         # signed USDT volume imbalance
    fair_velocity_bps_per_sec: Optional[float] = None

    # Liquidity
    mexc_book_top10_notional: Optional[float] = None

    # MEXC own microstructure
    mexc_book_imbalance: Optional[float] = None       # log(bid_notional/ask_notional) over top-5
    mexc_spread_bps: Optional[float] = None           # current (best_ask - best_bid) / mid * 1e4
    mexc_spread_bps_avg: Optional[float] = None       # rolling 30s mean of mexc_spread_bps

    # Binance event-flow
    binance_burst_usdt_1s: Optional[float] = None     # max signed notional over any 1s window in last 5s

    # Multi-timeframe Binance velocity (for trend-aware filters)
    fair_velocity_5s_bps: Optional[float] = None      # avg Δprice over last 5s
    fair_velocity_30s_bps: Optional[float] = None     # avg Δprice over last 30s

    # MEXC own-price Bollinger band (for self-reverting strategies)
    mexc_mid_mean_60s: Optional[float] = None
    mexc_mid_std_60s: Optional[float] = None
    mexc_mid_z_60s: Optional[float] = None            # (current - mean) / std

    # Score
    score: float = 0.0
    side_hint: Optional[str] = None     # LONG / SHORT / None
    blocked_reason: Optional[str] = None

    last_update_ts: float = 0.0


@dataclass
class ManagedPosition:
    """A position the bot is managing (paper or real)."""

    symbol: str
    side: str                       # LONG / SHORT
    entry_price: float
    notional_usdt: float
    margin_usdt: float
    leverage: float
    qty: float                      # base tokens (long+) or contracts
    open_ts: float

    fair_at_open: float
    sigma_at_open: float
    quote_ts: float = 0.0        # when the limit quote was placed (for entry_latency)

    # SL / TP state
    stop_price: Optional[float] = None
    tp_price: Optional[float] = None
    best_excursion: Optional[float] = None  # max favorable price reached
    last_sl_update_ts: float = 0.0

    # R-based trailing: R = initial SL distance from entry (price units, positive)
    initial_sl_distance: Optional[float] = None
    # Snapshot of MEXC book imbalance at open — used by signal-flip exit
    entry_imbalance: Optional[float] = None

    # Tracking
    last_pnl_usdt: float = 0.0
    last_pnl_pct: float = 0.0

    # Real-money only
    mexc_position_id: Optional[int] = None
    mexc_stop_plan_id: Optional[int] = None
    mexc_entry_order_id: Optional[int] = None

    closed: bool = False
    close_reason: Optional[str] = None
    close_ts: float = 0.0
    close_price: Optional[float] = None
    realized_pnl: float = 0.0


@dataclass
class TradeLogEntry:
    t: float
    level: str
    msg: str


class AppState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()

        # Per-symbol books
        self.binance_books: Dict[str, OrderBook] = {}     # key: "BTCUSDT"
        self.mexc_books: Dict[str, OrderBook] = {}        # key: "BTC_USDT"

        # Per-symbol rolling stats
        self.stats: Dict[str, SymbolStats] = {}           # key: MEXC symbol

        # Universe
        self.universe: List[str] = []                      # MEXC 0-fee symbols
        self.universe_refs: Dict[str, str] = {}            # MEXC -> Binance symbol

        # Top candidates (sorted, refreshed periodically)
        self.candidates: List[Dict[str, Any]] = []         # snapshot for UI

        # Open positions managed by us
        self.positions: Dict[str, ManagedPosition] = {}    # MEXC symbol -> pos

        # Cooldowns per symbol (prevent re-entry)
        self.cooldown_until: Dict[str, float] = {}

        # Equity curve (paper or real)
        self.balance: float = 0.0
        self.equity_history: Deque[Dict[str, float]] = deque(maxlen=2000)
        self.session_starting_balance: float = 0.0
        self.session_peak_balance: float = 0.0

        # Daily PnL tracking
        self.day_start_ts: float = 0.0
        self.day_start_balance: float = 0.0

        # Trade history (in-memory recent; full in SQLite)
        self.recent_trades: Deque[Dict[str, Any]] = deque(maxlen=200)

        # Engine state
        self.engine_running: bool = False
        self.engine_mode: str = "paper"      # paper / real / logger
        self.kill_switch: bool = False
        self.last_kill_reason: str = ""

        # Connectivity
        self.binance_ws_ok: bool = False
        self.mexc_ws_ok: bool = False
        self.mexc_auth_ok: Optional[bool] = None
        self.mexc_auth_msg: str = ""

        # Logs
        self.logs: Deque[TradeLogEntry] = deque(maxlen=500)

    async def add_log(self, level: str, msg: str) -> None:
        async with self.lock:
            self.logs.append(TradeLogEntry(t=time.time(), level=level, msg=msg))

    async def snapshot(self) -> Dict[str, Any]:
        async with self.lock:
            positions_out = []
            for p in self.positions.values():
                positions_out.append({
                    "symbol": p.symbol,
                    "side": p.side,
                    "entry": p.entry_price,
                    "qty": p.qty,
                    "notional": p.notional_usdt,
                    "margin": p.margin_usdt,
                    "lev": p.leverage,
                    "stop": p.stop_price,
                    "tp": p.tp_price,
                    "open_ts": p.open_ts,
                    "pnl": p.last_pnl_usdt,
                    "pnl_pct": p.last_pnl_pct,
                })

            candidates_out = list(self.candidates)
            stats_out = {}
            for sym, st in self.stats.items():
                stats_out[sym] = {
                    "fair": st.fair,
                    "mexc_mid": st.mexc_mid,
                    "spread_bps": st.spread_bps,
                    "z": st.z_score,
                    "sigma": st.sigma_spread,
                    "ofi": st.ofi,
                    "fv": st.fair_velocity_bps_per_sec,
                    "depth": st.mexc_book_top10_notional,
                    "score": st.score,
                    "side": st.side_hint,
                    "blocked": st.blocked_reason,
                }

            equity_out = list(self.equity_history)[-300:]
            recent_trades = list(self.recent_trades)[-50:]
            logs = [
                {"t": l.t, "level": l.level, "msg": l.msg}
                for l in list(self.logs)[-100:]
            ]

            return {
                "engine": {
                    "running": self.engine_running,
                    "mode": self.engine_mode,
                    "kill": self.kill_switch,
                    "kill_reason": self.last_kill_reason,
                    "binance_ok": self.binance_ws_ok,
                    "mexc_ok": self.mexc_ws_ok,
                    "mexc_auth_ok": self.mexc_auth_ok,
                    "mexc_auth_msg": self.mexc_auth_msg,
                },
                "balance": self.balance,
                "session_starting_balance": self.session_starting_balance,
                "session_peak_balance": self.session_peak_balance,
                "universe_size": len(self.universe),
                "candidates": candidates_out[:20],
                "stats_summary": stats_out,
                "positions": positions_out,
                "equity": equity_out,
                "recent_trades": recent_trades,
                "logs": logs,
            }
