# AI-Assisted Historical Option Data Collection

## 🎯 Goal
Use AI (Claude, ChatGPT, Perplexity, etc.) to find and download historical Indian option chain data for backtesting.

---

## 📝 Prompt 1: Find Free Data Sources

```
I need historical option chain data for Indian index options (NIFTY, BANKNIFTY, FINNIFTY) for backtesting a trading strategy.

Please search the web and provide:

1. FREE sources of historical NSE option chain data with:
   - Strike prices
   - Option premiums (LTP, bid, ask)
   - Open Interest (OI)
   - Volume
   - Implied Volatility (IV)
   - Greeks (delta, theta, gamma, vega) if available

2. Date range available for each source

3. Data format (CSV, JSON, API, etc.)

4. Download method (direct download, API, web scraping)

5. Any known limitations or quality issues

Please check:
- NSE India official website (nseindia.com)
- Kaggle datasets
- GitHub repositories
- Data providers (TrueData, Upstox, Zerodha, etc.)
- Academic datasets
- Trading forums (Traderji, ValuePickr, etc.)

Prioritize:
1. Free sources first
2. Data from 2024 (most recent)
3. Intraday data if available (minute-level preferred)
4. At least 3-6 months of history

Format your response as:
**Source Name**
- URL: [link]
- Cost: Free/Paid
- Date Range: YYYY-MM-DD to YYYY-MM-DD
- Granularity: Daily/Hourly/1-minute
- Format: CSV/JSON/API
- Fields: [list available data fields]
- Download Method: [step-by-step]
- Quality: [known issues or reliability rating]
```

---

## 📝 Prompt 2: Find Specific Dataset Links

```
I need direct download links or API endpoints for historical NSE option chain data.

Requirements:
- Index: NIFTY 50 and BANKNIFTY
- Time period: October 2024 to December 2024 (at minimum)
- Data points needed per option:
  * Strike price
  * Option type (CE/PE)
  * Expiry date
  * Last traded price (LTP)
  * Open Interest
  * Volume
  * Bid/Ask prices (if available)
  * Implied Volatility (if available)

Please search for:
1. NSE official historical data downloads
2. Kaggle datasets with direct CSV links
3. GitHub repos with pre-collected data
4. Google Drive/Dropbox shared datasets
5. Research paper datasets
6. Open data portals

For each source, provide:
- Direct download URL (if available)
- File size estimate
- Number of records
- Last updated date
- Data quality assessment

If you find API endpoints, provide:
- Base URL
- Authentication method (if any)
- Rate limits
- Sample API call
- Response format
```

---

## 📝 Prompt 3: Create NSE Scraping Script

```
I need a Python script to download historical option chain data from NSE India website.

Requirements:
1. Bypass NSE bot detection (they require cookies/headers)
2. Download option chain for NIFTY and BANKNIFTY
3. Date range: User-specified (e.g., Oct 1 - Dec 31, 2024)
4. Save to CSV or JSON format
5. Handle rate limiting (max 1 request per 2 seconds)
6. Resume capability if interrupted
7. Progress bar showing download status

NSE Option Chain URL:
https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY

Please provide:
1. Complete working Python script
2. Required libraries (requests, beautifulsoup4, etc.)
3. Error handling for:
   - Network failures
   - Rate limiting (429 errors)
   - Bot detection (403 errors)
4. Output format specification

The script should:
- First get cookies from nseindia.com homepage
- Then make authenticated requests to option-chain API
- Parse JSON response
- Extract: strike, CE/PE LTP, OI, volume, IV, Greeks
- Save incrementally (don't lose progress if crash)
- Log errors to file

Include comments explaining each step.
```

---

## 📝 Prompt 4: Find Paid Data Provider Comparison

