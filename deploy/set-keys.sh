#!/usr/bin/env bash
# Stores the API keys on the server and restarts the bot. Run as root.
# The keys are typed here by hand and never leave the server. Leave a field empty to keep it.
set -euo pipefail
CONF=/etc/trading-bot
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen."; exit 1; }
mkdir -p "$CONF"; chmod 700 "$CONF"; umask 077

read -rp  "Alpaca API Key ID (leer = unverändert): " K
if [ -n "$K" ]; then
  read -rsp "Alpaca Secret (Eingabe bleibt unsichtbar): " S; echo
  [ -n "$S" ] || { echo "Secret fehlt, abgebrochen."; exit 1; }
  printf 'APCA_API_KEY_ID=%s\nAPCA_API_SECRET_KEY=%s\nAPCA_BASE_URL=https://paper-api.alpaca.markets\n' \
    "$K" "$S" > "$CONF/alpaca.env"
  echo "Alpaca gespeichert."
fi
read -rsp "Anthropic API Key (unsichtbar, leer = unverändert): " A; echo
if [ -n "$A" ]; then
  printf 'ANTHROPIC_API_KEY=%s\n' "$A" > "$CONF/llm.env"
  echo "Anthropic gespeichert."
fi
unset K S A
systemctl restart trading-bot && echo "Bot neu gestartet. Prüfen: bash /opt/trading-agent/deploy/check.sh"
