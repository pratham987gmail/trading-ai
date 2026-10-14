# Condor edge study v2 — results (14 Oct 2026)

Pre-registration: condor_v2_prereg.md (written before running). 452 expiry-eves 2015-2026,
bot_v1 code, protocol spreads. DESIGN 2015-21 / HOLDOUT 2022-26.

| cfg | idea | DESIGN n / avgR / PF / P | HOLDOUT n / avgR / PF / P |
|---|---|---|---|
| V00 | baseline VIX>=18, 15d, 1σ | 87 / +0.029 / 1.38 / 0.17 | 55 / +0.060 / 1.52 / 0.12 |
| V01 | VIX>=15 | 146 / -0.008 / 0.92 / 0.62 | 98 / +0.068 / 1.76 / 0.03 |
| V02 | VIX>=13 | 181 / -0.019 / 0.83 / 0.78 | 157 / +0.042 / 1.42 / 0.06 |
| V03 | VRP filter | 98 / -0.018 / 0.84 / 0.70 | 64 / +0.006 / 1.05 / 0.43 |
| V04 | VRP + VIX>=15 | 79 / -0.017 / 0.85 / 0.67 | 38 / +0.049 / 1.53 / 0.16 |
| V05 | calm 5-day | 51 / +0.034 / 1.65 / 0.13 | 35 / +0.059 / 1.50 / 0.19 |
| V06 | no VIX spike | 84 / +0.025 / 1.32 / 0.21 | 52 / +0.069 / 1.65 / 0.08 |
| V07 | VRP + calm | 68 / -0.029 / 0.73 / 0.78 | 44 / -0.039 / 0.74 / 0.75 |
| V08 | 10-delta shorts | 85 / +0.033 / 1.80 / 0.09 | 55 / +0.047 / 1.68 / 0.10 |
| V09 | 20-delta shorts | 87 / +0.039 / 1.40 / 0.14 | 55 / +0.073 / 1.51 / 0.11 |
| V10 | 0.5σ wings | 86 / +0.009 / 1.06 / 0.41 | 55 / +0.075 / 1.46 / 0.14 |
| V11 | VRP + 10d | 96 / -0.020 / 0.74 / 0.76 | 64 / +0.010 / 1.14 / 0.35 |

Selected on DESIGN (best avg R, n>=40): **V09**. HOLDOUT PF 1.51, P 0.111 → **FAIL** (needs < 0.10).
Sensitivity (not used for the verdict): with real 2026 recorder spreads V09 holdout P = 0.096,
V00 = 0.099 — but 2026 spreads cannot be assumed for earlier years.

Findings
- The edge only exists when VIX >= 18; lower floors are negative in 2015-21 (their good 2022-26
  numbers are one regime). The variance-risk-premium filter did not help (worse in both periods).
- Short-strike choice (10/15/20 delta) barely matters: all positive in both periods → the
  structure is robust, but no tweak adds real edge. Differences are within noise.
- V08 (10-delta) has P < 0.10 in both periods but was not the pre-registered pick; promoting it
  now would be cherry-picking. It may be logged as a second shadow variant going forward.
- Real spreads (recorder bid/ask, Sep 2026) are 5-8x tighter than the backtest model: ~+0.01R
  per trade. Actual fills from the live shadow log will settle this.
- The binding problem is sample size (~13 qualifying expiry-eves a year), not a weak rule.
