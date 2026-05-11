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

    def top_notional(self, levels: int = 10, contract_size: float = 1.0) -> float:
        """Sum notional for top N levels both sides.

        On MEXC futures the book quantity is contract volume, not always base
        asset size, so callers can pass `contract_size` to convert contracts to
        underlying units before translating into USDT notional.
        """
        s = 0.0
        for p, q in self.bids[:levels]:
            try:
                s += float(p) * float(q) * float(contract_size)
            except Exception:
                pass
        for p, q in self.asks[:levels]:
            try:
                s += float(p) * float(q) * float(contract_size)
            except Exception:
                pass
        return s

    def side_levels(self, side: str) -> List[List[float]]:
        return self.asks if str(side).upper() == "LONG" else self.bids

    def available_qty(self, side: str, levels: int = 3) -> float:
        rows = self.side_levels(side)[: max(1, int(levels or 1))]
        total = 0.0
        for _price, qty in rows:
            try:
                total += float(qty)
            except Exception:
                pass
        return total

    def inferred_tick_size(self, levels: int = 5) -> float:
        gaps: List[float] = []
        depth = max(2, int(levels or 2))
        for rows in (self.bids[:depth], self.asks[:depth]):
            for idx in range(len(rows) - 1):
                try:
                    gap = abs(float(rows[idx][0]) - float(rows[idx + 1][0]))
                except Exception:
                    continue
                if gap > 0:
                    gaps.append(gap)
        return min(gaps) if gaps else 0.0

    def path_hole_points(self, side: str, levels: int = 3, point_size: float = 0.0) -> float:
        rows = self.side_levels(side)[: max(2, int(levels or 2))]
        if len(rows) < 2:
            return 0.0
        tick = float(point_size or 0.0)
        if tick <= 0:
            tick = self.inferred_tick_size(levels=levels)
        if tick <= 0:
            return 0.0
        worst_gap = 0.0
        long_side = str(side).upper() == "LONG"
        for idx in range(len(rows) - 1):
            try:
                p1 = float(rows[idx][0])
                p2 = float(rows[idx + 1][0])
            except Exception:
                continue
            gap = (p2 - p1) if long_side else (p1 - p2)
            worst_gap = max(worst_gap, max(0.0, gap / tick))
        return worst_gap

    def level_shape_ratio(self, side: str, levels: int = 3) -> float:
        rows = self.side_levels(side)[: max(1, int(levels or 1))]
        if not rows:
            return 0.0
        try:
            top_qty = max(float(rows[0][1]), 1e-18)
        except Exception:
            return 0.0
        total_qty = 0.0
        for _price, qty in rows:
            try:
                total_qty += float(qty)
            except Exception:
                pass
        return total_qty / top_qty

    def support_ratio(self, side: str, levels: int = 3) -> float:
        path_qty = self.available_qty(side, levels)
        support_side = "SHORT" if str(side).upper() == "LONG" else "LONG"
        support_qty = self.available_qty(support_side, levels)
        return support_qty / max(path_qty, 1e-18)


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
    external_fair_available: bool = True  # False for stocks without Binance reference

    # Order flow imbalance (Binance trades) — positive = aggressive buys
    ofi: Optional[float] = None         # signed USDT volume imbalance
    fair_velocity_bps_per_sec: Optional[float] = None

    # Liquidity
    mexc_book_top10_notional: Optional[float] = None
    mexc_book_age_ms: Optional[float] = None
    binance_book_age_ms: Optional[float] = None

    # MEXC own microstructure
    mexc_book_imbalance: Optional[float] = None       # log(bid_notional/ask_notional) over top-5
    mexc_spread_bps: Optional[float] = None           # current (best_ask - best_bid) / mid * 1e4
    mexc_spread_bps_avg: Optional[float] = None       # rolling 30s mean of mexc_spread_bps
    long_path_hole_points: Optional[float] = None
    short_path_hole_points: Optional[float] = None
    long_path_shape: Optional[float] = None
    short_path_shape: Optional[float] = None
    long_support_ratio: Optional[float] = None
    short_support_ratio: Optional[float] = None
    long_support_shape: Optional[float] = None
    short_support_shape: Optional[float] = None
    long_back_hole_points: Optional[float] = None
    short_back_hole_points: Optional[float] = None

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
    selected_algorithm: Optional[str] = None

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
    qty: float                      # contracts (MEXC book quantities are contract counts)
    open_ts: float

    fair_at_open: float
    sigma_at_open: float
    contract_size: float = 1.0      # base units per contract
    quote_ts: float = 0.0        # when the limit quote was placed (for entry_latency)

    # Speed metrics (NEW)
    signal_ts: float = 0.0       # when signal was detected
    entry_latency_ms: float = 0.0  # signal_ts -> open_ts (milliseconds)
    entry_algo: Optional[str] = None  # which algorithm triggered entry
    entry_score: float = 0.0     # score at entry
    max_hold_sec: float = 0.0
    entry_fill_ratio: Optional[float] = None
    entry_levels_eaten: Optional[int] = None
    entry_spread_bps: Optional[float] = None
    entry_ofi: Optional[float] = None
    entry_imbalance: Optional[float] = None
    entry_fv1: Optional[float] = None
    entry_fv5: Optional[float] = None
    entry_fv30: Optional[float] = None
    entry_mexc_book_age_ms: Optional[float] = None
    entry_binance_book_age_ms: Optional[float] = None

    # SL / TP state
    stop_price: Optional[float] = None
    tp_price: Optional[float] = None
    best_excursion: Optional[float] = None  # max favorable price reached
    best_realized_bps: float = 0.0
    last_sl_update_ts: float = 0.0

    # R-based trailing: R = initial SL distance from entry (price units, positive)
    initial_sl_distance: Optional[float] = None
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

    # Exit speed metrics (NEW)
    exit_signal_ts: float = 0.0  # when exit decision was made
    exit_latency_ms: float = 0.0  # exit_signal_ts -> close_ts (milliseconds)
    settled_profit_since: float = 0.0
    settled_profit_anchor_bps: float = 0.0


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
        self.available_balance: float = 0.0
        self.equity_history: Deque[Dict[str, float]] = deque(maxlen=2000)
        self.session_starting_balance: float = 0.0
        self.session_peak_balance: float = 0.0
        self.strategy_realized_pnl: float = 0.0
        self.strategy_equity_history: Deque[Dict[str, float]] = deque(maxlen=2000)
        self.strategy_session_starting_balance: float = 0.0
        self.strategy_session_peak_balance: float = 0.0

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
                    "entry_latency_ms": p.entry_latency_ms,
                    "entry_algo": p.entry_algo,
                    "entry_score": p.entry_score,
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
                    "fv5": st.fair_velocity_5s_bps,
                    "fv30": st.fair_velocity_30s_bps,
                    "depth": st.mexc_book_top10_notional,
                    "imbalance": st.mexc_book_imbalance,
                    "score": st.score,
                    "side": st.side_hint,
                    "blocked": st.blocked_reason,
                }

            equity_out = list(self.equity_history)[-300:]
            strategy_equity_out = list(self.strategy_equity_history)[-300:]
            recent_trades = list(self.recent_trades)[-50:]
            logs = [
                {"t": l.t, "level": l.level, "msg": l.msg}
                for l in list(self.logs)[-100:]
            ]
            strategy_start = (
                self.strategy_session_starting_balance
                if self.strategy_session_starting_balance > 0
                else self.session_starting_balance
            )
            strategy_open_pnl = sum(float(p.last_pnl_usdt or 0.0) for p in self.positions.values())
            strategy_equity = strategy_start + self.strategy_realized_pnl + strategy_open_pnl

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
                "available_balance": self.available_balance,
                "session_starting_balance": self.session_starting_balance,
                "session_peak_balance": self.session_peak_balance,
                "account": {
                    "equity": self.balance,
                    "available_balance": self.available_balance,
                    "session_starting_balance": self.session_starting_balance,
                    "session_peak_balance": self.session_peak_balance,
                },
                "strategy": {
                    "realized_pnl": self.strategy_realized_pnl,
                    "open_pnl": strategy_open_pnl,
                    "equity": strategy_equity,
                    "session_starting_balance": strategy_start,
                    "session_peak_balance": self.strategy_session_peak_balance or strategy_start,
                },
                "universe_size": len(self.universe),
                "candidates": candidates_out[:20],
                "stats_summary": stats_out,
                "positions": positions_out,
                "equity": equity_out,
                "strategy_equity": strategy_equity_out,
                "recent_trades": recent_trades,
                "logs": logs,
            }
