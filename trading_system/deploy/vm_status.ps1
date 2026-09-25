<#
One-screen health check of the cloud recorder.

  .\deploy\vm_status.ps1 -VmHost ubuntu@1.2.3.4 -KeyFile C:\Users\you\.ssh\gcp_vm.key

A full trading day is 75 capture cycles per underlying.
#>
param(
    [Parameter(Mandatory)] [string] $VmHost,
    [Parameter(Mandatory)] [string] $KeyFile
)
$sshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=accept-new")

$remote = @'
cd ~/trading_system
echo "== VM time: $(date '+%Y-%m-%d %H:%M %Z')   disk free: $(df -h ~ | awk 'NR==2 {print $4}')"
echo "== options.db: $(du -h data/options.db 2>/dev/null | cut -f1 || echo 'none yet')   historical_bars.db: $(du -h data/historical_bars.db 2>/dev/null | cut -f1 || echo 'none yet')"
echo "== capture cycles per day (last 7 days, 75 = full day)"
.venv/bin/python - <<'PY'
import sqlite3
con = sqlite3.connect("data/options.db")
for row in con.execute(
    "SELECT trade_date, underlying, COUNT(DISTINCT cycle_id), ROUND(AVG(completeness),3) FROM snapshots "
    "WHERE trade_date >= date('now','-7 day') GROUP BY trade_date, underlying ORDER BY trade_date DESC, underlying"
):
    print("  ", *row)
PY
echo "== Upstox backfill coverage (top 5 expiries per underlying)"
.venv/bin/python - <<'PY'
import sqlite3
try:
    con = sqlite3.connect("data/historical_bars.db")
    for row in con.execute(
        "SELECT underlying, COUNT(DISTINCT expiry), MAX(expiry) FROM bars WHERE expiry IS NOT NULL GROUP BY underlying"
    ):
        print("  ", *row)
except sqlite3.OperationalError:
    print("   (no historical_bars.db yet -- backfill hasn't run here)")
PY
echo "== recent errors (incl. Upstox backfill)"
grep -h -E "ERROR|CRITICAL|Traceback" logs/errors.log logs/cron_*.log 2>/dev/null | tail -n 8
echo "== latest weekly report"
head -n 12 research/reports/credit_spread_live/latest.md 2>/dev/null || echo "   (none yet; first one runs Saturday 10:00 IST)"
'@
# Sent over stdin: Windows PowerShell mangles double quotes in native-command
# arguments, and the tr strips the CRLFs it adds when piping.
$remote | ssh @sshOpts $VmHost "tr -d '\r' | bash -s"
