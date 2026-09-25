<#
Pulls only NEW rows from the VM's live-recorder database into
data\cloud\options.db, a full copy of its Upstox historical-backfill
database into data\cloud\historical_bars.db, plus the latest weekly
reports. It never overwrites the laptop's own data\options.db or
data\historical_bars.db.

  .\deploy\pull_from_vm.ps1 -VmHost azureuser@1.2.3.4 -KeyFile C:\Users\you\.ssh\azure_vm.key

options.db is pulled incrementally (deploy\db_sync.py exports and merges
only rows newer than the last pull) because it grows fast: ~5-6 MB/day
compressed at full capture. historical_bars.db grows only on the weekly
Upstox backfill cron job and stays small (a few MB), so a plain full copy
each time is simpler and the bandwidth cost is negligible.
#>
param(
    [Parameter(Mandatory)] [string] $VmHost,
    [Parameter(Mandatory)] [string] $KeyFile
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$sshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=accept-new")
$py = Join-Path $root ".venv\Scripts\python.exe"
$localDb = Join-Path $root "data\cloud\options.db"
$localReports = Join-Path $root "research\reports\credit_spread_live_cloud"
New-Item -ItemType Directory -Force (Split-Path $localDb), $localReports | Out-Null

$mark = (& $py (Join-Path $root "deploy\db_sync.py") since $localDb).Trim() -split " "
$sinceId, $sinceDate = $mark[0], $mark[1]
Write-Host "== requesting rows after snapshot $sinceId, cycles from $sinceDate"

$remoteGz = "~/trading_system/data/increment.db.gz"
ssh @sshOpts $VmHost "cd ~/trading_system && .venv/bin/python deploy/db_sync.py export $sinceId $sinceDate $remoteGz"
if ($LASTEXITCODE -ne 0) { throw "remote export failed" }

$localGz = Join-Path $env:TEMP "increment.db.gz"
scp @sshOpts "${VmHost}:$remoteGz" $localGz
ssh @sshOpts $VmHost "rm -f $remoteGz"

$sizeKB = [math]::Round((Get-Item $localGz).Length / 1KB, 0)
Write-Host "== downloaded increment: $sizeKB KB"
& $py (Join-Path $root "deploy\db_sync.py") merge $localGz $localDb
Remove-Item $localGz -Force

scp @sshOpts -r "${VmHost}:~/trading_system/research/reports/credit_spread_live/*" $localReports 2>$null

$totalMB = [math]::Round((Get-Item $localDb).Length / 1MB, 1)
Write-Host "`ndata\cloud\options.db is now $totalMB MB; reports refreshed in $localReports"

Write-Host "`n== pulling historical_bars.db (Upstox backfill, full copy)"
$localBars = Join-Path $root "data\cloud\historical_bars.db"
$remoteBarsExists = ssh @sshOpts $VmHost "test -f ~/trading_system/data/historical_bars.db && echo yes || echo no"
if ($remoteBarsExists.Trim() -eq "yes") {
    $remoteExport = "~/trading_system/data/historical_bars_export.db"
    ssh @sshOpts $VmHost "cd ~/trading_system && .venv/bin/python deploy/db_sync.py backup data/historical_bars.db data/historical_bars_export.db"
    scp @sshOpts "${VmHost}:$remoteExport" $localBars
    ssh @sshOpts $VmHost "rm -f $remoteExport"
    $barsMB = [math]::Round((Get-Item $localBars).Length / 1MB, 1)
    Write-Host "data\cloud\historical_bars.db is now $barsMB MB"
} else {
    Write-Host "VM has no historical_bars.db yet (Upstox backfill hasn't run there)"
}

Write-Host "`nRun the study on it locally with:"
Write-Host "  .venv\Scripts\python.exe -m research.backtest.credit_spread_live_study --db data\cloud\options.db"
