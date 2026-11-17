#!/usr/bin/env python3
"""Connectivity self-test. Read-only unless --order-test is given.

  python tools/check.py               # account, clock, data, news, KI
  python tools/check.py --order-test  # also places and immediately cancels a far-away bracket order
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from bot.alpaca import Alpaca  # noqa: E402
from bot.common import env, load_config  # noqa: E402
from bot.llm import Classifier  # noqa: E402


async def main():
    cfg = load_config()
    key, secret = env("APCA_API_KEY_ID", required=True), env("APCA_API_SECRET_KEY", required=True)
    api = Alpaca(key, secret, env("APCA_BASE_URL", "https://paper-api.alpaca.markets"))
    ok = True

    async def step(name, coro):
        nonlocal ok
        try:
            print(f"OK    {name}: {await coro}")
        except Exception as e:
            ok = False
            print(f"FEHLER {name}: {e!r}"[:300])

    async def account():
        a = await api.account()
        return f'{a["status"]}, Depot {a["equity"]} $, Kaufkraft {a["buying_power"]} $, Hebel {a["multiplier"]}x'

    async def clock():
        c = await api.clock()
        return f'offen={c["is_open"]}, nächste Öffnung {c["next_open"]}'

    async def data():
        s = await api.snapshots(["AAPL", "NVDA"])
        return {k: v["latestTrade"]["p"] for k, v in s.items()}

    async def screener():
        m = await api.movers(5)
        return f'{len(m.get("gainers", []))} Gewinner, {len(m.get("losers", []))} Verlierer'

    async def news_stream():
        async with websockets.connect(
            "wss://stream.data.alpaca.markets/v1beta1/news",
            additional_headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        ) as ws:
            await ws.recv()
            auth = json.loads(await ws.recv())[0]
            if auth.get("code") == 406:
                return "Zugang in Ordnung; die eine erlaubte Verbindung hält gerade der laufende Bot"
            if auth.get("T") == "error":
                raise RuntimeError(auth)
            return auth.get("msg")

    async def llm():
        """The deciding model and the ones rating along: each must accept the request the bot sends."""
        llm_key = env("ANTHROPIC_API_KEY", "")
        if not llm_key:
            return "kein ANTHROPIC_API_KEY gesetzt (News-Strategie bleibt inaktiv)"
        art = {"headline": "Acme Corp raises full-year guidance well above analyst expectations",
               "summary": "", "source": "test", "created_at": ""}
        out = []
        for m in [cfg["llm"]["model"], *[x for x in cfg["llm"].get("shadow", []) if x != cfg["llm"]["model"]]]:
            r = await Classifier(llm_key, cfg["llm"], model=m).rate(art, ["ACME"])
            if r["error"]:
                raise RuntimeError(f"{m}: {r['error']}")
            v = r["verdict"]
            out.append(f'{m}: {v["direction"]}, Überraschung {v["surprise"]}, Wirkung {v["impact"]}, {r["seconds"]} s')
        return "; ".join(out)

    async def order_test():
        o = await api.submit({
            "symbol": "KO", "qty": "1", "side": "buy", "type": "limit", "limit_price": "20.00",
            "time_in_force": "day", "order_class": "bracket",
            "take_profit": {"limit_price": "25.00"}, "stop_loss": {"stop_price": "15.00"},
        })
        full = await api.order(o["id"])
        await api.cancel(o["id"])
        legs = [(l["type"], l["status"]) for l in full.get("legs") or []]
        return f'Order angenommen ({o["status"]}), Stop und Ziel {legs}, wieder gestrichen'

    await step("Alpaca-Konto", account())
    await step("Börsenuhr", clock())
    await step("Kursdaten", data())
    await step("Screener", screener())
    async def wires():
        import httpx
        from bot.wires import UA, parse_feed
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as c:
            r = await c.get(cfg["wires"]["feeds"][0])
            r.raise_for_status()
            items = parse_feed(r.text)
        return f'{len(items)} Meldungen, neueste: {items[0]["title"][:60]!r}'

    await step("Nachrichtenstrom", news_stream())
    await step("Pressemitteilungen", wires())
    await step("KI-Bewertung", llm())
    if "--order-test" in sys.argv:
        await step("Order mit Stop und Ziel", order_test())
    sys.exit(0 if ok else 1)


asyncio.run(main())
