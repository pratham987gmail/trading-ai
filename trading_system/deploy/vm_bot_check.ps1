<#
Checks bot_v1 + the Upstox recorder on the Azure VM and copies today's token there.
Writes data\botv1_live\logs\vm_bot_check.txt
#>
param(
    [string] $VmHost = "azureuser@172.198.152.213",
    [string] $KeyFile = "$env:USERPROFILE\.ssh\azure_vm.key"
)
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "data\botv1_live\logs\vm_bot_check.txt"
& (Join-Path $PSScriptRoot "push_upstox_token.ps1") -VmHost $VmHost -KeyFile $KeyFile *> $out
$sshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=accept-new")
$remote = @'
cd ~/trading_system
export BOTV1_DATA=data/botv1_live
echo "== time $(date '+%F %T %Z')  IP $(curl -s --max-time 5 https://api.ipify.org)"
echo "== files"; ls bot_v1 | tr '\n' ' '; echo; ls -la data/botv1_live | head; ls -la .env.upstox_token 2>&1
echo "== tests"; .venv/bin/python -m pytest tests/bot_v1 -q -p no:cacheprovider 2>&1 | tail -2
echo "== check"; .venv/bin/python -m bot_v1.run check 2>&1 | tail -3
echo "== upstox recorder"; .venv/bin/python -m bot_v1.recorder_upstox 2>&1 | tail -3; .venv/bin/python -m bot_v1.recorder_upstox --status 2>&1 | tail -5
echo "== report"; .venv/bin/python -m bot_v1.run report 2>&1 | tail -8
echo "== cron"; crontab -l | grep -E "bot_v1|record_chains" 
echo "== backups"; ls -la data/pre_migrate_backup_* 2>&1
'@
$remote | ssh @sshOpts $VmHost "tr -d '\r' | bash -s" *>> $out
Get-Content $out
