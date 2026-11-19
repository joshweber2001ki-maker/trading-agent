"""Finnhub: when companies report and what analysts expect (free plan).

A quarterly report can only be judged against what was expected. Finnhub's earnings calendar
names the day, whether the report comes before the open or after the close, and the expected
profit per share and revenue.

The key comes from the environment (FINNHUB_API_KEY) and is stored only on the server. It is
sent in a header, never in the address, so it cannot end up in a log line or an error message.
"""
import asyncio
import time

import httpx

BASE = "https://finnhub.io/api/v1"


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