```
I'm willing to pay ₹500-2000/month for high-quality historical Indian option chain data.

Please research and compare these providers:

1. **TrueData** (truedata.in)
2. **Upstox Historical API**
3. **Zerodha Kite Historical Data**
4. **IIFL Markets**
5. **Samco Data API**
6. **Algoji**
7. **Shoonya by Finvasia**
8. Any other NSE data providers

For each, find:
- Pricing tiers (monthly/annual)
- Historical data availability (how many years back?)
- Granularity (tick/1min/5min/daily)
- Data fields included (LTP, OI, volume, Greeks, bid-ask)
- API vs bulk download
- Rate limits
- Free trial availability
- User reviews/reliability
- Contact information

Rank them by:
1. Best value for money
2. Data quality/completeness
3. Ease of integration
4. Customer support

I specifically need:
- At least 6 months of historical data
- 1-minute or better granularity
- Option chains (not just underlying)
- Indian market data (NSE/BSE)
```

---

## 📝 Prompt 5: Kaggle Dataset Search

```
Search Kaggle for historical NSE option chain datasets.

Use these search terms:
- "NSE option chain"
- "NIFTY options historical"
- "Indian stock options"
- "BSE NSE derivatives"
- "BANKNIFTY options data"

For each dataset found, provide:
1. Dataset name and author
2. Direct Kaggle URL
3. Dataset size (MB/GB)
4. Number of records
5. Date range covered
6. Columns/fields available
7. Last updated date
8. Number of downloads (popularity)
9. User rating/comments
10. Download command (kaggle CLI)

Example format:
**NSE Option Chain Historical Data (2020-2024)**
- URL: https://www.kaggle.com/datasets/[...]
- Size: 2.5 GB
- Records: 12M rows
- Date Range: Jan 2020 - Dec 2024
- Fields: date, symbol, strike, CE_LTP, PE_LTP, CE_OI, PE_OI, volume
- Last Updated: 2024-12-01
- Downloads: 5,432
- Rating: 4.5/5 stars
- Download: `kaggle datasets download -d [dataset-name]`

Find at least 3-5 datasets if available.
```

---

## 📝 Prompt 6: GitHub Repository Search

```
Search GitHub for repositories containing NSE option chain historical data or scrapers.

Search terms:
- "NSE option chain scraper"
- "NIFTY options historical data"
- "Indian stock market data"
- "NSE derivatives data"
- "option chain download"

For each repo found, provide:
1. Repository name and URL
2. Description
3. Stars/forks count
4. Last commit date
5. Language (Python/JavaScript/etc.)
6. Data format (CSV/JSON/SQL)
7. Date range available
8. Whether it includes pre-collected data or just scraper code
9. Setup instructions
10. Known issues from Issues tab

Look for:
- Repos with actual data files (not just code)
- Recently updated (2024 activity preferred)
- Good documentation
- Active maintainers

Example format:
**nse-option-chain-analyzer**
- URL: https://github.com/[user]/[repo]
- Description: Historical NIFTY option chain data from 2020-2024
- Stars: 234 | Forks: 89
- Last Updated: 2024-11-15
- Language: Python
- Data: CSV files (500 MB compressed)
- Range: Jan 2020 - Nov 2024
- Pre-collected: YES ✅
- Setup: `git clone [url] && cd data/`
```

---

## 📝 Prompt 7: Academic/Research Data Sources

```
Search for academic or research datasets containing Indian option chain data.

Check:
1. **Google Dataset Search** (datasetsearch.research.google.com)
2. **IEEE DataPort**
3. **Zenodo** (research data repository)
4. **figshare**
5. **Dryad Digital Repository**
6. **Harvard Dataverse**
7. **University repositories** (IIMs, IITs)
8. **RBI/SEBI research publications**

Search terms:
- "Indian options market data"
- "NSE derivatives dataset"
- "Emerging market options"
- "Index options India"

For each dataset:
1. Title and DOI (if available)
2. Publisher/Institution
3. Publication date
4. Data description
5. Access method (open/restricted)
6. File format
7. Size and record count
8. Citation requirement

Academic datasets are often:
- High quality (peer-reviewed)
- Well-documented
- Freely available
- Covering longer time periods
```

---

## 📝 Prompt 8: Community Forum Data Requests

