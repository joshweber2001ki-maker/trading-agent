"""Opening range breakout on the day's "stocks in play".

Before the open a pool of candidates is collected (fixed list, biggest movers, most active,
pre-market news). Once the opening range is complete, the pool is ranked by relative volume
and the best names are watched for a breakout out of that range.
"""
import asyncio
import json
from datetime import datetime, timedelta

from .common import DATA, ET, iso, now, parse_ts


def opening_range(bars, open_dt, minutes):
    """High, low, volume and direction of the 1-minute bars inside the opening range."""
    end = open_dt + timedelta(minutes=minutes)
    inside = [b for b in bars if open_dt <= parse_ts(b["t"]) < end]
    if not inside:
        return None
    first_open, last_close = inside[0].get("o"), inside[-1].get("c")
    return {
        "high": max(b["h"] for b in inside), "low": min(b["l"] for b in inside),
        "volume": sum(b["v"] for b in inside), "bars": len(inside),
        "open": first_open, "close": last_close,
        "range_dir": None if first_open is None or last_close is None
        else ("up" if last_close > first_open else "down"),
    }


def relative_volume(today_vol, earlier_vols):
    """Today's opening-range volume against the average of the same window on earlier days."""
    vols = [v for v in earlier_vols if v][-10:]
    if len(vols) < 3:
        return None
    avg = sum(vols) / len(vols)
    return today_vol / avg if avg > 0 else None


def breakout(price, rng, buffer_pct):
    if price > rng["high"] * (1 + buffer_pct / 100):
        return "long"
    if price < rng["low"] * (1 - buffer_pct / 100):
        return "short"
    return None


def stop_distance_pct(width_pct, frac, lo, hi):
    """Stop distance in percent of price: a fraction of the opening range, clamped."""
    return max(lo, min(hi, frac * width_pct))


def assess(rng, gap_pct, rvol, cfg, is_news=False):
    """Why a candidate is rejected, or None if it qualifies."""
    mid = (rng["high"] + rng["low"]) / 2
    width = (rng["high"] - rng["low"]) / mid * 100
    if rng["volume"] * mid < cfg["min_range_dollar_volume"]:
        return "illiquid"
    if abs(gap_pct) > cfg["max_gap_pct"]:
        return "gap_implausible"
    if abs(gap_pct) < cfg["min_gap_pct"] and not is_news:
        return "gap_too_small"
    if width < cfg["min_range_pct"]:
        return "range_too_narrow"
    if width > cfg["max_range_pct"]:
        return "range_too_wide"
    if rvol is None:
        return "no_volume_history"
    if rvol < cfg["min_rvol"]:
        return "low_rvol"
    return None


