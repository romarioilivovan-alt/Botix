#!/usr/bin/env python3
"""Check recent candidates from database."""

import sqlite3
from datetime import datetime, timedelta

conn = sqlite3.connect('data.sqlite')
cursor = conn.cursor()

cutoff = (datetime.now() - timedelta(minutes=5)).timestamp()
cursor.execute('''
    SELECT ts, symbol, side, score, z, spread_bps, fair, mexc, depth, blocked, accepted
    FROM candidates_log
    WHERE ts > ?
    ORDER BY ts DESC
    LIMIT 30
''', (cutoff,))

rows = cursor.fetchall()
print(f'Recent candidates (last 5 minutes): {len(rows)} entries')
print('-' * 120)

if rows:
    for row in rows:
        ts, sym, side, score, z, spread_bps, fair, mexc, depth, blocked, accepted = row
        dt = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
        side_str = side if side else '---'
        score_str = f'{score:.2f}' if score else '0.00'
        z_str = f'{z:.2f}' if z else '0.00'
        blocked_str = blocked if blocked else 'none'
        print(f'{dt} {sym:15} {side_str:5} score={score_str:6} z={z_str:6} blocked={blocked_str}')
else:
    print('No candidates logged in last 5 minutes')

conn.close()
