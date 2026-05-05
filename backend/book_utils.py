from __future__ import annotations

from typing import List, Tuple, Dict


def _bucket_price(price: float, step: float, side: str) -> float:
    # For bids floor, for asks ceil to keep spread reasonable
    if step <= 0:
        return price
    if side == "bid":
        return float(int(price / step) * step)
    else:
        # ceil
        return float(int((price + step - 1e-12) / step) * step)


def aggregate_book(bids: List[List[float]], asks: List[List[float]], step: float, levels: int) -> Tuple[List[List[float]], List[List[float]]]:
    """Aggregate raw levels into price buckets. Returns top `levels` for each side."""
    b: Dict[float, float] = {}
    a: Dict[float, float] = {}

    for p, q in bids:
        if q <= 0:
            continue
        bp = _bucket_price(p, step, "bid")
        b[bp] = b.get(bp, 0.0) + q

    for p, q in asks:
        if q <= 0:
            continue
        ap = _bucket_price(p, step, "ask")
        a[ap] = a.get(ap, 0.0) + q

    bids_out = [[p, b[p]] for p in sorted(b.keys(), reverse=True)][:levels]
    asks_out = [[p, a[p]] for p in sorted(a.keys())][:levels]
    return bids_out, asks_out
