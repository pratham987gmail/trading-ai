<#
Uploads the code and a minimal .env to the cloud VM, then runs setup_vm.sh.
Re-run it any time the code changes; it is safe to repeat.

  .\deploy\deploy_to_vm.ps1 -VmHost ubuntu@1.2.3.4 -KeyFile C:\Users\you\.ssh\gcp_vm.key
  .\deploy\deploy_to_vm.ps1 -VmHost ubuntu@1.2.3.4 -KeyFile ... -SeedData

-SeedData copies your local data\options.db and data\historical_bars.db to
the VM, but only if the VM doesn't already have each one, so history
recorded or backfilled on the laptop carries over rather than being
re-downloaded from scratch.

The Angel One login, the Upstox credentials (the VM now also runs the
weekly Upstox historical backfill -- see deploy/crontab.txt), and a few
non-secret settings go to the VM. NewsAPI stays on the laptop only; it's
used for the one-off sentiment study, not by anything scheduled.
#>
param(
    [Parameter(Mandatory)] [string] $VmHost,
    [Parameter(Mandatory)] [string] $KeyFile,
    [switch] $SeedData
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$sshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=accept-new")
$remote = "trading_system"


# Every risk-limit field is included, not just capital/per-trade: Settings()
# validates them as a consistent whole at import time (confirmed live -- a
# .env missing MAX_OPEN_POSITIONS/MAX_DAILY_LOSS_PCT fell back to Pydantic
# defaults that don't satisfy the same invariant check the local .env's full
# set does, and record_chains.py failed to start over it). None of this is a
# credential; it's the same risk-limit config already in the tracked
# .env.example, replicated so the VM validates identically to the laptop.
$keep = '^(ANGEL_API_KEY|ANGEL_CLIENT_ID|ANGEL_PASSWORD|ANGEL_TOTP_SECRET|UPSTOX_CLIENT_ID|UPSTOX_CLIENT_SECRET|UPSTOX_ACCESS_TOKEN|UPSTOX_REDIRECT_URI|TOTAL_CAPITAL|RISK_PER_TRADE_PCT|MAX_DAILY_LOSS_PCT|MAX_WEEKLY_LOSS_PCT|MAX_OPEN_POSITIONS|RISK_PROFILE|TRADING_STYLE|MAX_TRADES_PER_DAY|MAX_TRADES_PER_WEEK|MAX_CONSECUTIVE_LOSSES|ORDER_RATE_LIMIT_PER_SEC|ORDER_TAG_PREFIX|WEEKLY_TARGET_PCT|PREFERRED_UNDERLYINGS|AUTONOMY_LEVEL|LOG_LEVEL)='
$tarPath = Join-Path $env:TEMP "trading_system_upload.tgz"
$envPath = Join-Path $env:TEMP "trading_system_cloud.env"

try {
    Write-Host "== packaging code"
    # Uses Git for Windows' GNU tar explicitly, NOT the "tar" on PATH.
    # Windows' own System32\tar.exe (bsdtar-based) writes some directories'
    # archive-header mode as literal 0555 (confirmed live: api/endpoints/,
    # .ruff_cache/** came out dr-xr-xr-x -- an artifact of an NTFS attribute
    # on those directories, not a real permission problem). GNU tar on the
    # Linux side then can't write into a directory it just created at 0555,
    # and no extraction-side flag fixes it (--no-same-permissions and
    # --mode=u+rwx were both tried against that archive and both still
    # failed). Git's GNU tar computes a normal mode for the same directories,
    # confirmed by extracting an archive it built with no errors.
    $gitBin = "C:\Program Files\Git\usr\bin"
    $gnuTar = "$gitBin\tar.exe"
    if (-not (Test-Path $gnuTar)) { throw "Git for Windows' tar.exe not found at $gnuTar -- required to avoid a Windows-tar packaging bug, see comment above" }
    # tar.exe shells out to gzip.exe by name; make sure it's found there too,
    # not wherever the ambient PATH happens to point.
    $env:PATH = "$gitBin;$env:PATH"

    # Top-level entries are listed explicitly: Windows tar's --exclude=data would
    # also drop research/data (a code package), since its patterns aren't anchored.
    $skipTop = @(".venv", "data", "logs", ".env", ".git", ".pytest_cache", "__pycache__")
    $entries = Get-ChildItem -Force $root | Where-Object { $skipTop -notcontains $_.Name } | ForEach-Object Name
    # -f gets a bare relative filename, not an absolute Windows path: this
    # MSYS tar parses "C:/..." or "C:\..." in the ARCHIVE FILENAME argument as
    # a "host:path" remote-shell spec (a real drive letter isn't recognized
    # there, even forward-slashed -- confirmed live, same error both ways).
    # The -C source directory argument is unaffected, so it stays absolute.
    Push-Location (Split-Path $tarPath)
    try {
        & $gnuTar -czf (Split-Path $tarPath -Leaf) -C $root --exclude=__pycache__ --exclude=.pytest_cache --exclude=*.pyc @entries
        if ($LASTEXITCODE -ne 0) { throw "tar failed" }
    } finally { Pop-Location }

    Write-Host "== building minimal .env (Angel One login only, analysis mode)"
    $lines = Get-Content (Join-Path $root ".env") | Where-Object { $_ -match $keep }
    $lines += "TRADING_MODE=ANALYSIS"
    [IO.File]::WriteAllText($envPath, (($lines -join "`n") + "`n"))

    Write-Host "== uploading"
    ssh @sshOpts $VmHost "mkdir -p ~/$remote"
    # A fresh extraction target for the code only (data/ is untouched, so a
    # prior seed or recorded history under ~/trading_system/data survives).
    # chmod first: a directory extracted read-only by a prior failed run (see
    # the --no-same-permissions note below) would otherwise block rm -rf too.
    ssh @sshOpts $VmHost "cd ~/$remote && chmod -R u+rwX . 2>/dev/null; find . -mindepth 1 -maxdepth 1 ! -name data -exec rm -rf {} +"
    scp @sshOpts $tarPath "${VmHost}:~/$remote/upload.tgz"
    scp @sshOpts $envPath "${VmHost}:~/$remote/.env"
    if ($LASTEXITCODE -ne 0) { throw "scp failed" }

    if ($SeedData) {
        # Both databases seed independently: the VM might already have grown
        # its own options.db (live recorder) while never having received a
        # historical_bars.db (Upstox backfill) yet, or vice versa.
        foreach ($dbName in @("options.db", "historical_bars.db")) {
            $localDb = Join-Path $root "data\$dbName"
            if (-not (Test-Path $localDb)) { continue }
            $exists = ssh @sshOpts $VmHost "test -f ~/$remote/data/$dbName && echo yes || echo no"
            if ($exists.Trim() -eq "no") {
                Write-Host "== seeding VM with local data\$dbName (consistent backup copy)"
                $seed = Join-Path $env:TEMP "${dbName}_seed.db"
                & (Join-Path $root ".venv\Scripts\python.exe") -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $localDb $seed
                ssh @sshOpts $VmHost "mkdir -p ~/$remote/data"
                scp @sshOpts $seed "${VmHost}:~/$remote/data/$dbName"
                Remove-Item $seed -Force
            } else {
                Write-Host "== VM already has data/$dbName; not overwriting it"
            }
        }
    }

    Write-Host "== unpacking and running setup on the VM"
    # --mode='u+rwx': some directories in the Windows working tree carry a
    # read-only-like attribute that Windows' tar.exe encodes as a literal Unix
    # mode (e.g. dr-xr-xr-x -- confirmed live for api/endpoints/ and
    # .ruff_cache/**). GNU tar reproduces that mode verbatim on extraction, so
    # the directory is created with no write bit before its contents are
    # written, and every file inside fails with "Cannot open: Permission
    # denied". --no-same-permissions does NOT fix this (tested, still failed);
    # --mode applies the given symbolic change on top of every extracted
    # entry's mode unconditionally, which does (tested clean). No quotes
    # around u+rwx: Windows PowerShell 5.1 mangles single quotes embedded in
    # a string passed to a native exe's argv (confirmed live -- the identical
    # command with quotes failed via this script but succeeded run by hand).
    # u+rwx has no shell-special characters, so bash needs no quoting here.
    ssh @sshOpts $VmHost "cd ~/$remote && tar --warning=no-unknown-keyword --mode=u+rwx -xzf upload.tgz && rm upload.tgz && sed -i 's/\r`$//' deploy/*.sh deploy/crontab.txt .env && bash deploy/setup_vm.sh"
    if ($LASTEXITCODE -ne 0) { throw "remote setup failed" }
}
finally {
    Remove-Item $tarPath, $envPath -Force -ErrorAction SilentlyContinue
}
Write-Host "`nDone. Check it tomorrow (or after 09:15 IST today) with: .\deploy\vm_status.ps1 -VmHost $VmHost -KeyFile $KeyFile"
