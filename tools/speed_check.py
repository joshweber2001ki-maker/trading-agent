#!/usr/bin/env python3
"""Measure one announcement by hand.

  python tools/speed_check.py SYMBOL 2026-11-17T14:30:00Z

Prints what the speed measurement would log for an announcement about that stock at that time
(which must be at least 50 minutes ago). Needs the Alpaca keys; on the server:
  bash deploy/run-tool.sh tools/speed_check.py SYMBOL TIME
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.alpaca import Alpaca  # noqa: E402
from bot.common import env, load_config, parse_ts  # noqa: E402
from bot.speed import Speed  # noqa: E402


async def main(symbol, when):
    api = Alpaca(env("APCA_API_KEY_ID", required=True), env("APCA_API_SECRET_KEY", required=True),
                 env("APCA_BASE_URL", "https://paper-api.alpaca.markets"))

    class Uni:
        cache = {symbol: {}}
    res = await Speed(api, load_config()["speed"], None, Uni()).measure(
        {"src": "manual", "ref": "manual", "symbol": symbol, "t0": parse_ts(when)})
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    asyncio.run(main(sys.argv[1].upper(), sys.argv[2]))
