# Cheapest tail protection that keeps the premium: pre-registration (15 Oct 2026, before any result)

**Question.** vrp_results.md showed that a real premium exists in 1–3-day index options, and that 1.5σ condor wings give back about half of it. Which defined-risk structure keeps the most premium and still fits a ₹1L account?

## Menu (24 structures, all held to expiry, entry at the close)
- **Entry:** k = 1, 2 or 3 sessions before expiry.
- **Short legs:** 15-delta (BS delta with VIX, as in the bot). Both-sided structures also get a 20-delta version.
- **Wings:** 1.5σ or 2.5σ beyond each short strike. σ is the index's expected move to expiry, spot × VIX × √(days/365).
- **Sides:**
  - **both** (iron condor)
  - **put** (put credit spread only)
  - **call** (call credit spread only)
- Total: k {1,2,3} × wings {1.5σ, 2.5σ} × sides {both, put, call} at 15-delta = 18, plus k {1,2,3} × wings {1.5σ, 2.5σ} at 20-delta both-sided = 6.

## Costs
- Per leg: the protocol half-spread (0.25 + 0.3% of premium) plus ₹23.6 brokerage incl. GST, converted to points at the lot size.
- STT: 0.1% of premium sold.

## Data and split
- **Data A:** NIFTY option EOD closes (only strikes that traded).
- **Training:** Jan 2015 – Dec 2021.
- **Validation:** Jan 2022 – Sep 2026, evaluated once.
- **Cross-check (Data B):** six indices at 15:05 on the eve, Oct 2024 – Oct 2026. B has no data for k = 2 or 3, so it can only cross-check k = 1 structures.

## Fits ₹1L?
A structure is eligible only if its median 1-lot max loss is ≤ ₹25,000. That is measured at today's NIFTY (≈22,300) and lot size (65): max-loss points ÷ spot × 22,300 × 65.

## Selection and verdict
- **Selection:** among eligible structures with ≥ 50 training trades, pick the highest training avg R (R = P&L ÷ max loss).
- **Validation:** that one structure is checked on 2022–26. **PASS** if PF ≥ 1.15 and the week-clustered bootstrap P(avg R ≤ 0) < 0.10.
- **Also reported:**
  - every structure's training and validation numbers
  - Spearman correlation of training vs validation avg R
  - ₹ per lot per trade
  - the worst trade
  - the share of weeks with a trade
