#!/usr/bin/env bash
# Connectivity self-test with the server's keys. Run as root.
#   bash deploy/check.sh               read-only
#   bash deploy/check.sh --order-test  also places and cancels a far-away test order
set -euo pipefail
APP=/opt/trading-agent
exec systemd-run --quiet --pipe --wait --collect \
  -p User=trader -p WorkingDirectory="$APP" \
  -p EnvironmentFile=/etc/trading-bot/alpaca.env \
  -p EnvironmentFile=-/etc/trading-bot/llm.env \
  "$APP/.venv/bin/python" tools/check.py "$@"
