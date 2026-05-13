import sqlite3
import json
import sys
from datetime import datetime, timezone

DB_PATH = "C:/fluflip_work/code/data.sqlite"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Get tables
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
print(f"TABLES: {tables}")

# Find trades table
trades_table = None
for t in tables:
    if 'trade' in t.lower():
        trades_table = t
        break

if not trades_table:
    # Try to find any table with relevant data
    for t in tables:
        cur.execute(f"SELECT count(*) FROM {t}")
        cnt = cur.fetchone()[0]
        print(f"  {t}: {cnt} rows")
        cur.execute(f"PRAGMA table_info({t})")
        cols = [r[1] for r in cur.fetchall()]
        print(f"    cols: {cols[:15]}")
    sys.exit(0)

# Get schema
cur.execute(f"PRAGMA table_info({trades_table})")
cols = [r[1] for r in cur.fetchall()]
print(f"\nTRADES TABLE: {trades_table}")
print(f"COLUMNS: {cols}")

# Get total count
cur.execute(f"SELECT count(*) FROM {trades_table}")
total = cur.fetchone()[0]
print(f"TOTAL TRADES: {total}")

# Get all trades
cur.execute(f"SELECT * FROM {trades_table} ORDER BY rowid DESC")
rows = cur.fetchall()

# Print summary stats
if total > 0:
    # Try to find PnL column
    pnl_col = None
    for c in cols:
        if 'pnl' in c.lower() or 'profit' in c.lower():
            pnl_col = c
            break
    
    symbol_col = None
    for c in cols:
        if 'symbol' in c.lower():
            symbol_col = c
            break
    
    time_col = None
    for c in cols:
        if 'time' in c.lower() or 'ts' in c.lower() or 'open' in c.lower():
            time_col = c
            break

    print(f"\nPnL col: {pnl_col}, Symbol col: {symbol_col}, Time col: {time_col}")
    
    if pnl_col:
        # Overall stats
        cur.execute(f"SELECT sum({pnl_col}), avg({pnl_col}), count(*) FROM {trades_table}")
        r = cur.fetchone()
        print(f"\nOVERALL: total_pnl=${r[0]:.4f}, avg_pnl=${r[1]:.4f}, trades={r[2]}")
        
        # Wins vs losses
        cur.execute(f"SELECT count(*) FROM {trades_table} WHERE {pnl_col} > 0")
        wins = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FROM {trades_table} WHERE {pnl_col} <= 0")
        losses = cur.fetchone()[0]
        print(f"WINS: {wins} ({100*wins/total:.1f}%), LOSSES: {losses} ({100*losses/total:.1f}%)")
        
        # Per symbol
        if symbol_col:
            cur.execute(f"""SELECT {symbol_col}, count(*), sum({pnl_col}), avg({pnl_col}),
                           sum(case when {pnl_col}>0 then 1 else 0 end) as wins
                           FROM {trades_table} GROUP BY {symbol_col} ORDER BY sum({pnl_col}) DESC""")
            print(f"\nPER SYMBOL:")
            for r in cur.fetchall():
                wr = 100*r[4]/r[1] if r[1]>0 else 0
                print(f"  {r[0]}: trades={r[1]}, pnl=${r[2]:.4f}, avg=${r[3]:.4f}, WR={wr:.0f}%")
    
    # Print last 50 trades as JSON for detailed analysis
    print(f"\n\n=== LAST 50 TRADES (JSON) ===")
    last_trades = []
    for row in rows[:50]:
        d = {}
        for i, c in enumerate(cols):
            val = row[i]
            if isinstance(val, bytes):
                val = val.decode('utf-8', errors='replace')
            d[c] = val
        last_trades.append(d)
    print(json.dumps(last_trades, default=str, indent=1))

conn.close()
