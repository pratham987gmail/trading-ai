# Blind sandboxes, 1 Jan 2015 – Oct 2026: results (protocol in sandbox_prereg.md)

**Setup.** Each strategy ran in its own process with its own ₹1,00,000 book and output folder (`data/sandbox/runs/<NAME>/`). It saw only data up to each day's close.
- Equity orders paid real Upstox charges. Option trades paid the research spread plus F&O charges.
- Reproduce with: `python -m sandbox build --realdata …`, then `python -m sandbox run`, `python -m sandbox leakcheck`, `python -m sandbox report`.

## Leak test: PASS
Every price, VIX value and option quote after the cutoff was scrambled. All decisions, fills and equity marks up to the cutoff were identical in all six sandboxes, at both cutoffs tried (2019-06-28 and 2023-03-31). The scrambling did change results after the cutoff, which shows the test can see it.

## Results (₹1L each, 2015-01-01 → 2026-10-09)

| Sandbox | Final ₹ | CAGR | Max DD | Sharpe | Excess CAGR vs index fund | P(monthly excess ≤ 0) | Trades | Win | PF |
|---|---|---|---|---|---|---|---|---|---|
| BENCH_NIFTY_FUND (buy & hold) | 3,12,982 | 10.2% | −37.8% | 0.70 | — | — | — | — | — |
| TREND_LEARN | 2,35,260 | 7.5% | −37.8% | 0.58 | −2.6% | 0.96 | 9 | 89% | — |
| **MOM_LEARN** | **6,08,598** | **16.6%** | −37.7% | **0.96** | **+6.4%** | **0.081** | 350 | 59% | 2.70 |
| MOM_FIXED_HINDSIGHT (today's bot rule) | 2,27,729 | 7.2% | −29.6% | 0.59 | −2.9% | 0.76 | 668 | 50% | 1.81 |
| CONDOR_LEARN | 84,928 | −1.4% | −24.0% | −0.15 | −11.6% | 0.99 | 22 | 64% | 0.68 |
| CONDOR_FIXED_HINDSIGHT | 1,71,173 | 4.7% | −18.7% | 0.57 | −5.5% | 0.85 | 142 | 80% | 1.60 |

- Win and PF are per closed position (equity) or per condor (options).
- The condor books only risk about 10–25% of capital per trade and sit in cash otherwise, so compare their CAGR with that in mind.

### Per year
| Year | Bench | Trend | Mom learn | Mom fixed | Condor learn | Condor fixed |
|---|---|---|---|---|---|---|
| 2015 | −4.2% | −4.2% | −3.2% | −9.6% | 0 | −1.1% |
| 2016 | +4.2% | +4.2% | +8.2% | +5.1% | 0 | +3.5% |
| 2017 | +29.6% | +29.6% | +24.8% | +16.1% | 0 | 0 |
| 2018 | +4.3% | +4.3% | −10.7% | −16.3% | 0 | +1.6% |
| 2019 | +13.2% | +13.2% | +8.4% | −6.6% | 0 | +8.3% |
| 2020 | +16.1% | +13.1% | +48.7% | +14.2% | −19.7% | −6.9% |
| 2021 | +25.3% | +13.3% | +91.2% | +61.0% | −4.8% | +23.4% |
| 2022 | +5.6% | +1.1% | −19.0% | −9.6% | 0 | −1.9% |
| 2023 | +21.3% | +9.3% | +43.8% | +25.2% | 0 | 0 |
| 2024 | +10.1% | +12.6% | +20.5% | +15.3% | 0 | +2.8% |
| 2025 | +11.8% | +8.0% | +22.4% | +11.0% | 0 | +13.0% |
| 2026 (to Oct) | −10.6% | −10.4% | −1.5% | −0.8% | +11.1% | +16.2% |

## What each learner chose, blind
- **Momentum.**
  - From 2006–2014 data alone it picked **top 15, 9/12-month lookback, 200-SMA filter**.
  - It later moved between 6/12 and 3/6 lookbacks and filter on/off, but **always top 15**.
  - The gate (beat the index fund over the trailing 5 years) stayed open the whole time.
- **Trend.** It held the index until mid-2020, then switched to 50/100-day SMA filters, which cost about 2.6%/yr. A trend filter on the index did not help.
- **Condor.**
  - All 12 variants paper-traded from Jan 2015.
  - The promotion gate first opened in **March 2020, at the COVID crash**. The champion was the 20-delta, VIX ≥ 18 variant.
  - Its live trades then lost about 20% of the book. The gate opened again only briefly in 2021 and in Mar–May 2026.

## Paper (fractional, no ₹1L friction) record of every momentum variant, 2015–2026
- Best variant: 9/12-month, top 15, no filter, at 21.5%/yr.
- Today's bot rule (6/12, top 30, 200-SMA): **13.3%/yr on paper**, against the index fund at 10.6%.
- In the ₹1L integer-share book the bot rule made only **7.2%/yr**:
  - Fees came to ₹51,809 over 1,303 orders averaging ₹3,662, about 3.3% of equity per year.
  - Expensive shares that cost more than one equal-weight slot (₹3.3k) were skipped.
- Top 15 halves the order count and doubles the order size. Its fees were 1.1%/yr.

## Condor paper record of every variant, 2015–2026 (R per trade, after costs)
- Every variant is positive, but small: avg R 0.001–0.050.
- The best is 20-delta, VIX ≥ 18: 142 trades, 80% wins, PF 1.42, avg R 0.05.
- With no VIX filter it is roughly break-even (PF 1.01–1.08).

## Verdict (pre-registered: excess > 0 and P < 0.10)
- **MOM_LEARN: PASS.** It found a momentum edge without hindsight: +6.4%/yr over the index fund, P = 0.08.
  - Caveat: the menu was written knowing momentum was the research candidate. It is one 11-year path, and its drawdowns were as deep as the index's (−38%).
- **TREND_LEARN: FAIL.**
- **CONDOR_LEARN: FAIL.** A blind learner lost money because its evidence only turned positive in a volatility spike, just before the spike hurt it. Even the hindsight rule trailed the index fund.

## What this changes in the plan
1. **₹1L momentum should hold 15 names, not 30.** The blind learner chose 15 using pre-2015 data, so this is not a hindsight tweak. With 30 names on ₹1L, Upstox charges and skipped expensive shares eat most of the edge.
2. **Keep the condor in shadow mode.** No live condor until it passes on data recorded after today.
3. **Drop the trend filter question as an index strategy.** For momentum it is a coin-flip: the blind learner switched it on and off.
