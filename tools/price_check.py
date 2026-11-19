#!/usr/bin/env python3
"""Which price source shows the market right now? Compares Alpaca's feed with Finnhub.

Run on the server:  bash deploy/run-tool.sh tools/price_check.py [--stream SECONDS] [SYMBOL ...]

Read-only. For a handful of stocks: how old is the last trade each source knows? With --stream
the tool also listens to Finnhub's live stream of trades for that many seconds and counts what
arrives per stock. The point: the free Alpaca feed is one small exchange and nearly blind before
the open and after the close; this shows whether another source sees more at this hour.
Keys are never printed.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.alpaca import Alpaca  # noqa: E402
from bot.common import ET, env, now  # noqa: E402
from bot.executor import read_quote  # noqa: E402
from bot.finnhub import Finnhub, FinnhubError  # noqa: E402
from bot.market import Session  # noqa: E402
from tools.ext_check import DEFAULT, age  # noqa: E402

STREAM = "wss://ws.finnhub.io"


async def listen(key: str, symbols, seconds: float) -> dict:
    """Trades per symbol from Finnhub's live stream: count, last price, delay of the last one, conditions seen."""
    import websockets
    out = {s: {"n": 0, "last": None, "lag_s": None, "cond": set(), "shares": 0} for s in symbols}
    try:
        async with websockets.connect(f"{STREAM}?token={key}", ping_interval=20) as ws:
            for s in symbols:
                await ws.send(json.dumps({"type": "subscribe", "symbol": s}))
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                msg = json.loads(raw)
                if msg.get("type") != "trade":
                    continue
                got = time.time()
                for x in msg.get("data") or []:
                    row = out.get(x.get("s"))
                    if row is None:
                        continue
                    row["n"] += 1
                    row["last"] = x.get("p")
                    row["shares"] += x.get("v") or 0
                    row["lag_s"] = round(got - x["t"] / 1000, 1) if x.get("t") else None
                    row["cond"].update(str(c) for c in x.get("c") or [])
    except Exception as e:      # never show the address: it carries the key
        return {"error": type(e).__name__}
    return out


async def main(symbols, stream_s):
    api = Alpaca(env("APCA_API_KEY_ID", required=True), env("APCA_API_SECRET_KEY", required=True),
                 env("APCA_BASE_URL", "https://paper-api.alpaca.markets"), feed=env("APCA_DATA_FEED", "iex"))
    fh = Finnhub(env("FINNHUB_API_KEY", ""))
    session = Session(api)
    await session.refresh()
    t = now()
    print(f"New York: {t.astimezone(ET):%a %d.%m. %H:%M:%S}, Sitzung: {session.phase() or 'keine'}")
    snaps = await api.snapshots(symbols)
    print(f"{'':<6}{'Alpaca (' + api.feed + ')':<26}{'Finnhub':<26}")
    fresh = {"alpaca": 0, "finnhub": 0}
    for sym in symbols:
        a = read_quote(snaps.get(sym))
        left = f"{a['last']:.2f} vor {age(a['last_age_s'])}" if a["last"] else "kein Handel"
        fresh["alpaca"] += a["last_age_s"] is not None and a["last_age_s"] <= 60
        try:
            q = await fh.quote(sym)
            f_age = time.time() - q["t"] if q.get("t") else None
            right = f"{q['c']:.2f} vor {age(f_age)}" if q.get("c") else "kein Kurs"
            fresh["finnhub"] += f_age is not None and f_age <= 60
        except FinnhubError as e:
            right = f"Fehler {e.status}"
        print(f"{sym:<6}{left:<26}{right:<26}")
    print(f"Handel in der letzten Minute: Alpaca {fresh['alpaca']} von {len(symbols)}, Finnhub {fresh['finnhub']} von {len(symbols)}")
    if stream_s and fh.available:
        print(f"Finnhub-Strom, {stream_s:.0f} Sekunden:")
        got = await listen(fh.key, symbols, stream_s)
        if "error" in got:
            print(f"  nicht erreichbar ({got['error']})")
            return
        for sym in symbols:
            r = got[sym]
            if r["n"]:
                print(f"  {sym:<6}{r['n']:>5} Abschlüsse, {r['shares']:>9.0f} Stück, zuletzt {r['last']:.2f}, "
                      f"Verzögerung {r['lag_s']} s, Kennzeichen {sorted(r['cond'])[:8]}")
            else:
                print(f"  {sym:<6}    0 Abschlüsse")
        print(f"  Aktien mit mindestens einem Abschluss: {sum(1 for r in got.values() if r['n'])} von {len(symbols)}")


if __name__ == "__main__":
    args, stream = sys.argv[1:], 0.0
    if "--stream" in args:
        i = args.index("--stream")
        stream = float(args[i + 1])
        del args[i:i + 2]
    asyncio.run(main([s.upper() for s in args] or DEFAULT, stream))
