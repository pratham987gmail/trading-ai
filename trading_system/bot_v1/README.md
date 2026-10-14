# bot_v1 — Momentum30 (paper) + expiry-eve condor (shadow)

Built on the October 2026 research. Only Momentum30 + NIFTY trend filter passed a
pre-registered design (2006-16) / holdout (2017-26) test. The condor did not pass,
so it is shadow-only: it logs what it would trade at real bid/ask and never orders.

## One-time setup (run from the `trading_system` folder)
1. Download the NIFTY 200 constituent list from niftyindices.com (`ind_nifty200list.csv`)
   and save it as `data/bot_v1/ind_nifty200list.csv`. Refresh it when NSE reconstitutes
   the index (March and September).
2. Make sure `.env` has `UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET`, `UPSTOX_REDIRECT_URI`.
3. Run the tests: `.venv\Scripts\python.exe -m pytest tests/bot_v1 -q`

## Every trading day
- Morning: `.venv\Scripts\python.exe -m research.cli.upstox_login` (tokens expire daily)
- After 15:30: `python -m bot_v1.run mark` (records paper equity vs NIFTY)

## Schedule
| When | Command |
|---|---|
| First trading day of each month, ~15:00 | `python -m bot_v1.run momentum-rebalance` (asks you to type YES) |
| Day before NIFTY weekly expiry, ~15:10 | `python -m bot_v1.run condor-scan` |
| Expiry day after 16:00 (or any later day) | `python -m bot_v1.run condor-settle` |
| Any time | `python -m bot_v1.run report` / `momentum-signal` / `check` |

## Modes and safety
- Default `BOTV1_MODE=PAPER`: fills are simulated at the current last price and journaled
  in `data/bot_v1/journal.sqlite`.
- LIVE (`BOTV1_MODE=LIVE`) additionally requires `BOTV1_STATIC_IPS=primary,backup` registered
  with Upstox, and the machine's public IP must match; orders are LIMIT only, carry the
  `X-Algo-Name` header, are throttled to 2/s, and every batch needs you to type YES.
  Do not switch to LIVE until 3+ months of paper results are reviewed.
- The condor's promotion rule (`report.py`): >= 50 settled shadow trades, profit factor
  >= 1.15 net of charges, and bootstrap P(avg R <= 0) < 10%. Only then consider paper/live.

## Known deviations from the research backtest (all small, documented)
- Integer shares on ₹1 lakh: very expensive stocks (1 share > ~₹3,300) are skipped.
- 2% cash reserve for charges/limit buffers; kept holdings are only resized if >20% off target.
- Symbols with a >20% one-day move in the past year are excluded and flagged (possible
  unadjusted split/bonus) — check them manually.
- Pick parity with the research on real data 2017-26: 98.5% top-30 overlap, trend filter 39/39.
