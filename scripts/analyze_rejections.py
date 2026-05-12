#!/usr/bin/env python3
"""Analyze signal rejection reasons from the last 24 hours.

Shows top-10 rejection reasons and their frequency to help tune strategy filters.
"""

import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Tuple


def get_db_path() -> Path:
    """Locate data.sqlite in project root."""
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    return project_root / "data.sqlite"


def analyze_rejections(hours: int = 24) -> List[Tuple[str, int, float]]:
    """Query signal_decisions table for rejection reasons in the last N hours.

    Returns:
        List of (reason, count, percentage) tuples sorted by count descending.
    """
    db_path = get_db_path()
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return []

    cutoff_ts = time.time() - (hours * 3600)

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Total rejected signals
    cursor.execute(
        "SELECT COUNT(*) FROM signal_decisions WHERE ts >= ? AND decision = 'rejected'",
        (cutoff_ts,)
    )
    total_rejected = cursor.fetchone()[0]

    if total_rejected == 0:
        print(f"No rejected signals in the last {hours} hours.", file=sys.stderr)
        conn.close()
        return []

    # Top reasons
    cursor.execute(
        """SELECT reason, COUNT(*) as cnt
           FROM signal_decisions
           WHERE ts >= ? AND decision = 'rejected' AND reason IS NOT NULL
           GROUP BY reason
           ORDER BY cnt DESC
           LIMIT 10""",
        (cutoff_ts,)
    )

    results = []
    for reason, count in cursor.fetchall():
        pct = (count / total_rejected) * 100.0
        results.append((reason, count, pct))

    conn.close()
    return results


def main():
    hours = 24
    if len(sys.argv) > 1:
        try:
            hours = int(sys.argv[1])
        except ValueError:
            print(f"Invalid hours argument: {sys.argv[1]}", file=sys.stderr)
            sys.exit(1)

    print(f"Signal Rejection Analysis (last {hours} hours)")
    print("=" * 60)

    results = analyze_rejections(hours)

    if not results:
        print("No data available.")
        return

    print(f"{'Reason':<40} {'Count':>8} {'%':>8}")
    print("-" * 60)

    for reason, count, pct in results:
        print(f"{reason:<40} {count:>8} {pct:>7.1f}%")

    print("=" * 60)
    total_shown = sum(c for _, c, _ in results)
    print(f"Total rejections shown: {total_shown}")


if __name__ == "__main__":
    main()
