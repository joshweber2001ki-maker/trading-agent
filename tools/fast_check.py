#!/usr/bin/env python3
"""Dry run of the immediate entry on the newest real announcements. Sends NO order.

Run on the server:  bash deploy/run-tool.sh tools/fast_check.py [COUNT]        press releases
                    bash deploy/run-tool.sh tools/fast_check.py sec [COUNT]    filings with the SEC

Takes the newest releases from PR Newswire (or the newest 8-K filings without quarterly results)
that belong to a tradable stock and runs each through the steps the bot takes: full text, what
analysts expect, the rating, the price from before the announcement, and the order it WOULD send.
This shows whether every step works against the real services. The ratings cost a few cents.

The look at the market before the entry (bid and offer, if need be by trading one share there and
back) is left out here: a dry run sends no order, not even that one. The limit shown is therefore
the widest the bot would use; the real one sits just above the offer it sees.
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.alpaca import Alpaca  # noqa: E402
from bot.common import Journal, env, load_config, now, parse_ts  # noqa: E402
from bot.edgar import Edgar  # noqa: E402
from bot.fast import FastStrategy  # noqa: E402
from bot.finnhub import Expectations, Finnhub  # noqa: E402
from bot.llm import Classifier  # noqa: E402
from bot.market import StockStats, Universe  # noqa: E402
from bot.wires import NameIndex, Wires, parse_feed, to_article  # noqa: E402


class AfterHours:
    """Stands in for the trading calendar: pretend the after-hours session is running."""
    def phase(self): return "post"


class NoOrders:
    """Stands in for the executor: allows everything, sends nothing, keeps what it was asked to send."""
    def __init__(self): self.asked = None
    async def can_enter(self, *a, **kw): return True, ""
    async def enter(self, sig):
        self.asked = sig
        return True


async def filings(cfg, universe, journal, count):
    """The newest filings the bot would read: of a tradable stock, not quarterly results, not a formality."""
    edgar = Edgar(cfg["edgar"], env("SEC_CONTACT", ""), universe, journal)
    if not edgar.contact:
        print("Keine Kontaktadresse für die SEC auf dem Server (/etc/trading-bot/sec.env, SEC_CONTACT=...).")
        return []
    await edgar._load_tickers()
    skip_with, skip_only = set(cfg["edgar"].get("skip_with_items") or []), set(cfg["edgar"].get("skip_items") or [])
    out, seen = [], set()
    for e in await edgar._page(0, 100):
        sym, items = edgar.symbol_for(e["cik"]), set(e["items"])
        if not sym or e["form"] != "8-K" or e["acc"] in seen or items & skip_with or (items and items <= skip_only):
            continue
        seen.add(e["acc"])
        out.append(edgar.article(e, sym))
    return out[:count]


async def main(count, sec=False):
    cfg = load_config()
    # Old announcements are fine for a dry run; no look at the market (it could trade a share), one model.
    cfg["fast"] = {**cfg["fast"], "enabled": True, "max_age_s": 10 ** 9, "shadows": False,
                   "look": {**(cfg["fast"].get("look") or {}), "enabled": False}}
    api = Alpaca(env("APCA_API_KEY_ID", required=True), env("APCA_API_SECRET_KEY", required=True),
                 env("APCA_BASE_URL", "https://paper-api.alpaca.markets"), feed=env("APCA_DATA_FEED", "iex"))
    universe = Universe(api)
    await universe.load()
    journal = Journal(Path(tempfile.mkdtemp(prefix="fast-check-")))
    wires = Wires(cfg["wires"], universe, None, journal)
    index, arts = NameIndex(universe.cache), {}
    expectations = Expectations(Finnhub(env("FINNHUB_API_KEY", "")), journal)
    if sec:
        newest = await filings(cfg, universe, journal, count)
        print(f"Geprüft werden die {len(newest)} neuesten Pflichtmitteilungen handelbarer Firmen ohne Quartalszahlen.")
        await expectations.refresh(force=True)
    else:
        for url in cfg["wires"]["feeds"][:4]:
            r = await wires.http.get(url)
            for item in parse_feed(r.text):
                art = to_article(item, index, universe.cache, "prnewswire", 420)
                if art:
                    arts[art["id"]] = art
        newest = sorted(arts.values(), key=lambda a: a["created_at"], reverse=True)[:count]
        print(f"{len(arts)} zugeordnete Pressemitteilungen in den Feeds, geprüft werden die {len(newest)} neuesten. "
              f"Finnhub nennt {await expectations.refresh(force=True)} Firmen mit Bericht gestern bis morgen.")
    llm, stats = Classifier(env("ANTHROPIC_API_KEY", ""), cfg["llm"]), StockStats(api)
    for art in newest:
        sym, published = art["symbols"][0], parse_ts(art["created_at"])
        print(f"\n{sym}  {art['created_at']}  (vor {(now() - published).total_seconds() / 60:.0f} min)  {art['headline'][:90]}")
        if sec:
            t0 = time.monotonic()
            text = await art["fetch"]()
            art = {**art, "fetch": None, "text": text}       # fetched once: the bot's own run below reuses it
            print(f"  Text der Pflichtmitteilung: {len(text)} Zeichen in {time.monotonic() - t0:.1f} s"
                  + (f"  «{text[:110]}…»" if text else "  (nicht abrufbar: der Bot würde sie nicht bewerten)"))
            if not text:
                continue
        else:
            text = await wires.fetch_text(art["url"])
            print(f"  Text der Meldung: {len(text)} Zeichen" + (f"  «{text[:110]}…»" if text else "  (nicht abrufbar, es bleibt der Anriss)"))
        dry = NoOrders()
        fast = FastStrategy(api, cfg, journal, AfterHours(), universe, dry, llm, stats=stats,
                            expectations=expectations, fetch_text=wires.fetch_text)
        ref, ref_age = await fast.reference_price(sym, published)
        print("  Kurs vor der Meldung: " + (f"{ref:.2f} (vor {ref_age:.0f} min)" if ref else "keiner gefunden"))
        t0 = time.monotonic()
        await fast.on_article(art)
        took = time.monotonic() - t0
        mine = [r for r in journal.recent_events(1) if r.get("article") == art["id"]]
        for r in mine:
            if r["kind"] == "fast_skipped":
                print(f"  nicht bewertet: {r['reason']}")
            elif r["kind"] == "fast_rated":
                v = r["verdict"]
                print(f"  Bewertung ({r['model']}, {r['model_s']} s, mit Erwartungen: {'ja' if r['with_expectations'] else 'nein'}): "
                      f"{v['direction']}, Überraschung {v['surprise']}, Wirkung {v['impact']}, Sicherheit {v['confidence']} – {v['reason'][:90]}")
            elif r["kind"] == "fast_rejected":
                print(f"  kein Einstieg: {r['reason']}")
            elif r["kind"] == "error":
                print(f"  FEHLER: {r.get('error')}")
        if dry.asked:
            s = dry.asked
            print(f"  WÜRDE senden: {s['side']} bis {s['max_notional']} $, Limit {s['limit_price']:.2f} "
                  f"(Kurs vor der Meldung {s['price']:.2f}), Stop {s['stop_pct']:.2f} %, Ziel {s['target_pct']:.2f} %, "
                  f"Zeitlimit {s['max_hold_min']} min")
        print(f"  Dauer vom Eintreffen bis zur Entscheidung: {took:.1f} s")
    print("\nEs wurde keine Order gesendet.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "sec"]
    asyncio.run(main(int(args[0]) if args else 3, sec="sec" in sys.argv[1:]))
