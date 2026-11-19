"""Finnhub: when companies report and what analysts expect (free plan).

A quarterly report can only be judged against what was expected. Finnhub's earnings calendar
names the day, whether the report comes before the open or after the close, and the expected
profit per share and revenue.

The key comes from the environment (FINNHUB_API_KEY) and is stored only on the server. It is
sent in a header, never in the address, so it cannot end up in a log line or an error message.
"""
import asyncio
import time
from datetime import date, timedelta

import httpx

from .common import ET, now

BASE = "https://finnhub.io/api/v1"
HOURS = {"bmo": "before the market opens", "amc": "after the market close", "dmh": "during market hours"}


class FinnhubError(Exception):
    def __init__(self, status, path, body=""):
        super().__init__(f"{status} {path}: {str(body)[:200]}")
        self.status, self.path = status, path


class Finnhub:
    def __init__(self, key: str, max_per_min: int = 50, http=None):
        self.key = (key or "").strip()
        self.http = http or httpx.AsyncClient(timeout=15)
        # The free plan allows 60 calls per minute; stay a little below it.
        self._max, self._stamps, self._lock = max_per_min, [], asyncio.Lock()
        self.plan_limit = None      # calls per minute, as the service reports it

    @property
    def available(self):
        return bool(self.key)

    async def _throttle(self):
        async with self._lock:
            while True:
                t = time.monotonic()
                self._stamps = [s for s in self._stamps if t - s < 60]
                if len(self._stamps) < self._max:
                    self._stamps.append(t)
                    return
                await asyncio.sleep(0.5)

    async def get(self, path: str, **params):
        """One call. Raises FinnhubError for anything but a normal answer (403 = not part of this plan)."""
        if not self.available:
            raise FinnhubError("no_key", path)
        r = None
        for attempt in (1, 2):
            await self._throttle()
            try:
                r = await self.http.get(BASE + path, params=params, headers={"X-Finnhub-Token": self.key})
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise FinnhubError("network", path, type(e).__name__)
                await asyncio.sleep(1)
                continue
            if r.status_code != 429 or attempt == 2:
                break
            await asyncio.sleep(2)      # over the limit: wait a moment, then one more try
        limit = r.headers.get("x-ratelimit-limit")
        if limit and limit.isdigit():
            self.plan_limit = int(limit)
        if r.status_code != 200:
            raise FinnhubError(r.status_code, path, r.text)
        try:
            return r.json()
        except ValueError:
            raise FinnhubError("not_json", path, r.text)

    async def earnings_calendar(self, start: str, end: str, symbol: str = None) -> list:
        """Reports between two days (YYYY-MM-DD). Each row: symbol, date, hour ("bmo" before the open,
        "amc" after the close, "dmh" during the session, "" unknown), quarter, year, epsEstimate,
        revenueEstimate, and epsActual / revenueActual once the report is out."""
        params = {"from": start, "to": end}
        if symbol:
            params["symbol"] = symbol
        d = await self.get("/calendar/earnings", **params)
        return (d or {}).get("earningsCalendar") or []

    async def quote(self, symbol: str) -> dict:
        """Last price of one stock: {"c": price, "t": time of that trade in seconds since 1970, "pc": previous close, ...}."""
        return await self.get("/quote", symbol=symbol) or {}

    async def past_quarters(self, symbol: str) -> list:
        """The last quarters of one company: expected and reported profit per share, newest first."""
        return await self.get("/stock/earnings", symbol=symbol) or []


def calendar_summary(rows) -> dict:
    """How complete a stretch of the earnings calendar is."""
    hours = {}
    for r in rows:
        h = r.get("hour") or "unbekannt"
        hours[h] = hours.get(h, 0) + 1
    return {
        "entries": len(rows),
        "with_eps": sum(1 for r in rows if r.get("epsEstimate") is not None),
        "with_revenue": sum(1 for r in rows if r.get("revenueEstimate") is not None),
        "with_hour": sum(1 for r in rows if r.get("hour")),
        "hours": dict(sorted(hours.items())),
    }


