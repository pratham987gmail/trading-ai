# Cheapest tail protection that keeps the premium: results (protocol: structure_prereg.md)

## Verdict: FAIL, and the ranking reversed
- **Pre-registered pick:** sell 2 sessions before expiry, 20-delta shorts, 1.5σ wings, both sides. It was the highest training avg R among structures that fit ₹1L.
- **Validation, 2022–26:** avg R 0.000, PF 1.00, P 0.50. **FAIL.**
- **The training ranking predicted the validation ranking backwards:** Spearman ρ = **−0.59** (p = 0.003).
  - The structures that worked best in 2015–21 (put spreads 2–3 days out) were among the worst in 2022–26.
  - The structures that were worst in 2015–21 (condors and call spreads on the eve) were the best in 2022–26.

## All 24 structures (NIFTY EOD; R = P&L ÷ max loss; ₹ at today's NIFTY 22,300 × lot 65)

| Structure | Train avg R | Train PF | Valid. avg R | Valid. PF | Valid. P | ₹/trade (valid.) | Worst ₹ | Median max loss ₹ | Fits ₹1L |
|---|---|---|---|---|---|---|---|---|---|
| k3 15Δ 1.5σ put | 0.014 | 1.44 | 0.004 | 1.09 | 0.35 | 184 | −43,188 | 35,504 | no |
| k2 15Δ 1.5σ put | 0.012 | 1.36 | 0.000 | 1.01 | 0.48 | 8 | −39,318 | 26,556 | no |
| k2 15Δ 1.5σ both | 0.012 | 1.20 | −0.003 | 0.96 | 0.59 | −92 | −38,073 | 25,159 | no |
| k3 15Δ 2.5σ put | 0.011 | 1.49 | 0.006 | 1.26 | 0.17 | 362 | −47,045 | 58,908 | no |
| k2 15Δ 2.5σ both | 0.010 | 1.27 | 0.002 | 1.05 | 0.40 | 113 | −37,621 | 44,035 | no |
| k2 15Δ 2.5σ put | 0.010 | 1.46 | 0.004 | 1.15 | 0.28 | 184 | −38,922 | 45,000 | no |
| k2 20Δ 2.5σ both | 0.005 | 1.10 | 0.005 | 1.09 | 0.32 | 222 | −40,662 | 42,875 | no |
| k1 15Δ 2.5σ both | 0.002 | 1.03 | 0.020 | 1.46 | 0.036 | 519 | −44,329 | 31,210 | no |
| k1 15Δ 2.5σ put | 0.001 | 1.03 | 0.008 | 1.31 | 0.14 | 208 | −45,314 | 32,222 | no |
| **k2 20Δ 1.5σ both (PICK)** | **0.000** | 1.00 | **0.000** | **1.00** | **0.50** | −14 | −36,959 | 24,520 | yes |
| k1 15Δ 2.5σ call | 0.000 | 1.01 | 0.010 | 1.47 | 0.072 | 311 | −23,781 | 32,450 | no |
| k1 20Δ 2.5σ both | −0.000 | 1.00 | 0.026 | 1.48 | 0.018 | 669 | −43,009 | 30,644 | no |
| k1 15Δ 1.5σ put | −0.001 | 0.97 | 0.009 | 1.20 | 0.21 | 120 | −28,529 | 19,152 | yes |
| k2 15Δ 2.5σ call | −0.001 | 0.94 | −0.001 | 0.94 | 0.56 | −71 | −33,581 | 45,620 | no |
| k2 15Δ 1.5σ call | −0.002 | 0.96 | −0.003 | 0.92 | 0.61 | −100 | −27,863 | 26,520 | no |
| k1 15Δ 1.5σ call | −0.004 | 0.90 | 0.014 | 1.43 | 0.074 | 271 | −14,065 | 19,340 | yes |
| k1 15Δ 1.5σ both | −0.005 | 0.94 | 0.026 | 1.36 | 0.052 | 391 | −27,582 | 18,276 | yes |
| k1 20Δ 1.5σ both | −0.005 | 0.94 | 0.036 | 1.40 | 0.028 | 518 | −26,305 | 17,567 | yes |
| k3 15Δ 2.5σ both | −0.007 | 0.87 | 0.016 | 1.48 | 0.025 | 842 | −45,564 | 57,476 | no |
| k3 20Δ 2.5σ both | −0.009 | 0.87 | 0.017 | 1.38 | 0.040 | 904 | −47,629 | 56,209 | no |
| k3 15Δ 1.5σ both | −0.011 | 0.86 | 0.019 | 1.31 | 0.087 | 576 | −41,742 | 34,265 | no |
| k3 20Δ 1.5σ both | −0.016 | 0.85 | 0.020 | 1.25 | 0.107 | 602 | −39,670 | 33,031 | no |
| k3 15Δ 2.5σ call | −0.017 | 0.54 | 0.009 | 1.63 | 0.018 | 480 | −23,873 | 58,909 | no |
| k3 15Δ 1.5σ call | −0.024 | 0.57 | 0.013 | 1.51 | 0.042 | 393 | −23,899 | 35,604 | no |

