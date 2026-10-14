<#
Copies the bot's state from the Azure VM to data\botv1_vm\ for review on the laptop:
STATUS.txt, today's logs, a consistent copy of the journal, and the Upstox recorder database.
#>
param(
    [string] $VmHost = "azureuser@172.198.152.213",
    [string] $KeyFile = "$env:USERPROFILE\.ssh\azure_vm.key"
)
$root = Split-Path -Parent $PSScriptRoot
$dst = Join-Path $root "data\botv1_vm"
New-Item -ItemType Directory -Force $dst, (Join-Path $dst "logs") | Out-Null
$sshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20")
ssh @sshOpts $VmHost "cd ~/trading_system && .venv/bin/python deploy/db_sync.py backup data/botv1_live/journal.sqlite data/botv1_live/journal_export.sqlite && .venv/bin/python deploy/db_sync.py backup data/upstox_chains.db data/upstox_chains_export.db"
scp @sshOpts "${VmHost}:~/trading_system/data/botv1_live/STATUS.txt" $dst
scp @sshOpts "${VmHost}:~/trading_system/data/botv1_live/journal_export.sqlite" (Join-Path $dst "journal.sqlite")
scp @sshOpts "${VmHost}:~/trading_system/data/upstox_chains_export.db" (Join-Path $dst "upstox_chains.db")
scp @sshOpts "${VmHost}:~/trading_system/data/botv1_live/logs/*" (Join-Path $dst "logs")
ssh @sshOpts $VmHost "rm -f ~/trading_system/data/botv1_live/journal_export.sqlite ~/trading_system/data/upstox_chains_export.db"
Get-Content (Join-Path $dst "STATUS.txt") -ErrorAction SilentlyContinue | Select-Object -First 25
