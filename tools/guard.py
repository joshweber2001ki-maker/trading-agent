#!/usr/bin/env python3
"""Safety net, run every minute by systemd, independent of the bot.

Regular session: the bot closes everything ten minutes before the close. If it has crashed or
hangs, positions would stay open overnight without protection, because the stop and target
orders are day orders and expire at the close. A few minutes before the close this script checks
that the account is flat and, if not, cancels all orders and closes all positions itself.

Before the open and after the close (only with [extended] enabled): positions there have no
stop at the broker at all, the bot watches them itself. So the script also steps in
- a few minutes after the bot's own closing time at the end of each of these two sessions, and
- at any moment in them when the bot has given no sign of life for a few minutes.
There the broker takes no market orders; positions are closed with limit orders through the market.

  python tools/guard.py          act if needed
  python tools/guard.py --dry    only report
"""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.alpaca import Alpaca  # noqa: E402
from bot.common import DATA, Journal, env, load_config, now, parse_ts  # noqa: E402
from bot.executor import close_with_limit  # noqa: E402
from bot.market import Session  # noqa: E402

MARGIN_MIN = 3   # the bot goes first; the guard acts this many minutes later


def due(clock: dict, flatten_before_close_min: float) -> bool:
    """Regular session: true from a few minutes after the bot's own flatten time until the close."""
    if not clock.get("is_open"):
        return False
    left = (parse_ts(clock["next_close"]) - parse_ts(clock["timestamp"])).total_seconds() / 60
    return left <= flatten_before_close_min - MARGIN_MIN


def ext_due(phase, minutes_left, ext: dict, status_age_s):
    """Before the open or after the close: why everything has to be closed now, or None.

    status_age_s: seconds since the bot last wrote its status (None = no status at all).
    """
    if phase not in ("pre", "post") or not ext.get("enabled"):
        return None
    if minutes_left <= ext[f"flatten_before_end_min_{phase}"] - MARGIN_MIN:
        return "session_end"
    if status_age_s is None or status_age_s > ext["guard_stale_s"]:
        return "bot_silent"
    return None


def status_age_s(root: Path = None):
    """Seconds since the bot's last sign of life, or None if there is none."""
    try:
        t = json.loads(((root or DATA) / "logs" / "status.json").read_text())["t"]
        return (now() - parse_ts(t)).total_seconds()
    except (OSError, ValueError, KeyError):
        return None


def worth_a_look(ext_enabled: bool, t: datetime = None) -> bool:
    """Cheap check before asking the broker anything: can a session be running at this hour (UTC)?"""
    t = t or datetime.now(timezone.utc)
    if ext_enabled:
        return True
    return t.weekday() < 5 and 16 <= t.hour <= 21      # regular close in both daylight-saving regimes


async def main(dry=False):
    cfg = load_config()
    ext = cfg.get("extended") or {}
    if not worth_a_look(bool(ext.get("enabled"))):
        return
    base = env("APCA_BASE_URL", "https://paper-api.alpaca.markets")
    if "paper" not in base and env("ALLOW_LIVE") != "yes":
        raise SystemExit("Nur Paper-Trading erlaubt.")
    api = Alpaca(env("APCA_API_KEY_ID", required=True), env("APCA_API_SECRET_KEY", required=True), base)
    why = "close" if due(await api.clock(), cfg["execution"]["flatten_before_close_min"]) else None
    phase = None
    if not why and ext.get("enabled"):
        session = Session(api)
        await session.refresh()
        phase = session.phase()
        if phase in ("pre", "post"):
            left = (session.phase_end(phase) - now()).total_seconds() / 60
            why = ext_due(phase, left, ext, status_age_s())
    if not why:
        return
    # Open orders count too: an entry order left behind by a bot that has stopped could still fill.
    positions, orders = await api.positions(), await api.open_orders()
    if not positions and not orders:
        return
    found = {"reason": why, "phase": phase or "regular",
             "positions": [f'{p["symbol"]} {p["qty"]}' for p in positions], "orders": len(orders)}
    print("Wächter greift ein:", found)
    if dry:
        return
    await api.cancel_all()
    journal, failed = Journal(), []
    for p in positions:
        for attempt in range(5):
            try:
                if phase in ("pre", "post"):
                    if not await close_with_limit(api, p["symbol"], ext, journal):
                        failed.append(f'{p["symbol"]}: not closed')
                else:
                    await api.close_position(p["symbol"])
                break
            except Exception as e:   # shares still held by an order that is being cancelled
                if attempt == 4:
                    failed.append(f'{p["symbol"]}: {e!r}'[:200])
                await asyncio.sleep(1)
    journal.event("guard", **found, failed=failed)


if __name__ == "__main__":
    asyncio.run(main(dry="--dry" in sys.argv))
