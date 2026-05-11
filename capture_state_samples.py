import argparse
import json
import math
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def bucketize(value: float | None, step: float) -> str:
    if value is None:
        return "none"
    try:
        v = float(value)
    except Exception:
        return "bad"
    if math.isnan(v) or math.isinf(v):
        return "bad"
    lo = math.floor(v / step) * step
    hi = lo + step
    if step >= 1:
        return f"[{lo:.0f},{hi:.0f})"
    return f"[{lo:.2f},{hi:.2f})"


def top_items(counter: Counter, limit: int = 8) -> list[tuple[str, int]]:
    return counter.most_common(limit)


def main() -> None:
    ap = argparse.ArgumentParser(description="Poll /api/state and summarize blocked reasons and signal stats.")
    ap.add_argument("--base-url", required=True, help="Base URL, e.g. http://127.0.0.1:8084")
    ap.add_argument("--seconds", type=float, default=180.0)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--out", required=True, help="Output JSON path")
    args = ap.parse_args()

    samples: list[dict] = []
    per_symbol: dict[str, dict[str, Counter]] = defaultdict(lambda: {
        "blocked": Counter(),
        "side": Counter(),
        "score_bucket": Counter(),
        "spread_bucket": Counter(),
        "z_bucket": Counter(),
        "ofi_bucket": Counter(),
        "fv_bucket": Counter(),
        "imbalance_bucket": Counter(),
    })
    global_counts = {
        "blocked": Counter(),
        "side": Counter(),
        "score_bucket": Counter(),
        "spread_bucket": Counter(),
        "z_bucket": Counter(),
        "ofi_bucket": Counter(),
        "fv_bucket": Counter(),
        "imbalance_bucket": Counter(),
    }

    deadline = time.time() + args.seconds
    while time.time() < deadline:
        snap = fetch_json(args.base_url.rstrip("/") + "/api/state")
        stats = snap.get("stats_summary", {})
        now = time.time()
        samples.append({
            "t": now,
            "balance": snap.get("balance"),
            "running": ((snap.get("engine") or {}).get("running")),
            "universe_size": snap.get("universe_size"),
            "stats_summary": stats,
            "candidates": snap.get("candidates", []),
        })
        for sym, st in stats.items():
            blocked = st.get("blocked") or "none"
            side = st.get("side") or "none"
            score_b = bucketize(st.get("score"), 0.5)
            spread_b = bucketize(st.get("spread_bps"), 0.5)
            z_b = bucketize(st.get("z"), 0.5)
            ofi_b = bucketize(st.get("ofi"), 1000.0)
            fv_b = bucketize(st.get("fv"), 0.5)
            imb_b = bucketize(st.get("imbalance"), 0.1)
            items = {
                "blocked": blocked,
                "side": side,
                "score_bucket": score_b,
                "spread_bucket": spread_b,
                "z_bucket": z_b,
                "ofi_bucket": ofi_b,
                "fv_bucket": fv_b,
                "imbalance_bucket": imb_b,
            }
            for key, value in items.items():
                per_symbol[sym][key][value] += 1
                global_counts[key][value] += 1
        time.sleep(args.interval)

    out = {
        "meta": {
            "base_url": args.base_url,
            "seconds": args.seconds,
            "interval": args.interval,
            "samples": len(samples),
            "started_at": samples[0]["t"] if samples else None,
            "ended_at": samples[-1]["t"] if samples else None,
        },
        "global": {k: top_items(v, 20) for k, v in global_counts.items()},
        "per_symbol": {
            sym: {k: top_items(v, 20) for k, v in counters.items()}
            for sym, counters in sorted(per_symbol.items())
        },
        "raw_samples": samples,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out_path)


if __name__ == "__main__":
    main()
