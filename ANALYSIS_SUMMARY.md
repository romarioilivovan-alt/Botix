# FLUFLIP - COMPLETE ANALYSIS & FIXES
**Date:** 2026-05-06  
**Status:** ✅ ISSUES RESOLVED, SYSTEM OPERATIONAL

---

## 🎯 EXECUTIVE SUMMARY

### PROBLEMS FOUND:
1. ❌ Wrong symbol names in config (NVDA_USDT, MSTR_USDT don't exist on MEXC)
2. ❌ Stock symbols couldn't trade (no Binance reference prices)
3. ❌ Universe was empty (0 symbols loaded)
4. ❌ No candidates showing in Top Candidates panel

### FIXES APPLIED:
1. ✅ Corrected symbol names: NVIDIA_USDT, MSTRSTOCK_USDT
2. ✅ Added bb_revert algorithm for stocks (works without Binance)
3. ✅ Fixed opportunity.py to make bb_revert truly Binance-independent
4. ✅ Universe now loads all 5 symbols
5. ✅ System generating signals for crypto symbols

### CURRENT STATUS:
- ✅ **ENA_USDT**: Generating LONG/SHORT signals ✓
- ✅ **TAO_USDT**: Generating LONG/SHORT signals ✓
- ✅ **BCH_USDT**: Generating LONG/SHORT signals ✓
- ⚠️ **NVIDIA_USDT**: No MEXC order book data (depth=$0)
- ⚠️ **MSTRSTOCK_USDT**: No MEXC order book data (depth=$0)

---

## 📊 VERIFICATION

### Universe API Response:
```json
{
  "size": 5,
  "symbols": ["ENA_USDT", "NVIDIA_USDT", "MSTRSTOCK_USDT", "TAO_USDT", "BCH_USDT"],
  "refs": {
    "ENA_USDT": "ENAUSDT",
    "TAO_USDT": "TAOUSDT",
    "BCH_USDT": "BCHUSDT"
  }
}
```

### Recent Signals (Last 2 Minutes):
```
09:37:26 TAO_USDT   LONG  score=1.93 z=-1.76
09:37:24 BCH_USDT   LONG  score=2.05 z=-2.40
09:37:20 ENA_USDT   SHORT score=2.58 z=0.29
09:37:20 TAO_USDT   SHORT score=1.64 z=2.82
09:37:19 ENA_USDT   LONG  score=1.66 z=-2.37
09:37:11 ENA_USDT   LONG  score=2.26 z=-2.08
09:37:07 TAO_USDT   SHORT score=1.57 z=1.87
09:37:06 ENA_USDT   LONG  score=4.13 z=-0.28
```

### System Health:
```
Engine: RUNNING ✅
Mode: logger
Binance WS: Connected ✅
MEXC WS: Connected ✅
Universe: 5 symbols ✅
Signals: Active for 3/5 symbols ✅
```

---

## 🔧 CHANGES MADE

### 1. config.json
**Fixed symbol names:**
```json
"universe": {
  "include_only": ["ENA_USDT","NVIDIA_USDT","MSTRSTOCK_USDT","TAO_USDT","BCH_USDT"],
  "force_include_symbols": ["ENA_USDT","NVIDIA_USDT","MSTRSTOCK_USDT","TAO_USDT","BCH_USDT"]
}
```

**Updated symbol overrides:**
```json
"symbol_overrides": [
  {"symbol":"NVIDIA_USDT","enabled":true,"algorithms":["bb_revert"],"comment":"Stock - MEXC-only strategy"},
  {"symbol":"MSTRSTOCK_USDT","enabled":true,"algorithms":["bb_revert"],"comment":"Stock - MEXC-only strategy"}
]
```

### 2. backend/opportunity.py
**Fixed bb_revert algorithm:**
- Made `fair_velocity_30s_bps` check optional
- Now works without Binance data (for stocks)

---

## ⚠️ REMAINING ISSUES

### Stock Symbols Not Receiving Order Book Data

**Observation:**
```
NVIDIA_USDT:     mexc_price=196.695  depth=$0  ❌
MSTRSTOCK_USDT:  mexc_price=181.175  depth=$0  ❌
```

**Possible Causes:**
1. MEXC doesn't publish order book for stocks via WebSocket
2. Stocks trade during specific hours (market hours)
3. Low liquidity - MEXC doesn't update order book

**Investigation Needed:**
- Check MEXC website manually: https://futures.mexc.com/exchange/NVIDIA_USDT
- Verify trading hours for stock symbols
- Check if order book exists during market hours

**Temporary Solutions:**
1. Lower `min_book_depth_usdt` threshold in config
2. Use different stock symbols with better liquidity
3. Disable stocks and focus on crypto only

---

## 📈 TRADING PERFORMANCE

### Historical Stats:
- Total trades: 42
- Total PnL: **-$343.56** (losing)
- Average PnL: **-$8.18** per trade
- Recent trades: All stopped out

### Analysis:
- All recent trades hit stop-loss
- SL might be too tight (0.25% crypto, 0.10% stocks)
- Need fresh data after fixes before optimization

---

## 🚀 NEXT STEPS

### Immediate (Completed):
- [x] Fix symbol names
- [x] Add bb_revert for stocks
- [x] Fix opportunity.py
- [x] Restart system
- [x] Verify universe loads
- [x] Verify signals generate

### Short-term (1-2 hours):
- [ ] Investigate why stocks have no order book
- [ ] Check NVIDIA_USDT and MSTRSTOCK_USDT trading hours
- [ ] Collect signal statistics in logger mode
- [ ] Analyze win rate per symbol

### Medium-term (1-2 days):
- [ ] Tune algorithm parameters
- [ ] Optimize stop-loss levels
- [ ] Add more symbols if needed
- [ ] Test in paper mode with new settings

### Long-term:
- [ ] Add MEXC authentication (web_uid)
- [ ] Switch to real mode if paper profitable
- [ ] Deploy to VPS
- [ ] Add monitoring and alerts

---

## 📝 MONITORING COMMANDS

### Check system status:
```bash
cd C:\Users\romar\OneDrive\Desktop\fluflip
python monitor.py
```

### Check recent candidates:
```bash
python check_candidates.py
```

### Check universe:
```bash
curl http://127.0.0.1:8080/api/universe
```

### Start/stop engine:
```bash
curl -X POST http://127.0.0.1:8080/api/run/start
curl -X POST http://127.0.0.1:8080/api/run/stop
```

### Full diagnostic:
```bash
python diagnose.py
```

---

## ✅ CONCLUSIONS

### What's Working:
1. ✅ Universe loads all 5 symbols
2. ✅ Binance and MEXC WebSockets connected
3. ✅ Crypto symbols (ENA, TAO, BCH) generating signals
4. ✅ System logging candidates to database
5. ✅ Algorithms meanrev and raw_momentum operational

### What Needs Attention:
1. ⚠️ Stock symbols not receiving order book data (depth=$0)
2. ⚠️ bb_revert algorithm can't work without order book
3. ⚠️ Historical trading is unprofitable (-$343)
4. ⚠️ Parameters need tuning after fixes

### Recommendations:
1. **Now**: Investigate why stocks have no order book
2. **Today**: Run in logger mode for 2-4 hours, collect stats
3. **Tomorrow**: Tune parameters based on new data
4. **Optional**: Replace stocks with more liquid ones or disable them

---

**System restored and operational for cryptocurrencies. Stocks require additional investigation.**
