# No-VIX-filter condor on the live recorded quotes (Azure VM, 21 Sep – 13 Oct 2026)

- **Data:** the Angel One recorder on the Azure VM (`data/cloud/options.db`), with real bid/ask every ~5 min.
- **Method:**
  - Entry is the first snapshot at or after 15:05 on each expiry eve.
  - The bot's own `plan_condor`, with VIX taken from the 15:05 5-minute close.
  - Settled on the index close on expiry day. Charges included.
  - Script: `live_replay_novix.py`.
- **Excluded:** one eve (SENSEX, 23 Sep), because the recorder had no snapshot near 15:05 (last at 12:31).

## The frozen pick (15-delta, 1.5σ wings, no filter): 6 live trades

| Eve | Index | VIX | Overnight move | Strikes (S = sell, B = buy) | Credit (real bid/ask) | Spread cost | Net pts | R | ₹ (1 lot) |
|---|---|---|---|---|---|---|---|---|---|
| 21 Sep | NIFTY | 11.3 | −0.43% | S23600CE B23800CE S23300PE B23100PE | 25.10 | 0.12 | +23.6 | +0.13 | +1,534 |
| 28 Sep | NIFTY | 13.8 | −0.29% | S22950CE B23200CE S22600PE B22350PE | 36.85 | 0.15 | +35.3 | +0.17 | +2,296 |
| 30 Sep | SENSEX | 13.6 | −0.81% | S73000CE B73800CE S72000PE B71200PE | 150.70 | 0.35 | +55.4 | +0.09 | +1,108 |
| 5 Oct | NIFTY | 15.0 | +1.09% | S22700CE B22950CE S22350PE B22100PE | 41.90 | 0.12 | −35.7 | −0.17 | −2,323 |
| 7 Oct | SENSEX | 13.9 | −1.40% | S73200CE B74000CE S72100PE B71300PE | 113.05 | 0.27 | −398.6 | −0.58 | −7,973 |
| 12 Oct | NIFTY | 15.3 | +1.85% | S22500CE B22750CE S22100PE B21850PE | 28.05 | 0.13 | −195.4 | −0.88 | −12,702 |
| **Total** | | | | | | | | **avg −0.21 R** | **−18,059** |

- Result: 3 wins and 3 losses.
- **What went wrong:** the three losses came on overnight moves of 1.1%, 1.4% and 1.85%. With VIX at 13–15, a normal one-day move is about 0.8–0.9%, so these were roughly 1.3σ, 1.6σ and 2.0σ moves, three in three weeks.

## All 9 variants, same 6 eves (avg R)
- Order: d10 1.5σ −0.11, d10 1σ −0.17, d15 1.5σ −0.21, d20 1.5σ −0.24, d10 0.5σ −0.26, d20 1σ −0.29, d15 1σ −0.30, d15 0.5σ −0.37, d20 0.5σ −0.38.
- Every variant lost. The 1.5σ-wing variants were again among the least bad, the same ordering as the backtest.

## Did the backtest predict this?
- **Too few trades to judge.** Six trades cannot confirm or reject an edge of +0.024 R per trade.
- **How unlucky was it?** Drawing 6 trades at random from the frozen pick's 2022–26 validation trades, an average this bad (≤ −0.21 R) happens **2.8%** of the time. Three or more losses in six happens 7.8% of the time.
- So the first live weeks were worse than the backtest would normally produce. Either it was a rough patch or the 2022–26 edge is fading. We cannot tell which yet.
- **What the live data did confirm:** real spreads are tiny, 0.12–0.35 points for all four legs. That is cheaper than the backtest assumed, so costs are not the problem.

## Decision
- Track nv15w15 and nv20w15 in the live paper tracker from 15 Oct 2026 (condor_shadow_v3_prereg.md).
- Judge them only on new trades: at least 30 trades, PF ≥ 1.15 and P < 0.10.
- Weekly expiries on NIFTY plus SENSEX give about 2 trades a week, so the first verdict comes in roughly 4 months.
- No live money on the condor before then.
