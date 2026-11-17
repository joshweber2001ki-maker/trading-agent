"""Async Alpaca REST client (trading + market data)."""
import asyncio
import time

import httpx

DATA = "https://data.alpaca.markets"
FEED = "iex"  # free plan; switch to "sip" with the paid data plan


class AlpacaError(Exception):
    def __init__(self, status, body, url):
        super().__init__(f"{status} {url}: {str(body)[:300]}")
        self.status, self.body = status, body


class Alpaca:
    def __init__(self, key: str, secret: str, base: str, feed: str = FEED, max_per_min: int = 150):
        self.base = base.rstrip("/")
        if self.base.endswith("/v2"):
            self.base = self.base[:-3]
        self.feed = feed
        self.http = httpx.AsyncClient(
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, timeout=15
        )
        # Trading and market-data calls have separate limits at Alpaca; keeping two budgets
        # means a burst of data requests can never delay order management.
        self._stamps = {"trading": [], "data": []}
        self._max = max_per_min
        self._locks = {"trading": asyncio.Lock(), "data": asyncio.Lock()}

    async def _throttle(self, url):
        kind = "data" if url.startswith(DATA) else "trading"
        async with self._locks[kind]:
            while True:
                t = time.monotonic()
                self._stamps[kind] = [s for s in self._stamps[kind] if t - s < 60]
                if len(self._stamps[kind]) < self._max:
                    self._stamps[kind].append(t)
                    return
                await asyncio.sleep(0.5)

    async def _req(self, method, url, ok404=False, **kw):
        last = None
        for attempt in range(3):
            await self._throttle(url)
            try:
                r = await self.http.request(method, url, **kw)
            except httpx.HTTPError as e:
                last = AlpacaError("network", repr(e), url)
                await asyncio.sleep(1 + attempt)
                continue
            if r.status_code == 404 and ok404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                last = AlpacaError(r.status_code, r.text, url)
                await asyncio.sleep(1 + 2 * attempt)
                continue
            if r.status_code >= 400:
                raise AlpacaError(r.status_code, r.text, url)
            return r.json() if r.content else None
        raise last

    # ── trading ──────────────────────────────────────────────────────────────
    def _t(self, path):
        return f"{self.base}/v2{path}"

    async def account(self):
        return await self._req("GET", self._t("/account"))

    async def clock(self):
        return await self._req("GET", self._t("/clock"))

    async def calendar(self, day: str):
        return await self._req("GET", self._t("/calendar"), params={"start": day, "end": day})

    async def asset(self, symbol):
        return await self._req("GET", self._t(f"/assets/{symbol}"), ok404=True)

    async def assets(self):
        """All active US stocks in one call (a few MB)."""
        return await self._req(
            "GET", self._t("/assets"), params={"status": "active", "asset_class": "us_equity"}, timeout=60
        )

    async def positions(self):
        return await self._req("GET", self._t("/positions"))

    async def position(self, symbol):
        return await self._req("GET", self._t(f"/positions/{symbol}"), ok404=True)

    async def open_orders(self):
        return await self._req("GET", self._t("/orders"), params={"status": "open", "limit": 200})

    async def order(self, order_id):
        return await self._req("GET", self._t(f"/orders/{order_id}"), params={"nested": "true"})

    async def order_by_client_id(self, client_id):
        return await self._req(
            "GET", self._t("/orders:by_client_order_id"),
            params={"client_order_id": client_id, "nested": "true"}, ok404=True,
        )

    async def submit(self, payload: dict):
        return await self._req("POST", self._t("/orders"), json=payload)

    async def cancel(self, order_id):
        try:
            await self._req("DELETE", self._t(f"/orders/{order_id}"), ok404=True)
        except AlpacaError as e:
            if e.status != 422:  # 422 = already filled/cancelled
                raise

    async def cancel_all(self):
        return await self._req("DELETE", self._t("/orders"))

    async def close_position(self, symbol):
        return await self._req("DELETE", self._t(f"/positions/{symbol}"), ok404=True)

    # ── market data ──────────────────────────────────────────────────────────
    async def snapshot(self, symbol):
        return await self._req(
            "GET", f"{DATA}/v2/stocks/{symbol}/snapshot", params={"feed": self.feed}, ok404=True
        )

    async def snapshots(self, symbols):
        if not symbols:
            return {}
        return await self._req(
            "GET", f"{DATA}/v2/stocks/snapshots",
            params={"symbols": ",".join(symbols), "feed": self.feed},
        ) or {}

    async def bars(self, symbol, timeframe, start, end=None, limit=1000):
        params = {"timeframe": timeframe, "start": start, "feed": self.feed, "limit": limit}
        if end:
            params["end"] = end
        d = await self._req("GET", f"{DATA}/v2/stocks/{symbol}/bars", params=params)
        return (d or {}).get("bars") or []

    async def movers(self, top=20):
        return await self._req(
            "GET", f"{DATA}/v1beta1/screener/stocks/movers", params={"top": top}
        ) or {}

    async def most_actives(self, top=20):
        d = await self._req(
            "GET", f"{DATA}/v1beta1/screener/stocks/most-actives", params={"by": "volume", "top": top}
        )
        return (d or {}).get("most_actives") or []

    async def news(self, limit=10, start=None):
        params = {"limit": limit, "sort": "desc"}
        if start:
            params["start"] = start
        d = await self._req("GET", f"{DATA}/v1beta1/news", params=params)
        return (d or {}).get("news") or []
