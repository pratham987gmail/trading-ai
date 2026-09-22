# Live option chain recording (SENSEX)

A Windows Scheduled Task, not a long-running background process, records
SENSEX option chains during market hours. This note exists so a future
session (or you, months from now) can find it, check on it, or rebuild it
if the machine changes.

## Why a scheduled task, not a background process

The first attempt used a plain background process
(`scripts/record_chains.py`, long-running, its own internal 300s sleep
loop). It died silently when the session/machine environment reset,
taking hours of otherwise-unrecoverable market data with it (chain
snapshots cannot be reconstructed after the fact once an expiry passes).

The task now uses `--once` instead: each invocation captures exactly one
cycle and exits immediately — there is no long-lived process to die. The
Windows Scheduler itself provides the recurring trigger, independent of
any interactive session, login state, or this Claude Code session staying
open.

## What's registered

- **Task name:** `SensexChainRecorder` (Task Scheduler, "Ready" state,
  visible in `taskschd.msc` or via `Get-ScheduledTask -TaskName
  SensexChainRecorder` in PowerShell).
- **Command:** `.venv\Scripts\python.exe scripts\record_chains.py
  --symbols SENSEX --exchange BFO --expiries 7 --once`, working directory
  `trading_system\`.
- **Trigger:** weekdays (Mon-Fri), every 5 minutes from 09:00 to 16:00
  IST. Outside market hours the script checks the session state, logs a
  one-line reason, and exits — no capture, no error, negligible cost
  (a single Angel One login handshake).
- **`--expiries 7`**: tracks the 7 nearest SENSEX expiries, reaching out
  to the 30-45 day range (confirmed against the real expiry list on
  2026-09-22: 22OCT/29OCT/05NOV2026 land at 31/38/45 days). The default of
  3 would only ever reach ~17 days — see
  `research/reports/chain_replay_NIFTY_iron_condor_findings.md` for why
  that mattered: the equivalent NIFTY condor test could only use 7-10 DTE
  because the legacy recorder never captured anything past 10 days.
- Writes to `data/options.db` via `core.option_store.OptionChainStore` —
  the current recorder schema (distinct from the legacy
  `recorder.storage.ChainStorage` / `data/option_chains.db` NIFTY dataset
  `chain_replay.py` reads).

## A real gotcha hit while setting this up

`New-ScheduledTaskSettingsSet`'s default is
`DisallowStartIfOnBatteries = $true`. On a laptop running on battery, this
silently skips every recurrence after the first — no error, just a rising
`NumberOfMissedRuns` count on `Get-ScheduledTaskInfo`. **This was disabled
on the registered task** (`DisallowStartIfOnBatteries` and
`StopIfGoingOnBatteries` both set to `$false`), confirmed by watching a
real recurrence fire unattended at its scheduled minute. If this task
stops accumulating data again, check `NumberOfMissedRuns` and these two
settings first, before assuming the script itself is broken.

## Checking on it

```powershell
Get-ScheduledTaskInfo -TaskName "SensexChainRecorder" | Format-List LastRunTime, LastTaskResult, NextRunTime, NumberOfMissedRuns
```

`LastTaskResult` of `0` means the last run exited cleanly (which includes
a correct no-op outside market hours — check the coverage query below to
see whether it actually captured anything).

```python
import sqlite3
con = sqlite3.connect("data/options.db")
cur = con.cursor()
cur.execute(
    "SELECT captured_at, expiry, completeness FROM snapshots "
    "WHERE underlying='SENSEX' ORDER BY captured_at DESC LIMIT 10"
)
for row in cur.fetchall():
    print(row)
```

Or via the recorder's own CLI: `python scripts/record_chains.py --coverage`
(reads `data/options.db`, the current schema — not the legacy NIFTY one).

## Rebuilding it (if the machine or repo path changes)

```powershell
$pythonExe = '<repo>\trading_system\.venv\Scripts\python.exe'
$workDir   = '<repo>\trading_system'
$action  = New-ScheduledTaskAction -Execute $pythonExe -Argument '"scripts\record_chains.py" --symbols SENSEX --exchange BFO --expiries 7 --once' -WorkingDirectory $workDir
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '09:00'

# Repetition must be built as a proper CIM instance and assigned wholesale -
# $trigger.Repetition.Interval = "PT5M" fails silently otherwise.
$repClass = Get-CimClass -ClassName MSFT_TaskRepetitionPattern -Namespace Root/Microsoft/Windows/TaskScheduler
$rep = New-CimInstance -CimClass $repClass -ClientOnly
$rep.Interval = "PT5M"; $rep.Duration = "PT7H"; $rep.StopAtDurationEnd = $false
$trigger.Repetition = $rep

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -DontStopOnIdleEnd
$settings.DisallowStartIfOnBatteries = $false
$settings.StopIfGoingOnBatteries = $false

Register-ScheduledTask -TaskName "SensexChainRecorder" -Action $action -Trigger $trigger -Settings $settings -Description "Captures one SENSEX option-chain snapshot cycle (7 expiries) every 5 minutes, weekdays 09:00-16:00 IST."
```

## Extending to NIFTY / BANKNIFTY too

Add a second task the same way with `--symbols NIFTY BANKNIFTY --exchange
NFO` (NFO is the default, so `--exchange` can be omitted for that one).
Keep it as a separate task rather than combining symbols across exchanges
in one command — `RecorderConfig.exchange` is a single value applied to
every symbol in one invocation (see `core/chain_recorder.py`), so NFO and
BFO underlyings can't share one recorder process/task.
