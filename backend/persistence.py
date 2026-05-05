"""SQLite persistence for trades, candidates, equity.

Async-safe via aiosqlite. Schema is created on startup. Used by both paper and
real executors.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("ZFEE_DB_PATH") or _PROJECT_ROOT / "data.sqlite")


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,                -- paper | real
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                -- LONG | SHORT
    entry REAL NOT NULL,
    exit REAL,
    qty REAL NOT NULL,
    notional REAL NOT NULL,
    margin REAL NOT NULL,
    leverage REAL NOT NULL,
    open_ts REAL NOT NULL,
    close_ts REAL,
    duration_sec REAL,
    pnl_usdt REAL,
    pnl_pct REAL,
    fair_at_open REAL,
    sigma_at_open REAL,
    z_at_open REAL,
    close_reason TEXT,
    extra TEXT
);

CREATE INDEX IF NOT EXISTS ix_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS ix_trades_symbol ON trades(symbol);

CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    balance REAL NOT NULL,
    equity REAL NOT NULL,
    open_positions INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_equity_ts ON equity(ts);

CREATE TABLE IF NOT EXISTS candidates_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT,
    score REAL,
    z REAL,
    spread_bps REAL,
    fair REAL,
    mexc REAL,
    depth REAL,
    blocked TEXT,
    accepted INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_cand_ts ON candidates_log(ts);
"""


class Store:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(str(self.path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        # Migrations for columns added after initial release
        for migration in (
            "ALTER TABLE trades ADD COLUMN entry_latency_sec REAL",
        ):
            try:
                await self._db.execute(migration)
            except Exception:
                pass  # column already exists
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ---------- writes ----------

    async def insert_trade(self, row: Dict[str, Any]) -> None:
        if not self._db:
            return
        await self._db.execute(
            """INSERT INTO trades(
                ts, mode, symbol, side, entry, exit, qty, notional, margin, leverage,
                open_ts, close_ts, duration_sec, pnl_usdt, pnl_pct,
                fair_at_open, sigma_at_open, z_at_open, close_reason, extra,
                entry_latency_sec
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("ts", time.time()),
                row.get("mode", "paper"),
                row["symbol"],
                row["side"],
                float(row["entry"]),
                float(row["exit"]) if row.get("exit") is not None else None,
                float(row["qty"]),
                float(row["notional"]),
                float(row["margin"]),
                float(row["leverage"]),
                float(row["open_ts"]),
                float(row["close_ts"]) if row.get("close_ts") is not None else None,
                float(row["duration_sec"]) if row.get("duration_sec") is not None else None,
                float(row["pnl_usdt"]) if row.get("pnl_usdt") is not None else None,
                float(row["pnl_pct"]) if row.get("pnl_pct") is not None else None,
                float(row["fair_at_open"]) if row.get("fair_at_open") is not None else None,
                float(row["sigma_at_open"]) if row.get("sigma_at_open") is not None else None,
                float(row["z_at_open"]) if row.get("z_at_open") is not None else None,
                row.get("close_reason"),
                json.dumps(row.get("extra") or {}, ensure_ascii=False),
                float(row["entry_latency_sec"]) if row.get("entry_latency_sec") is not None else None,
            ),
        )
        await self._db.commit()

    async def insert_equity(self, ts: float, mode: str, balance: float,
                            equity: float, open_positions: int) -> None:
        if not self._db:
            return
        await self._db.execute(
            "INSERT INTO equity(ts, mode, balance, equity, open_positions) VALUES (?,?,?,?,?)",
            (ts, mode, balance, equity, open_positions),
        )
        await self._db.commit()

    async def insert_candidate(self, ts: float, symbol: str, side: Optional[str],
                               score: float, z: Optional[float],
                               spread_bps: Optional[float],
                               fair: Optional[float], mexc: Optional[float],
                               depth: Optional[float], blocked: Optional[str],
                               accepted: bool) -> None:
        if not self._db:
            return
        await self._db.execute(
            """INSERT INTO candidates_log(
                ts, symbol, side, score, z, spread_bps, fair, mexc, depth, blocked, accepted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, symbol, side, float(score or 0.0), z, spread_bps, fair, mexc, depth,
             blocked, 1 if accepted else 0),
        )
        await self._db.commit()

    # ---------- reads ----------

    async def list_trades(self, limit: int = 200, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self._db:
            return []
        if mode:
            cur = await self._db.execute(
                "SELECT * FROM trades WHERE mode=? ORDER BY id DESC LIMIT ?",
                (mode, limit),
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,),
            )
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def stats_summary(self, mode: Optional[str] = None) -> Dict[str, Any]:
        if not self._db:
            return {}
        where = "WHERE mode=?" if mode else ""
        params = (mode,) if mode else ()
        cur = await self._db.execute(
            f"""SELECT
                COUNT(*) AS n,
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN pnl_usdt <= 0 THEN 1 ELSE 0 END) AS losses,
                COALESCE(SUM(pnl_usdt), 0) AS total_pnl,
                COALESCE(AVG(pnl_usdt), 0) AS avg_pnl,
                COALESCE(AVG(duration_sec), 0) AS avg_duration
            FROM trades {where}""",
            params,
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return {}
        return dict(row)

    async def list_equity(self, limit: int = 500, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self._db:
            return []
        if mode:
            cur = await self._db.execute(
                "SELECT ts, balance, equity, open_positions FROM equity WHERE mode=? ORDER BY id DESC LIMIT ?",
                (mode, limit),
            )
        else:
            cur = await self._db.execute(
                "SELECT ts, balance, equity, open_positions FROM equity ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        rows = await cur.fetchall()
        await cur.close()
        return list(reversed([dict(r) for r in rows]))
