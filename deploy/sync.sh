#!/usr/bin/env bash
# Hourly: publish the bot's logs to GitHub and pick up code changes. Run as root (by systemd).
# Code changes restart the bot only outside US market hours.
set -uo pipefail
APP=/opt/trading-agent
DATA=/var/lib/trading-bot
g() { runuser -u trader -- git -C "$APP" "$@"; }

# 1. Copy the logs into the checkout and commit them.
runuser -u trader -- cp -a "$DATA/logs/." "$APP/logs/"
g add -A logs
g diff --cached --quiet || g commit -q -m "logs $(date -u '+%F %H:%M') UTC"

# 2. Pick up changes from GitHub, then publish.
if ! g pull -q --rebase; then
  g rebase --abort 2>/dev/null
  echo "pull fehlgeschlagen"
fi
g push -q origin HEAD:main || echo "push fehlgeschlagen (Deploy-Key bei GitHub hinterlegt?)"

# 3. Restart on code changes, outside market hours (13:25-21:05 UTC covers both DST regimes).
running=$(cat "$DATA/state/running_commit" 2>/dev/null || echo none)
head=$(g rev-parse HEAD)
[ "$running" = "$head" ] && exit 0
if [ "$running" != none ] && [ "$running" != unknown ] && \
   g diff --quiet "$running" "$head" -- bot config.toml requirements.txt deploy 2>/dev/null; then
  exit 0   # only logs or docs changed
fi
hm=$((10#$(date -u +%H%M)))
dow=$(date -u +%u)
if [ "$dow" -le 5 ] && [ "$hm" -ge 1325 ] && [ "$hm" -le 2105 ]; then
  echo "Code geändert, Neustart nach Börsenschluss"
  exit 0
fi
runuser -u trader -- "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
install -m 644 "$APP"/deploy/trading-bot.service "$APP"/deploy/trading-bot-sync.service \
  "$APP"/deploy/trading-bot-sync.timer /etc/systemd/system/
systemctl daemon-reload
systemctl restart trading-bot
echo "Code aktualisiert auf $head, Bot neu gestartet"
