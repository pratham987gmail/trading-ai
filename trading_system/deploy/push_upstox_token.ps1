<#
Copies today's Upstox access token to the Azure VM (as ~/trading_system/.env.upstox_token), where the
bot and the Upstox recorder use it. Called by bot_v1\scheduler\login.bat after the morning login.
Source: whichever of .env.upstox_token / .env was written most recently and has UPSTOX_ACCESS_TOKEN.
Only the single UPSTOX_ACCESS_TOKEN line is sent.
#>
param(
    [string] $VmHost = "azureuser@172.198.152.213",
    [string] $KeyFile = "$env:USERPROFILE\.ssh\azure_vm.key"
)
$root = Split-Path -Parent $PSScriptRoot
$src = @(".env.upstox_token", ".env") | ForEach-Object { Join-Path $root $_ } | Where-Object { Test-Path $_ } |
       Where-Object { Select-String -Path $_ -Pattern '^UPSTOX_ACCESS_TOKEN=' -Quiet } |
       Sort-Object { (Get-Item $_).LastWriteTime } -Descending | Select-Object -First 1
if (-not $src) { Write-Host "No UPSTOX_ACCESS_TOKEN found in .env.upstox_token or .env - log in first."; exit 1 }
$line = (Select-String -Path $src -Pattern '^UPSTOX_ACCESS_TOKEN=' | Select-Object -First 1).Line
$tmp = Join-Path $env:TEMP "upstox_token_push.txt"
[IO.File]::WriteAllText($tmp, $line + "`n")
$sshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20")
try {
    scp @sshOpts $tmp "${VmHost}:~/trading_system/.env.upstox_token"
    if ($LASTEXITCODE -ne 0) { Write-Host "Could not copy the token to the VM."; exit 1 }
    ssh @sshOpts $VmHost "chmod 600 ~/trading_system/.env.upstox_token"
    Write-Host "Token from $(Split-Path $src -Leaf) copied to the Azure VM."
} finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
