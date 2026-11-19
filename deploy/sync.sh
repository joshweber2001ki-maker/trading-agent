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

# 3. Keep the systemd units in step with the repo (does not restart the bot).
units_changed=0
timers_changed=""
for u in trading-bot.service trading-bot-sync.service trading-bot-sync.timer \
         trading-bot-guard.service trading-bot-guard.timer; do
  if ! cmp -s "$APP/deploy/$u" "/etc/systemd/system/$u"; then
    install -m 644 "$APP/deploy/$u" /etc/systemd/system/
    units_changed=1
    case "$u" in *.timer) timers_changed="$timers_changed $u" ;; esac
  fi
done
[ "$units_changed" = 1 ] && systemctl daemon-reload
for u in $timers_changed; do systemctl try-restart "$u"; done   # a changed schedule counts from now
systemctl enable -q --now trading-bot-guard.timer

# 4. Restart on code changes, outside market hours (13:25-21:05 UTC covers both DST regimes).
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
# Never restart under an open position: before the open and after the close the bot itself is the stop.
if "$APP/.venv/bin/python" - "$DATA/state/open_trades.json" <<'PY'
import json, sys
try:
    sys.exit(0 if json.load(open(sys.argv[1])).get("trades") else 1)
except Exception:
    sys.exit(1)
PY
then
  echo "Code geändert, aber eine Position ist offen: Neustart beim nächsten Abgleich"
  exit 0
fi
runuser -u trader -- "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
systemctl restart trading-bot
echo "Code aktualisiert auf $head, Bot neu gestartet"
