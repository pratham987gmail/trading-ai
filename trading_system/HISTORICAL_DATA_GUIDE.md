# Historical Option Data Import & Backtesting Guide

## 🎯 Goal

Skip the 30-day wait by importing historical option chain data and backtesting immediately.

---

## 📊 Data Source Options

### **Option 1: NSE Free Data (EOD Only) — Recommended First Step**

**Pros:**
- ✅ Completely free
- ✅ Official NSE data (reliable)
- ✅ Quick validation (test in 1-2 hours)

**Cons:**
- ❌ End-of-day only (no intraday)
- ❌ Manual download (no bulk API)
- ❌ Only 1 year easily accessible

**Use for:**
- Testing if strategy has ANY edge (daily signals)
- Quick validation before paying for data

**How to use:**

```powershell
cd "C:\Users\prath\OneDrive\Desktop\trading ai\trading-ai\trading_system"

# Download last 3 months (EOD)
& "C:\tmp\trading-system-verify-venv\Scripts\python.exe" scripts\import_nse_eod.py `
  --symbols NIFTY BANKNIFTY `
  --start-date 2024-10-01 `
  --end-date 2024-12-31
```

**Expected time:**
- Download: 2-3 hours (rate limited)
- Import: ~500 chains (250 days × 2 symbols)
- Database size: ~50-80 MB

---

### **Option 2: TrueData (Paid, Intraday) — Best Quality**

**Pricing:** ₹500/month (historical API only) or ₹1,500/month (live + historical)

**Pros:**
- ✅ 1-minute intraday data
- ✅ 5+ years history available
- ✅ API access (automated)
- ✅ Includes bid-ask, OI, volume, Greeks

**Cons:**
- ❌ Costs money (₹500/month)
- ❌ Requires symbol mapping setup
- ❌ Rate limits (10 req/sec)

**Use for:**
- Serious backtesting (after NSE validation shows promise)
- Intraday spread strategies

**Setup:**

1. **Subscribe:** https://truedata.in/pricing
2. **Get API key** from dashboard
3. **Add to .env:**
   ```bash
   echo "TRUEDATA_API_KEY=your_key_here" >> .env
   ```
4. **Download data:**
   ```powershell
   & "C:\tmp\trading-system-verify-venv\Scripts\python.exe" scripts\import_truedata.py `
     --symbols NIFTY BANKNIFTY FINNIFTY `
     --start-date 2024-01-01 `
     --end-date 2024-12-31 `
     --interval 1
   ```

**Expected time:**
- Download: 1-2 days (rate limits)
- Import: ~100,000+ intraday chains (365 days × 6 hours/day × 60 min/hour × 3 symbols)
- Database size: 5-10 GB

---

### **Option 3: Upstox Historical API (Free for Clients)**

**Requirements:**
- ✅ Free if you have Upstox trading account
- ✅ 1-year historical data
- ❌ Rate limited (need throttling)

**Setup:**

1. **Create Upstox account** (if not already)
2. **Get API credentials** from https://upstox.com/developer/
3. **Add to .env:**
   ```bash
   UPSTOX_API_KEY=your_key
   UPSTOX_API_SECRET=your_secret
   ```
4. **Download:**
   ```powershell
   & "C:\tmp\trading-system-verify-venv\Scripts\python.exe" scripts\import_upstox.py `
     --symbols NIFTY BANKNIFTY `
     --months 6
   ```

---

### **Option 4: Community Datasets (Free but Risky)**

**Sources:**
- Kaggle: https://www.kaggle.com/datasets (search "NSE option chain")
- GitHub: Various repos with scraped data

**Pros:**
- ✅ Free
- ✅ Sometimes large datasets (years of data)

**Cons:**
- ❌ Quality unknown (gaps, errors)
- ❌ May be outdated
- ❌ No guarantees on accuracy

**Verdict:** Only use for learning/testing, not real strategy validation.

---

## 🔬 Running Backtest

### **Step 1: Import Historical Data**

**Start with NSE free data (recommended):**

```powershell
cd "C:\Users\prath\OneDrive\Desktop\trading ai\trading-ai\trading_system"

