# FLUFLIP ANALYSIS AND FIXES
**Date:** 2026-05-06  
**Status:** ✅ ROOT CAUSES IDENTIFIED

---

## 🔴 CRITICAL ISSUES FOUND

### Issue #1: Wrong Symbol Names in Config
**Problem:** Config uses `NVDA_USDT` and `MSTR_USDT` but MEXC uses different names:
- ❌ `NVDA_USDT` → ✅ `NVIDIA_USDT` 
- ❌ `MSTR_USDT` → ✅ `MSTRSTOCK_USDT`

**Impact:** These 2 symbols are NOT being loaded into the universe, so only 3 out of 5 configured symbols are active.

**Evidence:**
```
ENA_USDT        in_cache=True  has_binance_ref=True  ✅
NVDA_USDT       in_cache=False has_binance_ref=False ❌ WRONG NAME
MSTR_USDT       in_cache=False has_binance_ref=False ❌ WRONG NAME
TAO_USDT        in_cache=True  has_binance_ref=True  ✅
BCH_USDT        in_cache=True  has_binance_ref=True  ✅
```

---

### Issue #2: Stock Symbols Have No Binance Reference
**Problem:** Stock symbols (NVIDIA_USDT, MSTRSTOCK_USDT) don't exist on Binance Futures because Binance doesn't trade stocks.

**Current State:**
- Config has `require_binance_ref: false` ✅ (correct)
- But the strategy algorithms (meanrev, raw_momentum, ofi) ALL require a fair value from Binance
- Without Binance reference, these algorithms cannot compute spread, z-score, or OFI

**Impact:** Even if we fix the symbol names, stock symbols won't generate signals because:
1. `st.fair` will be `None` (no Binance mid price)
2. All algorithms check `if st.fair is None: st.blocked_reason = "no_books"; return`
3. Stocks will show as "blocked: no_books" in Top Candidates

**Stock Symbols in MEXC 0-fee:**
- All 100+ stock symbols have `has_binance_ref=False`
- This includes: NVIDIA_USDT, MSTRSTOCK_USDT, TESLA_USDT, INTCSTOCK_USDT, etc.

---

### Issue #3: Universe Not Loading Symbols
**Problem:** Even though 3 symbols (ENA, TAO, BCH) are valid, the universe size is 0.

**Evidence from diagnostic:**
```
Universe size: 0
Universe symbols: []
Candidates: 0
```

**Root Cause:** The universe manager's `refresh()` method is likely failing silently or the `force_include_symbols` logic isn't working properly when symbols don't exist in the MEXC 0-fee list.

---

## 📊 CURRENT SYSTEM STATE

### Configuration
- Mode: `logger` (no trading, just logging)
- Autostart: `false`
- Algorithm: `raw_momentum`
- Max positions: 3
- Require Binance ref: `false` ✅

### Connectivity
- ✅ Binance WS: Connected
- ✅ MEXC WS: Connected
- ⚠️ MEXC Auth: Not configured (web_uid is empty)
- ❌ Universe: 0 symbols loaded
- ❌ Candidates: 0 in last 5 minutes

### Trading History
- Total trades: 42
- Total PnL: **-$343.56** (losing)
- Average PnL: **-$8.18** per trade
- Recent trades: BCH_USDT, PENGU_USDT (all stopped out)

---

## 🔧 FIXES REQUIRED

### Fix #1: Correct Symbol Names in Config ⚡ CRITICAL
**File:** `config.json`

**Change:**
```json
"universe": {
  "include_only": ["ENA_USDT", "NVIDIA_USDT", "MSTRSTOCK_USDT", "TAO_USDT", "BCH_USDT"],
  "force_include_symbols": ["ENA_USDT", "NVIDIA_USDT", "MSTRSTOCK_USDT", "TAO_USDT", "BCH_USDT"],
  "require_binance_ref": false
}
```

**Also update symbol_overrides:**
```json
{"symbol":"NVIDIA_USDT","enabled":true,"sl_pct":0.0010,"algorithms":["raw_momentum","ofi"],"algo_mode":"ANY"},
{"symbol":"MSTRSTOCK_USDT","enabled":true,"sl_pct":0.0010,"algorithms":["raw_momentum"],"algo_mode":"ANY"},
```

---

### Fix #2: Add Stock-Specific Algorithm ⚡ CRITICAL
**Problem:** Stock symbols need a different strategy since they have no Binance reference.

**Options:**

#### Option A: Use MEXC Self-Reverting Strategy (Recommended)
Use `bb_revert` algorithm which uses MEXC's own price history (Bollinger Bands on MEXC mid):
- Doesn't require Binance reference
- Trades mean-reversion on MEXC's own price
- Already implemented in `opportunity.py`

