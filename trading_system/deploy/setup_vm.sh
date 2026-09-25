#!/usr/bin/env bash
# One-time (and re-runnable) setup on the cloud VM. Run from ~/trading_system:
#   bash deploy/setup_vm.sh
# Safe to re-run after every code upload: it only (re)creates what's missing
# and reinstalls the crontab.
set -euo pipefail

cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"

echo "== system packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip sqlite3 cron >/dev/null

echo "== timezone -> Asia/Kolkata (cron schedule is written in IST)"
sudo timedatectl set-timezone Asia/Kolkata

# Free-tier micro VMs have ~1 GB RAM; pip installing pandas/numpy can OOM without swap.
if [ "$(free -m | awk '/Mem:/ {print $2}')" -lt 2000 ] && ! swapon --show | grep -q /swapfile; then
  echo "== adding 2 GB swap"
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== python venv + dependencies"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

mkdir -p data logs research/reports
if [ ! -f .env ]; then
  echo "ERROR: .env missing. Upload it with deploy/deploy_to_vm.ps1 first." >&2
  exit 1
fi
chmod 600 .env

echo "== crontab"
sed "s#__APP_DIR__#${APP_DIR}#g" deploy/crontab.txt | crontab -
sudo systemctl enable --now cron >/dev/null 2>&1 || true
crontab -l | grep -v '^#' | grep -v '^$'

echo "== smoke test (read-only: store status, no broker call)"
.venv/bin/python scripts/record_chains.py --status

echo
echo "Setup complete. Recording starts at the next scheduled slot (09:15 IST, Mon-Fri)."