# Import last 3 months
& "C:\tmp\trading-system-verify-venv\Scripts\python.exe" scripts\import_nse_eod.py `
  --symbols NIFTY BANKNIFTY `
  --start-date 2024-10-01 `
  --end-date 2024-12-31
```

**Progress output:**
```
📥 Importing NSE Historical Data
============================================================
Symbols: NIFTY, BANKNIFTY
Date range: 2024-10-01 to 2024-12-31
Total days: 92

[2024-10-01] Processing... ✅ 6 chains imported
[2024-10-02] Processing... ✅ 6 chains imported
[2024-10-03] Processing... ✅ 6 chains imported
...
[2024-12-31] Processing... ✅ 6 chains imported

============================================================
✅ Import complete!
Total chains imported: 552
Failed days: 3
```

---

### **Step 2: Run Backtest**

```powershell
& "C:\tmp\trading-system-verify-venv\Scripts\python.exe" scripts\backtest_signals.py `
  --symbol NIFTY `
  --start-date 2024-10-01 `
  --end-date 2024-12-31 `
  --hold-days 3
```

**Progress output:**
```
🔬 Backtesting NIFTY
============================================================
Date range: 2024-10-01 to 2024-12-31
Hold period: 3 days

[2024-10-01] Generating signal... ✅ P&L: ₹450 (+14.3%)
[2024-10-02] Generating signal... ❌ P&L: -₹800 (-25.6%)
[2024-10-03] Generating signal... ✅ P&L: ₹600 (+19.2%)
...
[2024-12-31] Generating signal... ✅ P&L: ₹350 (+11.1%)

============================================================
📊 BACKTEST RESULTS
============================================================

Total Trades:      58
Winning Trades:    32 (55.2%)
Losing Trades:     26

Average Win:       ₹612
Average Loss:      ₹748
Win/Loss Ratio:    0.82

Total P&L:         ₹3,240
Total Return:      +3.24%
Max Drawdown:      -8.5%
Sharpe Ratio:      0.45

Profit Factor:     1.12

────────────────────────────────────────────────────────────
💡 Interpretation:

⚠️  Weak performance (0-5% annual)
✅ Good win rate (≥55%)
⚠️  Weak risk-adjusted returns (Sharpe <1.0)

============================================================
```

---

## 📈 Interpreting Results

### **What Makes a Good Strategy?**

| Metric | Excellent | Good | Acceptable | Poor |
|--------|-----------|------|------------|------|
| **Annual Return** | >30% | 15-30% | 5-15% | <5% |
| **Win Rate** | >60% | 55-60% | 50-55% | <50% |
| **Sharpe Ratio** | >2.0 | 1.5-2.0 | 1.0-1.5 | <1.0 |
| **Max Drawdown** | <5% | 5-10% | 10-15% | >15% |
| **Profit Factor** | >2.0 | 1.5-2.0 | 1.2-1.5 | <1.2 |

---

### **Example Interpretations**

**Scenario 1: Strong Strategy**
```
Total Return:  +18.5%
Win Rate:      58%
Sharpe:        1.65
Max DD:        -6.8%
```
**Verdict:** ✅ Worth paper trading → pilot → scale up

---

**Scenario 2: Marginal Strategy**
```
Total Return:  +6.2%
Win Rate:      52%
Sharpe:        0.85
Max DD:        -12.5%
```
**Verdict:** ⚠️ Tweak parameters (hold days, filters) and re-test

---

**Scenario 3: Failed Strategy**
```
Total Return:  -4.8%
Win Rate:      45%
Sharpe:        -0.25
Max DD:        -18.2%
```
**Verdict:** ❌ Abandon or complete redesign

---

## 🎯 Decision Tree

