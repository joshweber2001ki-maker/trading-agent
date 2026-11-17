"""Opening range breakout on the day's "stocks in play".

Before the open a pool of candidates is collected (fixed list, biggest movers, most active,
pre-market news). Once the opening range is complete, the pool is ranked by relative volume
and the best names are watched for a breakout out of that range.
"""
import asyncio
from datetime import timedelta

from .common import ET, iso, now, parse_ts


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


def relative_volume(today_vol, hist_bars, open_hm=(9, 30)):
    """Today's opening-range volume against the average of the same window on earlier days."""
    vols = []
    for b in hist_bars:
        t = parse_ts(b["t"]).astimezone(ET)
        if (t.hour, t.minute) == open_hm:
            vols.append(b["v"])
    vols = vols[-10:]
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
    def __init__(self, api, cfg, journal, session, universe, executor):
        self.api, self.journal, self.session = api, journal, session
        self.universe, self.executor = universe, executor
        self.cfg = cfg["orb"]
        self.reset_day()

    def reset_day(self):
        self.pool = {}        # symbol -> why it is in the pool
        self.ranges = {}      # symbol -> opening range, gap, rvol (the watched candidates)
        self.fired = set()
        self.stage = "idle"   # idle -> pooled -> ranged -> done

    def add_candidate(self, symbol, **info):
        self.pool.setdefault(symbol, info)

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
            self.add_candidate(sym, source="static")
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
        self.journal.event("orb_pool", size=len(self.pool), symbols=sorted(self.pool))

    async def _measure(self, sym, gap, sem):
        """Opening range and relative volume for one symbol."""
        s, n = self.session, self.cfg["range_min"]
        async with sem:
            bars = await self.api.bars(sym, "1Min", iso(s.open), iso(s.open + timedelta(minutes=n)))
            rng = opening_range(bars, s.open, n)
            if not rng:
                return sym, None, "no_bars"
            hist = await self.api.bars(sym, f"{n}Min", iso(s.open - timedelta(days=21)), iso(s.open))
        rvol = relative_volume(rng["volume"], hist, (s.open.hour, s.open.minute))
        mid = (rng["high"] + rng["low"]) / 2
        info = {
            **rng, "gap_pct": round(gap, 2), "rvol": round(rvol, 2) if rvol is not None else None,
            "width_pct": round((rng["high"] - rng["low"]) / mid * 100, 2),
            "source": self.pool[sym].get("source"),
        }
        return sym, info, assess(rng, gap, rvol, self.cfg, is_news=info["source"] == "news")

    async def _rank(self):
        """After the opening range: measure every pool symbol and keep the best by relative volume."""
        today, cfg = self.session.day, self.cfg
        gaps, syms = {}, list(self.pool)
        for i in range(0, len(syms), 50):
            for sym, sn in (await self.api.snapshots(syms[i:i + 50])).items():
                d, p = sn.get("dailyBar") or {}, sn.get("prevDailyBar") or {}
                if not d or not p or not p.get("c"):
                    continue
                if parse_ts(d["t"]).astimezone(ET).strftime("%Y-%m-%d") != today:
                    continue  # no trade today yet on this data feed
                if d["o"] >= cfg["min_price"]:
                    gaps[sym] = (d["o"] - p["c"]) / p["c"] * 100
        sem = asyncio.Semaphore(4)
        results = await asyncio.gather(
            *(self._measure(sym, gap, sem) for sym, gap in gaps.items()), return_exceptions=True
        )
        passed = []
        for r in results:
            if isinstance(r, Exception):
                self.journal.event("error", where="orb_rank", error=repr(r)[:200])
                continue
            sym, info, reason = r
            if reason:
                self.journal.event("rejected", stage="orb_rank", symbol=sym, reason=reason, **(info or {}))
            else:
                passed.append((info["rvol"], sym, info))
        passed.sort(reverse=True)
        self.ranges = {sym: info for _, sym, info in passed[: cfg["max_candidates"]]}
        for _, sym, info in passed[cfg["max_candidates"]:]:
            self.journal.event("rejected", stage="orb_rank", symbol=sym, reason="not_top_rvol", **info)
        self.journal.event("orb_ranges", measured=len(gaps), ranges=self.ranges)

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
            dist = stop_distance_pct(rng["width_pct"], cfg["stop_range_frac"], cfg["stop_min_pct"], cfg["stop_max_pct"])
            await self.executor.enter({
                "strategy": "orb", "symbol": sym, "side": side, "price": price,
                "stop": price * (1 - dirn * dist / 100),
                "target_r": cfg["target_r"], "max_hold_min": cfg["max_hold_min"],
                "meta": {
                    **rng, "stop_dist_pct": round(dist, 3),
                    "with_gap": (rng["gap_pct"] > 0) == (side == "long"),
                    "with_range_dir": rng.get("range_dir") == ("up" if side == "long" else "down"),
                },
            })
