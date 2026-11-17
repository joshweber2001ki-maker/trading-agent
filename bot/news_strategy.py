"""News strategy: classify a fresh article, wait for the price to confirm, trade the continuation.

The KI rates each article (direction, surprise, impact, confidence). The impact class decides
whether the article is traded at all and how wide the trade is set up: stop, target and time
limit come from a profile per class, scaled by how far the stock usually swings.
"""
import asyncio
import json
from datetime import timedelta

from .common import iso, now, parse_ts
from .llm import is_noise

HORIZONS = (5, 15, 30, 60)
OUTCOME_DELAY_MIN = 20   # after the close; whole-market prices are free once they are 15 minutes old


def vol_factor(stats, cfg):
    """How much wilder (>1) or calmer (<1) than an average stock this one usually is."""
    if not stats or not stats.get("daily_range_pct"):
        return 1.0
    f = stats["daily_range_pct"] / cfg["reference_daily_range_pct"]
    return max(cfg["vol_factor_min"], min(cfg["vol_factor_max"], f))


def trade_plan(impact, factor, bars, price, cfg):
    """Stop distance (percent), target (multiple of the stop distance) and time limit for one trade."""
    prof = cfg["impact"][str(max(1, min(3, impact)))]
    base = prof["stop_pct"] * factor
    recent = bars[-5:]
    noise = 0.0
    if recent and price > 0:
        noise = (max(b["h"] for b in recent) - min(b["l"] for b in recent)) / price * 100 * cfg["noise_mult"]
    # The stop must sit outside the current noise, but noise may at most double it.
    stop = min(max(base, noise), base * 2)
    stop = max(cfg["stop_floor_pct"], min(cfg["stop_cap_pct"], stop))
    return stop, prof["target_r"], prof["max_hold_min"]


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


def outcome(bars, t0, until):
    """What the price did after t0: percent change after 5/15/30/60 minutes and the extremes.

    `until` is the time up to which the bars are complete; later horizons are None.
    A stock that did not trade in between simply has not moved. Returns None without a
    price from before t0.
    """
    before = [b for b in bars if parse_ts(b["t"]) + timedelta(minutes=1) <= t0]
    if not before:
        return None
    ref = before[-1]["c"]
    after = [b for b in bars if parse_ts(b["t"]) + timedelta(minutes=1) > t0]
    out = {"ref_price": ref, "bars": len(after)}
    for h in HORIZONS:
        end = t0 + timedelta(minutes=h)
        if end > until:
            out[f"ret_{h}"] = None
            continue
        upto = [b for b in after if parse_ts(b["t"]) < end]
        out[f"ret_{h}"] = round(((upto[-1]["c"] if upto else ref) - ref) / ref * 100, 3)
    window = [b for b in after if parse_ts(b["t"]) < t0 + timedelta(minutes=HORIZONS[-1])]
    out["max_up"] = round((max([b["h"] for b in window] + [ref]) - ref) / ref * 100, 3)
    out["max_down"] = round((min([b["l"] for b in window] + [ref]) - ref) / ref * 100, 3)
    return out


