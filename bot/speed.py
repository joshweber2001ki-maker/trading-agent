"""How fast do prices react to a company announcement?

Once a day after the close, every press release and SEC filing logged since the last run is
measured against the individual trades of the whole market around its publication time (free
once 15 minutes old): how far the price had moved 1, 2, 5, 10, 30, 60, 120 and 300 seconds
later, how much was traded in that time, and where the price stood after 15 and 30 minutes.
Nothing here trades. The numbers say what a faster news source would be worth.
"""
import asyncio
import time
from datetime import timedelta

from .common import ET, iso, now, parse_ts

HORIZONS_S = (1, 2, 5, 10, 30, 60, 120, 300)
LATER_S = (900, 1800)
BUCKETS = ((0, 2), (2, 10), (10, 60), (60, 300))    # seconds after publication, for traded volume
MAX_REF_AGE_S = 4 * 86400                           # the last trade before may be days old (thin stock, weekend)
FRESH_REF_S = 600                                   # a reference price this recent says the stock was trading
SETTLE_S = LATER_S[-1] + 20 * 60                    # measurable once the last horizon is 20 minutes old
# Trade conditions that do not show the price at that moment (late, averaged or special-term reports).
NOT_A_PRICE = set("WZUL479CNRPHV")
US = timedelta(microseconds=1)


def session_of(t0) -> str:
    """Part of the New York trading day a moment falls into (regular half days are not considered)."""
    t = t0.astimezone(ET)
    m = t.hour * 60 + t.minute
    if t.weekday() >= 5 or m < 240 or m >= 1200:
        return "closed"
    return "pre" if m < 570 else "regular" if m < 960 else "post"


def clean(trades):
    """(time, price, size) of the trades that show a real price at their moment."""
    out = []
    for x in trades:
        if x.get("p", 0) > 0 and x.get("s", 0) > 0 and not NOT_A_PRICE.intersection(x.get("c") or []):
            out.append((parse_ts(x["t"]), x["p"], x["s"]))
    return out


def price_path(pre, post, t0, covered_s=None):
    """What the price did around t0.

    pre: trades before t0, newest first. post: trades from t0 on, oldest first. covered_s: how many
    seconds after t0 `post` is complete (None = all of it); later values are left empty.
    Returns None without any trade before t0. ref_age_s tells how old the reference price is:
    in a thinly traded stock it can be hours old, and the first trade after t0 then shows a gap.
    """
    if not pre or (t0 - pre[0][0]).total_seconds() > MAX_REF_AGE_S:
        return None
    ref, oldest = pre[0][1], pre[-1]
    out = {
        "price": ref,
        "ref_age_s": round((t0 - pre[0][0]).total_seconds()),
        "pre_move": round((ref - oldest[1]) / oldest[1] * 100, 3),     # move over the trades fetched before t0
        "pre_span_s": round((t0 - oldest[0]).total_seconds()),
        "first_trade_s": round((post[0][0] - t0).total_seconds(), 2) if post else None,
    }
    last, i = ref, 0
    for h in HORIZONS_S:
        end = t0 + timedelta(seconds=h)
        while i < len(post) and post[i][0] <= end:
            last = post[i][1]
            i += 1
        out[f"r{h}"] = round((last - ref) / ref * 100, 3) if covered_s is None or h <= covered_s else None
    for lo, hi in BUCKETS:
        if covered_s is not None and hi > covered_s:
            out[f"n_{lo}_{hi}"] = out[f"usd_{lo}_{hi}"] = None
            continue
        xs = [x for x in post if lo <= (x[0] - t0).total_seconds() < hi or (hi == BUCKETS[-1][1] and x[0] - t0 == timedelta(seconds=hi))]
        out[f"n_{lo}_{hi}"] = len(xs)
        out[f"usd_{lo}_{hi}"] = round(sum(p * s for _, p, s in xs))
    return out


