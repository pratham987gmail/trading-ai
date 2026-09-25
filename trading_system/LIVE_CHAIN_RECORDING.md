# Live option chain recording (SENSEX + NIFTY)

Windows Scheduled Tasks, not long-running background processes, record
SENSEX and NIFTY option chains during market hours, and a third task
re-runs the credit-spread replay weekly. This note exists so a future
session (or you, months from now) can find them, check on them, or rebuild
them if the machine changes.

## Why scheduled tasks, not a background process

The first attempt used a plain background process
(`scripts/record_chains.py`, long-running, its own internal 300s sleep
loop). It died silently when the session/machine environment reset,
taking hours of otherwise-unrecoverable market data with it (chain
snapshots cannot be reconstructed after the fact once an expiry passes).

The tasks use `--once` instead: each invocation captures exactly one cycle
and exits. The Windows Scheduler provides the recurring trigger.

## What's registered

| Task | Command (from `trading_system\`) | Trigger |
|---|---|---|
| `SensexChainRecorder` | `scripts\record_chains.py --symbols SENSEX --exchange BFO --expiries 7 --strike-count 45 --once` | Mon-Fri, every 5 min, 09:15-15:30 IST |
| `NiftyChainRecorder` | `scripts\record_chains.py --symbols NIFTY --exchange NFO --expiries 7 --strike-count 35 --once` | Mon-Fri, every 5 min, 09:17-15:30 IST |
| `CreditSpreadWeeklyReport` | `-m research.backtest.credit_spread_live_study` | Saturday 10:00 |

All three have `WakeToRun` on, run on battery, and `StartWhenAvailable`
(a missed run fires once the machine is back). Wake-to-run also needs
"Allow wake timers" enabled in the Windows power plan, which is a system
setting left to the machine owner.

- **Two recorder tasks, offset 2 minutes.** `RecorderConfig.exchange` is a
  single value per invocation, so NFO (NIFTY) and BFO (SENSEX) can't share
  one process. The offset keeps both from hitting Angel One's quote rate
  limit at the same instant.
- **Market hours are enforced twice.** The trigger window is the session
  itself, and the script checks `core.market_hours.is_market_open()` and
  records nothing outside 09:15-15:30 (holidays included).
- **`--expiries 7`** reaches the 30-45 DTE range the credit-spread test
  needs. The legacy recorder never captured anything past ~10 DTE.
- **`--strike-count`** was widened on 2026-09-25 from the default 20 (which
  covered only about +-2.7% on SENSEX and +-4.4% on NIFTY) to 45 / 35
  (+-6.1% / +-7.6%). A 30-45 DTE credit spread sells about 1 standard
  deviation out, roughly 4-5%, which the old range often didn't reach.
  A SENSEX cycle takes about 34 seconds at this width.
- Writes to `data/options.db` via `core.option_store.OptionChainStore`,
  which has real bid/ask. This is not the legacy `data/option_chains.db`
  NIFTY dataset that `chain_replay.py` reads.

## Liquidity finding that shapes what this data can test

Measured on 2026-09-25 (share of recorded legs with both a bid and an ask):

- **NIFTY:** 99-100% on the 30-45 DTE expiries except 03NOV (60%).
- **SENSEX:** only the monthly expiry is quoted that far out (29OCT at 34
  DTE: 95%). The far weeklies are empty: 22OCT 15%, 05NOV 0%, 12NOV 0%.

So SENSEX offers about one 30-45 DTE credit-spread entry per month per
side, while NIFTY offers roughly one per week. The credit-spread sample
will come mostly from NIFTY.

## Weekly report

`research/reports/credit_spread_live/latest.md` (plus a dated `.md` and
`.json` per run). The verdict reads `INSUFFICIENT_SAMPLE (n/30)` until 30
trades have closed; after that it's `PASS` (positive expectancy and
profit factor >= 1.3 after costs) or `FAIL`. See the module docstring in
`research/backtest/credit_spread_live_study.py` for the exact rules.

## A real gotcha hit while setting this up

`New-ScheduledTaskSettingsSet`'s default is
`DisallowStartIfOnBatteries = $true`. On a laptop on battery this silently
skips every recurrence after the first. The only symptom is a rising
`NumberOfMissedRuns`. It's disabled on all three tasks. If data stops
accumulating, check that first.

## Checking on it

```powershell
foreach ($n in "SensexChainRecorder","NiftyChainRecorder","CreditSpreadWeeklyReport") {
  Get-ScheduledTaskInfo -TaskName $n | Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime, NumberOfMissedRuns
}
```

`LastTaskResult` of `0` means a clean exit, which includes a correct no-op
outside market hours. To see what was actually captured per day:

```python
import sqlite3
con = sqlite3.connect("data/options.db")
for row in con.execute(
    "SELECT underlying, trade_date, COUNT(DISTINCT cycle_id), ROUND(AVG(completeness),3) "
    "FROM snapshots GROUP BY underlying, trade_date ORDER BY trade_date DESC LIMIT 20"
):
    print(row)
```

A full day is 75 cycles per underlying. Days well below that are gaps
(sleep, Wi-Fi drops, Angel One timeouts; see `logs/errors.log`).

## Rebuilding (if the machine or repo path changes)

```powershell
$root = '<repo>\trading_system'
$py   = "$root\.venv\Scripts\python.exe"
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4)

function Add-Recorder($name, $arguments, $start, $duration) {
  $action  = New-ScheduledTaskAction -Execute $py -Argument $arguments -WorkingDirectory $root
  $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $start
  $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration $duration).Repetition
  Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings
}
Add-Recorder "SensexChainRecorder" '"scripts\record_chains.py" --symbols SENSEX --exchange BFO --expiries 7 --strike-count 45 --once' "09:15" (New-TimeSpan -Hours 6 -Minutes 15)
Add-Recorder "NiftyChainRecorder"  '"scripts\record_chains.py" --symbols NIFTY --exchange NFO --expiries 7 --strike-count 35 --once' "09:17" (New-TimeSpan -Hours 6 -Minutes 13)

$report = New-ScheduledTaskAction -Execute $py -Argument "-m research.backtest.credit_spread_live_study" -WorkingDirectory $root
Register-ScheduledTask -TaskName "CreditSpreadWeeklyReport" -Action $report -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At "10:00") `
  -Settings (New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30))
```
