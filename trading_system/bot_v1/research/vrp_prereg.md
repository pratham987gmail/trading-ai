# Does the option-selling edge exist? A direct test of the volatility premium (pre-registered 15 Oct 2026, before any result)

**Idea.** A short condor earns money only if options are priced for a bigger move than the one that actually happens. Instead of simulating condors (one noisy trade per eve), measure that gap directly on every eve, every index and every strike band.

## Measures (per observation = one underlying × expiry × entry time)
- **Straddle test (ATM):** sell the call and put at the strike K nearest spot.
  - Seller P&L = C + P − |S_T − K|, in basis points of spot.
  - Ratio = |S_T − K| ÷ (C + P). Below 1 means options were priced for a bigger move than the one that happened.
- **Strangle test (the condor's short legs):** strikes picked exactly as the bot does (Black-Scholes delta with VIX).
  - Deltas 0.05, 0.10, 0.15, 0.20, 0.25, 0.30.
  - Seller P&L = premium − intrinsic at expiry, in bps of spot.
- **Costs:** each measure is reported gross and net of the protocol half-spread (0.25 + 0.3% of premium per leg).
- **Settlement:** S_T is the index close on expiry day.

## Data
- **A.** NIFTY option EOD closes, Jan 2015 → Sep 2026 (NSE bhavcopy; only contracts that traded).
  - Entries at the close, 1–5 sessions before expiry.
- **B.** Upstox 5-minute candles for NIFTY, SENSEX, BANKNIFTY, FINNIFTY, MIDCPNIFTY and BANKEX, Oct 2024 → Oct 2026.
  - Entries on the eve at 10:00, 12:00 and 15:05, and on expiry day at 10:00.
- **C.** Azure recorder live bid/ask mids, 21 Sep → 13 Oct 2026, any expiry that settled in the window, entries at 15:05.

## Tests and pass marks
1. **Premium exists.** The primary measure is the mean net strangle P&L at 15-delta on the eve. Pass if > 0 with a day-clustered bootstrap P(mean ≤ 0) < 0.05.
   - On A, 2015–26.
   - On B, all six indices pooled, clustered by date.
   - Straddle ratio reported alongside.
2. **Eve-specific? (placebo on timing)**
   - Same measure at 2–5 sessions before expiry (A) and at 10:00 / 12:00 on the eve and 10:00 on expiry day (B).
   - If those earn as much as 15:05 on the eve, the eve timing itself is not the edge.
3. **Smooth across strikes.** P&L should change gradually from 0.05 to 0.30 delta. One isolated spike counts as a warning.
4. **Stable or regime-driven.** Report the yearly premium for NIFTY 2015–26.
   - Correlation with eve option activity (total contracts traded on the eve), used as a proxy for retail flow.
   - Before vs after SEBI's 20 Nov 2024 changes (fewer weekly expiries, larger lots).
   - **Hypothesis H:** the premium rose with retail volume after 2022 and shrank after Nov 2024.
5. **Live check (C).** Does the live straddle ratio sit in the same range as the history?

The results are reported once. Nothing in this protocol changes after the results are seen.
