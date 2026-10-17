#!/bin/bash
# Runs ON the Azure VM. Removes the bot's schedule and moves the bot's data aside (nothing is deleted).
# Keeps: Angel One recorders, Upstox live recorder, weekly Upstox backfills, data/options.db,
#        data/historical_bars.db, data/upstox_chains.db.
set -u
cd ~/trading_system || exit 1
STAMP=$(date +%Y%m%d_%H%M)
HOLD=~/_to_delete_bot_$STAMP
mkdir -p "$HOLD/data" "$HOLD/logs"

echo "== 1/3 crontab"
crontab -l > ~/crontab_before_bot_removal_$STAMP.txt 2>/dev/null || { echo "no crontab found"; exit 1; }
grep -v -e 'bot_v1\.run' -e 'credit_spread_live_study' -e '^# ---- bot_v1' -e '^# The Upstox token arrives each morning' -e '^# Weekly credit-spread report' \
    ~/crontab_before_bot_removal_$STAMP.txt > ~/crontab_after_$STAMP.txt
crontab ~/crontab_after_$STAMP.txt && echo "new crontab installed (old one saved as ~/crontab_before_bot_removal_$STAMP.txt)"
echo "removed lines:"; diff ~/crontab_before_bot_removal_$STAMP.txt ~/crontab_after_$STAMP.txt | grep '^<' | cut -c1-110

echo "== 2/3 moving bot data to $HOLD"
for p in data/botv1_live data/bot_v1 data/botv1_seed.tgz data/sandbox research/reports paper_trades.json; do
  if [ -e "$p" ]; then mkdir -p "$HOLD/$(dirname "$p")"; mv "$p" "$HOLD/$p" && echo "moved $p"; fi
done
for f in logs/cron_botv1.log logs/cron_report.log; do [ -e "$f" ] && mv "$f" "$HOLD/logs/" && echo "moved $f"; done

echo "== 3/3 what is left"
echo "cron lines now:"; crontab -l | grep -v '^#' | grep -v '^$' | grep -v '^SHELL\|^APP' | sed -E 's|cd \$APP && ||; s|>> logs.*||' | cut -c1-120
echo "data/:"; ls -la data | head -20
ls -la logs | head -12
echo "held for you to delete: $HOLD"
