<#
Copies ONLY the changed bot_v1 files to the Azure VM (nothing else is touched: no data, logs or
crontab), keeps a .bak of each old file, then runs the bot test suite on the VM.
  powershell -ExecutionPolicy Bypass -File .\deploy\update_bot_code_on_vm.ps1
Log: data\botv1_live\logs\update_code.txt
#>
param(
    [string] $VmHost = "azureuser@172.198.152.213",
    [string] $KeyFile = "$env:USERPROFILE\.ssh\azure_vm.key",
    [string[]] $Files = @("bot_v1/run.py", "tests/bot_v1/test_bot_v1.py")
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "data\botv1_live\logs\update_code.txt"
Start-Transcript -Path $log -Force | Out-Null
try {
    $sshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=accept-new")
    $stamp = Get-Date -Format "yyyyMMdd_HHmm"
    foreach ($f in $Files) {
        ssh @sshOpts $VmHost "cd ~/trading_system && test -f $f && cp $f $f.bak_$stamp || true"
        scp @sshOpts (Join-Path $root $f) "${VmHost}:~/trading_system/$f"
        Write-Host "copied $f"
    }
    $remote = @'
cd ~/trading_system
md5sum bot_v1/run.py tests/bot_v1/test_bot_v1.py
.venv/bin/python -m pytest tests/bot_v1 -q -p no:cacheprovider 2>&1 | tail -1
.venv/bin/python -c "from bot_v1.run import CONDOR_VARIANTS as V; print('variants:', ', '.join(V))"
'@
    ssh @sshOpts $VmHost $remote
    Write-Host "DONE"
} finally { Stop-Transcript | Out-Null }
