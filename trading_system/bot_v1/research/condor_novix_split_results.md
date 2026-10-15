# Condor without a VIX filter: train ≤ 2021, validate 2022 → Sep 2026 (protocol in condor_novix_split_prereg.md)

- **Leak test:** PASS (cutoff 2023-06-30; both sandboxes identical before the cutoff).
- **Frozen pick (5 Jan 2022):** short delta 0.15, wings 1.5σ, trade every eve.
  - Training record: 200 trades, avg R −0.005, PF 0.94, P 0.61.
  - **Gate CLOSED.** Every one of the 9 variants lost money in 2015–21.

## Training vs validation, all 9 variants (paper, R per trade after spread and charges)

| Variant | Train n | Train avg R | Train PF | Test n | Test win | Test avg R | Test PF | Test P | Test rank |
|---|---|---|---|---|---|---|---|---|---|
| d15, 1.5σ (frozen pick) | 200 | −0.005 | 0.94 | 247 | 82% | 0.024 | 1.33 | 0.058 | 3 |
| d20, 1.5σ | 200 | −0.005 | 0.95 | 247 | 77% | 0.034 | 1.37 | 0.038 | 1 |
| d10, 1.5σ | 200 | −0.006 | 0.87 | 247 | 87% | 0.020 | 1.42 | 0.052 | 5 |
| d20, 1σ | 200 | −0.017 | 0.88 | 247 | 77% | 0.034 | 1.25 | 0.086 | 2 |
| d15, 1σ | 200 | −0.019 | 0.83 | 247 | 81% | 0.022 | 1.21 | 0.148 | 4 |
| d10, 1σ | 198 | −0.022 | 0.73 | 247 | 87% | 0.019 | 1.28 | 0.112 | 6 |
| d10, 0.5σ | 198 | −0.039 | 0.63 | 247 | 84% | 0.001 | 1.01 | 0.476 | 9 |
| d20, 0.5σ | 200 | −0.043 | 0.79 | 247 | 73% | 0.016 | 1.08 | 0.311 | 7 |
| d15, 0.5σ | 199 | −0.047 | 0.72 | 247 | 79% | 0.008 | 1.05 | 0.391 | 8 |

**Ranking carried over:** Spearman ρ = 0.83, p = 0.005. Wider wings beat narrow wings in both periods.

**The sign did not carry over:** every variant lost money in 2015–21, and every variant made money in 2022–26.

## ₹1L books, 3 Jan 2022 → Sep 2026

| Sandbox | Final ₹ | CAGR | Max DD | Trades | Win | PF (₹) | vs index fund | P |
|---|---|---|---|---|---|---|---|---|
| Index fund | 1,36,694 | 6.8% | −15.7% | — | — | — | — | — |
| CONDOR_NOVIX_FROZEN (obeys gate) | 1,00,000 | 0% | 0 | 0 | — | — | −6.8%/yr | — |
| CONDOR_NOVIX_FORCED (ignores gate) | 2,03,794 | 16.1% | −21.7% | 246 | 82% | 1.49 | +9.3%/yr | 0.21 |

FORCED by year:
| 2022 | 2023 | 2024 | 2025 | 2026 (to Sep) |
|---|---|---|---|---|
| +1.5% | +1.0% | +23.1% | +32.8% | +21.6% |

- The worst single trade was −₹16,235 (Feb 2022, the Ukraine shock). Five trades lost ₹11.5k–16k each.

## Verdict
- **Did training predict the future? Half.**
  - It predicted **which shape is best**: wide 1.5σ wings, around 15–20 delta.
  - It did **not** predict **whether the strategy makes money at all**. Training said "no edge", and 2022–26 turned out profitable.
  - Following the pre-registered gate meant sitting out a +103% period. Ignoring the gate would have looked foolish in 2022–23 (+1.5%, +1.0%) before paying off in 2024–26.
- **Why the flip:** the same rule lost in 2015–21 and won in 2022–26. That points to a change in the market (weekly-expiry option pricing on the eve, spreads, and the lot-size changes in 2024–25), not to a stable edge we can rely on. Almost all the ₹ gains came in 2024–26.
- **Still unproven in validation:** in R terms the frozen pick's test result is PF 1.33, P 0.058. It passes the bar on paper. But the ₹ book's P versus the index fund is 0.21, and the earlier audit showed the condor fails if spreads double.
- **Action:** add "no-filter, 15–20 delta, 1.5σ wings" to the live paper (shadow) tracker. Promote it only if it passes PF ≥ 1.15 and P < 0.10 on trades recorded from now on with real Upstox quotes.
