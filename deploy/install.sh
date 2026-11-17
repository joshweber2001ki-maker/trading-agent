#!/usr/bin/env bash
# Installs or updates the trading bot on a Linux server. Run as root. Safe to run again.
#
# It only adds things: a user "trader", /opt/trading-agent, /var/lib/trading-bot,
# /etc/trading-bot and three systemd units. Nothing else on the server is changed.
set -euo pipefail

REPO_HTTPS=https://github.com/joshweber2001ki-maker/trading-agent.git
REPO_SSH=git@github.com:joshweber2001ki-maker/trading-agent.git
APP=/opt/trading-agent
DATA=/var/lib/trading-bot
CONF=/etc/trading-bot

[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen."; exit 1; }
as_trader() { runuser -u trader -- "$@"; }

echo "== 1/6 Pakete"
need=()
command -v git >/dev/null || need+=(git)
python3 -c 'import ensurepip' 2>/dev/null || need+=(python3-venv)
if [ ${#need[@]} -gt 0 ]; then
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"
fi

echo "== 2/6 Benutzer und Ordner"
id trader >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin trader
mkdir -p "$APP" "$DATA/logs" "$DATA/state" "$CONF"
chmod 700 "$CONF"
chown -R trader:trader "$APP" "$DATA"

echo "== 3/6 Code"
if [ -d "$APP/.git" ]; then
  as_trader git -C "$APP" pull -q --rebase
else
  as_trader git clone -q "$REPO_HTTPS" "$APP"
fi
as_trader git -C "$APP" config user.name "trading-bot"
as_trader git -C "$APP" config user.email "trading-bot@users.noreply.github.com"
as_trader git -C "$APP" remote set-url --push origin "$REPO_SSH"

echo "== 4/6 Python-Umgebung"
[ -x "$APP/.venv/bin/python" ] || as_trader python3 -m venv "$APP/.venv"
as_trader "$APP/.venv/bin/pip" install -q --upgrade pip
as_trader "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "== 5/6 Schlüssel für GitHub (zum Hochladen der Protokolle)"
KEY=/home/trader/.ssh/id_ed25519
if [ ! -f "$KEY" ]; then
  as_trader mkdir -p /home/trader/.ssh
  as_trader ssh-keygen -q -t ed25519 -N "" -C "trading-bot@$(hostname)" -f "$KEY"
fi
if ! as_trader grep -q "^github.com " /home/trader/.ssh/known_hosts 2>/dev/null; then
  as_trader bash -c 'ssh-keyscan -t ed25519 github.com >> /home/trader/.ssh/known_hosts 2>/dev/null'
fi

echo "== 6/6 Dienste"
install -m 644 "$APP"/deploy/trading-bot.service "$APP"/deploy/trading-bot-sync.service \
  "$APP"/deploy/trading-bot-sync.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q trading-bot.service trading-bot-sync.timer
systemctl start trading-bot-sync.timer

echo
echo "Fertig installiert."
echo "GitHub-Hostschlüssel: $(as_trader ssh-keygen -lf /home/trader/.ssh/known_hosts | head -1)"
if as_trader ssh -o BatchMode=yes -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
  as_trader git -C "$APP" remote set-url origin "$REPO_SSH"
  as_trader git -C "$APP" remote set-url --push origin "$REPO_SSH"
  echo "GitHub-Zugriff per Deploy-Key funktioniert."
else
  echo "Deploy-Key ist bei GitHub noch nicht hinterlegt. Öffentlicher Schlüssel"
  echo "(GitHub -> Repo -> Settings -> Deploy keys, mit Schreibrecht):"
  cat "$KEY.pub"
fi
echo
if [ -f "$CONF/alpaca.env" ]; then
  echo "Zugangsdaten vorhanden. Start: systemctl restart trading-bot"
else
  echo "Es fehlen noch die Alpaca-Zugangsdaten: bash $APP/deploy/set-keys.sh"
fi
