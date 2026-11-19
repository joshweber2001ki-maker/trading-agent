"""How much of the market does the bot's live price feed show? Measurement only, no trading.

Every few minutes while a session is running, the bot notes for two groups of stocks whether its
feed shows a price a stop could rely on (a trade of the last seconds, or bid and offer close
together - the same rule the executor uses before the open and after the close): a fixed list
of very liquid stocks, and the stocks with a company announcement in the last hour. The free
feed is one small exchange; these numbers say how blind that leaves the bot at each hour.
"""
import time
from datetime import timedelta

from .common import now, parse_ts
from .executor import mark_price, read_quote, spread_pct

NEWS_KINDS = ("wire_item", "sec_item")


def view(snaps: dict, symbols, trade_fresh_s, max_spread_pct, at=None) -> dict:
    """Of these symbols: how many show a usable price, a trade within a minute, a narrow quote."""
    out = {"n": 0, "seeing": 0, "trade_60s": 0, "quote_ok": 0}
    for sym in symbols:
        q = read_quote(snaps.get(sym), at)
        out["n"] += 1
        out["seeing"] += mark_price(q, trade_fresh_s, max_spread_pct) is not None
        out["trade_60s"] += q["last_age_s"] is not None and q["last_age_s"] <= 60
        sp = spread_pct(q)
        out["quote_ok"] += sp is not None and sp <= max_spread_pct
    return out


class FeedView:
    def __init__(self, api, cfg: dict, ext: dict, journal, session, universe, liquid):
        self.api, self.cfg, self.ext, self.journal = api, cfg, ext, journal
        self.session, self.universe, self.liquid = session, universe, list(liquid)
        self.last = None        # time of the last measurement (None: none yet, measure at once)

    def news_symbols(self, minutes=60):
        """Stocks with an announcement in the last hour, newest first, without the liquid list."""
        since, seen = now() - timedelta(minutes=minutes), []
        for r in self.journal.recent_events(1):
            if r.get("kind") in NEWS_KINDS and r.get("symbol") and parse_ts(r["t"]) >= since:
                seen.append(r["symbol"])
        out = []
        for sym in reversed(seen):
            if sym not in out and sym not in self.liquid and sym in self.universe.cache:
                out.append(sym)
        return out[:self.cfg["max_news"]]

    async def step(self):
        """Called from the control loop; does something only every every_s seconds in a running session."""
        phase = self.session.phase()
        if not self.cfg.get("enabled") or not phase:
            return
        if self.last is not None and time.monotonic() - self.last < self.cfg["every_s"]:
            return
        self.last = time.monotonic()
        try:
            news = self.news_symbols()
            snaps = await self.api.snapshots(self.liquid + news)
            fresh, spread = self.ext["trade_fresh_s"], self.ext["max_mark_spread_pct"]
            self.journal.event("feed_view", phase=phase, feed=getattr(self.api, "feed", None),
                               liquid=view(snaps, self.liquid, fresh, spread), news=view(snaps, news, fresh, spread))
        except Exception as e:      # a measurement must never disturb the loop that manages trades
            self.journal.event("error", where="feed_view", error=repr(e)[:200])
