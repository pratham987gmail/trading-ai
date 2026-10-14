# Overfitting audit — bot_v1 (14 Oct 2026)

Scripts: overfit_mom_harness.py, overfit_mom_tests.py, overfit_condor_grid.py, overfit_condor_tests.py.

## Verdicts
| Strategy | Parameter overfit | Selection / data-snooping | Backtest realism | Overall |
|---|---|---|---|---|
| Momentum30 + trend filter | LOW — 82% of 240 variants beat NIFTYBEES+2% in 2017-26; beats 60/60 random portfolios; deflated Sharpe 0.985 at 300 trials | MEDIUM — picked in a 2nd study after Track C's pick (C12) failed; design→holdout rank corr 0.03, PBO 0.43 (tuning is noise) | MEDIUM — price-index benchmark flattered excess by ~1.3%/yr; no tax; trend filter cost ~6%/yr in 2017-26 | Edge is real; backtest numbers are optimistic. Expect ~2-3%/yr over NIFTYBEES before tax, with multi-year losing stretches (2017-19: -7, -19, -17 pts vs NIFTYBEES) |
| Expiry-eve condor (VIX>=18) | LOW for structure (28/30 delta×wing grid positive in both periods) | HIGH — VIX 18 sits at the peak of the threshold sweep; VIX-label permutation p = 0.27 (filter not shown to pick better trades); holdout 2022-26 reused in 3 studies; post-hoc data fix changed the selection | HIGH — no historical bid/ask (2x spread fails: P 0.15/0.25); EOD close entry before 2024; 2015-21 unfiltered avg R negative, 2022-26 positive = regime | Edge NOT established. Keep shadow-only. |

## Key numbers
Momentum (stocks-only, research engine; holdout 2017-26): F03 15.2% vs NIFTYBEES 12.4% (total return), NIFTY price 11.1%.
- Grid 240 (signal×top N×rebalance×SMA): F03 holdout rank 179/240, design rank 62. Median by dimension: top10 21.5%, top50 14.9%; no filter 22.8% vs SMA200 16.1%.
- PBO (CSCV, 16 blocks, 3000 splits) 0.43; walk-forward re-selection 2011-26 15.8%/yr vs fixed F03 14.0% vs NIFTYBEES 9.6%.
- Placebos: equal-weight top-200 + filter 10.1% holdout; random 30 + filter median 9.6% (p95 12.7%).
- Without trend filter: 2006-16 10.1% (2008 crash), 2017-26 21.0%.
- Costs x2/x3/x5: holdout 14.3/13.5/11.9%.
Condor (NIFTY EOD 2015-26, 452 expiry-eves):
- VIX threshold sweep (PF): 16 1.28, 17 1.37, **18 1.45**, 19 1.35, 20 1.25, 22 1.07; unfiltered 1.23 (P 0.085).
- Design (2015-21) unfiltered avg R -0.016; VIX>=18 +0.029. Holdout unfiltered +0.058; VIX>=18 +0.060.
- PBO 0.38; structure rank corr design→holdout 0.03.
- Quarter-clustered bootstrap P 0.030 (iid 0.067); 90% CI avg R [0.005, 0.085].
- Tail: 28 losses /142; mean loss -0.46R vs mean win +0.16R; ~6 wins repay one max loss.

## Protections cut or not wired (charter vs code)
1. Stale-data halt — data.stale() exists, never called.
2. Kill switch / daily loss cap — config field only, unused; no consecutive-loss stop.
3. Stop-loss on every trade — momentum has none (5% stop tested and dropped); condor relies on defined max loss only.
4. Minimum R:R 1:1.5 — condor reward:risk is ~1:6 (credit/max loss ≈ 0.1-0.5); violates the charter rule.
5. The bot's 20% one-day-move filter was added after the research and kept after seeing it helped — an unvalidated tweak.

## Where the sandbox / backtests were not properly built
- Early sandbox runs and the Gemini swarm used synthetic prices (Gemini hard-coded IV = 1.15×HV+3%).
- ETFs/liquid funds inside the equity universe (fixed; stocks-only still passes).
- Corporate actions approximated (gap>21.5% treated as split, returns clipped ±40%) — can erase real gap moves.
- Benchmark: NIFTY price index instead of total return.
- Taxes, dividends, slippage beyond the cost model not included.
- Condor: synthetic quotes, entry at close not 15:10 (pre-2024), lot fixed at 75, exercise STT missing from bot charges, pre-2019 monthly-only expiries.
- Bot code bugs found only by backtesting through the bot: blocked rebalances (fixed), split detection (backtest-only, fixed).
