"""Immediate entry on a fresh company announcement: before the open, during the session, after the close.

The old news strategy waits up to five minutes for price and volume to confirm an article. By
then the reaction to an announcement is over. Here the order goes out as soon as the rating is in.

Second version (approved by Josh on 24.11.2026): the bot looks at the market before it enters.
Outside regular hours the gap between bid and offer right after an announcement is usually wider
than the move itself, and the free price feed shows next to nothing there. So:

- The reference is the last price of the whole market from BEFORE the announcement (free once 15
  minutes old; during the session also the free feed's last minute before it, if that is newer).
- Bid and offer come from the price feed if it shows both, fresh and close together, otherwise
  from a probe: one share bought (or sold short) and handed straight back. The paper account fills
  at the real offer and the real bid, so the two fills are the two sides of the market. (The free
  feed shows one exchange only: a narrow gap there is a narrow gap in the market, a wide one says
  nothing, so then the probe decides.)
- An entry needs a narrow spread, a price that has not run against the rating, and one that has
  not already run further than a share of the move the rating expects.
- The limit sits just through the price seen; stop and target are measured from the fill. Outside
  the session the executor watches them. During it they go to the broker with the order, counted
  from the price seen, which is where the order is filled.

Every rating is logged whether or not it leads to a trade; a source can also be rated without
being traded at all (`rate_only`), to measure how often the rating is right.
"""
import asyncio
from datetime import timedelta

from .common import iso, now, parse_ts, spawn
from .llm import is_noise
from .news_strategy import trade_plan, vol_factor

FREE_DELAY_MIN = 16     # whole-market prices are free once they are 15 minutes old


