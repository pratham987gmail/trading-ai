# bot_v1 — Momentum30 (paper) + expiry-eve condor (shadow)

Built on the October 2026 research. Only Momentum30 + NIFTY trend filter passed a
pre-registered design (2006-16) / holdout (2017-26) test. The condor did not pass,
so it is shadow-only: it logs what it would trade at real bid/ask and never orders.

## One-time setup (run from the `trading_system` folder)
1. Universe: by default the bot builds `universe_top200_stocks.csv` from `data/equities.db`
   (top 200 stocks by 60-day traded value, ETFs/index/liquid/gold/silver funds excluded —
   the research universe). Optionally drop NSE's `ind_nifty200list.csv` in the data folder instead.
2. Make sure `.env` has `UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET`, `UPSTOX_REDIRECT_URI`.
3. Run the tests: `.venv\Scripts\python.exe -m pytest tests/bot_v1 -q`

## Automatic running (Windows Task Scheduler)
Double-click `bot_v1\scheduler\install_tasks.bat` once. It creates three weekday tasks under
`TradingBot` in Task Scheduler:
- 09:10 opens `login.bat` (Upstox requires a fresh login every day — this cannot be automated)
- 15:10 `run_auto.bat` → month-start paper rebalance (auto-approved in PAPER only) + condor scan
- 15:40 `run_auto.bat` → daily mark + condor settlement
Everything is idempotent and checks NSE holidays via Upstox. Output: `data\botv1_live\STATUS.txt`
and `data\botv1_live\logs\`. If the token has expired you get a Windows notification.
The laptop must be on (tasks wake it from sleep) and logged in. Remove with `uninstall_tasks.ps1`.
LIVE mode is never auto-approved: real orders always need you to type YES.

## Every trading day (manual alternative)
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

## Running from a shared/mounted folder
If SQLite reports `disk I/O error` (network or VM-mounted folders without file locking), set
`BOTV1_SQLITE_NOLOCK=1`. Only run one bot command at a time in that mode.

## Known deviations from the research backtest (all small, documented)
- Integer shares on ₹1 lakh: very expensive stocks (1 share > ~₹3,300) are skipped.
- 2% cash reserve for charges/limit buffers; kept holdings are only resized if >20% off target.
- Symbols with a >20% one-day move in the past year are excluded and flagged (possible
  unadjusted split/bonus) — check them manually.
- ETFs/funds excluded. The original backtest let gold/silver/liquid ETFs into the top 30 in some
  months; the stocks-only re-test still passes (holdout 2017-26: 15.2%/yr vs NIFTY 11.1%,
  max drawdown 27.8% vs 38.4%; design 2006-16 max drawdown was 50.4% — 2008).
- Pick parity with the research on real data 2017-26: 98.5% top-30 overlap, trend filter 39/39.
