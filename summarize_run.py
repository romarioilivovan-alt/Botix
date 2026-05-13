import argparse
import sqlite3
from pathlib import Path
from statistics import median


def read_paths(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []
    for item in args.db:
        paths.append(Path(item))
    for item in args.path_file:
        raw = Path(item).read_text(encoding="utf-8").strip()
        if raw:
            paths.append(Path(raw))
    return paths


def fmt(v: float) -> str:
    return f"{v:+.4f}"


def summarize_db(path: Path) -> str:
    con = sqlite3.connect(path)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT symbol, pnl_usdt, pnl_pct, close_reason, duration_sec "
        "FROM trades ORDER BY ts"
    ).fetchall()
    con.close()

    if not rows:
        return f"{path}\n  no trades"

    pnls = [float(r[1] or 0.0) for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    total = sum(pnls)
    med = median(pnls)

    by_symbol: dict[str, dict[str, float]] = {}
    by_reason: dict[str, dict[str, float]] = {}

    for symbol, pnl, _pnl_pct, reason, dur in rows:
        pnl = float(pnl or 0.0)
        dur = float(dur or 0.0)
        sym = by_symbol.setdefault(
            symbol,
            {
                "count": 0,
                "pnl": 0.0,
                "dur": 0.0,
                "wins": 0,
                "avg_win": 0.0,
                "win_count": 0,
                "avg_loss": 0.0,
                "loss_count": 0,
            },
        )
        sym["count"] += 1
        sym["pnl"] += pnl
        sym["dur"] += dur
        if pnl > 0:
            sym["wins"] += 1
            sym["avg_win"] += pnl
            sym["win_count"] += 1
        elif pnl < 0:
            sym["avg_loss"] += pnl
            sym["loss_count"] += 1

        rs = by_reason.setdefault(reason or "unknown", {"count": 0, "pnl": 0.0})
        rs["count"] += 1
        rs["pnl"] += pnl

    lines = [
        str(path),
        f"  trades={len(rows)} pnl={fmt(total)} winrate={wins / len(rows) * 100:.1f}% avg={fmt(total / len(rows))} median={fmt(med)}",
        "  by symbol:",
    ]
    for symbol, data in sorted(by_symbol.items(), key=lambda kv: kv[1]["pnl"], reverse=True):
        avg_dur = data["dur"] / data["count"]
        avg_win = data["avg_win"] / data["win_count"] if data["win_count"] else 0.0
        avg_loss = data["avg_loss"] / data["loss_count"] if data["loss_count"] else 0.0
        winrate = data["wins"] / data["count"] * 100.0
        lines.append(
            f"    {symbol:<16} cnt={int(data['count']):3d} pnl={fmt(data['pnl'])} "
            f"winrate={winrate:5.1f}% avg_dur={avg_dur:5.2f}s avg_win={fmt(avg_win)} avg_loss={fmt(avg_loss)}"
        )
    lines.append("  by reason:")
    for reason, data in sorted(by_reason.items(), key=lambda kv: kv[1]["pnl"]):
        lines.append(f"    {reason:<24} cnt={int(data['count']):3d} pnl={fmt(data['pnl'])}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize sqlite run results.")
    parser.add_argument("--db", action="append", default=[], help="Path to sqlite DB")
    parser.add_argument("--path-file", action="append", default=[], help="Text file containing a DB path")
    args = parser.parse_args()

    paths = read_paths(args)
    if not paths:
        parser.error("provide --db or --path-file")

    for idx, path in enumerate(paths):
        if idx:
            print()
        print(summarize_db(path))


if __name__ == "__main__":
    main()
