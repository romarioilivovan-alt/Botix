import sqlite3, sys, os
db = sys.argv[1]
if not os.path.exists(db):
    print("(no db)"); sys.exit(0)
con = sqlite3.connect(db)
row = con.execute(
    "SELECT COUNT(*), COALESCE(SUM(pnl_usdt),0), "
    "SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END), "
    "SUM(CASE WHEN pnl_usdt<=0 THEN 1 ELSE 0 END), "
    "MAX(pnl_usdt), MIN(pnl_usdt), AVG(duration_sec) "
    "FROM trades WHERE mode='paper'"
).fetchone()
n, total, wins, losses, best, worst, avg_dur = row
wr = (wins / n * 100) if n else 0
print(f"  trades={n}  total_pnl={total:+.4f}  win_rate={wr:.1f}%  ({wins}W/{losses}L)")
print(f"  avg_dur={(avg_dur or 0):.1f}s  best={best or 0:+.3f}  worst={worst or 0:+.3f}")
print("  by close_reason:")
for cr, c, avg, sm, d in con.execute(
    "SELECT close_reason, COUNT(*), ROUND(AVG(pnl_usdt),4), ROUND(SUM(pnl_usdt),4), ROUND(AVG(duration_sec),1) "
    "FROM trades WHERE mode='paper' GROUP BY close_reason ORDER BY COUNT(*) DESC"
).fetchall():
    label = cr or "?"
    print(f"    {label:<14} n={c:>4}  avg={avg:>9}  sum={sm:>10}  dur={d:>5}s")
con.close()
