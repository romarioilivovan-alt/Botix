"""Compare results across the 3 strategy databases.

Usage:  python compare.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DBS = [
    # NIGHT EXPERIMENT — 10 algorithms × 8-coin basket
    ("night_raw_momentum_basic", ROOT / "data.night_raw_momentum_basic.sqlite"),
    ("night_raw_momentum_slow",  ROOT / "data.night_raw_momentum_slow.sqlite"),
    ("night_raw_momentum_inv",   ROOT / "data.night_raw_momentum_inv.sqlite"),
    ("night_bb_revert_tight",    ROOT / "data.night_bb_revert_tight.sqlite"),
    ("night_bb_revert_loose",    ROOT / "data.night_bb_revert_loose.sqlite"),
    ("night_imbalance",          ROOT / "data.night_imbalance.sqlite"),
    ("night_book_lean",          ROOT / "data.night_book_lean.sqlite"),
    ("night_ofi",                ROOT / "data.night_ofi.sqlite"),
    ("night_confluence",         ROOT / "data.night_confluence.sqlite"),
    ("night_meanrev",            ROOT / "data.night_meanrev.sqlite"),
    # Bollinger band mean-reversion (NEW — different strategy class)
    ("bb_tao",       ROOT / "data.bb_tao.sqlite"),
    ("bb_arb",       ROOT / "data.bb_arb.sqlite"),
    ("bb_aave",      ROOT / "data.bb_aave.sqlite"),
    ("bb_fartcoin",  ROOT / "data.bb_fartcoin.sqlite"),
    ("bb_icp",       ROOT / "data.bb_icp.sqlite"),
    ("bb_eth",       ROOT / "data.bb_eth.sqlite"),
    ("bb_btc",       ROOT / "data.bb_btc.sqlite"),
    ("bb_bch",       ROOT / "data.bb_bch.sqlite"),
    # Per-symbol tests (top performers, multi-tf filter)
    ("solo_tao",       ROOT / "data.solo_tao.sqlite"),
    ("solo_arb",       ROOT / "data.solo_arb.sqlite"),
    ("solo_aster",     ROOT / "data.solo_aster.sqlite"),
    ("solo_ena",       ROOT / "data.solo_ena.sqlite"),
    ("solo_aave",      ROOT / "data.solo_aave.sqlite"),
    ("solo_fartcoin",  ROOT / "data.solo_fartcoin.sqlite"),
    ("solo_etc",       ROOT / "data.solo_etc.sqlite"),
    ("solo_icp",       ROOT / "data.solo_icp.sqlite"),
    # STRESS TEST (4s latency + fees)
    ("stress_confluence",   ROOT / "data.stress_confluence.sqlite"),
    ("stress_raw_momentum", ROOT / "data.stress_raw_momentum.sqlite"),
    # Top-WR basket (8 symbols, diversified)
    ("real_basket_top",  ROOT / "data.real_basket_top.sqlite"),
    # Single-symbol TAO experiment
    ("real_tao_solo",    ROOT / "data.real_tao_solo.sqlite"),
    # Current focused experiment (taker entry, $50, 100x)
    ("raw_momentum",     ROOT / "data.raw_momentum.sqlite"),
    ("inv_raw_momentum", ROOT / "data.inv_raw_momentum.sqlite"),
    ("book_lean",        ROOT / "data.book_lean.sqlite"),
    ("inv_book_lean",    ROOT / "data.inv_book_lean.sqlite"),
    # Older experiments (limit entry) — kept for reference
    ("meanrev",          ROOT / "data.meanrev.sqlite"),
    ("momentum",         ROOT / "data.momentum.sqlite"),
    ("imbalance",        ROOT / "data.imbalance.sqlite"),
    ("wide_spread",      ROOT / "data.wide_spread.sqlite"),
    ("inv_meanrev",      ROOT / "data.inv_meanrev.sqlite"),
    ("inv_momentum",     ROOT / "data.inv_momentum.sqlite"),
    ("inv_imbalance",    ROOT / "data.inv_imbalance.sqlite"),
    ("inv_wide_spread",  ROOT / "data.inv_wide_spread.sqlite"),
]


def stat(con: sqlite3.Connection) -> dict:
    cur = con.execute("""
        SELECT COUNT(*) n,
               COALESCE(SUM(pnl_usdt), 0) total_pnl,
               COALESCE(AVG(pnl_usdt), 0) avg_pnl,
               COALESCE(AVG(duration_sec), 0) avg_dur,
               COALESCE(SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END), 0) wins,
               COALESCE(MIN(pnl_usdt), 0) worst,
               COALESCE(MAX(pnl_usdt), 0) best
        FROM trades WHERE mode='paper'
    """)
    row = cur.fetchone()
    n, total, avg, dur, wins, worst, best = row
    cur.close()
    cur = con.execute("SELECT close_reason, COUNT(*), AVG(pnl_usdt) FROM trades WHERE mode='paper' GROUP BY close_reason")
    reasons = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    cur.close()
    cur = con.execute("SELECT MAX(equity), MIN(equity), balance FROM (SELECT equity, balance FROM equity WHERE mode='paper' ORDER BY ts DESC LIMIT 5000)")
    eq = cur.fetchone()
    cur.close()
    cur = con.execute("SELECT balance FROM equity WHERE mode='paper' ORDER BY ts DESC LIMIT 1")
    last = cur.fetchone()
    cur.close()
    final_balance = last[0] if last else None
    return {
        "n": n, "total_pnl": total, "avg_pnl": avg, "avg_dur": dur,
        "wins": wins, "worst": worst, "best": best,
        "win_rate": (wins / n * 100) if n else 0,
        "reasons": reasons,
        "peak_equity": eq[0] if eq else None,
        "trough_equity": eq[1] if eq else None,
        "final_balance": final_balance,
    }


def main() -> int:
    print(f"{'strategy':<10} | {'trades':>6} | {'PnL $':>10} | {'avg':>7} | {'win%':>5} | {'final':>10} | {'reasons (n/avg)':<40}")
    print("-" * 110)
    rows = []
    for name, db in DBS:
        if not db.exists():
            print(f"{name:<10} | (no db: {db.name})")
            continue
        con = sqlite3.connect(str(db))
        s = stat(con)
        con.close()
        reasons_str = ", ".join(f"{k}:{v[0]} ({v[1]:+.2f})" for k, v in (s["reasons"] or {}).items())
        print(f"{name:<10} | {s['n']:>6} | {s['total_pnl']:>+10.2f} | {s['avg_pnl']:>+7.2f} | {s['win_rate']:>4.1f}% | {s.get('final_balance', 0):>10.2f} | {reasons_str}")
        rows.append((name, s))

    if not rows:
        print("\nNo data yet — bots haven't traded.")
        return 0

    print()
    print("Best by total PnL:")
    rows.sort(key=lambda r: r[1]["total_pnl"], reverse=True)
    for name, s in rows:
        marker = "→" if s == rows[0][1] else " "
        print(f"  {marker} {name:<10} {s['total_pnl']:+.2f} USDT after {s['n']} trades")
    return 0


if __name__ == "__main__":
    sys.exit(main())
