#!/usr/bin/env python3
"""Diagnostic script to check fluflip system state."""

import asyncio
import json
import sqlite3
from pathlib import Path
from datetime import datetime

async def main():
    print("=" * 80)
    print("FLUFLIP DIAGNOSTIC REPORT")
    print("=" * 80)
    print(f"Timestamp: {datetime.now()}\n")

    # 1. Check config
    print("1. CONFIG CHECK")
    print("-" * 80)
    config_path = Path("config.json")
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
        print(f"Mode: {cfg.get('mode', 'N/A')}")
        print(f"Autostart: {cfg.get('autostart', 'N/A')}")
        print(f"Universe include_only: {cfg.get('universe', {}).get('include_only', [])}")
        print(f"Universe force_include: {cfg.get('universe', {}).get('force_include_symbols', [])}")
        print(f"Require Binance ref: {cfg.get('universe', {}).get('require_binance_ref', True)}")
        print(f"Algorithm: {cfg.get('strategy', {}).get('algorithm', 'N/A')}")
        print(f"Max concurrent positions: {cfg.get('risk', {}).get('max_concurrent_positions', 'N/A')}")

        print("\nSymbol overrides:")
        for ov in cfg.get('symbol_overrides', []):
            print(f"  {ov['symbol']}: enabled={ov.get('enabled', True)}, algos={ov.get('algorithms', [])}")
    else:
        print("ERROR: config.json not found!")

    # 2. Check universe cache
    print("\n2. UNIVERSE CACHE CHECK")
    print("-" * 80)
    cache_path = Path(".universe_cache.json")
    if cache_path.exists():
        with open(cache_path) as f:
            cache = json.load(f)
        print(f"Cache timestamp: {datetime.fromtimestamp(cache.get('ts', 0))}")
        print(f"Total symbols in cache: {len(cache.get('entries', {}))}")
        print(f"Symbols with Binance ref: {sum(1 for e in cache.get('entries', {}).values() if e.get('has_ref'))}")

        # Check specific symbols
        entries = cache.get('entries', {})
        target_symbols = ['ENA_USDT', 'NVDA_USDT', 'MSTR_USDT', 'TAO_USDT', 'BCH_USDT']
        print("\nTarget symbols status:")
        for sym in target_symbols:
            if sym in entries:
                e = entries[sym]
                print(f"  {sym}: has_ref={e.get('has_ref')}, binance_symbol={e.get('binance_symbol')}")
            else:
                print(f"  {sym}: NOT IN CACHE")
    else:
        print("WARNING: .universe_cache.json not found!")

    # 3. Check database
    print("\n3. DATABASE CHECK")
    print("-" * 80)
    db_path = Path("data.sqlite")
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Recent candidates
        cursor.execute("""
            SELECT symbol, side, score, z, spread_bps, fair, mexc, depth, blocked, accepted, ts
            FROM candidates_log
            WHERE ts > ?
            ORDER BY ts DESC
            LIMIT 20
        """, (datetime.now().timestamp() - 300,))  # Last 5 minutes

        candidates = cursor.fetchall()
        print(f"Candidates logged in last 5 minutes: {len(candidates)}")
        if candidates:
            print("\nRecent candidates:")
            for row in candidates[:10]:
                sym, side, score, z, spread_bps, fair, mexc, depth, blocked, accepted, ts = row
                print(f"  {datetime.fromtimestamp(ts).strftime('%H:%M:%S')} {sym:12} {side or 'N/A':5} "
                      f"score={score:.2f if score else 0:.2f} z={z:.2f if z else 0:.2f} "
                      f"fair={fair:.6f if fair else 0:.6f} mexc={mexc:.6f if mexc else 0:.6f} "
                      f"blocked={blocked or 'N/A'} accepted={accepted}")

        # Recent trades
        cursor.execute("""
            SELECT symbol, side, entry, exit, pnl_usdt, pnl_pct, close_reason, open_ts, close_ts
            FROM trades
            ORDER BY close_ts DESC
            LIMIT 10
        """)
        trades = cursor.fetchall()
        print(f"\nRecent trades: {len(trades)}")
        for row in trades[:5]:
            sym, side, entry, exit, pnl, pnl_pct, reason, open_ts, close_ts = row
            if close_ts:
                exit_str = f"{exit:.6f}" if exit else "0.000000"
                pnl_str = f"{pnl:.2f}" if pnl else "0.00"
                pnl_pct_str = f"{pnl_pct:.2f}" if pnl_pct else "0.00"
                print(f"  {sym:12} {side:5} entry={entry:.6f} exit={exit_str} "
                      f"pnl=${pnl_str} ({pnl_pct_str}%) reason={reason}")

        # Stats
        cursor.execute("SELECT COUNT(*), SUM(pnl_usdt), AVG(pnl_usdt) FROM trades WHERE close_ts IS NOT NULL")
        total_trades, total_pnl, avg_pnl = cursor.fetchone()
        print(f"\nTotal closed trades: {total_trades or 0}")
        total_pnl_str = f"{total_pnl:.2f}" if total_pnl else "0.00"
        avg_pnl_str = f"{avg_pnl:.2f}" if avg_pnl else "0.00"
        print(f"Total PnL: ${total_pnl_str}")
        print(f"Average PnL per trade: ${avg_pnl_str}")

        conn.close()
    else:
        print("WARNING: data.sqlite not found!")

    # 4. Check WebSocket connectivity (if app is running)
    print("\n4. CONNECTIVITY CHECK")
    print("-" * 80)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get('http://127.0.0.1:8080/api/state', timeout=aiohttp.ClientTimeout(total=2)) as resp:
                if resp.status == 200:
                    state = await resp.json()
                    eng = state.get('engine', {})
                    print(f"Engine running: {eng.get('running', False)}")
                    print(f"Engine mode: {eng.get('mode', 'N/A')}")
                    print(f"Kill switch: {eng.get('kill', False)}")
                    print(f"Binance WS: {eng.get('binance_ok', False)}")
                    print(f"MEXC WS: {eng.get('mexc_ok', False)}")
                    print(f"MEXC Auth: {eng.get('mexc_auth_ok', 'N/A')}")
                    print(f"Universe size: {len(state.get('universe', []))}")
                    print(f"Universe symbols: {state.get('universe', [])}")
                    print(f"Open positions: {len(state.get('positions', []))}")
                    print(f"Candidates: {len(state.get('candidates', []))}")

                    if state.get('candidates'):
                        print("\nTop candidates from live state:")
                        for c in state['candidates'][:10]:
                            print(f"  {c.get('symbol', 'N/A'):12} {c.get('side', 'N/A'):5} "
                                  f"score={c.get('score', 0):.2f} z={c.get('z', 0):.2f} "
                                  f"fair={c.get('fair', 0):.6f} mexc={c.get('mexc', 0):.6f} "
                                  f"blocked={c.get('blocked', 'N/A')}")
                else:
                    print(f"API returned status {resp.status}")
    except Exception as e:
        print(f"Cannot connect to API (app may not be running): {e}")

    print("\n" + "=" * 80)
    print("DIAGNOSTIC COMPLETE")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(main())
