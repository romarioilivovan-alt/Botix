# 0fee Scanner

Multi-symbol mean-reversion bot for MEXC 0-fee perpetuals. Scans every 0-fee
contract, builds a fair-value reference from Binance Futures, and fades
deviations with maker limit entries — picking up the half-spread on every fill
on a venue where there are no fees.

> **Status: experimental.** Run in `paper` mode first. Real-money mode trades
> with full balance and **does not hedge**, so losses are real and can compound.
> Read the strategy and risk sections before going live.

---

## Architecture

```
universe ──┐
           │ (refresh every N min)
           ▼
       binance multi-WS  + mexc multi-WS
                       │
                       ▼
           aggregator (per-symbol stats: σ, OFI, depth, fair velocity)
                       │
                       ▼
              opportunity engine  →  capital allocator
                                              │
                                              ▼
                                executor (paper | real)
                                              │
                                              ▼
                                  SQLite (trades, equity, candidates)
```

Modules:
- `universe.py` — list MEXC 0-fee + cross-check Binance availability
- `binance_ws.py` / `mexc_ws.py` — multi-symbol WS clients (depth + trades)
- `aggregator.py` — rolling `σ_spread`, OFI, fair velocity, book depth
- `opportunity.py` — score = `|z| × liquidity × regime`, emits entry signals
- `allocator.py` — slot management, sizing, leverage, book-depth cap
- `paper.py` — paper executor (position state machine + SL ladder)
- `real.py` — real executor (talks to MEXC private endpoints)
- `engine.py` — orchestrator
- `app.py` — FastAPI + WS push + dashboard
- `persistence.py` — SQLite store

---

## Strategy

For each MEXC 0-fee symbol with a Binance reference:

1. Compute `F = Binance mid` (extensible to Bybit/OKX later).
2. Compute `spread = MEXC_mid − F`, rolling `σ_spread` over 30 s.
3. **Entry:** when `|z = spread / σ| > entry_z` (default 1.8) and:
   - fair velocity is small (regime is quiet),
   - OFI on Binance trades does not push *with* the deviation,
   - MEXC top-10 book depth ≥ threshold,
   place a **maker limit** at MEXC best bid/ask in the fade direction.
4. **Cancel** the quote if `|z|` drops below `cancel_z` or after `quote_timeout_sec`.
5. On fill: place exchange-side `TP = F` and a hard SL at `entry × (1 ± hard_sl_pct)`.
6. **SL ladder** (re-evaluated every 200 ms):
   - travel < 25 %: SL = hard SL
   - 25 – 50 %: SL halfway back
   - 50 – 100 %: breakeven
   - 100 % (price = F): lock 30 % of the move
   - F + σ overshoot: SL = F (lock min profit)
   - F + 2 σ overshoot: trail at 0.5 σ
7. Time exit at `max_hold_sec` (default 30 s) if price never reached F.
8. Per-symbol cooldown after each close.

Risk caps:
- `max_concurrent_positions` (K) — slot limit
- `margin_pct_per_slot` — fraction of free balance allocated per slot
- `book_depth_consume_pct` — never take > N % of MEXC top-10 depth
- `daily_loss_pct_kill` and `max_drawdown_pct_kill` — automatic kill switch

---

## Setup

```powershell
cd metascalp_web_terminal
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy config.example.json config.json
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8080
```

Open http://127.0.0.1:8080 .

---

## Running

The dashboard has three modes:

- **Logger** — observe candidates only, no trading. Use this first.
- **Paper** — simulated fills against the live order book. PnL goes to SQLite.
- **Real** — places real orders on MEXC. Requires `mexc_web.web_uid` in config.

Workflow:

1. Start in **Logger** for a few hours; watch the *Top candidates* and *Logs* panels.
   Make sure signals look reasonable (not constant, not zero).
2. Switch to **Paper** with a 1000-USDT virtual balance. Run for 12 – 48 h.
   Look at SQLite (`data.sqlite` → `trades`) for win rate, average duration,
   PnL distribution.
3. Only if paper is profitable, switch to **Real** with a small balance and a
   single slot (`max_concurrent_positions = 1`). Verify a few trades by hand.
4. Scale slots and balance gradually.

The **KILL ALL** button cancels all open MEXC orders and closes every position
at market. It also flips the kill switch so the engine stops opening new
positions until you toggle it off (set `kill_switch=false` in state via restart).

---

## MEXC credentials

- **Web UID**: copy the `Authorization` header value from any authenticated
  request on `mexc.com/futures/...` in DevTools → Network. Paste into the
  *MEXC Web UID* config field.
- **OpenAPI key/secret** (optional): only needed for the private positions WS.
  Positions are also polled, so the bot works without them.

---

## Latency

This strategy is latency-sensitive. For real trading:
- Run on a VPS in the same datacenter as MEXC. Resolve `contract.mexc.com`
  with `nslookup` and check the IP's region (`whois` / ipinfo). MEXC commonly
  hosts on AWS in Asia-Pacific.
- Aim for < 10 ms ping to `contract.mexc.com`.

---

## Disclaimer

For research and testing only. Trading with leverage on perpetuals carries the
risk of total loss. The author is not responsible for any losses incurred from
running this software.
