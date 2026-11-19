#!/usr/bin/env python3
"""What does the bot see of the market before the open and after the close?

Run on the server:  bash deploy/run-tool.sh tools/ext_check.py [SYMBOL ...]

Read-only, changes nothing. Shows the trading hours the broker's calendar names for the next
days (with the pre-market and after-hours session) and, for a handful of stocks, what the bot's
data feed delivers at this moment: how old the last trade is, where bid and offer stand, and
whether that is enough to watch a stop ([extended] trade_fresh_s and max_mark_spread_pct).
With the free feed (one small exchange, IEX) this tells how blind the bot is at this hour.
"""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.alpaca import DATA, Alpaca, AlpacaError  # noqa: E402
from bot.common import ET, env, load_config, now  # noqa: E402
from bot.executor import mark_price, read_quote, spread_pct  # noqa: E402
from bot.market import Session  # noqa: E402

DEFAULT = ["NVDA", "TSLA", "AAPL", "AMD", "PLTR", "WMT", "BABA", "DELL", "HPQ", "TGT", "LOW", "ZIM", "LZB", "DLB", "URBN", "BJ"]


def age(seconds):
    if seconds is None:
        return "?"
    return f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.0f} min" if seconds < 7200 else f"{seconds / 3600:.0f} h"


def describe(sym, snap, ext, at=None):
    q = read_quote(snap, at)
    sp, mark = spread_pct(q), mark_price(q, ext["trade_fresh_s"], ext["max_mark_spread_pct"])
    last = f"{q['last']:.2f} (vor {age(q['last_age_s'])})" if q["last"] else "kein Handel"
    quote = (f"Geld {q['bid']:.2f} Brief {q['ask']:.2f} Spanne {sp:.2f} % (seit {age(q['quote_age_s'])})"
             if sp is not None else "kein beidseitiger Kurs")
    return f"{sym:<6}{last:<24}{quote:<58}{'sieht ' + format(mark, '.2f') if mark else 'BLIND'}", mark is not None


async def main(symbols):
    cfg = load_config()
    ext = cfg["extended"]
    api = Alpaca(env("APCA_API_KEY_ID", required=True), env("APCA_API_SECRET_KEY", required=True),
                 env("APCA_BASE_URL", "https://paper-api.alpaca.markets"), feed=env("APCA_DATA_FEED", "iex"))
    session = Session(api)
    await session.refresh()
    t = now().astimezone(ET)
    print(f"New York: {t:%a %d.%m. %H:%M:%S}, Sitzung jetzt: {session.phase() or 'keine'}, Datenquelle: {api.feed}")
    today = t.date()
    for row in await api.calendar(today.isoformat(), (today + timedelta(days=9)).isoformat()):
        print(f"  {row['date']}  Börse {row['open']}-{row['close']}  mit Vor- und Nachbörse "
              f"{row.get('session_open', '?')}-{row.get('session_close', '?')}")
    snaps = await api.snapshots(symbols)
    seeing = 0
    for sym in symbols:
        line, ok = describe(sym, snaps.get(sym), ext)
        seeing += ok
        print(line)
    print(f"{seeing} von {len(symbols)} Aktien mit einem Kurs, auf den sich ein Stop stützen könnte "
          f"(Handel höchstens {ext['trade_fresh_s']} s alt oder Spanne höchstens {ext['max_mark_spread_pct']} %)")
    try:
        await api._req("GET", f"{DATA}/v2/stocks/{symbols[0]}/snapshot", params={"feed": "sip"})
        print("Echtzeit-Kurse des gesamten Marktes (sip): verfügbar")
    except AlpacaError as e:
        print(f"Echtzeit-Kurse des gesamten Marktes (sip): nicht im Datenpaket ({e.status})")


if __name__ == "__main__":
    asyncio.run(main([s.upper() for s in sys.argv[1:]] or DEFAULT))
