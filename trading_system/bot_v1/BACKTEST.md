# bot_v1 historical test (run 14 Oct 2026)

`python -m bot_v1.backtest` — the bot's own code (decide → plan_orders → risk_check → Journal),
not the research engine, on point-in-time NSE bhavcopy data 2006-01 → 2026-10.
Fills at the next session's close after each month-end decision; integer shares; 2% cash
reserve; Upstox delivery charges (₹20/order, ₹20 DP per stock sold, STT 0.1% both sides,
stamp, exchange, SEBI, 18% GST). NIFTY = price index (NIFTYBEES spliced before 2010).
Not included: income tax (short-term gains 20% for holdings < 1 year), dividends.

## Momentum30 + trend filter

| Run | 2006-16 /yr | worst fall | 2017-26 /yr | worst fall | 2006-26 /yr |
|---|---|---|---|---|---|
| Upstox ₹1 lakh (bot as built) | 13.0% | -42.8% | 14.3% | -26.4% | 13.7% |
| Upstox ₹1 lakh, no 20%-move filter | 13.8% | -45.0% | 13.5% | -28.1% | 13.7% |
| Zero-brokerage broker ₹1 lakh | 14.6% | -42.5% | 15.0% | -26.1% | 14.9% |
| Upstox ₹5 lakh | 15.4% | -46.6% | 15.3% | -26.9% | 15.4% |
| Upstox ₹10 lakh | 15.8% | -46.2% | 15.4% | -27.0% | 15.7% |
| NIFTY 50 (price) | 10.0% | -59.7% | 11.2% | -38.4% | 10.6% |

₹1 lakh → ₹14.4 lakh over 20.8 years (₹1.19 lakh paid in charges). Calendar years
(₹1 lakh, Upstox): 2006 +19.6, 2007 +68.9, 2008 -35.8, 2009 +57.2, 2010 +17.8, 2011 -10.1,
2012 +12.5, 2013 -6.5, 2014 +64.1, 2015 -3.2, 2016 +6.3, 2017 +23.5, 2018 -11.6, 2019 -3.8,
2020 +23.9, 2021 +75.8, 2022 -10.5, 2023 +33.0, 2024 +19.7, 2025 +15.0, 2026 (to Oct) -0.1.
Losing years: 7 of 21.

Parity: with charges off and the 20%-move filter off, the bot's picks overlap the research
29.4/30 per rebalance and returns match (2006-16 17.1% vs 17.4%; 2017-26 15.2% vs 14.9%);
trend filter agrees 249/249 month-ends.

Bugs found and fixed by this test:
1. Rebalances could be blocked: kept holdings inside the 20% resize tolerance are not trimmed,
   so full-size new buys exceeded cash and the risk check rejected the whole rebalance (7 times
   in 20 years at ₹10 lakh). Buys are now scaled to the cash available.
2. (Backtest only) split/bonus detection widened so 5:1 / 10:1 splits are not booked as crashes.

## Expiry-eve condor (shadow)

Bot's plan_condor/settle/charges on EOD chains; quotes = close ∓ (0.25 + 0.3%) (no historical
bid/ask exists). R = net P&L / max loss.

| Variant | Period | Trades | Win | Avg R | PF | P(avg R ≤ 0) |
|---|---|---|---|---|---|---|
| 1σ wings (research B_WSIG) | 2015-21 | 87 | 79% | +0.029 | 1.38 | 0.16 |
| 1σ wings | 2022-26 | 55 | 82% | +0.060 | 1.52 | 0.13 |
| 100-pt wings | 2015-21 | 86 | 79% | +0.019 | 1.18 | 0.30 |
| 100-pt wings | 2022-26 | 55 | 80% | +0.070 | 1.40 | 0.17 |

Matches the research (holdout 55 trades, PF 1.515, P 0.121). Still fails the promotion rule
(needs P < 0.10), so it stays shadow-only.