```
Help me draft a post for Indian trading forums to request historical option data.

Forums to post on:
1. Traderji.com
2. ValuePickr.com
3. r/IndiaInvestments (Reddit)
4. r/AlgoTrading (Reddit)
5. QuantInsti Community
6. Zerodha Trading Q&A

Draft post:

Subject: [Request] Historical NSE Option Chain Data for Backtesting

Body:
[Create a polite, detailed request explaining:
- What data I need (NIFTY/BANKNIFTY option chains)
- Date range (6-12 months minimum)
- Fields required (strike, LTP, OI, volume, IV)
- Purpose (backtesting spread strategies)
- What I can offer in return (share backtest results, contribute to community)
- Any data source suggestions appreciated
]

Make it:
- Professional and respectful
- Show I've done my homework
- Offer value in return
- Thank contributors in advance
```

---

## 📝 Prompt 9: Web Scraping Legal Check

```
Before I scrape NSE website for historical data, help me understand the legal implications.

Research and summarize:

1. **NSE Terms of Service**
   - What does NSE say about web scraping?
   - Are there explicit prohibitions?
   - Is historical data considered public information?

2. **Indian IT Act 2000**
   - Relevant sections about data scraping
   - Legal precedents in India

3. **Best Practices**
   - Rate limiting (requests per second)
   - robots.txt compliance
   - Attribution requirements
   - Personal use vs commercial use

4. **Alternatives to Scraping**
   - Official NSE data subscription
   - Authorized data vendors
   - Open data initiatives

5. **Risk Assessment**
   - What could go wrong legally?
   - What are common practices in algo trading community?
   - How do other Indian quant traders get historical data?

Provide:
- Clear summary (legal/gray area/illegal)
- Recommendations for staying compliant
- Official data access methods if scraping is risky
```

---

## 📝 Prompt 10: Download Automation Script

```
Create a complete automated script to download and organize historical NSE option data.

Requirements:

1. **Data Source Selection**
   - Try multiple sources in order (NSE → Kaggle → GitHub → TrueData)
   - Fallback to next source if one fails
   - Log which source provided what data

2. **Download Management**
   - Parallel downloads (3-5 concurrent)
   - Resume capability
   - Checksum verification
   - Retry failed downloads (3 attempts)

3. **Data Organization**
   ```
   data/
   ├── raw/
   │   ├── nse_eod/          # NSE official EOD
   │   ├── kaggle/           # Kaggle datasets
   │   └── github/           # GitHub repos
   ├── processed/
   │   ├── 2024-10/          # By month
   │   └── 2024-11/
   └── metadata.json         # Source tracking
   ```

4. **Data Validation**
   - Check for missing dates
   - Verify data completeness (all strikes present?)
   - Flag outliers (prices too high/low)
   - Generate quality report

5. **Progress Tracking**
   - Show download progress (MB/s, ETA)
   - Display validation status
   - Save state (resume from last position)

6. **Error Handling**
   - Network timeouts
   - Corrupted downloads
   - Insufficient disk space
   - Rate limiting

Provide:
- Complete Python script (150-200 lines)
- Required libraries list
- Configuration file format
- Usage examples
- Expected output
```

---

## 🎯 How to Use These Prompts

### **Step 1: Choose Your AI Tool**
- **Claude** (best for coding)
- **ChatGPT** (good for research)
- **Perplexity** (best for web search)
- **Gemini** (good for multi-step tasks)

### **Step 2: Start with Search Prompts**
Use Prompts 1, 2, 5, 6, 7 first to find existing datasets.

### **Step 3: Evaluate Options**
- Free sources: Try first
- Paid sources: Compare with Prompt 4
- Legal: Check with Prompt 9

### **Step 4: Automate Collection**
Use Prompts 3 or 10 to create download scripts.

### **Step 5: Community Backup**
If automated methods fail, use Prompt 8 to request from trading communities.

---

## 🔄 Workflow Example

