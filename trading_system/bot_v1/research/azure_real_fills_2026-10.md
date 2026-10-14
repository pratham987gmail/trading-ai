# Condor on real recorder bid/ask (Azure VM data, Sep 28 – Oct 13 2026)

Bot code (plan_condor / settle / charges) on the 15:05-15:20 snapshot of each expiry-eve, real bid/ask,
settled on the last expiry-day snapshot. VIX was 13.5-15.3 on every eve, so the RULE SAID NO TRADE on
all five; the strikes below were computed with 18% vol purely to measure fills. Lot: NIFTY 65, SENSEX 20.

| underlying | eve | credit at real bid/ask | at mid | spread cost (all 4 legs) | max loss | net pts | R | ₹ (1 lot) |
|---|---|---|---|---|---|---|---|---|
| NIFTY | 2026-09-28 | 23.60 | 23.70 | 0.10 | 176.4 | +22.1 | +0.13 | +1,436 |
| NIFTY | 2026-10-05 | 26.20 | 26.32 | 0.12 | 173.8 | +24.7 | +0.14 | +1,605 |
| NIFTY | 2026-10-12 | 21.25 | 21.40 | 0.15 | 178.8 | -132.3 | -0.74 | -8,600 |
| SENSEX | 2026-09-30 | 87.05 | 87.32 | 0.27 | 613.0 | +82.2 | +0.13 | +1,643 |
| SENSEX | 2026-10-07 | 72.20 | 72.42 | 0.22 | 627.8 | -371.0 | -0.59 | -7,421 |

Real round-trip spread cost per condor: 0.10-0.27 pts. The backtest's protocol spread charges
~1.2 pts per NIFTY condor, i.e. ~0.006R/trade too pessimistic. Too few trades to say anything about edge.
