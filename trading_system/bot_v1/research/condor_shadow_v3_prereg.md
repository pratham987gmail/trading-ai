# Shadow condor v3: no-VIX-filter, wide-wing variants (pre-registered 15 Oct 2026)

Added to the live shadow tracker (bot_v1.run CONDOR_VARIANTS). They log paper trades only, on NIFTY and SENSEX expiry eves, at the 15:10 scan with real Upstox bid/ask.

| Tag | Short delta | Wings | VIX filter |
|---|---|---|---|
| nv15w15 | 0.15 | 1.5σ | none |
| nv20w15 | 0.20 | 1.5σ | none |

- **Origin:** nv15w15 is the frozen pick of the train-to-2021 / validate-2022–26 study (condor_novix_split_results.md). nv20w15 ranked #1 in that validation.
- **Status:** information only. The v2 primary (`ALL:sigma`, VIX ≥ 18) is unchanged.
- **Promotion rule, applied to these trades only, counted from 15 Oct 2026:**
  - pooled NIFTY + SENSEX nv15w15
  - at least 30 settled trades
  - profit factor ≥ 1.15 and bootstrap P(avg R ≤ 0) < 0.10, after charges
  - no earlier trades count, including the 6-trade replay in live_replay_novix_results.md
