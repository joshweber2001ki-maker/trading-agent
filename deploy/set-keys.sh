#!/usr/bin/env bash
# Stores API keys on the server. Run as root.
# The keys are typed here by hand and never leave the server. Leave a field empty to keep it.
#   bash deploy/set-keys.sh            Alpaca and Anthropic; restarts the bot if one of them changed
#   bash deploy/set-keys.sh finnhub    analysts' expectations (Finnhub). No restart: the bot reads
#                                      this key the next time it starts.
#   bash deploy/set-keys.sh wirealpha  press releases by push (WireAlpha, measured only). Offers a
#                                      restart if the bot holds no position.
#   bash deploy/set-keys.sh alphavantage   estimates for coming quarters (Alpha Vantage). The bot
#                                      does not use this key yet; only tools/alphavantage_check.py does.
set -euo pipefail
CONF="${BOT_CONF_DIR:-/etc/trading-bot}"
DATA="${BOT_DATA_DIR:-/var/lib/trading-bot}"
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen."; exit 1; }
mkdir -p "$CONF"; chmod 700 "$CONF"; umask 077

# One further key that the running bot does not depend on: file, variable, label, check tool.
# Returns 1 if nothing was entered.
one_key() {
  local K
  read -rsp "$3 (Eingabe bleibt unsichtbar, leer = unverändert): " K; echo
  K="${K//[[:space:]]/}"
  if [ -z "$K" ]; then echo "Nichts geändert."; return 1; fi
  printf '%s=%s\n' "$2" "$K" > "$CONF/$1"
  echo "$3 gespeichert (${#K} Zeichen)."
  echo "Prüfen: bash /opt/trading-agent/deploy/run-tool.sh $4"
}

# The bot reads a key when it starts. A restart is offered only while it holds no position:
# before the open and after the close the bot itself is the stop of an open position.
offer_restart() {
  local A
  if python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("trades") else 1)' \
       "$DATA/state/open_trades.json" 2>/dev/null; then
    echo "Der Bot hält gerade eine Position: kein Neustart. Er liest den Schlüssel bei seinem nächsten Start."
    return 0
  fi
  read -rp "Keine Position offen. Bot jetzt neu starten, damit er den Schlüssel liest? [j/N] " A
  case "$A" in
    j|J|ja|Ja|JA) systemctl restart trading-bot && echo "Bot neu gestartet." ;;
    *) echo "Kein Neustart. Der Bot liest den Schlüssel bei seinem nächsten Start (spätestens nach der nächsten Änderung am Code)." ;;
  esac
}

case "${1:-}" in
  finnhub)
    one_key finnhub.env FINNHUB_API_KEY "Finnhub API Key" tools/finnhub_check.py \
      && echo "Der Bot liest ihn bei seinem nächsten Start."
    exit 0 ;;
  wirealpha)
    if one_key wirealpha.env WIREALPHA_API_KEY "WireAlpha API Key" tools/wirealpha_check.py; then offer_restart; fi
    exit 0 ;;
  alphavantage)
    one_key alphavantage.env ALPHAVANTAGE_API_KEY "Alpha Vantage API Key" tools/alphavantage_check.py \
      && echo "Der Bot nutzt diesen Schlüssel noch nicht; die Prüfung zeigt, was er liefert."
    exit 0 ;;
  "") ;;
  *) echo "Unbekannt: $1. Möglich: finnhub, wirealpha, alphavantage, oder ohne Angabe für Alpaca und Anthropic."; exit 1 ;;
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
