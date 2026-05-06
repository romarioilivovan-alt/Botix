# FluFlip Implementation Roadmap
**Start:** 2026-05-06 01:37 UTC
**Goal:** Fix losses, improve UI, add proper stock price source

## Phase 1: Emergency Fixes (30 min)

### Task 1.1: Disable ZEC ✓ CRITICAL
- Update config.json: set ZEC enabled=false
- Reason: -7.67 USD loss, 40.5% WR, 1028 overtrading
- Expected impact: Stop bleeding ~$8/day

### Task 1.2: Add debug logging for stock prices
- Modify aggregator.py compute_stats()
- Log where fair price comes from (Binance vs MEXC index vs None)
- Goal: Understand how stocks actually work

### Task 1.3: Disable TSLA and INTC temporarily
- Both losing money (TSLA -2.48, INTC -1.96)
- Keep only profitable stocks (NVDA, MSTR)
- Re-enable after we understand price source

## Phase 2: Stock Price Source (2-3 hours)

### Task 2.1: Investigate MEXC index price API
- Check if MEXC provides indexPrice/fairPrice for stocks
- Test with NVDA_USDT contract detail endpoint
- Document findings

### Task 2.2: Implement proper stock price handling
**Option A:** If MEXC provides index price
- Use MEXC index price as fair for stocks
- Modify aggregator to fetch and use it

**Option B:** If MEXC doesn't provide index price
- Implement NYSE WebSocket (Polygon.io or Yahoo Finance)
- Create nyse_ws.py module
- Wire into aggregator

## Phase 3: Dashboard Redesign (3-4 hours)

### Task 3.1: Simplify main dashboard
New layout:
```
┌─ Status Bar ─────────────────────────────────────┐
│ Balance: $1000 | PnL: +149.18 | Positions: 2/3  │
└──────────────────────────────────────────────────┘

┌─ Symbols (sortable by PnL) ──────────────────────┐
│ ✓ ENA    +68.53  65%WR  547t  [momentum+meanrev]│
│ ✓ NVDA   +46.28  67%WR  437t  [momentum]        │
│ ✓ TAO    +23.00  72%WR  117t  [CONSENSUS]       │
│ ✓ MSTR   +23.48  52%WR  450t  [momentum]        │
│ ✗ ZEC     -7.67  41%WR 1028t  [DISABLED]        │
│ ✗ TSLA    -2.48  36%WR  109t  [DISABLED]        │
│ ✗ INTC    -1.96  44%WR   36t  [DISABLED]        │
└──────────────────────────────────────────────────┘

┌─ Open Positions ─────────────────────────────────┐
│ ENA Long @0.1103  +0.82  SL:0.1100  Age:2s      │
│   Entry: raw_momentum score=2.1, spread=-3.2bps │
│ NVDA Short @197.2  +1.20  SL:197.4  Age:5s      │
│   Entry: raw_momentum score=2.8, ofi=+5000      │
└──────────────────────────────────────────────────┘

┌─ Recent Trades (last 20, auto-scroll) ───────────┐
│ 01:35:42 ENA Long +0.82 [momentum 2.1] 2s       │
│ 01:35:38 ZEC Short -0.16 [meanrev 1.2] 3s       │
│ 01:35:35 NVDA Long +1.20 [momentum 2.8] 4s      │
└──────────────────────────────────────────────────┘
```

### Task 3.2: Add trade reasoning
- Store entry_reason in Position model
- Include: algo name, score, key metrics (spread, ofi, z_score)
- Display in UI and export to CSV

### Task 3.3: Add per-symbol performance charts
- New endpoint: GET /api/symbol-performance/{symbol}
- Return: equity curve, trade count, WR over time
- Render mini sparkline charts in Symbols tab

## Phase 4: Strategy Optimization (ongoing)

### Task 4.1: Per-symbol parameter tuning
Based on current data:
- ENA: Keep current (momentum+meanrev, ANY mode) ✓
- NVDA: Keep current (momentum, ANY mode) ✓
- TAO: Keep current (CONSENSUS mode) ✓
- MSTR: Test CONSENSUS mode (currently 52% WR)
- ZEC: Disabled until we find why it loses
- TSLA: Disabled until we find why it loses
- INTC: Disabled (too few trades to optimize)

### Task 4.2: Add cooldown mechanism
- Prevent rapid-fire entries on same symbol
- Min cooldown: 5-10 seconds between trades
- Especially important for ZEC if re-enabled

### Task 4.3: Add paper testing framework
- Run multiple strategy configs in parallel
- Compare results side-by-side
- A/B test before deploying to real mode

## Success Metrics

### Phase 1 Success:
- ZEC stops trading (0 new trades)
- Stock price source identified and documented
- Only profitable symbols active

### Phase 2 Success:
- Stocks have reliable fair price source
- NVDA/MSTR continue trading profitably
- No "no_books" blocks for stocks

### Phase 3 Success:
- Dashboard loads in <1s
- Trade log updates in real-time
- Entry reasons visible for all trades
- User can understand bot behavior at a glance

### Phase 4 Success:
- Overall WR improves from 49.8% to 55%+
- Profit factor improves from 1.406 to 1.6+
- No single symbol loses more than $5/day

## Timeline

- Phase 1: 30 minutes (URGENT)
- Phase 2: 2-3 hours
- Phase 3: 3-4 hours
- Phase 4: Ongoing optimization

**Total initial work: ~6-8 hours**
**Expected improvement: +20-30% profitability**
