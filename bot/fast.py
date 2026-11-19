"""Immediate entry on a fresh company announcement, before the open and after the close.

The old news strategy waits up to five minutes for price and volume to confirm an article. By
then the reaction to a press release is over. Here the order goes out as soon as the rating is
in. The bot has no live price at those hours (the free feed is one small exchange), so it does
not look at the market first:

- The limit starts from the last price of the whole market from BEFORE the announcement (free
  once 15 minutes old) and allows half of the move the rating expects. The paper account fills
  at the real market or not at all, so a stock that has already run further is simply not bought.
- Stop and target are measured from the fill. If the fill shows that the market had already run
  the other way, the executor closes the trade at once.
- From then on the executor watches the position through the broker's own valuation of it.
"""
import asyncio
from datetime import timedelta

from .common import iso, now, parse_ts
from .llm import is_noise
from .news_strategy import trade_plan, vol_factor

FREE_DELAY_MIN = 16     # whole-market prices are free once they are 15 minutes old


class FastStrategy:
    def __init__(self, api, cfg, journal, session, universe, executor, classifier, stats=None,
                 expectations=None, fetch_text=None):
        self.api, self.journal, self.session, self.universe = api, journal, session, universe
        self.executor, self.llm, self.stock_stats = executor, classifier, stats
        self.expectations, self.fetch_text = expectations, fetch_text
        self.cfg, self.news_cfg, self.min_price = cfg["fast"], cfg["news"], cfg["risk"]["min_price"]
        self.seen = set()          # article ids and (symbol, headline) pairs already handled
        self.sem = asyncio.Semaphore(4)
        self.stats = {"received": 0, "rated": 0, "candidates": 0, "entered": 0}

    def reset_day(self):
        if len(self.seen) > 20000:
            self.seen.clear()
        self.stats = {k: 0 for k in self.stats}

    async def on_article(self, art: dict):
        try:
            await self._handle(art, now())
        except Exception as e:      # one bad article must never take the bot down
            self.journal.event("error", where="fast", article=art.get("id"), error=repr(e)[:300])

    async def reference_price(self, sym, published):
        """The last price of the whole market from before the announcement, as far as the free
        data plan hands it out: (price, its age in minutes), or (None, None)."""
        end = min(now() - timedelta(minutes=FREE_DELAY_MIN), published)
        start = end - timedelta(hours=self.cfg["reference_max_age_h"])
        bar = await self.api.last_bar(sym, iso(start), iso(end))
        if not bar:
            return None, None
        return float(bar["c"]), round((now() - parse_ts(bar["t"])).total_seconds() / 60, 1)

    async def _handle(self, art, received):
        cfg, phase = self.cfg, self.session.phase()
        if not cfg["enabled"] or phase not in cfg["sessions"]:
            return
        aid, symbols, head = art.get("id"), art.get("symbols") or [], art.get("headline", "")
        if not aid or aid in self.seen or len(symbols) != 1:
            return                  # an announcement must belong to exactly one stock
        self.seen.add(aid)
        sym = symbols[0]
        title_key = (sym, " ".join(head.lower().split())[:120])
        if title_key in self.seen:
            return                  # the same release again (second feed, correction)
        self.seen.add(title_key)
        self.stats["received"] += 1
        published = parse_ts(art["created_at"]) if art.get("created_at") else received
        age = max(0.0, (received - published).total_seconds())
        base = dict(article=aid, source=art.get("source"), symbol=sym, headline=head[:140], phase=phase,
                    age_s=round(age))

        def skip(reason, **kw):
            self.journal.event("fast_skipped", reason=reason, **base, **kw)

        if age > cfg["max_age_s"]:
            return skip("stale")
        if is_noise(head):
            return skip("noise")
        if not await self.universe.asset(sym):
            return
        if await self.universe.is_fund(sym):
            return skip("fund")
        st = (await self.stock_stats.get([sym])).get(sym) if self.stock_stats else None
        if not st:
            return skip("no_stats")
        if st["price"] < self.min_price:
            return skip("min_price")
        if st["dollar_volume"] < cfg["min_dollar_volume"]:
            return skip("too_small", dollar_volume=st["dollar_volume"])

        article = dict(art)
        if cfg.get("fetch_text") and self.fetch_text and art.get("url"):
            text = await self.fetch_text(art["url"])
            if text:
                article["text"] = text
        context = await self.expectations.context(sym) if self.expectations else None
        async with self.sem:
            res = await self.llm.rate(article, [sym], {sym: await self.universe.name(sym)}, {sym: st},
                                      context=context, text_chars=cfg["text_chars"])
        if res["error"]:
            return skip(res["error"][:200])
        self.stats["rated"] += 1
        v = res["verdict"]
        rated_age = (now() - published).total_seconds()
        base.update(verdict=v, model=res["model"], model_s=res["seconds"], tokens_in=res["tokens_in"],
                    tokens_out=res["tokens_out"], size=st["size"], daily_range_pct=st["daily_range_pct"],
                    published=iso(published), rated_age_s=round(rated_age, 1),
                    with_text=bool(article.get("text")), with_expectations=bool(context))
        # Every rating is logged, traded or not: the evening's price measurement of the same
        # announcement (news_speed) then shows whether the direction was right.
        self.journal.event("fast_rated", **base)
        if v["direction"] == "none" or v["impact"] < cfg["min_impact"] \
                or v["surprise"] < cfg["min_surprise"] or v["confidence"] < cfg["min_confidence"]:
            return
        self.stats["candidates"] += 1

        def reject(reason, **kw):
            self.journal.event("fast_rejected", reason=reason, **base, **kw)

        if rated_age > cfg["max_age_s"]:
            return reject("too_old")
        ok, why = await self.executor.can_enter("fast", sym, extended=True, max_open=cfg["max_open"])
        if not ok:
            return reject(why)
        ref, ref_age_min = await self.reference_price(sym, published)
        if not ref:
            return reject("no_reference")
        dirn = 1 if v["direction"] == "up" else -1
        # How far the price may already have run: a share of the move the rating expects.
        chase = cfg["expected_move_pct"][str(v["impact"])] * cfg["chase_frac"]
        stop_pct, target_r, _ = trade_plan(v["impact"], vol_factor(st, self.news_cfg), [], ref, self.news_cfg)
        wide_stop = min(stop_pct * cfg["stop_mult"], self.news_cfg["stop_cap_pct"])
        entered = await self.executor.enter({
            "strategy": "fast", "symbol": sym, "side": "long" if dirn > 0 else "short",
            "price": ref, "limit_price": ref * (1 + dirn * chase / 100),
            "stop_pct": wide_stop, "target_pct": stop_pct * target_r,
            "ref_price": ref, "max_adverse_pct": chase,
            "max_hold_min": cfg["max_hold_min"][phase], "extended": True,
            "risk_pct": cfg["risk_pct"], "max_open": cfg["max_open"], "entry_timeout_s": cfg["entry_timeout_s"],
            "max_notional": cfg["max_notional_small"] if st["size"] == "small" else cfg["max_notional"],
            "meta": {
                "article": aid, "headline": head[:140], "source": art.get("source"), "verdict": v,
                "model": res["model"], "published": iso(published), "news_to_order_s": round(rated_age, 1),
                "ref_price": ref, "ref_age_min": ref_age_min, "chase_pct": chase, "size": st["size"],
                "with_text": bool(article.get("text")), "with_expectations": bool(context),
            },
        })
        if entered:
            self.stats["entered"] += 1
        else:                       # the executor has logged why ("rejected", stage "risk")
            reject("entry_refused", ref_price=ref)
