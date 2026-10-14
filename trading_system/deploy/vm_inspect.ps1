<#
Read-only snapshot of the Azure VM for review. Never prints secret values (only .env key NAMES).
  .\deploy\vm_inspect.ps1        (defaults to the trading-recorder-vm and your azure_vm.key)
Writes data\botv1_live\logs\vm_inspect.txt
#>
param(
    [string] $VmHost = "azureuser@172.198.152.213",
    [string] $KeyFile = "$env:USERPROFILE\.ssh\azure_vm.key"
)
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "data\botv1_live\logs\vm_inspect.txt"
New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null
$sshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=accept-new")
$remote = @'
cd ~/trading_system 2>/dev/null || { echo "NO ~/trading_system"; exit 0; }
echo "== host: $(hostname)  time: $(date '+%Y-%m-%d %H:%M %Z')  uptime: $(uptime -p)"
echo "== os: $(. /etc/os-release; echo $PRETTY_NAME)  python: $(.venv/bin/python -V 2>&1)"
echo "== cpu/mem/disk"; nproc; free -m | sed -n 1,3p; df -h ~ | sed -n 2p
echo "== public IP: $(curl -s --max-time 5 https://api.ipify.org)"
echo "== crontab"; crontab -l 2>&1 | grep -v '^#' | grep -v '^$'
echo "== top-level"; ls -la | head -50
echo "== bot_v1 present?"; ls bot_v1 2>&1 | head
echo "== .env keys (names only)"; sed -n 's/=.*//p' .env 2>/dev/null | sort | tr '\n' ' '; echo
echo "== upstox token files"; ls -la --time-style=+%F_%T .env.upstox_token data/.upstox_token 2>&1
echo "== data/"; ls -la --time-style=+%F_%T data | head -40
echo "== capture cycles per day (last 10 days)"
.venv/bin/python - <<'PY'
import sqlite3
con = sqlite3.connect("file:data/options.db?mode=ro", uri=True)
for r in con.execute("SELECT trade_date, underlying, COUNT(DISTINCT cycle_id), COUNT(*) FROM snapshots WHERE trade_date >= date('now','-10 day') GROUP BY 1,2 ORDER BY 1 DESC, 2"):
    print("  ", *r)
PY
echo "== upstox token check (HTTP status only)"
.venv/bin/python - <<'PY'
import os, re, requests, pathlib
tok = ""
for f in (".env.upstox_token", ".env"):
    p = pathlib.Path(f)
    if p.exists():
        m = re.search(r'^UPSTOX_ACCESS_TOKEN=["\']?([^"\'\n]+)', p.read_text(), re.M)
        if m: tok = m.group(1); print("   token from", f); break
if not tok: print("   no token found")
else:
    r = requests.get("https://api.upstox.com/v2/market-quote/ltp", params={"instrument_key": "NSE_INDEX|Nifty 50"},
                     headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"}, timeout=10)
    print("   HTTP", r.status_code, (r.json().get("status") if r.headers.get("content-type","").startswith("application/json") else ""))
PY
echo "== recent errors"; grep -h -E "ERROR|CRITICAL|Traceback" logs/errors.log logs/cron_*.log 2>/dev/null | tail -n 15
echo "== cron log tails"; for f in logs/cron_*.log; do echo "-- $f"; tail -n 4 "$f"; done
echo "== python processes"; pgrep -af python | head
'@
$remote | ssh @sshOpts $VmHost "tr -d '\r' | bash -s" *> $out
Get-Content $out
Write-Host "`nSaved to $out"
