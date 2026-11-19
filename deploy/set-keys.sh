#!/usr/bin/env bash
# Stores API keys on the server. Run as root.
# The keys are typed here by hand and never leave the server. Leave a field empty to keep it.
#   bash deploy/set-keys.sh            Alpaca and Anthropic; restarts the bot if one of them changed
#   bash deploy/set-keys.sh finnhub    analysts' expectations (Finnhub). No restart: the bot reads
#                                      this key the next time it starts.
set -euo pipefail
CONF="${BOT_CONF_DIR:-/etc/trading-bot}"
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen."; exit 1; }
mkdir -p "$CONF"; chmod 700 "$CONF"; umask 077

# One further key that the running bot does not depend on: file, variable, label.
one_key() {
  local K
  read -rsp "$3 (Eingabe bleibt unsichtbar, leer = unverändert): " K; echo
  K="${K//[[:space:]]/}"
  if [ -z "$K" ]; then echo "Nichts geändert."; return 0; fi
  printf '%s=%s\n' "$2" "$K" > "$CONF/$1"
  echo "$3 gespeichert (${#K} Zeichen). Der Bot liest ihn bei seinem nächsten Start."
  echo "Prüfen: bash /opt/trading-agent/deploy/run-tool.sh $4"
}

case "${1:-}" in
  finnhub)
    one_key finnhub.env FINNHUB_API_KEY "Finnhub API Key" tools/finnhub_check.py
    exit 0 ;;
  "") ;;
  *) echo "Unbekannt: $1. Möglich: finnhub, oder ohne Angabe für Alpaca und Anthropic."; exit 1 ;;
esac

changed=0
read -rp  "Alpaca API Key ID (leer = unverändert): " K
if [ -n "$K" ]; then
  read -rsp "Alpaca Secret (Eingabe bleibt unsichtbar): " S; echo
  [ -n "$S" ] || { echo "Secret fehlt, abgebrochen."; exit 1; }
  printf 'APCA_API_KEY_ID=%s\nAPCA_API_SECRET_KEY=%s\nAPCA_BASE_URL=https://paper-api.alpaca.markets\n' \
    "$K" "$S" > "$CONF/alpaca.env"
  echo "Alpaca gespeichert."; changed=1
fi
read -rsp "Anthropic API Key (unsichtbar, leer = unverändert): " A; echo
if [ -n "$A" ]; then
  printf 'ANTHROPIC_API_KEY=%s\n' "$A" > "$CONF/llm.env"
  echo "Anthropic gespeichert."; changed=1
fi
unset K S A
if [ "$changed" = 1 ]; then
  systemctl restart trading-bot && echo "Bot neu gestartet. Prüfen: bash /opt/trading-agent/deploy/check.sh"
else
  echo "Nichts geändert, der Bot läuft unverändert weiter."
fi