class OrbStrategy:
    def __init__(self, api, cfg, journal, session, universe, executor, root=None):
        self.api, self.journal, self.session = api, journal, session
        self.universe, self.executor = universe, executor
        self.cfg = cfg["orb"]
        self.pool_file = (root or DATA) / "state" / "orb_news_pool.json"
        self.pool = self._load_news_pool()
        self.reset_day()

    def _load_news_pool(self):
        """News candidates survive a restart (they are collected over many hours)."""
        try:
            raw = json.loads(self.pool_file.read_text())
            return {s: {**i, "added": datetime.fromisoformat(i["added"])} for s, i in raw.items()}
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def _save_news_pool(self):
        try:
            self.pool_file.parent.mkdir(parents=True, exist_ok=True)
            news = {s: {**i, "added": i["added"].isoformat()} for s, i in self.pool.items() if i.get("source") == "news"}
            self.pool_file.write_text(json.dumps(news))
        except OSError:
            pass

    def reset_day(self):
        # News candidates collected since yesterday's close stay; everything else is rebuilt.
        keep_after = now() - timedelta(hours=self.cfg["news_candidate_max_age_h"])
        old = getattr(self, "pool", {})
        self.pool = {s: i for s, i in old.items() if i.get("source") == "news" and i["added"] >= keep_after}
        self._save_news_pool()
        self.ranges = {}      # symbol -> opening range, gap, rvol (the watched candidates)
        self.fired = set()
        self.stage = "idle"   # idle -> pooled -> ranged -> done

    def add_candidate(self, symbol, **info):
        if info.get("source") == "news" or symbol not in self.pool:
            self.pool[symbol] = {**info, "added": now()}
            if info.get("source") == "news":
                self._save_news_pool()

    def entry_deadline(self):
        h, m = map(int, self.cfg["entry_until"].split(":"))
        return self.session.open.replace(hour=h, minute=m)

    async def step(self):
        """Called every few seconds; advances the day's state machine."""
        s, cfg = self.session, self.cfg
        if not cfg["enabled"] or not s.trading_day or self.stage == "done":
            return
        t = now()
        if self.stage == "idle" and t >= s.open - timedelta(minutes=4):
            await self._build_pool()
            self.stage = "pooled"
        elif self.stage == "pooled" and t >= s.open + timedelta(minutes=cfg["range_min"], seconds=20):
            await self._rank()
            self.stage = "ranged" if self.ranges else "done"
        elif self.stage == "ranged":
            if t >= self.entry_deadline() or len(self.fired) >= len(self.ranges):
                self.stage = "done"
                self.journal.event("orb_done", traded=sorted(self.fired), watched=sorted(self.ranges))
            else:
                await self._watch()

    async def _build_pool(self):
        for sym in self.cfg["static_pool"]:
            self.add_candidate(sym, source="static")   # does not replace a news entry for the same symbol
        try:
            mv = await self.api.movers(30)
            for row in (mv.get("gainers") or []) + (mv.get("losers") or []):
                if row.get("price", 0) >= self.cfg["min_price"]:
                    self.add_candidate(row["symbol"], source="movers")
            for row in await self.api.most_actives(30):
                self.add_candidate(row["symbol"], source="most_active")
        except Exception as e:
            self.journal.event("error", where="orb_pool", error=repr(e)[:200])
        for sym in list(self.pool):
            if not await self.universe.asset(sym) or (
                self.cfg["exclude_funds"] and await self.universe.is_fund(sym)
            ):
                self.pool.pop(sym)
        # Keep the pool measurable in one go: all fixed and screener names, the most surprising news names.
        news = sorted((s for s, i in self.pool.items() if i.get("source") == "news"),
                      key=lambda s: -self.pool[s].get("verdict", {}).get("surprise", 0))
        for sym in news[self.cfg["max_news_candidates"]:]:
            self.pool.pop(sym)
        self.journal.event("orb_pool", size=len(self.pool), news=news[: self.cfg["max_news_candidates"]],
                           symbols=sorted(self.pool))

    async def _earlier_opens(self, days=10):
        """Opening times of the previous trading days (handles holidays and half days)."""
        s = self.session
        start = (s.open - timedelta(days=days * 2 + 6)).strftime("%Y-%m-%d")
        end = (s.open - timedelta(days=1)).strftime("%Y-%m-%d")
        out = []
        for c in await self.api.calendar(start, end):
            h, m = map(int, c["open"].split(":"))
            y, mo, d = map(int, c["date"].split("-"))
            out.append(s.open.replace(year=y, month=mo, day=d, hour=h, minute=m))
        return out[-days:]

    async def _rank(self):
        """After the opening range: measure every pool symbol and keep the best by relative volume."""
        s, cfg, n = self.session, self.cfg, self.cfg["range_min"]
        gaps, syms = {}, list(self.pool)
        for i in range(0, len(syms), 50):
            for sym, sn in (await self.api.snapshots(syms[i:i + 50])).items():
                d, p = sn.get("dailyBar") or {}, sn.get("prevDailyBar") or {}
                if not d or not p or not p.get("c"):
                    continue
                if parse_ts(d["t"]).astimezone(ET).strftime("%Y-%m-%d") != s.day:
                    continue  # no trade today yet on this data feed
                if d["o"] >= cfg["min_price"]:
                    gaps[sym] = (d["o"] - p["c"]) / p["c"] * 100
        live = list(gaps)
        span = timedelta(minutes=n)
        today = await self.api.bars_multi(live, "1Min", iso(s.open), iso(s.open + span))
        opens = await self._earlier_opens()
        earlier = await asyncio.gather(
            *(self.api.bars_multi(live, f"{n}Min", iso(o), iso(o + span - timedelta(seconds=1))) for o in opens)
        )
        passed = []
        for sym in live:
            rng = opening_range(today.get(sym) or [], s.open, n)
            if not rng:
                self.journal.event("rejected", stage="orb_rank", symbol=sym, reason="no_bars")
                continue
            vols = [day[sym][0]["v"] for day in earlier if day.get(sym)]
            rvol = relative_volume(rng["volume"], vols)
            mid = (rng["high"] + rng["low"]) / 2
            info = {
                **rng, "gap_pct": round(gaps[sym], 2), "rvol": round(rvol, 2) if rvol is not None else None,
                "width_pct": round((rng["high"] - rng["low"]) / mid * 100, 2),
                "source": self.pool[sym].get("source"),
            }
            reason = assess(rng, gaps[sym], rvol, cfg, is_news=info["source"] == "news")
            if reason:
                self.journal.event("rejected", stage="orb_rank", symbol=sym, reason=reason, **info)
            else:
                passed.append((info["rvol"], sym, info))
        passed.sort(reverse=True)
        self.ranges = {sym: info for _, sym, info in passed[: cfg["max_candidates"]]}
        for _, sym, info in passed[cfg["max_candidates"]:]:
            self.journal.event("rejected", stage="orb_rank", symbol=sym, reason="not_top_rvol", **info)
        self.journal.event("orb_ranges", pool=len(syms), measured=len(live), ranges=self.ranges)

    async def _watch(self):
        cfg = self.cfg
        open_syms = [s for s in self.ranges if s not in self.fired]
        snaps = await self.api.snapshots(open_syms)
        for sym in open_syms:
            sn = snaps.get(sym) or {}
            if not sn.get("latestTrade"):
                continue
            price = float(sn["latestTrade"]["p"])
            rng = self.ranges[sym]
            side = breakout(price, rng, cfg["breakout_buffer_pct"])
            if not side:
                continue
            self.fired.add(sym)
            dirn = 1 if side == "long" else -1
            with_gap = (rng["gap_pct"] > 0) == (side == "long")
            with_range = rng.get("range_dir") == ("up" if side == "long" else "down")
            if (cfg["direction"] == "gap" and not with_gap) or (cfg["direction"] == "range" and not with_range):
                self.journal.event("rejected", stage="orb_breakout", symbol=sym, side=side,
                                   reason=f"against_{cfg['direction']}")
                continue
            dist = stop_distance_pct(rng["width_pct"], cfg["stop_range_frac"], cfg["stop_min_pct"], cfg["stop_max_pct"])
            await self.executor.enter({
                "strategy": "orb", "symbol": sym, "side": side, "price": price,
                "stop": price * (1 - dirn * dist / 100),
                "target_r": cfg["target_r"], "max_hold_min": cfg["max_hold_min"],
                "meta": {
                    **rng, "stop_dist_pct": round(dist, 3),
                    "with_gap": with_gap, "with_range_dir": with_range,
                },
            })