- n ≈ 200 trades in training and 248 in validation per structure.
- k = sessions before expiry; Δ = short delta; "both" = iron condor.

## Cross-check: six indices at 15:05 on the eve, 2024–26 (k1 only)

| Structure | n | Win | Avg R | PF | P | Median max loss ₹ |
|---|---|---|---|---|---|---|
| k1 20Δ 1.5σ both | 325 | 75% | 0.045 | 1.40 | 0.025 | 12,994 |
| k1 15Δ 1.5σ both | 325 | 79% | 0.033 | 1.36 | 0.043 | 13,647 |
| k1 20Δ 2.5σ both | 325 | 77% | 0.032 | 1.46 | 0.020 | 23,326 |
| k1 15Δ 1.5σ call | 325 | 91% | 0.029 | 1.73 | 0.007 | 14,654 |
| k1 15Δ 2.5σ both | 325 | 79% | 0.026 | 1.45 | 0.027 | 23,738 |
| k1 15Δ 2.5σ call | 325 | 91% | 0.021 | 1.83 | 0.004 | 25,203 |
| k1 15Δ 2.5σ put | 325 | 86% | 0.003 | 1.07 | 0.37 | 25,276 |
| k1 15Δ 1.5σ put | 325 | 85% | −0.001 | 0.99 | 0.50 | 14,828 |

## What this means
1. **Wider wings did not keep more of the premium per unit of risk.**
   - 2.5σ wings make more ₹ per trade, but they raise max loss even more. Avg R is no better.
   - Most 2.5σ structures also do not fit ₹1L: median max loss is ₹30k–59k per lot.
2. **Entering 2–3 days out did not help either.** It worked in 2015–21 and failed in 2022–26.
3. **One-sided spreads also flipped.**
   - Put spreads were best in 2015–21 and roughly flat since.
   - Call spreads were worst in 2015–21 and best since: 90% wins, PF 1.4–1.8 in 2022–26 and on all six indices.
   - That is a regime effect, likely the strong 2023–25 uptrend changing which side is overpriced. It is not a rule we can rely on going forward.
4. **The premium is real** (vrp_results.md). But every structure that fits ₹1L keeps only **0–0.04 R per trade**, and *which* structure works flips between regimes.
   - History can tell us the premium exists. It cannot tell us which defined-risk shape will capture it next.
5. **The best 2022–26 structure on both data sets** is the eve condor with 20-delta shorts and 1.5σ wings: k1 20Δ 1.5σ both, about ₹500 per lot per trade, worst −₹26k.
   - This is essentially the `nv20w15` variant already in the live paper tracker.
   - Choosing it now would be hindsight, so it stays paper-only and must earn promotion on new trades.

## Decision
- **Stop optimizing condor structure on historical data.** The rankings are unstable (ρ = −0.59), so more tuning would only overfit.
- **Keep the live paper tracker as it is.** The primary rule (VIX ≥ 18) and the two no-filter variants (nv15w15, nv20w15) together already cover the best candidates.
- **The bot's real-money focus stays on momentum** (top 15, frozen settings). The condor remains a paper experiment until live evidence accumulates.
