#!/usr/bin/env python3
"""Does the paper account itself know the real price outside regular hours?

Run on the server:  bash deploy/run-tool.sh tools/price_probe.py [SYMBOL] [SECONDS]

This tool TRADES in the paper account: it buys ONE share, watches it for a minute and sells it
again. Paper account only; it refuses anything else. Start it by hand, not from the bot.

Why: the free price feed is nearly blind before the open and after the close. The paper account
fills orders against the real market, and the broker values an open position itself. If that
valuation follows the real market also outside regular hours, the bot can watch a stop through
its own position, without a paid price feed. The tool shows
- the price the buy and the sell were filled at (the real offer and the real bid at that moment),
- the position's price as the broker reports it every two seconds, next to the last trade the
  free feed knows and, if a Finnhub key is there, the trades Finnhub's stream delivered.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.alpaca import Alpaca, AlpacaError  # noqa: E402
from bot.common import ET, env, iso, load_config, now  # noqa: E402
from bot.executor import close_with_limit, is_live, read_quote  # noqa: E402
from bot.market import Session  # noqa: E402
from tools.ext_check import age  # noqa: E402
from tools.price_check import listen  # noqa: E402


async def filled(api, order_id, seconds=12):
    """Wait for an order to fill. Returns the order as the broker last reported it."""
    o = await api.order(order_id)
    for _ in range(int(seconds / 0.5)):
        if not is_live(o):
            break
        await asyncio.sleep(0.5)
        o = await api.order(order_id)
    return o


async def main(symbol, seconds):
    base = env("APCA_BASE_URL", "https://paper-api.alpaca.markets")
    if "paper" not in base:
        raise SystemExit("Nur im Paper-Konto.")
    cfg = load_config()
    api = Alpaca(env("APCA_API_KEY_ID", required=True), env("APCA_API_SECRET_KEY", required=True), base,
                 feed=env("APCA_DATA_FEED", "iex"))
    session = Session(api)
    await session.refresh()
    phase = session.phase()
    print(f"New York: {now().astimezone(ET):%a %d.%m. %H:%M:%S}, Sitzung: {phase or 'keine'}")
    if not phase:
        raise SystemExit("Gerade läuft keine Sitzung, in der eine Order ausgeführt würde.")
    if await api.position(symbol) is not None or any(o["symbol"] == symbol for o in await api.open_orders()):
        raise SystemExit(f"Im Konto gibt es schon eine Position oder Order in {symbol}. Abgebrochen.")
    q = read_quote(await api.snapshot(symbol))
    if not q["last"]:
        raise SystemExit(f"Kein Kurs für {symbol}.")
    start = iso(now())
    limit = round(q["last"] * 1.03, 2)
    print(f"Kostenlose Kursquelle: letzter Handel {q['last']:.2f} vor {age(q['last_age_s'])}")
    print(f"Kauf 1 {symbol}, Limit {limit:.2f} (3 % über diesem Kurs) ...")
    try:
        o = await api.submit({"symbol": symbol, "qty": "1", "side": "buy", "type": "limit", "limit_price": str(limit),
                              "time_in_force": "day", "extended_hours": True})
    except AlpacaError as e:
        raise SystemExit(f"Order abgelehnt: {e}")
    o = await filled(api, o["id"])
    if o["status"] != "filled":
        await api.cancel(o["id"])
        raise SystemExit(f"Nicht ausgeführt (Status {o['status']}): Der Markt steht mehr als 3 % über dem bekannten Kurs. Order gestrichen.")
    bought = float(o["filled_avg_price"])
    print(f"  ausgeführt zu {bought:.2f}: das war der echte Briefkurs")

    async def watch():
        rows = []
        for _ in range(int(seconds / 2)):
            pos = await api.position(symbol)
            feed = read_quote(await api.snapshot(symbol))
            rows.append((float(pos["current_price"]) if pos else None, feed["last"], feed["last_age_s"]))
            await asyncio.sleep(2)
        return rows

    key = env("FINNHUB_API_KEY", "")
    try:
        rows, stream = await asyncio.gather(watch(), listen(key, [symbol], seconds) if key else asyncio.sleep(0))
    finally:
        done = await close_with_limit(api, symbol, cfg["extended"])
    sold = None
    for c in await api.closed_orders(symbol, start):
        if c.get("side") == "sell" and c.get("filled_avg_price"):
            sold = float(c["filled_avg_price"])
            break
    print(f"Verkauf 1 {symbol}: " + (f"ausgeführt zu {sold:.2f}: das war der echte Geldkurs" if sold else "KEINE Ausführung gefunden"))
    if not done:
        print(f"ACHTUNG: {symbol} ist noch im Konto. Der Bot schließt die Position vor Börsenschluss; bitte im Konto nachsehen.")
    marks = [r[0] for r in rows if r[0] is not None]
    print(f"Kurs der Position laut Broker, alle 2 Sekunden ({len(marks)} Werte):")
    print("  " + " ".join(f"{m:.2f}" for m in marks))
    feed_last = [r[1] for r in rows]
    print(f"Letzter Handel der kostenlosen Kursquelle in derselben Zeit: {min(feed_last):.2f} bis {max(feed_last):.2f}, "
          f"am Ende {age(rows[-1][2])} alt")
    if isinstance(stream, dict) and symbol in stream:
        s = stream[symbol]
        print(f"Finnhub-Strom in derselben Zeit: {s['n']} Abschlüsse" + (f", zuletzt {s['last']:.2f}" if s["n"] else ""))
    changes = sum(1 for a, b in zip(marks, marks[1:]) if a != b)
    print(f"Der Kurs der Position hat sich {changes}-mal geändert, von {min(marks):.2f} bis {max(marks):.2f}." if marks else "Keine Position gesehen.")
    if sold:
        print(f"Spanne laut den beiden Ausführungen: {(bought - sold) / bought * 100:.2f} %; "
              f"Kosten dieses Versuchs im Paper-Konto: {bought - sold:.2f} $")


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(main((args[0] if args else "NVDA").upper(), float(args[1]) if len(args) > 1 else 60))