class FastStrategy:
    def __init__(self, api, cfg, journal, session, universe, executor, classifier, stats=None,
                 expectations=None, fetch_text=None, shadows=()):
        self.api, self.journal, self.session, self.universe = api, journal, session, universe
        self.executor, self.llm, self.stock_stats = executor, classifier, stats
        self.expectations, self.fetch_text = expectations, fetch_text
        self.cfg, self.news_cfg, self.min_price = cfg["fast"], cfg["news"], cfg["risk"]["min_price"]
        # Further models that rate the same announcements for comparison. They decide nothing.
        self.shadows = [c for c in shadows if c.model != classifier.model] if self.cfg.get("shadows") else []
        self.seen = set()          # article ids and (symbol, headline) pairs already handled
        self.entering = set()      # stocks for which an entry is being decided this moment
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

    async def reference_price(self, sym, published, phase=None):
        """The last price from before the announcement: (price, its age in minutes), or (None, None).

        The whole market's price, as far as the free data plan hands it out (15 minutes old). During
        the regular session the free feed's own last full minute before the announcement counts
        instead when it is newer: there the stock may have moved in those 15 minutes."""
        end = min(now() - timedelta(minutes=FREE_DELAY_MIN), published)
        start = end - timedelta(hours=self.cfg["reference_max_age_h"])
        bar = await self.api.last_bar(sym, iso(start), iso(end))
        if phase == "regular":
            try:
                own = await self.api.last_bar(sym, iso(published - timedelta(minutes=FREE_DELAY_MIN)),
                                              iso(published - timedelta(minutes=1)), feed=getattr(self.api, "feed", "iex"))
            except Exception:       # the older price is still a price
                own = None
            if own and (not bar or parse_ts(own["t"]) > parse_ts(bar["t"])):
                bar = own
        if not bar:
            return None, None
        return float(bar["c"]), round((now() - parse_ts(bar["t"])).total_seconds() / 60, 1)

    def regular_window(self) -> bool:
        """During the session: not in the first minutes after the open and not shortly before the close."""
        since, left = self.session.since_open_min(), self.session.to_close_min()
        return since is not None and left is not None \
            and since >= self.cfg["regular_start_after_open_min"] and left >= self.cfg["regular_no_entry_before_close_min"]

    async def look_at_market(self, sym, dirn, ref, chase, old_limit, look) -> dict:
        """Bid and offer right now, and whether they allow an entry.

        Returns {"ok": True, "how", "bid", "ask", "spread_pct", "moved_pct", "limit", "seconds"} or
        {"ok": False, "reason", ...} with reason spread, moved_against, too_far, probe_no_fill or probe_error.
        """
        try:
            q = await self.executor.quote(sym)
        except Exception:           # the feed is only the shortcut
            q = {"bid": 0.0, "ask": 0.0, "quote_age_s": None}
        fresh = q["bid"] > 0 and q["ask"] >= q["bid"] and q["quote_age_s"] is not None \
            and q["quote_age_s"] <= look["quote_fresh_s"]
        if fresh and (q["ask"] - q["bid"]) / ((q["ask"] + q["bid"]) / 2) * 100 <= look["max_spread_pct"]:
            bid, ask, how, took = q["bid"], q["ask"], "feed", 0.0
        else:
            p = await self.executor.probe(sym, "long" if dirn > 0 else "short", old_limit,
                                          wait_s=look["probe_wait_s"], back_pct=look["probe_back_pct"])
            if not p["ok"]:
                return {**p, "how": "probe"}        # with bid, offer and spread if the probe found both sides
            bid, ask, how, took = p["bid"], p["ask"], "probe", p["seconds"]
        mid = (bid + ask) / 2
        spread = max(0.0, ask - bid) / mid * 100
        price = ask if dirn > 0 else bid                       # the side the entry would pay
        moved = (price - ref) / ref * 100 * dirn               # how far the price has run in the rating's direction
        seen = {"how": how, "bid": bid, "ask": ask, "spread_pct": round(spread, 3), "moved_pct": round(moved, 3),
                "seconds": took}
        if spread > look["max_spread_pct"]:
            return {"ok": False, "reason": "spread", **seen}
        if moved < -look["max_against_pct"]:
            return {"ok": False, "reason": "moved_against", **seen}
        if moved > chase:
            return {"ok": False, "reason": "too_far", **seen}
        through = price * (1 + dirn * look["limit_offset_pct"] / 100)
        limit = min(through, old_limit) if dirn > 0 else max(through, old_limit)
        return {"ok": True, "limit": limit, **seen}

    async def _shadow(self, clf, article, sym, names, st, context, base):
        """Another model rates the same announcement. The result is logged (fast_shadow), nothing else."""
        try:
            res = await clf.rate(article, [sym], names, {sym: st}, context=context, text_chars=self.cfg["text_chars"])
        except Exception as e:      # a comparison must never disturb the trading path
            res = {"verdict": None, "error": repr(e)[:200], "model": clf.model, "seconds": None,
                   "tokens_in": None, "tokens_out": None}
        self.journal.event("fast_shadow", **base, model=res["model"], verdict=res["verdict"], model_s=res["seconds"],
                           tokens_in=res["tokens_in"], tokens_out=res["tokens_out"],
                           **({"error": res["error"][:200]} if res["error"] else {}))

    async def _handle(self, art, received):
        cfg, phase = self.cfg, self.session.phase()
        if not cfg["enabled"] or phase not in cfg["sessions"]:
            return
        aid, symbols, head = art.get("id"), art.get("symbols") or [], art.get("headline", "")
        if not aid or aid in self.seen or len(symbols) != 1:
            return                  # an announcement must belong to exactly one stock
        self.seen.add(aid)
        sym = symbols[0]
        # A filing's headline only names its kind: the same words do not make it the same filing.
        title_key = (sym, art.get("dedupe_key") or " ".join(head.lower().split())[:120])
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

        article = {k: v for k, v in art.items() if k not in ("fetch", "note", "rate_only", "dedupe_key")}
        if not article.get("text"):
            if art.get("fetch"):
                # A source whose headline says nothing by itself (a filing): without its text there is nothing to rate.
                article["text"] = await art["fetch"]() or ""
                if not article["text"]:
                    return skip("no_text")
            elif cfg.get("fetch_text") and self.fetch_text and art.get("url"):
                text = await self.fetch_text(art["url"])
                if text:
                    article["text"] = text
        expected = await self.expectations.context(sym) if self.expectations else None
        # What the reader should know beforehand: what kind of source this is, and what analysts expect.
        context = "\n".join(x for x in (art.get("note"), expected) if x) or None
        names = {sym: await self.universe.name(sym)}
        for clf in self.shadows:
            spawn(self._shadow(clf, article, sym, names, st, context, dict(base)))
        async with self.sem:
            res = await self.llm.rate(article, [sym], names, {sym: st},
                                      context=context, text_chars=cfg["text_chars"])
        if res["error"]:
            return skip(res["error"][:200])
        self.stats["rated"] += 1
        v = res["verdict"]
        rated_age = (now() - published).total_seconds()
        base.update(verdict=v, model=res["model"], model_s=res["seconds"], tokens_in=res["tokens_in"],
                    tokens_out=res["tokens_out"], size=st["size"], daily_range_pct=st["daily_range_pct"],
                    published=iso(published), rated_age_s=round(rated_age, 1),
                    with_text=bool(article.get("text")), with_expectations=bool(expected))
        # Every rating is logged, traded or not: the evening's price measurement of the same
        # announcement (news_speed) then shows whether the direction was right.
        self.journal.event("fast_rated", **base)
        if v["direction"] == "none" or v["impact"] < cfg["min_impact"] \
                or v["surprise"] < cfg["min_surprise"] or v["confidence"] < cfg["min_confidence"]:
            return
        self.stats["candidates"] += 1

        def reject(reason, **kw):
            self.journal.event("fast_rejected", reason=reason, **base, **kw)

        if art.get("rate_only"):
            return reject("source_off")         # this source is rated to measure it, but not traded yet
        if rated_age > cfg["max_age_s"]:
            return reject("too_old")
        phase = self.session.phase()            # reading took a moment: the session may have changed meanwhile
        if phase not in cfg["sessions"]:
            return reject("session")
        if phase == "regular" and not self.regular_window():
            return reject("session_window")
        if sym in self.entering:
            return reject("busy")               # another announcement of this stock is on its way into the market
        self.entering.add(sym)
        try:
            await self._enter(art, sym, v, res, st, published, rated_age, phase, reject,
                              with_text=bool(article.get("text")), with_expectations=bool(expected))
        finally:
            self.entering.discard(sym)

    async def _enter(self, art, sym, v, res, st, published, rated_age, phase, reject, with_text, with_expectations):
        """From a rating that qualifies to the order: limits, reference price, look at the market, entry."""
        cfg = self.cfg
        ok, why = await self.executor.can_enter("fast", sym, extended=True, max_open=cfg["max_open"])
        if not ok:
            return reject(why)
        if v["direction"] == "down" and not await self.universe.shortable(sym):
            return reject("not_shortable")
        ref, ref_age_min = await self.reference_price(sym, published, phase)
        if not ref:
            return reject("no_reference")
        dirn = 1 if v["direction"] == "up" else -1
        # How far the price may already have run: a share of the move the rating expects.
        chase = cfg["expected_move_pct"][str(v["impact"])] * cfg["chase_frac"]
        limit, seen, expected = ref * (1 + dirn * chase / 100), None, None
        look = cfg.get("look") or {}
        if look.get("enabled"):
            seen = await self.look_at_market(sym, dirn, ref, chase, limit, look)
            if not seen.pop("ok"):
                return reject(seen.pop("reason"), ref_price=ref, **seen)
            limit = seen.pop("limit")
            expected = seen["ask"] if dirn > 0 else seen["bid"]     # where the order will be filled
        stop_pct, target_r, _ = trade_plan(v["impact"], vol_factor(st, self.news_cfg), [], ref, self.news_cfg)
        wide_stop = min(stop_pct * cfg["stop_mult"], self.news_cfg["stop_cap_pct"])
        head = art.get("headline", "")
        entered = await self.executor.enter({
            "strategy": "fast", "symbol": sym, "side": "long" if dirn > 0 else "short",
            "price": ref, "limit_price": limit, "expected_fill": expected,
            "stop_pct": wide_stop, "target_pct": stop_pct * target_r,
            "ref_price": ref, "max_adverse_pct": chase,
            "max_hold_min": cfg["max_hold_min"][phase], "extended": True,
            "risk_pct": cfg["risk_pct"], "max_open": cfg["max_open"], "entry_timeout_s": cfg["entry_timeout_s"],
            "max_notional": cfg["max_notional_small"] if st["size"] == "small" else cfg["max_notional"],
            "meta": {
                "article": art.get("id"), "headline": head[:140], "source": art.get("source"), "verdict": v,
                "model": res["model"], "published": iso(published), "news_to_order_s": round(rated_age, 1),
                "ref_price": ref, "ref_age_min": ref_age_min, "chase_pct": chase, "size": st["size"],
                "with_text": with_text, "with_expectations": with_expectations,
                **({"market": seen} if seen else {}),
            },
        })
        if entered:
            self.stats["entered"] += 1
        else:                       # the executor has logged why ("rejected", stage "risk")
            reject("entry_refused", ref_price=ref)