```
You → [Prompt 1 to Perplexity]
     ↓
Perplexity finds 3 Kaggle datasets + 2 GitHub repos
     ↓
You → [Prompt 5 to Claude] "Download these specific Kaggle datasets"
     ↓
Claude generates download script
     ↓
You run script → Download 2.5 GB of data
     ↓
You → [Prompt to me] "Import this data into trading system"
     ↓
I create import script
     ↓
You run backtest within 24 hours! ✅
```

---

## 💡 Pro Tips

### **Combine Prompts**
```
"Using Prompt 1 and Prompt 5, find me the top 3 free datasets 
for NSE option chain data, then generate a Python script to 
download and merge them."
```

### **Iterate**
```
First pass: Get overview with Prompt 1
Second pass: "Now focus on the Kaggle option you found"
Third pass: "Generate download code for that specific dataset"
```

### **Verify AI Output**
AI might hallucinate URLs or datasets. Always:
1. Click links to verify they exist
2. Test API endpoints
3. Check dataset dates match what AI claims

---

## 🎯 Expected Outcomes

### **Best Case** (80% chance)
- AI finds 2-3 free Kaggle/GitHub datasets
- You download 6-12 months of data
- Run backtest within 24 hours
- Cost: ₹0

### **Good Case** (15% chance)
- AI finds paid source (TrueData)
- You subscribe for ₹500/month
- Download 1-2 years of data
- Run comprehensive backtest
- Cost: ₹500

### **Worst Case** (5% chance)
- No free data exists
- Paid sources too expensive
- You use AI to build scraper
- Manually collect data over 7-14 days
- Cost: Time

---

## 🚀 Quick Start Command

**Copy this to ChatGPT/Claude/Perplexity RIGHT NOW:**

```
I need historical Indian option chain data (NIFTY, BANKNIFTY) from October-December 2024 for backtesting a trading strategy.

Please search the web and find:
1. Free CSV/JSON datasets (Kaggle, GitHub, academic sources)
2. Direct download links
3. Dataset size, date range, and data quality

For each source found, provide:
- URL
- Download method
- Fields available (strike, LTP, OI, volume, IV)
- Any known issues

Prioritize free sources with recent data (2024).
```

**You'll have answers in 2-3 minutes.** 🎉

---

## 📞 Next Steps After AI Finds Data

1. **AI finds dataset** → Copy download link
2. **Send to me** → "I found this dataset at [URL], help me import it"
3. **I create import script** → Custom for that data format
4. **You run script** → Data imported to your database
5. **Run backtest** → Know if strategy works within 24 hours!

---

## ⚠️ Warning

**Don't trust AI blindly:**
- ❌ AI might suggest URLs that don't exist
- ❌ AI might misstate pricing (verify on actual websites)
- ❌ AI might suggest illegal scraping methods

**Always verify:**
- ✅ Click links to confirm they work
- ✅ Check data provider pricing on their official site
- ✅ Test API endpoints before building integration
- ✅ Read terms of service for data usage restrictions

---

## 🎯 Success Probability

| Method | Success Rate | Time to Data | Cost |
|--------|--------------|--------------|------|
| **AI finds Kaggle dataset** | 70% | 1 hour | ₹0 |
| **AI finds GitHub repo** | 50% | 2 hours | ₹0 |
| **AI builds NSE scraper** | 60% | 1 day | ₹0 |
| **AI finds paid API** | 95% | 1 hour | ₹500/mo |
| **Traditional recording** | 100% | 30 days | ₹0 |

**Best strategy:** Try AI search first (Prompts 1, 5, 6), if fails in 2-3 hours → subscribe to TrueData.

---

## 🏁 Your Action Plan

**Right now (5 minutes):**
1. Copy Prompt 1 above
2. Paste into Perplexity or ChatGPT
3. Wait for results

**Within 1 hour:**
- AI returns 3-5 data sources
- You evaluate them
- Choose best free option

**Within 24 hours:**
- Download data
- Import to database
- Run backtest
- **Know if strategy works!**

**No more 30-day wait.** Start now! 🚀