class NewsStrategy:
    def __init__(self, api, cfg, journal, session, universe, executor, classifier, orb=None, stats=None):
        self.api, self.journal, self.session = api, journal, session
        self.universe, self.executor, self.llm, self.orb = universe, executor, classifier, orb
        self.stock_stats = stats
        self.cfg = cfg["news"]
        self.orb_min_surprise = cfg["orb"]["premarket_news_min_surprise"]
        self.premarket_from = cfg["orb"]["premarket_news_from"]
        self.seen = set()          # article ids and (symbol, headline) pairs already handled
        self.recent = {}           # symbol -> time of the last trade candidate
        self.sem = asyncio.Semaphore(6)
        self.stats = {"received": 0, "classified": 0, "candidates": 0}

    def reset_day(self):
        if len(self.seen) > 50000:
            self.seen.clear()
        self.recent.clear()
        self.stats = {k: 0 for k in self.stats}

    def phase(self):
        """'live' while trades are allowed, 'premarket' while news only feeds tomorrow's or today's
        breakout candidates (from the close until shortly before the next open), else None."""
        s = self.session
        if s.is_open():
            if s.since_open_min() >= self.cfg["start_after_open_min"] and \
               s.to_close_min() >= self.cfg["no_entry_before_close_min"]:
                return "live"
            return None
        if not (self.orb and self.orb.cfg["enabled"]):
            return None
        if s.premarket(self.premarket_from) or (s.close is not None and now() >= s.close):
            return "premarket"
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
        max_age = art.get("max_age_s") or self.cfg["max_article_age_s"]
        if phase == "live" and age > max_age:
            return skip("stale", age_s=round(age))
        if phase == "premarket" and age > self.orb.cfg["news_candidate_max_age_h"] * 3600:
            return  # old backlog from a feed, irrelevant for the next open
        if is_noise(head):
            return skip("noise")
        tradable = [s for s in symbols if await self.universe.asset(s)]
        if not tradable:
            return
        title_key = (tradable[0], " ".join(head.lower().split())[:120])
        if title_key in self.seen:
            return  # the same release again (second feed, second language version, correction)
        self.seen.add(title_key)
        names = {s: await self.universe.name(s) for s in tradable}
        stats = await self.stock_stats.get(tradable) if self.stock_stats else {}
        snap_task = asyncio.create_task(self.api.snapshots(tradable))
        verdict, err = await self.llm.classify(art, tradable, names, stats)
        snaps = await snap_task
        if err:
            return skip(err)
        self.stats["classified"] += 1
        sym = verdict["symbol"]
        st = stats.get(sym)
        factor = vol_factor(st, self.cfg)
        base.update(symbol=sym, verdict=verdict, age_s=round(age), size=(st or {}).get("size"),
                    daily_range_pct=(st or {}).get("daily_range_pct"), received=iso(received),
                    llm_s=round((now() - received).total_seconds(), 1))
        self.journal.event("news_classified", **base)

        if phase == "premarket":
            if self.orb and verdict["direction"] != "none" and verdict["impact"] >= self.cfg["min_impact"] \
                    and verdict["surprise"] >= self.orb_min_surprise:
                self.orb.add_candidate(sym, source="news", verdict=verdict, headline=head[:140])
            return

        # From here on: during the session. What the price did after each of these articles,
        # traded or not, is measured after the close (log_outcomes), so that the ratings can
        # be checked against it.
        if verdict["direction"] == "none" or verdict["impact"] < self.cfg["min_impact"] \
                or verdict["surprise"] < self.cfg["min_surprise"] \
                or verdict["confidence"] < self.cfg["min_confidence"]:
            return
        self.stats["candidates"] += 1
        last = self.recent.get(sym)
        if last and (now() - last).total_seconds() < self.cfg["symbol_cooldown_min"] * 60:
            # A later article about the same event must not trigger a late entry.
            return self.journal.event("rejected", stage="pre_confirm", reason="recent_candidate", **base)
        self.recent[sym] = now()
        ok, why = await self.executor.can_enter("news", sym)
        if not ok:
            return self.journal.event("rejected", stage="pre_confirm", reason=why, **base)
        snap = snaps.get(sym) or {}
        if not snap.get("latestTrade"):
            return self.journal.event("rejected", stage="pre_confirm", reason="no_price", **base)
        ref = float(snap["latestTrade"]["p"])
        return sym, verdict, ref, received, base, factor

    async def _confirm_and_enter(self, sym, verdict, ref, received, base, factor):
        # A calm stock confirms with a smaller move than a wild one.
        cfg = {**self.cfg, "min_move_pct": self.cfg["min_move_pct"] * factor,
               "max_chase_pct": self.cfg["max_chase_pct"] * factor}
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
            mb = snap.get("minuteBar") or {}
            mvol = float(mb.get("v") or 0)
            state, detail = confirmation(ref, price, dirn, mvol, avg_vol, spread, cfg)
            last_detail = detail
            if state == "abort":
                break
            if state == "confirmed":
                if mb and (not bars or mb.get("t") != bars[-1].get("t")):
                    bars = bars + [mb]   # include the newest minute in the noise measurement
                stop_pct, target_r, max_hold = trade_plan(verdict["impact"], factor, bars, price, self.cfg)
                entered = await self.executor.enter({
                    "strategy": "news", "symbol": sym, "side": "long" if dirn > 0 else "short",
                    "price": price, "stop": price * (1 - dirn * stop_pct / 100),
                    "target_r": target_r, "max_hold_min": max_hold,
                    "meta": {
                        "article": base["article"], "headline": base["headline"], "source": base["source"],
                        "verdict": verdict, "ref_price": ref, "vol_factor": round(factor, 2),
                        "size": base.get("size"),
                        "news_to_signal_s": round((now() - received).total_seconds(), 1) + base["age_s"],
                        "stop_dist_pct": round(stop_pct, 3), "confirm": detail,
                    },
                })
                if entered:
                    return
                last_detail = "entry_rejected"
                break
        self.journal.event("rejected", stage="confirm", reason=last_detail, ref_price=ref, **base)

    async def log_outcomes(self):
        """After the close: log what the price did after every article rated during the session.

        One batch per day, built from the day's own event log, so a restart loses nothing. Prices
        come from the whole market ("sip"): the free data plan allows that once they are 15 minutes
        old, and the IEX prices the bot sees live have gaps of minutes to hours in smaller stocks,
        which would make their news look as if nothing had happened. Returns the number logged.
        """
        s = self.session
        try:
            path = self.journal.dir / f"events-{s.day}.jsonl"
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
            done = {(r.get("article"), r.get("symbol")) for r in rows if r.get("kind") == "news_outcome"}
            todo = [r for r in rows if r.get("kind") == "news_classified" and r.get("phase") == "live"
                    and r.get("symbol") and (r.get("article"), r["symbol"]) not in done]
            if not todo:
                return 0
            syms = sorted({r["symbol"] for r in todo})
            start, end = iso(s.open - timedelta(minutes=30)), iso(s.close - timedelta(seconds=1))
            bars, feed = {}, "sip"
            try:
                for i in range(0, len(syms), 100):
                    bars.update(await self.api.bars_multi(syms[i:i + 100], "1Min", start, end, feed="sip"))
            except Exception as e:   # refused or unavailable: the bot's own feed is better than nothing
                self.journal.event("error", where="news_outcomes_sip", error=repr(e)[:200])
                bars, feed = {}, getattr(self.api, "feed", "iex")
                for i in range(0, len(syms), 100):
                    bars.update(await self.api.bars_multi(syms[i:i + 100], "1Min", start, end))
            logged = 0
            for r in todo:
                v = r.get("verdict") or {}
                # Older log lines carry no receive time: the line was written right after the KI answered.
                t0 = parse_ts(r["received"]) if r.get("received") else \
                    parse_ts(r["t"]) - timedelta(seconds=r.get("llm_s") or 0)
                out = outcome(bars.get(r["symbol"]) or [], t0, min(t0 + timedelta(minutes=HORIZONS[-1]), s.close))
                if not out:
                    continue
                logged += 1
                self.journal.event(
                    "news_outcome", article=r.get("article"), symbol=r["symbol"], source=r.get("source"),
                    received=iso(t0), feed=feed, direction=v.get("direction"), surprise=v.get("surprise"),
                    impact=v.get("impact"), confidence=v.get("confidence"), category=v.get("category"),
                    size=r.get("size"), daily_range_pct=r.get("daily_range_pct"), **out,
                )
            return logged
        except Exception as e:
            self.journal.event("error", where="news_outcomes", error=repr(e)[:300])
            return 0
