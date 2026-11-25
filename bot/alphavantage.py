"""What analysts expect of the coming quarters and years (Alpha Vantage, free key).

Finnhub's free plan names the expectations for the quarter a company is reporting right now. How
a company's own outlook is to be read depends on what analysts expect of the NEXT quarter and of
the year, and those come from here: one request per company (EARNINGS_ESTIMATES).

The free key allows 25 requests a day and one a second (checked on the server on 25.11.2026). So
an answer is kept for a week, also across restarts; the companies that report next are asked for
first; and a few requests are held back for the day, for an announcement about a company's outlook
that comes outside its results.

The key comes from the environment (ALPHAVANTAGE_API_KEY). It is part of every request's address:
no address and no error text of the HTTP library is ever logged.
"""
import asyncio
import json
import re
import time
from datetime import date, timedelta

import httpx

from .common import DATA, ET, iso, now, parse_ts, spawn

URL = "https://www.alphavantage.co/query"
SPACING_S = 1.6         # the free key takes one request a second
# An announcement that speaks of what the company expects: there the analysts' numbers are worth a request.
OUTLOOK = re.compile(r"\b(guidance|outlook|preliminary (unaudited )?(results|revenue|sales|financial))\b", re.I)


class Refused(Exception):
    """The service answered with a notice instead of data. `today` is False for "not so fast"."""
    def __init__(self, today: bool):
        super().__init__("daily limit" if today else "too fast")
        self.today = today


def read_answer(data) -> list:
    """The records of an answer (one per fiscal quarter or year). Raises Refused for a notice about limits."""
    if not isinstance(data, dict):
        return []
    note = " ".join(str(data.get(k) or "") for k in ("Information", "Note"))
    if note.strip():
        raise Refused(today=not re.search(r"sparingly|per second", note, re.I))
    rows = next((v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)), None)
    return rows or []