```
Import NSE Data (3 months)
    ↓
Run Backtest
    ↓
    ├─ Return >10% AND Sharpe >1.0?
    │   ↓ YES
    │   Subscribe to TrueData (₹500/month)
    │       ↓
    │   Import 6-12 months intraday data
    │       ↓
    │   Run detailed backtest
    │       ↓
    │       ├─ Still profitable?
    │       │   ↓ YES
    │       │   Paper trade 60 days
    │       │       ↓
    │       │       ├─ Matches backtest?
    │       │       │   ↓ YES
    │       │       │   Deploy ₹10K pilot
    │       │       │       ↓
    │       │       │       Scale to ₹1L
    │       │       │
    │       │       └─ NO → Investigate divergence
    │       │
    │       └─ NO → Tweak strategy or abandon
    │
    └─ NO
        ↓
        Keep recording live data
        (Maybe strategy works in different market regime)
```

---

## 💾 Database Sizes

| Data Source | Duration | Interval | Approx Size |
|-------------|----------|----------|-------------|
| NSE EOD | 3 months | Daily | 50 MB |
| NSE EOD | 1 year | Daily | 200 MB |
| TrueData | 1 month | 1-minute | 800 MB |
| TrueData | 6 months | 1-minute | 5 GB |
| TrueData | 1 year | 1-minute | 10 GB |

---

## ⏱️ Timeline Comparison

### **Traditional Approach (30+ Days)**
```
Day 1:     Start recording live data
Day 2-29:  Wait...
Day 30:    Backtest (finally!)
Day 31+:   Paper trade if backtest passes
```

### **Historical Data Approach (2-3 Days)**
```
Day 1:     Import NSE data (3 hours)
           Run backtest (30 min)
           Know if strategy works!

Day 2:     If promising → Subscribe TrueData
           Import detailed data (overnight)

Day 3:     Run comprehensive backtest
           Decide: paper trade or abandon
```

**Time saved:** 27 days 🎉

---

## 🚀 Quick Start (Recommended)

**Tonight (30 minutes):**
```powershell
cd "C:\Users\prath\OneDrive\Desktop\trading ai\trading-ai\trading_system"

# Import NSE data (last 3 months)
& "C:\tmp\trading-system-verify-venv\Scripts\python.exe" scripts\import_nse_eod.py `
  --symbols NIFTY BANKNIFTY `
  --start-date 2024-10-01 `
  --end-date 2024-12-31
```

**Tomorrow (30 minutes):**
```powershell
# Run backtest
& "C:\tmp\trading-system-verify-venv\Scripts\python.exe" scripts\backtest_signals.py `
  --symbol NIFTY `
  --start-date 2024-10-01 `
  --end-date 2024-12-31 `
  --hold-days 3
```

**Within 24 hours, you'll know:**
- ✅ Does the strategy have any edge?
- ✅ What's the approximate win rate?
- ✅ Is it worth pursuing further?

---

## 📞 If You Get Stuck

**NSE download failing:**
- NSE blocks aggressive scraping
- Solution: Add delays between requests (already in script)
- Alternative: Download CSVs manually from NSE website

**TrueData subscription:**
- Contact: support@truedata.in
- Ask for: "Historical option chain data API access"
- Mention: "Need 1-minute bars for NIFTY/BANKNIFTY options"

**Backtest errors:**
- Check database has data: `python -c "from recorder.storage import ChainStorage; print(ChainStorage().get_chain_count())"`
- Should show: ">0 chains" (if zero, import failed)

---

## 🎯 Bottom Line

**Question:** "Can we gather old data and test?"  
**Answer:** **Yes! Do this first before waiting 30 days.**

**Cost:** ₹0 (NSE free data) to ₹500/month (TrueData)  
**Time:** 2-3 hours (NSE) to 2-3 days (TrueData)  
**Result:** Know if strategy works in 1-3 days instead of 30+ days

**Start tonight with NSE free data →** You'll know by tomorrow if this is worth pursuing.
