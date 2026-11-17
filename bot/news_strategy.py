"""News strategy: classify a fresh article, wait for the price to confirm, trade the continuation."""
import asyncio
from datetime import timedelta

from .common import iso, now, parse_ts
from .llm import is_noise


def stop_distance_pct(bars, price, mult, lo, hi):
    """Stop distance in percent: a fraction of the last 10 minutes' price range, clamped."""
    recent = bars[-10:]
    if not recent or price <= 0:
        return lo
    rng = max(b["h"] for b in recent) - min(b["l"] for b in recent)
    return max(lo, min(hi, mult * rng / price * 100))


def confirmation(ref, price, dirn, minute_vol, avg_vol, spread_pct, cfg):
    """Returns (state, detail): 'confirmed', 'wait', or 'abort'."""
    move = (price - ref) / ref * 100 * dirn
    if move > cfg["max_chase_pct"]:
        return "abort", "too_late"
    if move < -cfg["max_chase_pct"] / 2:
        return "abort", "moved_against"
    if move < cfg["min_move_pct"]:
        return "wait", "no_move"
    if spread_pct is not None and spread_pct > cfg["max_spread_pct"]:
        return "wait", "spread"
    if avg_vol > 0 and minute_vol < cfg["volume_mult"] * avg_vol:
        return "wait", "volume"
    return "confirmed", f"move {move:.2f}%"


class NewsStrategy:
    def __init__(self, api, cfg, journal, session, universe, executor, classifier, orb=None):
        self.api, self.journal, self.session = api, journal, session
        self.universe, self.executor, self.llm, self.orb = universe, executor, classifier, orb
        self.cfg = cfg["news"]
        self.orb_min_surprise = cfg["orb"]["premarket_news_min_surprise"]
        self.premarket_from = cfg["orb"]["premarket_news_from"]
        self.seen = set()
        self.sem = asyncio.Semaphore(6)
        self.stats = {"received": 0, "classified": 0, "candidates": 0}

    def reset_day(self):
        self.seen.clear()
        self.stats = {k: 0 for k in self.stats}

    def phase(self):
        s = self.session
        if s.premarket(self.premarket_from):
            return "premarket" if self.orb and self.orb.cfg["enabled"] else None
        if s.is_open():
            if s.since_open_min() >= self.cfg["start_after_open_min"] and \
               s.to_close_min() >= self.cfg["no_entry_before_close_min"]:
                return "live"
        return None

    async def on_article(self, art: dict):
        received = now()
        try:
            # The semaphore only limits parallel KI calls. Waiting for price confirmation
            # happens outside of it, so a few candidates cannot block a whole batch of news.
            async with self.sem:
                plan = await self._classify(art, received)
            if plan:
                await self._confirm_and_enter(*plan)
        except Exception as e:
            self.journal.event("error", where="news", article=art.get("id"), error=repr(e)[:300])

    async def _classify(self, art, received):
        """Filter and classify one article. Returns the arguments for the confirmation step, or None."""
        aid = art.get("id")
        if aid in self.seen:
            return
        self.seen.add(aid)
        self.stats["received"] += 1
        phase = self.phase()
        if not phase or not self.cfg["enabled"]:
            return
        created = parse_ts(art["created_at"]) if art.get("created_at") else received
        age = (received - created).total_seconds()
        symbols = art.get("symbols") or []
        head = art.get("headline", "")
        base = dict(article=aid, headline=head[:140], source=art.get("source"), phase=phase)

        def skip(reason, **kw):
            self.journal.event("news_skipped", reason=reason, **base, **kw)

        if not symbols or len(symbols) > self.cfg["max_symbols"]:
            return  # market-wide or list articles: not logged, far too many
        if phase == "live" and age > self.cfg["max_article_age_s"]:
            return skip("stale", age_s=round(age))
        if is_noise(head):
            return skip("noise")
        tradable = [s for s in symbols if await self.universe.asset(s)]
        if not tradable:
            return
        names = {s: await self.universe.name(s) for s in tradable}
        snap_task = asyncio.create_task(self.api.snapshots(tradable))
        verdict, err = await self.llm.classify(art, tradable, names)
        snaps = await snap_task
        if err:
            return skip(err)
        self.stats["classified"] += 1
        sym = verdict["symbol"]
        base.update(symbol=sym, verdict=verdict, age_s=round(age),
                    llm_s=round((now() - received).total_seconds(), 1))
        self.journal.event("news_classified", **base)
        if verdict["direction"] == "none":
            return

        if phase == "premarket":
            if self.orb and verdict["surprise"] >= self.orb_min_surprise:
                self.orb.add_candidate(sym, source="news", verdict=verdict, headline=head[:140])
            return

        if verdict["surprise"] < self.cfg["min_surprise"] or verdict["confidence"] < self.cfg["min_confidence"]:
            return
        self.stats["candidates"] += 1
        ok, why = await self.executor.can_enter("news", sym)
        if not ok:
            return self.journal.event("rejected", stage="pre_confirm", reason=why, **base)
        snap = snaps.get(sym) or {}
        if not snap.get("latestTrade"):
            return self.journal.event("rejected", stage="pre_confirm", reason="no_price", **base)
        ref = float(snap["latestTrade"]["p"])
        return sym, verdict, ref, received, base

    async def _confirm_and_enter(self, sym, verdict, ref, received, base):
        cfg = self.cfg
        dirn = 1 if verdict["direction"] == "up" else -1
        start = iso(now() - timedelta(minutes=35))
        bars = await self.api.bars(sym, "1Min", start)
        hist = bars[:-1] if len(bars) > 1 else bars
        avg_vol = sum(b["v"] for b in hist) / len(hist) if hist else 0
        deadline = received + timedelta(seconds=cfg["confirm_window_s"])
        last_detail = "timeout"
        while now() < deadline:
            await asyncio.sleep(5)
            snap = await self.api.snapshot(sym)
            if not snap or not snap.get("latestTrade"):
                continue
            price = float(snap["latestTrade"]["p"])
            q = snap.get("latestQuote") or {}
            bp, ap = float(q.get("bp") or 0), float(q.get("ap") or 0)
            spread = (ap - bp) / ((ap + bp) / 2) * 100 if bp > 0 and ap > bp else None
            mvol = float((snap.get("minuteBar") or {}).get("v") or 0)
            state, detail = confirmation(ref, price, dirn, mvol, avg_vol, spread, cfg)
            last_detail = detail
            if state == "abort":
                break
            if state == "confirmed":
                dist_pct = stop_distance_pct(
                    bars, price, cfg["stop_range_mult"], cfg["stop_min_pct"], cfg["stop_max_pct"]
                )
                entered = await self.executor.enter({
                    "strategy": "news", "symbol": sym, "side": "long" if dirn > 0 else "short",
                    "price": price, "stop": price * (1 - dirn * dist_pct / 100),
                    "target_r": cfg["target_r"], "max_hold_min": cfg["max_hold_min"],
                    "meta": {
                        "article": base["article"], "headline": base["headline"],
                        "verdict": verdict, "ref_price": ref,
                        "news_to_signal_s": round((now() - received).total_seconds(), 1) + base["age_s"],
                        "stop_dist_pct": round(dist_pct, 3), "confirm": detail,
                    },
                })
                if entered:
                    return
                last_detail = "entry_rejected"
                break
        self.journal.event("rejected", stage="confirm", reason=last_detail, ref_price=ref, **base)