def number(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def forward_rows(rows, today: date, back_days: int = 70, quarters: int = 3, years: int = 2) -> list:
    """The periods that matter now: the quarters and fiscal years that end from about two months ago
    on (a quarter that has just ended is usually the one about to be reported), earliest first."""
    out = {"fiscal quarter": [], "fiscal year": []}
    for r in rows or []:
        kind = str(r.get("horizon") or "").lower()
        try:
            end = date.fromisoformat(str(r.get("date")))
        except ValueError:
            continue
        eps, rev = number(r.get("eps_estimate_average")), number(r.get("revenue_estimate_average"))
        if kind not in out or end < today - timedelta(days=back_days) or (eps is None and rev is None):
            continue
        out[kind].append({"horizon": kind, "date": end.isoformat(), "eps": eps, "revenue": rev,
                          "eps_analysts": number(r.get("eps_estimate_analyst_count")),
                          "revenue_analysts": number(r.get("revenue_estimate_analyst_count"))})
    pick = lambda kind, n: sorted(out[kind], key=lambda r: r["date"])[:n]
    return pick("fiscal quarter", quarters) + pick("fiscal year", years)


def money(x) -> str:
    x = float(x)
    if abs(x) >= 1e9:
        return f"{x / 1e9:.2f} billion"
    if abs(x) >= 1e6:
        return f"{x / 1e6:.1f} million"
    return f"{x:,.0f}"


def estimates_text(symbol: str, rows: list, asked: str = "") -> str:
    """The periods as a few lines for the model that rates an announcement; empty without any."""
    lines = []
    for r in rows:
        parts = []
        if r.get("eps") is not None:
            n = f" ({r['eps_analysts']:.0f} analysts)" if r.get("eps_analysts") else ""
            parts.append(f"earnings per share {r['eps']:.2f}{n}")
        if r.get("revenue") is not None:
            n = f" ({r['revenue_analysts']:.0f} analysts)" if r.get("revenue_analysts") else ""
            parts.append(f"revenue {money(r['revenue'])}{n}")
        name = "quarter" if r["horizon"] == "fiscal quarter" else "fiscal year"
        lines.append(f"- {name} ending {r['date']}: " + ", ".join(parts))
    if not lines:
        return ""
    return (f"What analysts expect of {symbol} on average, by period"
            + (f" (as of {asked[:10]})" if asked else "") + "; a period that has ended may already be reported:\n"
            + "\n".join(lines)
            + "\nCompare an outlook or preliminary figures in the article with these. An outlook in line with them is no surprise.")


class Estimates:
    def __init__(self, key: str, cfg: dict = None, journal=None, path=None):
        self.key, self.cfg, self.journal = (key or "").strip(), cfg or {}, journal
        self.path = path or DATA / "state" / "estimates.json"
        self.http = httpx.AsyncClient(timeout=self.cfg.get("request_timeout_s", 8))
        self.per_day = self.cfg.get("per_day", 24)
        self.reserve = self.cfg.get("reserve_per_day", 8)       # held back from the look ahead, for the day's announcements
        self.ttl = timedelta(days=self.cfg.get("ttl_days", 7))
        self.cache = {}           # symbol -> {"at": when asked, "rows": the periods that mattered then}
        self.day, self.used = None, 0
        self.lock, self.last = asyncio.Lock(), 0.0
        self.stats = {"asked": 0, "with_estimates": 0, "errors": 0}
        self._load()

    @property
    def available(self):
        return bool(self.cfg.get("enabled") and self.key)

    # ── what is kept ────────────────────────────────────────────────────────
    def _load(self):
        try:
            d = json.loads(self.path.read_text())
            self.cache, self.day, self.used = d.get("symbols") or {}, d.get("day"), int(d.get("used") or 0)
        except (OSError, ValueError, TypeError):
            pass

    def _save(self):
        old = iso(now() - timedelta(days=30))
        self.cache = {s: v for s, v in self.cache.items() if v.get("at", "") >= old}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"day": self.day, "used": self.used, "symbols": self.cache}))
        except OSError:
            pass                    # then it is asked again after a restart

    def _today(self):
        """The day the limit counts for. A new day starts with a fresh count."""
        day = now().astimezone(ET).date().isoformat()
        if day != self.day:
            self.day, self.used = day, 0
        return day

    def left(self, reserve: int = 0) -> int:
        self._today()
        return max(0, self.per_day - reserve - self.used)

    def fresh(self, symbol) -> bool:
        got = self.cache.get(symbol)
        try:
            return bool(got) and now() - parse_ts(got["at"]) < self.ttl
        except (KeyError, ValueError):
            return False

    # ── asking ──────────────────────────────────────────────────────────────
    async def _ask(self, symbol) -> list:
        gap = SPACING_S - (time.monotonic() - self.last)
        if gap > 0:
            await asyncio.sleep(gap)
        self.last = time.monotonic()
        self.used += 1
        self.stats["asked"] += 1
        r = await self.http.get(URL, params={"function": "EARNINGS_ESTIMATES", "symbol": symbol, "apikey": self.key})
        if r.status_code != 200:
            raise RuntimeError(f"status {r.status_code}")
        return read_answer(r.json())

    async def fetch(self, symbol) -> bool:
        """Ask for one company and keep the answer. True if an answer (with or without estimates) is kept. Never raises."""
        async with self.lock:
            if self.fresh(symbol):
                return True
            if not self.available or self.left() <= 0:
                return False
            try:
                try:
                    rows = await self._ask(symbol)
                except Refused as e:
                    if e.today:
                        raise
                    await asyncio.sleep(2.0)            # "not so fast": once more, a little later
                    rows = await self._ask(symbol)
            except Refused as e:
                if e.today:
                    self.used = self.per_day            # nothing more today
                    self._event("estimates_limit", symbol=symbol)
                self._save()
                return False
            except Exception as e:
                # the request's address holds the key: only the kind of failure is logged
                self.stats["errors"] += 1
                self._event("error", where="estimates", symbol=symbol, error=type(e).__name__)
                self._save()
                return False
            keep = forward_rows(rows, now().astimezone(ET).date())
            self.cache[symbol] = {"at": iso(now()), "rows": keep}
            self.stats["with_estimates"] += bool(keep)
            self._save()
            return True

    def _event(self, kind, **kw):
        if self.journal:
            self.journal.event(kind, **kw)

    async def prefetch(self, symbols) -> int:
        """Ask ahead for these companies, most important first, as far as today's requests reach
        (less the ones held back). Returns how many were asked."""
        n = 0
        for sym in symbols:
            if not self.available or self.left(self.reserve) <= 0:
                break
            if self.fresh(sym):
                continue
            n += await self.fetch(sym)
        return n

    async def context(self, symbol, fetch: bool = False, timeout: float = 1.2):
        """The lines for the rating of an announcement of this company, or None. With `fetch`, a
        company not asked for yet is asked now, but never for longer than `timeout`."""
        if not self.available:
            return None
        if fetch and not self.fresh(symbol) and self.left() > 0:
            try:
                # The rating does not wait longer than this; the answer is still taken when it comes,
                # for the next announcement of the same company.
                await asyncio.wait_for(asyncio.shield(spawn(self.fetch(symbol))), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        try:
            got = self.cache.get(symbol)
            if not got or not self.fresh(symbol):
                return None
            return estimates_text(symbol, got.get("rows") or [], got.get("at", "")) or None
        except Exception:           # a damaged entry in the kept file must not cost the rating
            return None