def money(x) -> str:
    """A revenue figure in words a reader can compare at a glance."""
    x = float(x)
    if abs(x) >= 1e9:
        return f"{x / 1e9:.2f} billion"
    if abs(x) >= 1e6:
        return f"{x / 1e6:.1f} million"
    return f"{x:,.0f}"


def expectation_text(row: dict, quarters=None) -> str:
    """What is known before a company's results, as a few lines for the model that rates the release."""
    when = HOURS.get(row.get("hour") or "", "time of day not known")
    lines = [f"{row['symbol']} reports quarterly results on {row.get('date')} ({when})"
             + (f", fiscal Q{row['quarter']} {row.get('year')}" if row.get("quarter") else "") + "."]
    eps, rev = row.get("epsEstimate"), row.get("revenueEstimate")
    if eps is not None or rev is not None:
        parts = ([f"earnings per share of {eps:.2f}"] if eps is not None else []) \
            + ([f"revenue of {money(rev)}"] if rev is not None else [])
        lines.append("Analysts expect " + " and ".join(parts) + " (in the currency the company reports in).")
    past = [q for q in (quarters or []) if q.get("actual") is not None and q.get("estimate") is not None][:4]
    if past:
        lines.append("Earlier quarters, reported against expected earnings per share: " + "; ".join(
            f"{q.get('period')}: {q['actual']:.2f} against {q['estimate']:.2f}" for q in past) + ".")
    lines.append("Judge the results in the article against these expectations, and against the company's own "
                 "outlook if the article gives one. Results in line with expectations are no surprise.")
    return "\n".join(lines)


class Expectations:
    """What analysts expect of the companies that report around today.

    The calendar is loaded once at the start of the day and every few hours after that (one
    call). The earlier quarters of a company are fetched when its release arrives, at most once
    a day per company and never for longer than two seconds, so that a rating is not held up.
    """

    def __init__(self, finnhub: Finnhub, journal=None, refresh_h: float = 4.0):
        self.fh, self.journal, self.refresh_s = finnhub, journal, refresh_h * 3600
        self.rows = {}          # symbol -> calendar row of the report nearest to today
        self.quarters = {}      # symbol -> earlier quarters
        self.day = None
        self.loaded = None      # monotonic time of the last successful load

    async def refresh(self, today: date = None, force=False) -> int:
        """Load the calendar from yesterday to tomorrow. Returns how many companies it names. Never raises."""
        if not self.fh.available:
            return 0
        if not force and self.loaded is not None and time.monotonic() - self.loaded < self.refresh_s:
            return len(self.rows)
        today = today or now().astimezone(ET).date()
        try:
            rows = await self.fh.earnings_calendar((today - timedelta(days=1)).isoformat(),
                                                   (today + timedelta(days=1)).isoformat())
        except FinnhubError as e:
            if self.journal:
                self.journal.event("error", where="expectations", error=str(e)[:200])
            return len(self.rows)
        best = {}
        for r in rows:
            sym, d = r.get("symbol"), r.get("date") or ""
            if not sym or not d:
                continue
            # several reports of one company in three days do not happen; if they do, the one nearest to today counts
            old = best.get(sym)
            if old is None or abs((date.fromisoformat(d) - today).days) < abs((date.fromisoformat(old["date"]) - today).days):
                best[sym] = r
        if today != self.day:
            self.quarters, self.day = {}, today
        self.rows, self.loaded = best, time.monotonic()
        if self.journal:
            self.journal.event("expectations", companies=len(best),
                               with_eps=sum(1 for r in best.values() if r.get("epsEstimate") is not None),
                               with_revenue=sum(1 for r in best.values() if r.get("revenueEstimate") is not None))
        return len(best)

    async def context(self, symbol: str):
        """The lines for the rating of this company's release, or None if it is not reporting now."""
        await self.refresh()
        row = self.rows.get(symbol)
        if not row:
            return None
        if symbol not in self.quarters:
            try:
                self.quarters[symbol] = await asyncio.wait_for(self.fh.past_quarters(symbol), timeout=2.0)
            except (FinnhubError, asyncio.TimeoutError):
                self.quarters[symbol] = []
        return expectation_text(row, self.quarters[symbol])
