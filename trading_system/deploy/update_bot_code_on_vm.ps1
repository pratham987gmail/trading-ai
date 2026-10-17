<#
Copies ONLY the changed bot_v1 files to the Azure VM (nothing else is touched: no data, logs or
crontab), keeps a .bak of each old file, then runs the bot test suite on the VM.
  powershell -ExecutionPolicy Bypass -File .\deploy\update_bot_code_on_vm.ps1
Log: data\botv1_live\logs\update_code.txt

Uses only FOUR ssh/scp connections (the VM firewall stalls bursts of connections) and times out
instead of hanging: ConnectTimeout + ServerAlive options.
#>
param(
    [string] $VmHost = "azureuser@172.198.152.213",
    [string] $KeyFile = "$env:USERPROFILE\.ssh\azure_vm.key",
    [string[]] $BotFiles = @("run.py", "config.py", "momentum.py", "monitor.py", "advisor_tauric.py"),
    [string[]] $TestFiles = @("test_bot_v1.py")
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "data\botv1_live\logs\update_code.txt"
Start-Transcript -Path $log -Force | Out-Null
try {
    $sshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=25",
                 "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=4")
    $stamp = Get-Date -Format "yyyyMMdd_HHmm"
    $bak = ($BotFiles | ForEach-Object { "test -f bot_v1/$_ && cp bot_v1/$_ bot_v1/$_.bak_$stamp" }) -join "; "
    $bak += "; test -f tests/bot_v1/test_bot_v1.py && cp tests/bot_v1/test_bot_v1.py tests/bot_v1/test_bot_v1.py.bak_$stamp; true"
    Write-Host "1/4 backing up old files on the VM"
    ssh @sshOpts $VmHost "cd ~/trading_system && ($bak)"
    Start-Sleep -Seconds 8
    Write-Host "2/4 copying bot_v1 files"
    Push-Location $root          # relative paths: scp reads "C:\..." as a remote host called "C"
    $src = @($BotFiles | ForEach-Object { "bot_v1/$_" })
    scp @sshOpts @src "${VmHost}:~/trading_system/bot_v1/"
    Start-Sleep -Seconds 8
    Write-Host "3/4 copying tests"
    $tsrc = @($TestFiles | ForEach-Object { "tests/bot_v1/$_" })   # @() keeps a single path from being splatted as characters
    scp @sshOpts @tsrc "${VmHost}:~/trading_system/tests/bot_v1/"
    Pop-Location
    Start-Sleep -Seconds 8
    $remote = @'
cd ~/trading_system
md5sum bot_v1/run.py bot_v1/config.py bot_v1/momentum.py bot_v1/monitor.py bot_v1/advisor_tauric.py tests/bot_v1/test_bot_v1.py
.venv/bin/python -m pytest tests/bot_v1 -q -p no:cacheprovider 2>&1 | tail -1
grep -o '"nv[0-9a-z]*"' bot_v1/run.py | sort -u | tr '\n' ' '; echo "<- v3 variants present"
'@
    Write-Host "4/4 checking files and running tests on the VM"
    ssh @sshOpts $VmHost $remote
    Write-Host "DONE"
} finally { Stop-Transcript | Out-Null }
