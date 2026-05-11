#!/usr/bin/env python3
"""Monitor fluflip system state."""

import requests
import time
from datetime import datetime

def check_state():
    try:
        r = requests.get('http://127.0.0.1:8080/api/state', timeout=2)
        state = r.json()

        print("=" * 80)
        print(f"FLUFLIP STATUS - {datetime.now().strftime('%H:%M:%S')}")
        print("=" * 80)

        eng = state.get('engine', {})
        print(f"Engine: {'RUNNING' if eng.get('running') else 'STOPPED'} | Mode: {eng.get('mode')} | Kill: {eng.get('kill')}")
        print(f"Binance WS: {eng.get('binance_ok')} | MEXC WS: {eng.get('mexc_ok')}")
        print(f"Universe: {len(state.get('universe', []))} symbols")

        candidates = state.get('candidates', [])
        print(f"\nCandidates: {len(candidates)}")
        print("-" * 80)

        for c in candidates:
            sym = c.get('symbol', 'N/A')
            side = c.get('side') or '---'
            score = c.get('score', 0) or 0
            z = c.get('z', 0) or 0
            fair = c.get('fair') or 0
            mexc = c.get('mexc') or 0
            depth = c.get('depth') or 0
            blocked = c.get('blocked', '')

            status = 'SIGNAL' if score > 0 and side != '---' else 'BLOCKED' if blocked else 'WAITING'

            print(f"{sym:15} {side:5} score={score:6.2f} z={z:6.2f} "
                  f"fair={fair:10.6f} mexc={mexc:10.6f} depth=${depth:8.0f} "
                  f"[{status}] {blocked}")

        positions = state.get('positions', [])
        if positions:
            print(f"\nOpen Positions: {len(positions)}")
            print("-" * 80)
            for p in positions:
                print(f"{p['symbol']:15} {p['side']:5} entry={p['entry']:.6f} "
                      f"pnl=${p.get('pnl', 0):.2f} ({p.get('pnl_pct', 0):.2f}%)")

        print("=" * 80)

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_state()