class Speed:
    def __init__(self, api, cfg: dict, journal, universe, stock_stats=None):
        self.api, self.cfg, self.journal, self.universe, self.stock_stats = api, cfg, journal, universe, stock_stats
        self.running = False

    def todo(self):
        """Logged announcements that are old enough to be measured and have no result yet, oldest first."""
        done, events = set(), {}
        for r in self.journal.recent_events(days=self.cfg["lookback_days"]):
            kind = r.get("kind")
            if kind == "news_speed":
                done.add((r.get("src"), r.get("ref")))
            elif kind == "wire_item" and r.get("symbol") and r.get("published") and r.get("id"):
                events[(r.get("source") or "wire", r["id"])] = {
                    "symbol": r["symbol"], "t0": parse_ts(r["published"]), "headline": (r.get("headline") or "")[:80]}
            elif kind == "sec_item" and r.get("symbol") and r.get("accepted") and r.get("acc"):
                events[("sec", r["acc"])] = {"symbol": r["symbol"], "t0": parse_ts(r["accepted"]), "items": r.get("items")}
        latest = now() - timedelta(seconds=SETTLE_S)
        earliest = now() - timedelta(days=self.cfg["lookback_days"])
        out = [{"src": k[0], "ref": k[1], **v} for k, v in events.items()
               if k not in done and earliest <= v["t0"] <= latest and v["symbol"] in self.universe.cache]
        out.sort(key=lambda e: e["t0"])
        return out[-self.cfg["max_events"]:]

    async def _price_in(self, sym, start, end):
        """Price of the last real trade between two moments, or None."""
        raw, _ = await self.api.trades(sym, start.isoformat(), end.isoformat(), limit=20, sort="desc")
        xs = clean(raw)
        return xs[0][1] if xs else None

    async def measure(self, ev):
        t0, sym = ev["t0"], ev["symbol"]
        base = {"src": ev["src"], "ref": ev["ref"], "symbol": sym, "t0": iso(t0), "session": session_of(t0)}
        base.update({k: ev[k] for k in ("headline", "items") if ev.get(k)})
        if base["session"] == "closed":
            return {**base, "skipped": "market_closed"}
        raw, _ = await self.api.trades(sym, (t0 - timedelta(seconds=MAX_REF_AGE_S)).isoformat(), (t0 - US).isoformat(),
                                       limit=200, sort="desc")
        pre = clean(raw)
        if not pre:
            return {**base, "skipped": "no_trade_before"}
        end = t0 + timedelta(seconds=HORIZONS_S[-1])
        raw, truncated = await self.api.trades(sym, t0.isoformat(), end.isoformat(), limit=10000)
        post = clean(raw)
        covered = (post[-1][0] - t0).total_seconds() if truncated and post else None
        path = price_path(pre, post, t0, covered)
        if path is None:
            return {**base, "skipped": "no_trade_before"}
        ref = path["price"]
        last = post[-1][1] if post else ref
        if covered is not None:      # a very busy stock: fill the later horizons with one small request each
            for h in HORIZONS_S:
                if path[f"r{h}"] is None:
                    p = await self._price_in(sym, post[-1][0], t0 + timedelta(seconds=h))
                    last = p or last
                    path[f"r{h}"] = round((last - ref) / ref * 100, 3)
        start = end + US
        for h in LATER_S:
            p = await self._price_in(sym, start, t0 + timedelta(seconds=h))
            last = p or last          # no trade in between: the price has not moved
            path[f"r{h}"] = round((last - ref) / ref * 100, 3)
            start = t0 + timedelta(seconds=h) + US
        return {**base, **path, "truncated": truncated}

    async def run(self):
        """Measure everything that is due. Returns the number of announcements measured."""
        if self.running:
            return 0
        self.running = True
        started, count = time.monotonic(), {"measured": 0, "skipped": 0, "errors": 0}
        try:
            todo = self.todo()
            if not todo:
                return 0
            sizes = {}
            if self.stock_stats:
                syms = sorted({e["symbol"] for e in todo})
                for i in range(0, len(syms), 200):
                    sizes.update(await self.stock_stats.get(syms[i:i + 200]))
            sem = asyncio.Semaphore(3)

            async def one(ev):
                async with sem:
                    if count["errors"] >= 25:       # the data source is refusing: stop, try again tomorrow
                        return
                    try:
                        res = await self.measure(ev)
                    except Exception as e:
                        count["errors"] += 1
                        if count["errors"] <= 5:
                            self.journal.event("error", where="speed", symbol=ev["symbol"], error=repr(e)[:200])
                        return
                st = sizes.get(ev["symbol"]) or {}
                self.journal.event("news_speed", **res, size=st.get("size"), dollar_volume=st.get("dollar_volume"))
                count["skipped" if res.get("skipped") else "measured"] += 1

            await asyncio.gather(*(one(ev) for ev in todo))
            self.journal.event("speed_done", due=len(todo), **count, seconds=round(time.monotonic() - started))
            return count["measured"]
        except Exception as e:
            self.journal.event("error", where="speed", error=repr(e)[:300])
            return 0
        finally:
            self.running = False
