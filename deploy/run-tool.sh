#!/usr/bin/env bash
# Runs a tool from this repo with the server's keys, as the unprivileged user. Run as root.
#   bash deploy/run-tool.sh tools/speed_check.py NVDA 2026-11-17T14:30:00Z
#   bash deploy/run-tool.sh tools/backtest_orb.py --days 90 --feed sip
set -euo pipefail
APP=/opt/trading-agent
[ $# -ge 1 ] || { echo "Aufruf: bash deploy/run-tool.sh tools/<datei>.py [Argumente]"; exit 1; }
exec systemd-run --quiet --pipe --wait --collect \
  -p User=trader -p WorkingDirectory="$APP" \
  -p EnvironmentFile=/etc/trading-bot/alpaca.env \
  -p EnvironmentFile=-/etc/trading-bot/llm.env \
  -p EnvironmentFile=-/etc/trading-bot/sec.env \
  "$APP/.venv/bin/python" "$@"
