<#
Removes the bot from the Azure VM: crontab lines for the bot + its weekly report are dropped and the bot's data is
MOVED into ~/_to_delete_bot_<time> on the VM (nothing is deleted). The Angel One / Upstox recorders, weekly
backfills and their databases keep running untouched.
  powershell -ExecutionPolicy Bypass -File .\deploy\vm_remove_bot.ps1
Uses two ssh/scp connections (the VM stalls on bursts). Log: data\vm_remove_bot.txt
#>
param(
    [string] $VmHost = "azureuser@172.198.152.213",
    [string] $KeyFile = "$env:USERPROFILE\.ssh\azure_vm.key"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "data\vm_remove_bot.txt"
Start-Transcript -Path $log -Force | Out-Null
try {
    $sshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=25",
                 "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=4")
    Push-Location $root
    Write-Host "1/2 copying the clean-up script to the VM"
    scp @sshOpts "deploy/vm_remove_bot.sh" "${VmHost}:~/vm_remove_bot.sh"
    Pop-Location
    Start-Sleep -Seconds 8
    Write-Host "2/2 running it on the VM"
    ssh @sshOpts $VmHost "bash ~/vm_remove_bot.sh"
    Write-Host "DONE"
} finally { Stop-Transcript | Out-Null }
