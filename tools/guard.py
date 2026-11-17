#!/usr/bin/env python3
"""Safety net, run every two minutes in the afternoon by systemd, independent of the bot.

The bot closes everything ten minutes before the close. If it has crashed or hangs, positions
would stay open overnight without protection, because the stop and target orders are day
orders and expire at the close. This script checks a few minutes before the close that the
account is flat and, if not, cancels all orders and closes all positions itself.

  python tools/guard.py          act if needed
  python tools/guard.py --dry    only report
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.alpaca import Alpaca  # noqa: E402
from bot.common import Journal, env, load_config, parse_ts  # noqa: E402

MARGIN_MIN = 3   # the bot goes first; the guard acts this many minutes later


def due(clock: dict, flatten_before_close_min: float) -> bool:
    """True from a few minutes after the bot's own flatten time until the close."""
    if not clock.get("is_open"):
        return False
    left = (parse_ts(clock["next_close"]) - parse_ts(clock["timestamp"])).total_seconds() / 60
    return left <= flatten_before_close_min - MARGIN_MIN


async def main(dry=False):
    cfg = load_config()
    base = env("APCA_BASE_URL", "https://paper-api.alpaca.markets")
    if "paper" not in base and env("ALLOW_LIVE") != "yes":
        raise SystemExit("Nur Paper-Trading erlaubt.")
    api = Alpaca(env("APCA_API_KEY_ID", required=True), env("APCA_API_SECRET_KEY", required=True), base)
    if not due(await api.clock(), cfg["execution"]["flatten_before_close_min"]):
        return
    positions, orders = await api.positions(), await api.open_orders()
    if not positions and not orders:
        return
    found = {"positions": [f'{p["symbol"]} {p["qty"]}' for p in positions], "orders": len(orders)}
    print("Wächter: kurz vor Börsenschluss noch offen:", found)
    if dry:
        return
    await api.cancel_all()
    failed = []
    for p in positions:
        for attempt in range(5):
            try:
                await api.close_position(p["symbol"])
                break
            except Exception as e:   # shares still held by an order that is being cancelled
                if attempt == 4:
                    failed.append(f'{p["symbol"]}: {e!r}'[:200])
                await asyncio.sleep(1)
    Journal().event("guard", **found, failed=failed)


if __name__ == "__main__":
    asyncio.run(main(dry="--dry" in sys.argv))
