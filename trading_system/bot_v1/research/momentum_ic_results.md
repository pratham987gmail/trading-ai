# Momentum signal quality: IC, ICIR and decay. Results (protocol: momentum_ic_prereg.md; script: momentum_ic_study.py)

## Primary period, Dec 2006 – Dec 2021 (181 month-ends): PASS

| Signal | Mean IC (1 m) | ICIR monthly | ICIR annualised | t | Months IC > 0 | Top-15 vs universe | Top-30 vs universe |
|---|---|---|---|---|---|---|---|
| S1 bot rule (6/12) | 0.041 | **0.179** | 0.62 | **2.40** | **60%** | +1.17%/month (66% of months, t 3.2) | +0.72%/month (61%, t 2.5) |
| S2 frozen pick (9/12) | 0.042 | **0.177** | 0.61 | **2.38** | **60%** | +1.07%/month (69%, t 2.8) | +0.60%/month (61%, t 2.1) |

- Both signals clear all three pre-registered marks: ICIR ≥ 0.15, t ≥ 2, and > 55% of months positive.
- **What this means:** the edge is small each month (IC ≈ 0.04) but consistent, which is what a real factor looks like.
- **Top 15 captures about 60–80% more spread than top 30.** This independently supports the switch to 15 names.

## Decay: half-life longer than 12 months
- Mean IC by forward horizon (S1, Newey-West t in brackets):
  - 1 month: 0.041 (2.3)
  - 2 months: 0.061 (2.9)
  - 3 months: 0.077 (3.2)
  - **6 months: 0.088 (2.8)**
  - 9 months: 0.084 (2.2)
  - 12 months: 0.071 (1.7)
- Predictive power builds up to about 6 months and is still about 80% of its peak at 12 months.
- So the **6-monthly rebalance is well matched**, and there is no case for trading more often.
- **Score persistence:** month-to-month rank correlation is 0.90 to 0.92, so the signal is slow and turnover is low.

## Weak years (1-month IC < 0)
- 2006, 2008, 2009, 2016 and 2020.
- These are crash-and-rebound years, when last year's losers lead the bounce. This is the textbook momentum-crash pattern; the 200-SMA filter partly protects against it.

## Already-seen period, Jan 2022 – 2026 (56 months; used in earlier studies, so not a clean test)

| Signal | Mean IC | ICIR monthly | t | Months IC > 0 | Top-15 vs universe |
|---|---|---|---|---|---|
| S1 | 0.017 | 0.10 | 0.76 | 59% | +0.32%/month |
| S2 | 0.016 | 0.10 | 0.74 | 57% | +0.88%/month (t 1.85) |

- **IC by year (S2):**
  - 2022: 0.011
  - 2023: 0.071
  - 2024: −0.022
  - 2025: −0.014
  - 2026: 0.040
- **Top-15 spread by year:**
  - 2022: +1.4%/month
  - 2023: +1.7%/month
  - 2024: −0.8%/month
  - 2025: 0.0%/month
  - 2026: +2.8%/month
- **The signal has been much weaker since 2022, and flat to negative in 2024–25.**
- The frozen book's 2022–26 lead over the index fund leaned on two things:
  - 2023, when the whole 200-stock universe beat NIFTY by about 36%
  - 2026, when the trend filter held cash through the fall
- Stock selection itself added little in 2024–25.

## Conclusions
1. **Momentum is a real, consistent long-run signal.** ICIR 0.62 annualised, t 2.4, over 15 years and 181 months, with a slow decay that fits a 6-monthly rebalance.
2. **Top 15 is better than top 30.** This confirms the earlier decision from a different angle.
3. **Warning:** the signal is in a weak patch (2024–25). This could be a normal lull, since 2016 and 2020 also dipped, or the start of fading. Only live data can tell.
4. **Proposed monitor for the bot's monthly report:**
   - trailing 12- and 24-month IC and top-15 spread
   - **review rule:** if the trailing 24-month ICIR goes below 0, pause new money and review
