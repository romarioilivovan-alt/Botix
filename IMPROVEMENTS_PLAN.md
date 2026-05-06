# FluFlip Improvements Plan

**Date:** 2026-05-06
**Goal:** Fix losses, improve UI, add NYSE price source

## Analysis Summary

### Current Performance (2724 trades, +149.18 USD vs Marik 1185 trades, +133.16 USD)

**Winners:**
- ENA: +68.53 (65.2% WR) - BEST performer
- NVDA: +46.28 (67.3% WR) - Strong
- TAO: +23.00 (72.4% WR) - Excellent WR
- MSTR: +23.48 (51.7% WR) - Profitable

**Losers:**
- ZEC: -7.67 (40.5% WR, 1028 trades!) - WORST, overtrading
- TSLA: -2.48 (35.7% WR) - Poor
- INTC: -1.96 (44.4% WR) - Marginal

### Key Problems

1. **ZEC overtrading**: 1028 trades (38% of total volume), losing money
2. **Stock price mystery**: Stocks trade successfully but code shows they should be blocked without Binance reference
3. **UI complexity**: Current dashboard hard to parse
4. **No trade reasoning**: Can't see why bot entered/exited

## Immediate Actions

### Action 1: Emergency ZEC fix (NOW)
**Problem:** ZEC is 38% of all trades but loses money
**Solution:** Disable ZEC or increase entry threshold dramatically
**Implementation:** Set enabled=false in symbol_overrides for ZEC

### Action 2: Investigate stock price source (30 min)
**Problem:** Code shows stocks should be blocked (no Binance ref) but they trade successfully
**Hypothesis:** MEXC provides index/fair price for stocks via their API
**Implementation:** Add debug logging in aggregator.compute_stats() to show fair price source

### Action 3: Dashboard redesign (2-3 hours)
**Problem:** Current UI cluttered, hard to understand
**Solution:** Clean layout with real-time trade log and entry reasons
**Implementation:** New frontend with simplified tabs

### Action 4: Add NYSE WebSocket (4-6 hours, if needed)
**Problem:** If MEXC doesn't provide stock prices, we need external source
**Solution:** Polygon.io or Yahoo Finance WebSocket for real-time NYSE prices
**Implementation:** New nyse_ws.py module similar to binance_ws.py

### Action 5: Per-symbol tuning (ongoing)
**Problem:** One-size-fits-all strategy doesn't work for all symbols
**Solution:** Different algo combinations and thresholds per symbol
**Implementation:** Use existing symbol_overrides system