**Change in config.json:**
```json
{"symbol":"NVIDIA_USDT","enabled":true,"sl_pct":0.0010,"algorithms":["bb_revert"],"algo_mode":"ANY"},
{"symbol":"MSTRSTOCK_USDT","enabled":true,"sl_pct":0.0010,"algorithms":["bb_revert"],"algo_mode":"ANY"},
```

#### Option B: Implement External Stock Price Feed
- Fetch real-time stock prices from Yahoo Finance, Alpha Vantage, or Polygon.io
- Use as fair value reference instead of Binance
- Requires new module: `stock_feed.py`
- More complex but more accurate

#### Option C: Remove Stock Symbols
- Focus only on crypto symbols that have Binance reference
- Simplest solution but limits trading universe

**Recommendation:** Start with Option A (bb_revert) for stocks, keep crypto with existing algorithms.

---

### Fix #3: Debug Universe Loading ⚡ HIGH PRIORITY
**Problem:** Universe manager isn't loading the 3 valid symbols (ENA, TAO, BCH).

**Investigation needed:**
1. Check if `force_include_symbols` logic is working
2. Add logging to `universe.py` refresh method
3. Check if MEXC API is returning 0-fee list correctly
4. Verify `trader.list_zero_fee_symbols()` is working

**Quick test:**
```python
# Add to engine.py startup
logger.info(f"Universe entries: {len(self.universe._entries)}")
logger.info(f"Working set: {self.universe.working_set}")
```

---

### Fix #4: Add MEXC Authentication ⚠️ MEDIUM PRIORITY
**Problem:** `web_uid` is empty, so real trading won't work.

**To fix:**
1. Open MEXC futures in browser
2. Open DevTools → Network tab
3. Find any authenticated request
4. Copy `Authorization` header value
5. Paste into config: `"mexc_web": {"web_uid": "YOUR_AUTH_TOKEN"}`

---

### Fix #5: Review Strategy Performance 📊 ANALYSIS
**Problem:** System is losing money (-$343 over 42 trades).

**Observations:**
- All recent trades hit stop-loss
- No profitable exits visible
- May need to:
  - Adjust SL percentages (currently 0.25% for crypto, 0.10% for stocks)
  - Review algorithm parameters
  - Check if signals are too aggressive
  - Analyze win rate by symbol

**Recommendation:** Fix Issues #1-#3 first, then collect fresh data before tuning strategy.

---

## 🚀 IMPLEMENTATION PLAN

### Phase 1: Immediate Fixes (5 minutes)
1. ✅ Update config.json with correct symbol names
2. ✅ Change stock symbols to use `bb_revert` algorithm
3. ✅ Restart application
4. ✅ Verify universe loads 5 symbols

### Phase 2: Verification (10 minutes)
1. Check dashboard shows 5 symbols in universe
2. Monitor Top Candidates for signals
3. Verify crypto symbols show fair price from Binance
4. Verify stock symbols show MEXC-only data

### Phase 3: Monitoring (1-2 hours)
1. Run in logger mode
2. Collect candidate data
3. Verify signals are being generated
4. Check if stock bb_revert strategy produces signals

### Phase 4: Optimization (after data collection)
1. Analyze which symbols generate best signals
2. Tune algorithm parameters
3. Adjust stop-loss levels
4. Consider adding more symbols

---

## 📝 NOTES

### Why Stocks Don't Work with Current Strategy
The current algorithms all depend on cross-exchange arbitrage:
- **meanrev**: Fades deviation between MEXC and Binance
- **raw_momentum**: Follows Binance momentum
- **ofi**: Uses Binance order flow imbalance

None of these work for stocks because Binance doesn't have stock futures.

### Alternative Stock Strategies
1. **bb_revert**: Bollinger Band mean reversion on MEXC price alone ✅ Already implemented
2. **book_lean**: MEXC order book imbalance (doesn't need Binance)
3. **wide_spread**: Trades when MEXC spread is abnormally wide
4. **External reference**: Use real stock market data as fair value

### Why Universe is Empty
The `force_include_symbols` should force symbols into the universe even if they're not in the 0-fee list, but it seems to not be working. This needs debugging in `universe.py`.

---

## ✅ SUCCESS CRITERIA

After fixes:
- [ ] Universe shows 5 symbols
- [ ] Top Candidates shows signals for crypto symbols (ENA, TAO, BCH)
- [ ] Top Candidates shows signals for stock symbols (NVIDIA, MSTRSTOCK) using bb_revert
- [ ] No "no_books" blocked reason for any symbol
- [ ] MEXC depth data visible for all symbols
- [ ] Binance fair price visible for crypto symbols only

---

**Next Steps:** Apply Fix #1 and Fix #2, then restart and verify.
