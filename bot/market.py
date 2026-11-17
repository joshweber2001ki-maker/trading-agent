"""Trading session times and tradable-universe checks."""
import re
from datetime import datetime, timedelta

from .common import ET, iso, now

EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}$")
FUND_RE = re.compile(
    r"\b(ETF|ETN|Fund|Trust|ProShares|Direxion|iShares|SPDR|Daily|Bull|Bear|[1-3]X|UltraPro|Ultra|Short)\b", re.I
)


class Session:
    """Today's regular session, from Alpaca's calendar (handles holidays and half days)."""

    def __init__(self, api):
        self.api = api
        self.day = None
        self.open = None
        self.close = None

    async def refresh(self):
        day = now().astimezone(ET).strftime("%Y-%m-%d")
        if day == self.day:
            return
        cal = await self.api.calendar(day)
        self.day, self.open, self.close = day, None, None
        if cal and cal[0]["date"] == day:
            d = datetime.strptime(day, "%Y-%m-%d")
            oh, om = map(int, cal[0]["open"].split(":"))
            ch, cm = map(int, cal[0]["close"].split(":"))
            self.open = d.replace(hour=oh, minute=om, tzinfo=ET)
            self.close = d.replace(hour=ch, minute=cm, tzinfo=ET)

    @property
    def trading_day(self):
        return self.open is not None

    def since_open_min(self):
        return (now() - self.open).total_seconds() / 60 if self.open else None

    def to_close_min(self):
        return (self.close - now()).total_seconds() / 60 if self.close else None

    def is_open(self):
        return self.trading_day and self.open <= now() < self.close

    def premarket(self, start_hm="04:00"):
        """From start_hm (New York time) until 5 minutes before the open."""
        if not self.trading_day:
            return False
        h, m = map(int, start_hm.split(":"))
        start = self.open.replace(hour=h, minute=m)
        return start <= now() < self.open - timedelta(minutes=5)


class Universe:
    """Caches asset lookups; answers whether a symbol is a normal, tradable US stock."""

    def __init__(self, api):
        self.api = api
        self.cache = {}
        self.loaded = False

    @staticmethod
    def _ok(a):
        return bool(
            a and a.get("class") == "us_equity" and a.get("status") == "active"
            and a.get("tradable") and a.get("exchange") in EXCHANGES
            and SYMBOL_RE.match(a.get("symbol") or "")
        )

    async def load(self):
        """Fetch the whole list once, so that news filtering needs no per-symbol requests."""
        rows = await self.api.assets()
        self.cache = {a["symbol"]: a for a in rows if self._ok(a)}
        self.loaded = True
        return len(self.cache)

    async def asset(self, symbol):
        if not SYMBOL_RE.match(symbol or ""):
            return None
        if self.loaded:
            return self.cache.get(symbol)
        if symbol not in self.cache:
            a = await self.api.asset(symbol)
            self.cache[symbol] = a if self._ok(a) else None
        return self.cache[symbol]

    async def name(self, symbol):
        a = await self.asset(symbol)
        return (a or {}).get("name", "")

    async def is_fund(self, symbol):
        return bool(FUND_RE.search(await self.name(symbol)))

    async def shortable(self, symbol):
        a = await self.asset(symbol)
        return bool(a and a.get("shortable") and a.get("easy_to_borrow"))


def summarize_daily(bars):
    """Size and usual swing of a stock from its recent daily bars, or None if there is too little data."""
    if len(bars) < 5:
        return None
    recent, swing = bars[-20:], bars[-14:]
    dollar_volume = sum(b["v"] * (b.get("vw") or b["c"]) for b in recent) / len(recent)
    daily_range = sum((b["h"] - b["l"]) / b["c"] for b in swing if b["c"]) / len(swing) * 100
    size = ("very large" if dollar_volume >= 1e9 else "large" if dollar_volume >= 2e8
            else "mid-sized" if dollar_volume >= 2e7 else "small")
    return {"price": bars[-1]["c"], "dollar_volume": round(dollar_volume),
            "daily_range_pct": round(daily_range, 2), "size": size}


class StockStats:
    """Per stock: traded value per day and usual daily swing, from full-market daily bars.

    The KI needs these numbers to judge how much a piece of news matters for this company,
    and the strategy uses the usual swing to scale stops and targets. Cached for the day.
    """

    def __init__(self, api):
        self.api = api
        self.cache = {}
        self.day = None

    async def get(self, symbols):
        today = now().strftime("%Y-%m-%d")
        if today != self.day:
            self.cache.clear()
            self.day = today
        missing = [s for s in symbols if s not in self.cache]
        if missing:
            end = now().replace(hour=0, minute=0, second=0, microsecond=0)   # completed days only
            try:
                bars = await self.api.bars_multi(missing, "1Day", iso(end - timedelta(days=45)), iso(end), feed="sip")
            except Exception:
                return {s: self.cache.get(s) for s in symbols}   # try again next time
            for s in missing:
                self.cache[s] = summarize_daily(bars.get(s) or [])
        return {s: self.cache.get(s) for s in symbols}
