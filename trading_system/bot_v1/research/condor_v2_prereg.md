# Condor edge study v2 — pre-registered 2026-10-14 12:45 IST, BEFORE any result was computed

Engine: bot_v1 code (condor.plan_condor / settle / report.condor_charges_points) on NIFTY EOD chains,
entry at the close of the session before (holiday-adjusted) expiry, held to expiry.
Quotes: close -/+ half-spread. Primary cost model = protocol spread (0.25 + 0.3% of premium).
Sensitivity (reported, NOT used for selection): real 2026 recorder spreads (median half-spread
0.03-0.07 pts on <=80-pt options) applied to entries from 2022 on.

Periods: DESIGN 2015-01-01..2021-12-31, HOLDOUT 2022-01-01..2026-09-25 (same as rulebook_test).
Selection: highest DESIGN avg R among configs with >= 40 DESIGN trades. Evaluate HOLDOUT once.
PASS (unchanged): HOLDOUT PF >= 1.15 AND bootstrap P(avg R <= 0) < 0.10.
Caveat stated up front: the baseline's holdout was already seen (rulebook_test), so holdout is
not fully fresh; the only truly fresh data is the live shadow log going forward.

Why these: the condor sells volatility; theory and the literature say the edge is the variance
risk premium (implied > realised), not the VIX level, and that the main risk is trend/jump days.
More trades also raise statistical power (the baseline fails on P, not on PF).

Configs (12, fixed):
 V00 baseline   VIX>=18, short 15-delta, wings 1 sigma
 V01 VIX>=15
 V02 VIX>=13
 V03 VRP        VIX >= 1.25 x NIFTY 20-day realised vol (annualised %), VIX>=13
 V04 VRP+15     VRP as V03 and VIX>=15
 V05 calm       VIX>=18 and |NIFTY 5-day return| < 2%
 V06 no-spike   VIX>=18 and VIX change on entry day < +10%
 V07 VRP calm   V03 and |5-day return| < 2%
 V08 10-delta   VIX>=18, short 10-delta, 1 sigma wings
 V09 20-delta   VIX>=18, short 20-delta, 1 sigma wings
 V10 narrow     VIX>=18, 15-delta, 0.5 sigma wings
 V11 VRP 10d    V03 with 10-delta shorts
