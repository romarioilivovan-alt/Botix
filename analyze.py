"""Analyze paper-mode results from data.sqlite.

Run locally:    python analyze.py
Or auto-commit: python analyze.py > analysis.md  (then git add/commit/push)

The remote scheduled agent imports the same SQL queries to produce its report.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


_default_db = Path(__file__).resolve().parent / "data.sqlite"
# Allow `python analyze.py path/to/db.sqlite` as override.
DB = Path(sys.argv[1]) if len(sys.argv) > 1 else _default_db


def fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def fmt_num(v, dp: int = 4) -> str:
    if v is None:
        return "-"
    try:
        v = float(v)
    except Exception:
        return str(v)
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:.{dp}f}"


def section(title: str) -> None:
    print()
    print(f"## {title}")
    print()


def query_one(con: sqlite3.Connection, sql: str, params: tuple = ()) -> dict:
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    row = cur.fetchone()
    cur.close()
    return dict(zip(cols, row)) if row else {}


def query_all(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()
    cur.close()
    return [dict(zip(cols, r)) for r in rows]


def print_table(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        print("_(no rows)_")
        return
    widths = {c: max(len(c), max(len(str(r.get(c, "-"))) for r in rows)) for c in cols}
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    sep = " | ".join("-" * widths[c] for c in cols)
    print(f"| {header} |")
    print(f"| {sep} |")
    for r in rows:
        line = " | ".join(str(r.get(c, "-")).ljust(widths[c]) for c in cols)
        print(f"| {line} |")


def main() -> int:
    if not DB.exists():
        print(f"ERROR: {DB} not found. Run the bot at least once.")
        return 1

    con = sqlite3.connect(str(DB))

    # ---------- header ----------
    print(f"# Paper-mode analysis report")
    print()
    print(f"_Generated: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}_")
    print(f"_Database: `{DB.name}`_")

    # ---------- tables present ----------
    section("Schema")
    tabs = query_all(con, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    for t in tabs:
        cnt = query_one(con, f"SELECT COUNT(*) AS n FROM {t['name']}")
        print(f"- `{t['name']}`: {cnt.get('n', 0)} rows")

    # ---------- equity ----------
    section("Equity curve")
    eq = query_all(con, "SELECT ts, balance, equity, open_positions FROM equity WHERE mode='paper' ORDER BY ts")
    if eq:
        first, last = eq[0], eq[-1]
        print(f"- start: balance={fmt_num(first['balance'])} at {fmt_ts(first['ts'])}")
        print(f"- end:   balance={fmt_num(last['balance'])} at {fmt_ts(last['ts'])}")
        print(f"- delta PnL: {fmt_num(last['balance'] - first['balance'])} USDT")
        peak = max(r["equity"] for r in eq)
        trough = min(r["equity"] for r in eq)
        print(f"- peak equity: {fmt_num(peak)}")
        print(f"- trough equity: {fmt_num(trough)}")
        print(f"- max drawdown from peak: {fmt_num((peak - trough) / peak * 100, 2)}%")
        print(f"- snapshots: {len(eq)}")
    else:
        print("_no equity rows yet — bot has not run long enough_")

    # ---------- overall trade stats ----------
    section("Overall trade stats (paper)")
    s = query_one(con, """
        SELECT COUNT(*) n,
               COALESCE(SUM(pnl_usdt), 0) total_pnl,
               COALESCE(AVG(pnl_usdt), 0) avg_pnl,
               COALESCE(AVG(duration_sec), 0) avg_dur,
               COALESCE(SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END), 0) wins,
               COALESCE(SUM(CASE WHEN pnl_usdt <= 0 THEN 1 ELSE 0 END), 0) losses,
               COALESCE(MAX(pnl_usdt), 0) best,
               COALESCE(MIN(pnl_usdt), 0) worst
        FROM trades WHERE mode='paper'
    """)
    n = s.get("n", 0) or 0
    print(f"- trades: **{n}**")
    if n:
        print(f"- total PnL: **{fmt_num(s['total_pnl'])}** USDT")
        print(f"- avg PnL/trade: {fmt_num(s['avg_pnl'])}")
        print(f"- avg duration: {fmt_num(s['avg_dur'], 1)}s")
        print(f"- win rate: **{s['wins'] / n * 100:.1f}%** ({s['wins']}W / {s['losses']}L)")
        print(f"- best trade: {fmt_num(s['best'])}")
        print(f"- worst trade: {fmt_num(s['worst'])}")

    # ---------- by close reason ----------
    section("By close reason")
    rows = query_all(con, """
        SELECT close_reason,
               COUNT(*) n,
               ROUND(AVG(pnl_usdt), 4) avg_pnl,
               ROUND(SUM(pnl_usdt), 4) total_pnl,
               ROUND(AVG(duration_sec), 1) avg_dur
        FROM trades WHERE mode='paper'
        GROUP BY close_reason
        ORDER BY n DESC
    """)
    print_table(rows, ["close_reason", "n", "avg_pnl", "total_pnl", "avg_dur"])

    # ---------- by side ----------
    section("By side")
    rows = query_all(con, """
        SELECT side,
               COUNT(*) n,
               ROUND(AVG(pnl_usdt), 4) avg_pnl,
               ROUND(SUM(pnl_usdt), 4) total_pnl,
               SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) wins
        FROM trades WHERE mode='paper'
        GROUP BY side
        ORDER BY total_pnl DESC
    """)
    print_table(rows, ["side", "n", "avg_pnl", "total_pnl", "wins"])

    # ---------- top symbols ----------
    section("Top 20 symbols by PnL")
    rows = query_all(con, """
        SELECT symbol,
               COUNT(*) n,
               ROUND(SUM(pnl_usdt), 4) total_pnl,
               ROUND(AVG(pnl_usdt), 4) avg_pnl,
               SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) wins
        FROM trades WHERE mode='paper'
        GROUP BY symbol
        HAVING n >= 3
        ORDER BY total_pnl DESC
        LIMIT 20
    """)
    print_table(rows, ["symbol", "n", "total_pnl", "avg_pnl", "wins"])

    section("Bottom 20 symbols by PnL")
    rows = query_all(con, """
        SELECT symbol,
               COUNT(*) n,
               ROUND(SUM(pnl_usdt), 4) total_pnl,
               ROUND(AVG(pnl_usdt), 4) avg_pnl,
               SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) wins
        FROM trades WHERE mode='paper'
        GROUP BY symbol
        HAVING n >= 3
        ORDER BY total_pnl ASC
        LIMIT 20
    """)
    print_table(rows, ["symbol", "n", "total_pnl", "avg_pnl", "wins"])

    # ---------- duration distribution ----------
    section("Duration distribution")
    buckets = [
        ("< 5s",   "duration_sec < 5"),
        ("5-15s",  "duration_sec >= 5  AND duration_sec < 15"),
        ("15-30s", "duration_sec >= 15 AND duration_sec < 30"),
        ("30-45s", "duration_sec >= 30 AND duration_sec < 45"),
        (">= 45s", "duration_sec >= 45"),
    ]
    rows = []
    for label, cond in buckets:
        r = query_one(con, f"""
            SELECT COUNT(*) n,
                   ROUND(AVG(pnl_usdt), 4) avg_pnl,
                   SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) wins
            FROM trades WHERE mode='paper' AND ({cond})
        """)
        rows.append({"bucket": label, **r})
    print_table(rows, ["bucket", "n", "avg_pnl", "wins"])

    # ---------- candidates seen vs taken ----------
    section("Candidates pipeline")
    cand = query_one(con, """
        SELECT COUNT(*) total,
               SUM(CASE WHEN accepted = 1 THEN 1 ELSE 0 END) accepted,
               SUM(CASE WHEN side = 'LONG'  THEN 1 ELSE 0 END) longs,
               SUM(CASE WHEN side = 'SHORT' THEN 1 ELSE 0 END) shorts
        FROM candidates_log
    """)
    print(f"- candidates emitted: {cand.get('total', 0)}")
    print(f"- accepted: {cand.get('accepted', 0)}")
    print(f"- longs: {cand.get('longs', 0)} / shorts: {cand.get('shorts', 0)}")

    section("Most-frequent symbols in candidate stream (top 15)")
    rows = query_all(con, """
        SELECT symbol, COUNT(*) n, ROUND(AVG(score), 2) avg_score, ROUND(AVG(z), 2) avg_z
        FROM candidates_log
        GROUP BY symbol
        ORDER BY n DESC
        LIMIT 15
    """)
    print_table(rows, ["symbol", "n", "avg_score", "avg_z"])

    # ---------- diagnostic ----------
    section("Verdict heuristics")
    if not n:
        print("⚠ **Zero trades**. Check whether the engine actually ran (binance_ok / mexc_ok), "
              "and whether `entry_z` is too high. Try lowering to 1.2.")
    else:
        wr = (s["wins"] / n) * 100
        notes = []
        if s["total_pnl"] > 0:
            notes.append(f"✅ Net positive PnL after {n} trades.")
        else:
            notes.append(f"❌ Net negative PnL after {n} trades.")
        if wr >= 55:
            notes.append(f"✅ Win rate {wr:.1f}% — solid for a SL>TP setup.")
        elif wr >= 45:
            notes.append(f"⚠ Win rate {wr:.1f}% — borderline. Check close-reason mix.")
        else:
            notes.append(f"❌ Win rate {wr:.1f}% — strategy is losing.")
        # close-reason imbalance
        sl_n = next((r["n"] for r in query_all(con, "SELECT close_reason, COUNT(*) n FROM trades WHERE mode='paper' GROUP BY close_reason") if r["close_reason"] == "sl"), 0)
        tp_n = next((r["n"] for r in query_all(con, "SELECT close_reason, COUNT(*) n FROM trades WHERE mode='paper' GROUP BY close_reason") if r["close_reason"] == "tp"), 0)
        if sl_n + tp_n:
            ratio = tp_n / max(1, sl_n + tp_n)
            if ratio >= 0.55:
                notes.append(f"✅ TP/SL ratio {ratio:.2f} — mean-reversion is working.")
            else:
                notes.append(f"⚠ TP/SL ratio {ratio:.2f} — most trades stopped out.")
        for nt in notes:
            print(f"- {nt}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
