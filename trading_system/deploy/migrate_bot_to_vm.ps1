<#
Moves bot_v1 (paper) onto the Azure VM and turns on the Upstox live recorder there.
  powershell -ExecutionPolicy Bypass -File .\deploy\migrate_bot_to_vm.ps1
Steps: 1) deploy code + .env + crontab (existing deploy_to_vm.ps1; data/ on the VM is untouched)
       2) seed the VM with the laptop's bot state (data\botv1_live) unless the VM already has it
       3) copy today's Upstox token   4) smoke-test on the VM
       5) switch the laptop's scheduled tasks to VM mode (login + token push, evening pull)
Log: data\botv1_live\logs\migrate.txt
#>
param(
    [string] $VmHost = "azureuser@172.198.152.213",
    [string] $KeyFile = "$env:USERPROFILE\.ssh\azure_vm.key"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "data\botv1_live\logs\migrate.txt"
Start-Transcript -Path $log -Force | Out-Null
try {
    $sshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=accept-new")
    Write-Host "== 1/5 deploy code, .env and crontab"
    & (Join-Path $PSScriptRoot "deploy_to_vm.ps1") -VmHost $VmHost -KeyFile $KeyFile

    Write-Host "== 2/5 seed bot state"
    $has = ssh @sshOpts $VmHost "test -f ~/trading_system/data/botv1_live/journal.sqlite && echo yes || echo no"
    if ($has.Trim() -eq "no") {
        $gnuTar = "C:\Program Files\Git\usr\bin\tar.exe"
        $env:PATH = "C:\Program Files\Git\usr\bin;$env:PATH"
        Push-Location (Join-Path $root "data")
        try { & $gnuTar -czf botv1_seed.tgz --exclude=STATUS.txt botv1_live; if ($LASTEXITCODE -ne 0) { throw "tar failed" } } finally { Pop-Location }
        $seed = Join-Path $root "data\botv1_seed.tgz"
        scp @sshOpts $seed "${VmHost}:~/trading_system/data/botv1_seed.tgz"
        ssh @sshOpts $VmHost "cd ~/trading_system/data && tar --mode=u+rwx -xzf botv1_seed.tgz && rm botv1_seed.tgz"
        Remove-Item $seed -Force
        Write-Host "seeded data/botv1_live on the VM"
    } else { Write-Host "VM already has data/botv1_live; not overwriting" }

    Write-Host "== 3/5 Upstox token"
    & (Join-Path $PSScriptRoot "push_upstox_token.ps1") -VmHost $VmHost -KeyFile $KeyFile

    Write-Host "== 4/5 smoke test on the VM"
    $remote = @'
cd ~/trading_system
export BOTV1_DATA=data/botv1_live
.venv/bin/python -m pytest tests/bot_v1 -q -p no:cacheprovider 2>&1 | tail -1
.venv/bin/python -m bot_v1.run check
.venv/bin/python -m bot_v1.recorder_upstox && .venv/bin/python -m bot_v1.recorder_upstox --status
.venv/bin/python -m bot_v1.run report
echo "public IP: $(curl -s --max-time 5 https://api.ipify.org)"
crontab -l | grep -E "bot_v1|record_chains" | sed 's/^/  cron: /'
'@
    $remote | ssh @sshOpts $VmHost "tr -d '\r' | bash -s"

    Write-Host "== 5/5 laptop scheduled tasks -> VM mode"
    & (Join-Path $root "bot_v1\scheduler\install_tasks.ps1") -Mode Vm
    Write-Host "`nMIGRATION COMPLETE"
}
catch { Write-Host "MIGRATION FAILED: $_" }
finally { Stop-Transcript | Out-Null }
