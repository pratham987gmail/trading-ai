# Train ≤ 2021, validate 2022 → Oct 2026: results (protocol in sandbox_2022_split_prereg.md)

- **Leak test:** PASS. Everything after 2023-06-30 was scrambled; all decisions, fills and equity marks up to that date were identical for TREND_FROZEN, MOM_FROZEN and CONDOR_FROZEN.
- **Ran once.** No settings were changed after seeing the result.

## What each strategy chose from training data only (decided 3–5 Jan 2022, then frozen)

| Strategy | Frozen pick | Training evidence | Gate |
|---|---|---|---|
| Momentum | 9/12-month lookback, **top 15**, 200-SMA filter | Sharpe 1.20, +10.8%/yr over the index fund (2006–21) | Open: trade it |
| Trend | 150-day SMA | Sharpe 0.76, but 2.0%/yr **behind** buy and hold | (no gate) |
| Condor | 20-delta, VIX ≥ 18 | 87 trades, PF 1.38, P 0.143 | **Closed**: fails PF ≥ 1.15 *and* P < 0.10, so no money committed |

## Validation, 3 Jan 2022 → 9 Oct 2026 (₹1L each)

| Sandbox | Final ₹ | CAGR | Max DD | Sharpe | vs index fund | P(excess ≤ 0) | Months beating fund |
|---|---|---|---|---|---|---|---|
| BENCH_NIFTY_FUND | 1,36,694 | 6.8% | −15.7% | 0.56 | — | — | — |
| **MOM_FROZEN** | **1,72,763** | **12.2%** | −20.9% | **0.86** | **+5.4%/yr** | 0.17 | 56% |
| TREND_FROZEN | 1,26,128 | 5.0% | −14.9% | 0.54 | −1.8%/yr | 0.69 | 40% |
| CONDOR_FROZEN | 1,00,000 | 0% (no trades: gate closed) | 0 | — | −6.8%/yr | — | — |
| MOM_LEARN (re-learns every 6 months) | 1,50,827 | 9.0% | −34.3% | 0.61 | +2.2%/yr | 0.33 | 49% |
| TREND_LEARN | 1,20,094 | 3.9% | −16.0% | 0.42 | −2.9%/yr | 0.84 | — |
| CONDOR_LEARN | 1,11,905 | 2.4% | −9.4% | 0.42 | −4.4%/yr | 0.82 | — |
| MOM_FIXED_HINDSIGHT (bot rule, top 30) | 1,40,230 | 7.4% | −19.2% | 0.62 | +0.6%/yr | 0.48 | 47% |
| CONDOR_FIXED_HINDSIGHT | 1,45,261 | 8.2% | −16.4% | 0.76 | +1.4%/yr | 0.50 | — |

Per year, MOM_FROZEN vs the index fund:
| Year | MOM_FROZEN | Fund |
|---|---|---|
| 2022 | −8.2% | +2.7% |
| 2023 | +39.5% | +21.0% |
| 2024 | +14.2% | +10.0% |
| 2025 | +19.7% | +11.7% |
| 2026 | −1.3% | −10.5% |

## Did training predict the future?

**Momentum: yes, mostly.**
- The frozen pick ranked **#1 of 12 variants** in validation (paper Sharpe 0.92, 15.7%/yr).
- The training ranking carried over: Spearman ρ = 0.61 between training and validation Sharpe, p = 0.04.
- The ₹1L book beat the index fund by 5.4%/yr and was ahead in 56% of months.
- **But** 4.75 years is too short to be statistically sure: P = 0.17 misses the pre-registered 0.10 bar. Read it as "consistent with a real edge, not yet proven on this window alone." Together with the 2015 blind run (P = 0.08), the evidence is reasonably strong.
- Re-learning every 6 months did *worse* than freezing: 9.0% vs 12.2%, and a −34% drawdown in 2022. That learner switched settings at the wrong times. Stability beats chasing.

**Trend: no.** Training already showed SMA filters trailing buy-and-hold, and that stayed true (ρ = 0.00). Buy and hold ranked #1 in validation.

**Condor: training correctly refused to claim an edge, but the ranking did not carry over.**
- The frozen champion (20-delta, VIX ≥ 18) did well on paper in validation: 55 trades, 78% wins, PF 1.48, ranked #2 of 12. But at P = 0.125 it would *still* fail the promotion bar.
- Every variant was positive in 2022–26, including no-filter ones that lost money in 2015–21. Training avg R did not predict validation avg R (ρ = 0.18, p = 0.57).
- So the condor's results depend on the period. Its edge is small and not stable enough to call from history.

## Conclusion for the bot
1. **Momentum, top 15, 9/12-month lookback, 200-SMA filter, settings frozen.** This is what training on data up to 2021 would have chosen, and it held up in 2022–26. Do not re-optimise it every few months.
2. Today's 30-stock rule only matched the index in validation on ₹1L (+0.6%/yr). Switch to 15.
3. Keep the condor in paper mode. History does not reliably say which variant will work next.
