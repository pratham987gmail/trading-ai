# The cloud recorder: what's actually deployed, and how to redeploy it

This is a record of what's really running, not a plan. Google Cloud was the
original choice, but its free tier turned out to need a billing account
that Google confirmed could not be reopened once closed, and this account's
one billing profile was in that state. Azure's free trial ($200 credit,
12 months of an always-free VM size) was used instead. If a future session
needs to rebuild this from scratch, follow **Rebuilding**, not the history
above it.

## What's running right now

- **VM:** `trading-recorder-vm`, resource group `trading-recorder-vm_group`,
  Azure subscription (free trial). `Standard_B1s` (the free-for-12-months
  size), Ubuntu 24.04, **India South Central** (not Central India --
  `Standard_B1s` came back `NotAvailableForSubscription` there specifically;
  India South Central had it). Real Indian IP, so neither Angel One nor
  Upstox needed any workaround.
- **Disk:** Standard SSD, 30 GB (Premium SSD was the console default;
  switched to Standard SSD to avoid any ambiguity about what the free tier's
  disk allowance actually covers at that tier).
- **SSH:** key-only (`azureuser`, ed25519 key generated locally at
  `~/.ssh/azure_vm.key`, never uploaded as a private key -- only the public
  key was pasted into the VM creation form). Port 22 open to all IPs (fine:
  key-only auth, no password login exists to guess).
- **Cron jobs** (`deploy/crontab.txt`, installed by `deploy/setup_vm.sh`):
  SENSEX and NIFTY chain recording every 5 min in market hours, the
  Saturday 09:00 Upstox historical backfill, the Saturday 10:00
  credit-spread report, and a Sunday log-trim job. See
  `LIVE_CHAIN_RECORDING.md` for what each recorder actually captures.
- **Credentials on the VM:** Angel One login + Upstox credentials (both
  needed by scheduled jobs) plus the same risk-limit settings as the
  laptop's `.env`, in a trimmed `.env` `deploy_to_vm.ps1` builds and
  uploads. **NewsAPI stays laptop-only** -- nothing scheduled uses it.
  `TRADING_MODE=ANALYSIS` is forced regardless of the laptop's setting;
  this VM never places an order.

## One real deployment bug worth knowing if `deploy_to_vm.ps1` ever fails

Windows' built-in `System32\tar.exe` (bsdtar-based) writes some
directories into the archive with a broken mode (`dr-xr-xr-x` -- no write
bit), apparently from an NTFS attribute on those specific folders in this
working tree. GNU tar on the Linux side then can't extract anything into
them ("Cannot open: Permission denied"), and no extraction-side flag fixes
it -- `--no-same-permissions` and `--mode=u+rwx` were both tried against an
archive built this way and both still failed. The fix is packaging with
Git for Windows' own GNU tar instead (`C:\Program Files\Git\usr\bin\tar.exe`),
which computes a normal mode for the same directories. `deploy_to_vm.ps1`
already does this; if it's ever changed back to plain `tar`, this comes
back. Two smaller PowerShell-specific gotchas came with it: the archive
filename can't be an absolute Windows path (the drive-letter colon gets
parsed as a remote-host spec by MSYS tar) and single quotes embedded in a
string passed to a native exe's argv get mangled by Windows PowerShell 5.1
-- both are why the script builds the archive in a relative-path working
directory and avoids quoting `u+rwx`.

## Redeploying or updating

From `trading_system\`, with the VM's IP and the private key:

```powershell
# Code/config changes only -- data/ on the VM is never touched
.\deploy\deploy_to_vm.ps1 -VmHost azureuser@<IP> -KeyFile "$env:USERPROFILE\.ssh\azure_vm.key"

# First deploy, or to carry local history across -- seeds data\options.db
# and data\historical_bars.db onto the VM, but only if it doesn't already
# have each one
.\deploy\deploy_to_vm.ps1 -VmHost azureuser@<IP> -KeyFile ... -SeedData

# Health check: capture cycles per day, recent errors, latest reports
.\deploy\vm_status.ps1 -VmHost azureuser@<IP> -KeyFile ...

# Pull new rows home: options.db incrementally, historical_bars.db in full
# (it's small and only grows weekly, so a full copy each time is simpler)
.\deploy\pull_from_vm.ps1 -VmHost azureuser@<IP> -KeyFile ...
```

## Things to know

- **Security.** The VM holds your Angel One password, TOTP secret and
  Upstox client secret/access token. None of these place a live order from
  this VM (`TRADING_MODE=ANALYSIS`), but the Angel One credentials could if
  the mode were ever changed. Keep the SSH private key file private. Don't
  open extra firewall rules or run the API server on the VM. If the key or
  VM is ever exposed, change your Angel One password, re-enrol TOTP,
  regenerate the Angel One API key, and revoke/regenerate the Upstox token.
- **Free-trial expiry.** Azure's $200 credit and the 12-months-free VM
  size both expire on a schedule tied to when the trial started -- check
  the Azure portal's billing section before assuming this stays free
  indefinitely, unlike an actual Always Free tier.
- **Holidays and hours.** Cron fires only within the recorder scripts'
  configured windows on weekdays (the VM's clock is set to IST), and the
  recorder itself independently skips exchange holidays.
- **If a VM needs to be recreated** (region ran out of capacity, account
  issue, etc.), `Standard_B1s` wasn't available in Central India for this
  subscription specifically -- try India South Central first, and treat
  `NotAvailableForSubscription` as "try another region", not "this size
  doesn't exist anywhere".

## Rebuilding from nothing (if this VM is ever deleted)

1. Create an Azure account, or use the existing one -- check
   **Subscriptions** in the portal for an active one with a working
   billing account before creating a VM; a billing account that shows
   "This billing account is closed" and "not in good standing" cannot be
   reopened, only replaced with a new one.
2. Compute Engine -> **Create a virtual machine**: Ubuntu 24.04 LTS,
   **Security type: Standard** (not Trusted launch -- `Standard_B1s` isn't
   offered under Trusted launch's Gen2-only size list), size `Standard_B1s`
   (search by name; if a region shows it as unavailable, try another Indian
   region), **OS disk: Standard SSD**, SSH public key auth with a locally
   generated key (`ssh-keygen -t ed25519 -f ~/.ssh/azure_vm.key -N ""`,
   paste the `.pub` contents into the form), inbound port 22 only.
3. Once running, confirm SSH works, then run `deploy_to_vm.ps1 -SeedData`
   as above.
