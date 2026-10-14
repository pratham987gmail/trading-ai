# Blind sandbox test: pre-registration (written 15 Oct 2026, before any sandbox was run)

**Question:** If a strategy had started on 1 Jan 2015 with ₹1,00,000 and could only use data up to each day, would it have found its edge, without being told which settings work?

## Rules that apply to every sandbox
- Each sandbox runs as its own process, with its own ₹1L book and its own output folder (`data/sandbox/runs/<NAME>/`). Sandboxes share nothing.
- **Point-in-time data.** On each session the strategy gets a `View` that is cut off at that session's close.
  - Equity orders fill at the next session's close.
  - Options fill at that day's close ± the research half-spread (0.25 + 0.3% of premium per leg) and settle at intrinsic value on expiry.
- **Costs.**
  - Equity: Upstox delivery charges (₹20 per order, STT 0.1%, DP charge, GST, stamp duty).
  - Options: ₹20 × 4 orders + GST, STT 0.1% of premium sold, exchange charge 0.035%, plus the spread.
  - Paper "shadow" books pay 0.175% per side on turnover.
- **Leak test.** Every value after a cutoff date (2019-06-28) is scrambled. Every decision, fill and equity mark up to the cutoff must come out identical, or the sandbox is invalid.
- **Data.**
  - NSE equity EOD for 2005–2026, including delisted names.
  - NIFTY (NIFTYBEES spliced in before 2010) and India VIX.
  - NIFTY option EOD chains for 2015–2026.
  - The index fund is NIFTY + 1.25%/yr (an approximation of NIFTYBEES total return).

## Sandboxes

| Name | What it is | Menu (variants it must choose between) | How it chooses | Gate before real money |
|---|---|---|---|---|
| BENCH_NIFTY_FUND | Buy the index fund on 1 Jan 2015 and hold | none | none | none |
| TREND_LEARN | Index fund or cash at month-ends | SMA 50/100/150/200, always in | Best trailing 5-year Sharpe (min 3 years), re-chosen every June/Dec | none (always-in is on the menu) |
| MOM_LEARN | Momentum on the 200 most liquid stocks | lookback (3/6, 6/12, 9/12 months) × top 15/30 × 200-SMA filter on/off = 12 | Best trailing 5-year Sharpe of the shadow books, re-chosen every June/Dec | The champion's trailing 5-year CAGR must beat the index fund's; otherwise hold the index fund |
| MOM_FIXED_HINDSIGHT | Today's bot rule (6/12, top 30, 200-SMA) from day 1 | none | none | none. Its settings were picked with data up to 2026, so it is the optimistic reference |
| CONDOR_LEARN | NIFTY expiry-eve iron condor, sigma wings | short delta 0.10/0.15/0.20 × min VIX 0/16/18/20 = 12 | Best trailing average R among variants with ≥ 20 settled paper trades | The live promotion rule on the champion's own past trades: profit factor ≥ 1.15 and bootstrap P(avg R ≤ 0) < 0.10. Paper-only until then |
| CONDOR_FIXED_HINDSIGHT | Today's rule (15-delta, VIX ≥ 18) from day 1 | none | none | none (hindsight reference) |

- **Momentum** shadow books can start in 2006, when the data begins. A person in 2015 would have had that history too.
- **Condor** shadow books start in Jan 2015, when the option data begins, so this sandbox has to learn while the clock runs.
- **Condor sizing on ₹1L:** lots = max(1, ⌊10% of equity ÷ max loss per lot⌋). The trade is skipped if a single lot would risk more than 25% of equity.

## What is reported (once, no re-runs with different settings)
For every sandbox:
- final ₹, CAGR, max drawdown and Sharpe
- excess CAGR over BENCH and a bootstrap P(monthly excess ≤ 0)
- trades, win rate and profit factor
- a per-year table
- the learner log (which variant it picked, when, and whether the gate was open)

**Verdict per LEARN sandbox:** the edge was "found blind" only if the excess over BENCH is positive **and** P < 0.10.

## Known limits (stated up front)
- The menus themselves were written by someone who already knows momentum and condors were the research candidates. This is hindsight in the choice of what to test. The trend sandbox is the generic control.
- NIFTY lot sizes are approximated by date.
- Historical bid/ask is not available before Oct 2024, so the protocol spread is used throughout.
