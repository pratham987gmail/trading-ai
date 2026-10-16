# Momentum signal quality: IC, ICIR and decay (pre-registered 17 Oct 2026, before any result)

**Question.** Does the bot's momentum score rank stocks by their future returns consistently, month after month, or only in a few big months? And how long does its predictive power last?

## Setup
- **Universe:** at each month-end t, the point-in-time top 200 by traded value, the same as the bot and the sandboxes.
- **Signals:** computed exactly by `bot_v1.momentum.rank_universe`, which also drops stocks with suspect split moves.
  - **S1:** today's bot rule (6/12-month, vol-adjusted).
  - **S2:** the frozen 9/12-month pick.
- **Forward return:** adjusted close from t+1 (the fill day) to t+1+h sessions, for h = 1, 2, 3, 6, 9 and 12 months (21 sessions per month). A stock that stops trading keeps its return up to its last price.
- **IC:** each month, the Spearman rank correlation between the score and the forward return across the ~200 stocks.

## Primary period and results
- **Primary period:** month-ends Dec 2006 to Dec 2021, about 180 months.
- 2022–26 is shown separately and labelled "already seen", because earlier studies used it.

**Reported:**
1. **1-month IC:** mean, standard deviation, ICIR = mean ÷ sd (monthly; also ×√12 annualised), t-stat, and % of months positive.
2. **Decay curve:** mean IC at each horizon, with Newey-West t-stats for overlapping horizons.
   - The month in which the IC falls to half its peak is the signal's half-life.
   - If IC is still positive at 6 months, the bot's 6-monthly rebalance is not too slow.
3. **Top-minus-universe:** each month's return spread of the top 15 and top 30 against the universe average, and the % of months the spread is positive.
4. **IC by calendar year.**
5. **Score persistence:** rank correlation of the score at month t vs t+1. This measures turnover, not decay.

## Pass marks for "consistent signal"
- monthly 1-month ICIR ≥ 0.15 (≈ 0.5 annualised)
- t-stat ≥ 2
- IC > 0 in at least 55% of months
