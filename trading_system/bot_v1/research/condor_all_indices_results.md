# Expiry-eve condor on all six indices: results (pre-registered in condor_all_indices_prereg.md)

- **Data:** Upstox expired-instruments 5-minute candles, Oct 2024 to Oct 2026. 333 expiries analysed (6 skipped for missing candles, 8 because the chain did not straddle spot).
- **Entry:** close of the 15:05 bar on the eve of expiry. Held to expiry.
- **Costs:** protocol half-spread (0.25 + 0.3% of premium per leg) plus F&O charges.

## Primary test: sigma variant, VIX ≥ 18 (protocol cost)

| Index | Trades | Win | Avg R | PF | P(avg R ≤ 0) | ₹ (1 lot each) |
|---|---|---|---|---|---|---|
| NIFTY | 15 | 93% | 0.194 | 4.36 | 0.018 | +36,935 |
| SENSEX | 15 | 80% | 0.110 | 2.27 | 0.124 | +22,041 |
| BANKNIFTY | 4 | 50% | −0.015 | 0.94 | 0.60 | −15,206 |
| FINNIFTY | 4 | 75% | 0.154 | 2.40 | 0.23 | −4,003 |
| MIDCPNIFTY | 4 | 100% | 0.308 | ∞ | 0.00 | +11,227 |
| BANKEX | 2 | 50% | 0.036 | 1.19 | 0.26 | −961 |
| **All six pooled** | **44** | **82%** | **0.146** | **2.62** | **0.006** (clustered by quarter: 0.004) | **+50,033** |
| Pooled without NIFTY (the only fresh evidence) | 29 | 76% | 0.121 | 2.13 | 0.126 (clustered) | +13,098 |
| The four monthly-only indices | 14 | 71% | 0.133 | 2.03 | 0.191 (clustered) | −8,943 |

- On the real-spread cost model the numbers are almost the same (pooled PF 2.66, P 0.005).

## All eves, no VIX filter (information only)
- Sigma variant, 326 trades, 78% wins, PF 1.29, P 0.046.
- By index:
  - MIDCPNIFTY: PF 2.31
  - BANKNIFTY: PF 2.19
  - SENSEX: PF 1.23
  - FINNIFTY: PF 1.14
  - NIFTY: PF 1.13
  - BANKEX: PF 0.95

## Verdict
- **The pooled primary passes the pre-registered rule** (PF 2.62 ≥ 1.15 and P 0.006 < 0.10). It is a weak pass, for four reasons:
  1. NIFTY was already used to design the rule, so it is not fresh evidence. Without NIFTY the result fails (P 0.126).
  2. Every VIX ≥ 18 trade falls in just 4 calendar quarters (2025 Q1–Q2, 2026 Q1–Q2). The sample is a few volatile episodes, not 44 independent bets.
  3. BANKNIFTY, FINNIFTY, MIDCPNIFTY and BANKEX have had monthly expiries only since Nov 2024. That gives 2–4 filtered trades each, which says almost nothing.
  4. ₹ results are positive in R terms but negative in rupees for the monthly four, because large-lot BANKNIFTY losses dominate.
- **Decision:** keep the condor in shadow (paper) mode. Do not trade it live on this evidence.
- The blind 2015–2026 sandbox (sandbox_results.md) points the same way: a learner that had to discover the condor on its own lost money.
