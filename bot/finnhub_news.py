"""News from Finnhub's free plan, as one more source to measure.

Finnhub's real-time news stream is a paid feature; the free plan answers questions: the latest
market news (one call for all stocks) and the news of one company (one call per company). With 60
calls a minute it cannot follow every stock, so it asks for the companies that report results today
(from the earnings calendar the bot already loads) and the general market news.

Finnhub collects its news from other sites, so it is expected to be later than the wires. Whether it
ever is first shows in `news_dup` (blocked: false) once it runs: until then it is measured, not traded
(it is not in `[wires] trade`). Every item is logged as `wire_item` with source "finnhub".
"""
import asyncio
from datetime import datetime, timezone

from .common import ET, iso, log, now, spawn
from .finnhub import FinnhubError


def to_article(item: dict, symbol: str, universe: dict, max_age_s: int):
    """A Finnhub news item as an article, if it belongs to exactly one tradable stock."""
    related = [s.strip().upper() for s in (item.get("related") or "").split(",") if s.strip()]
    if symbol:
        if related and symbol not in related:
            return None
        sym = symbol
    else:
        ours = [s for s in related if s in universe]
        if len(ours) != 1:
            return None
        sym = ours[0]
    if sym not in universe or not item.get("headline") or not item.get("datetime"):
        return None
    published = datetime.fromtimestamp(int(item["datetime"]), tz=timezone.utc)
    return {
        "id": f"finnhub:{item.get('id')}", "headline": item["headline"].strip(),
        "summary": (item.get("summary") or "").strip()[:600], "created_at": iso(published),
        "symbols": [sym], "source": "finnhub", "company": (item.get("source") or "")[:60],
        "max_age_s": max_age_s, "url": item.get("url") or "",
    }


class FinnhubNews:
    def __init__(self, finnhub, cfg: dict, expectations, universe, on_article, journal, max_age_s: int = 420):
        self.fh, self.cfg, self.expectations, self.universe = finnhub, cfg or {}, expectations, universe
        self.on_article, self.journal, self.max_age_s = on_article, journal, max_age_s
        self.seen = set()
        self.min_id = {}       # market news category -> highest id seen
        self.stats = {"polls": 0, "items": 0, "matched": 0, "errors": 0}

    def reset_day(self):
        self.stats = {k: 0 for k in self.stats}
        if len(self.seen) > 50000:
            self.seen.clear()

    def reporters(self):
        """Companies whose report falls on today (New York date)."""
        today = now().astimezone(ET).date().isoformat()
        return sorted(s for s, r in (self.expectations.rows or {}).items() if r.get("date") == today)

    def _take(self, items, symbol=None):
        for item in items or []:
            key = item.get("id")
            if key is None or key in self.seen:
                continue
            self.seen.add(key)
            self.stats["items"] += 1
            art = to_article(item, symbol, self.universe.cache, self.max_age_s)
            if not art:
                continue
            if (now() - datetime.fromisoformat(art["created_at"].replace("Z", "+00:00"))).total_seconds() > 900:
                continue                    # older news, e.g. on the first call for a company: not news any more
            self.stats["matched"] += 1
            self.journal.event("wire_item", id=art["id"], symbol=art["symbols"][0], published=art["created_at"],
                               headline=art["headline"][:140], source="finnhub", company=art["company"])
            spawn(self.on_article(art))

    async def market(self, category: str):
        params = {"category": category}
        if category in self.min_id:
            params["minId"] = self.min_id[category]
        items = await self.fh.get("/news", **params)
        self.stats["polls"] += 1
        ids = [i.get("id") for i in items or [] if isinstance(i.get("id"), int)]
        first_call = category not in self.min_id
        if ids:
            self.min_id[category] = max(ids + [self.min_id.get(category, 0)])
        if first_call:                      # what was there before the start is not news
            self.seen.update(i.get("id") for i in items or [])
            return
        self._take(items)

    async def company(self, symbol: str):
        today = now().astimezone(ET).date().isoformat()
        items = await self.fh.get("/company-news", symbol=symbol, **{"from": today, "to": today})
        self.stats["polls"] += 1
        self._take(items, symbol)

    async def _guarded(self, what, coro):
        try:
            await coro
        except FinnhubError as e:
            self.stats["errors"] += 1
            if self.stats["errors"] <= 20 or self.stats["errors"] % 100 == 0:
                self.journal.event("error", where="finnhub_news", call=what, error=str(e)[:200])
            if str(e.status) == "403":
                return "forbidden"
        except Exception as e:
            self.stats["errors"] += 1
            if self.stats["errors"] <= 20:
                self.journal.event("error", where="finnhub_news", call=what, error=repr(e)[:200])

    async def run(self):
        cfg = self.cfg
        if not cfg.get("enabled") or not self.fh.available:
            return
        log.info("finnhub news: market news every %s s, today's reporters every %s s",
                 cfg["market_every_s"], cfg["company_every_s"])
        await asyncio.gather(self._market_loop(), self._company_loop())

    async def _market_loop(self):
        while True:
            for category in self.cfg.get("categories", ["general", "merger"]):
                if self.universe.cache and await self._guarded(category, self.market(category)) == "forbidden":
                    return
            await asyncio.sleep(self.cfg["market_every_s"])

    async def _company_loop(self):
        """Each of today's reporters once every company_every_s, the calls spread evenly; at most
        max_companies of them (the calls per minute are limited)."""
        while True:
            syms = self.reporters()[: self.cfg.get("max_companies", 40)] if self.universe.cache else []
            if not syms:
                await asyncio.sleep(60)
                continue
            gap = max(2.0, self.cfg["company_every_s"] / len(syms))
            for sym in syms:
                if await self._guarded(sym, self.company(sym)) == "forbidden":
                    return
                await asyncio.sleep(gap)
