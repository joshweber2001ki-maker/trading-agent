"""Press releases from WireAlpha, by push.

WireAlpha (wirealpha.com) offers a free stream of press releases from several wires and says it
delivers them within a second. Nothing independent confirms that, the service is young, and its
terms let it end at any time. Every release that belongs to one tradable stock is logged as
`wire_item` with the source "wa-<wire>" (for example wa-businesswire), and the comparison with the
other sources (`news_dup`, weekly statistics "Nachrichtenquellen") shows whether it really is
first. A source is rated and traded if it is named in `[wires] trade` (Josh's decision of
24.11.2026: trade at once, no measuring first), otherwise only measured.

The stream carries the headline only. For a release that is rated, the full text is asked for over
the ordinary interface (`body`); if that fails, the headline is rated by itself.

The key comes from the environment (WIREALPHA_API_KEY) and is stored only on the server. It is
part of the stream's address, so no address is ever logged.

Message format as documented on 24.11.2026: after {"action": "subscribe", "tickers": ["*"]} the
server sends {"type": "article", "data": {"id", "title", "source", "tickers", "published_at", ...},
"timestamp": ...}; tickers are plain strings in the stream and {"ticker", "exchange"} objects in the
REST answers. The server sends "ping" every 30 s and expects "pong".
"""
import asyncio
import json
import time

import html
import re

import httpx
import websockets

from .common import iso, log, now, parse_ts, spawn

STREAM = "wss://api.wirealpha.com/v1/ws"
ARTICLE = "https://api.wirealpha.com/v1/articles/"
SAMPLES = 5      # the first messages of a day are logged as they came, to check the format against the documentation
HELD_S = 120     # a connection that lasted this long was a working one


def to_article(data: dict, universe: dict, sources, max_age_s: int):
    """A WireAlpha article as a press release of exactly one tradable stock, or None."""
    src = str(data.get("source") or "").lower()
    title = (data.get("translated_title") or data.get("title") or "").strip()
    if src not in sources or not title or not data.get("id") or not data.get("published_at"):
        return None
    tickers = []
    for t in data.get("tickers") or []:
        sym = (t.get("ticker") if isinstance(t, dict) else t) or ""
        sym = str(sym).upper().strip()
        if sym in universe and sym not in tickers:
            tickers.append(sym)
    if len(tickers) != 1:
        return None                         # none of ours, or unclear which one is meant
    try:
        published = parse_ts(str(data["published_at"]))
    except (TypeError, ValueError):
        return None
    if published.tzinfo is None:
        return None
    art = {
        "id": f"wa-{src}:{str(data['id'])[-60:]}", "headline": title, "summary": "",
        "created_at": iso(published), "symbols": tickers, "source": f"wa-{src}", "company": "",
        "max_age_s": max_age_s, "url": "",
    }
    if data.get("body"):
        art["text"] = str(data["body"])[:12000]
    return art


class WireAlpha:
    def __init__(self, cfg: dict, key: str, universe, on_article, journal, max_age_s: int = 420, is_alert=None):
        self.cfg, self.key, self.universe = cfg or {}, (key or "").strip(), universe
        self.on_article, self.journal, self.max_age_s, self.is_alert = on_article, journal, max_age_s, is_alert
        self.sources = {s.lower() for s in self.cfg.get("sources") or []}
        self.http = httpx.AsyncClient(timeout=self.cfg.get("text_timeout_s", 3))
        self.seen = set()
        self.connected = False
        self.samples = 0
        self.stats = {"messages": 0, "items": 0, "matched": 0, "errors": 0}

    @property
    def active(self):
        return bool(self.cfg.get("enabled") and self.key)

    def reset_day(self):
        self.stats = {k: 0 for k in self.stats}
        self.samples = 0
        if len(self.seen) > 50000:
            self.seen.clear()

    def take(self, msg, received=None):
        """One message from the stream. Returns the article if it was a new release of one of our stocks."""
        if not isinstance(msg, dict) or msg.get("type") != "article" or not isinstance(msg.get("data"), dict):
            return None
        data = msg["data"]
        self.stats["items"] += 1
        if self.samples < SAMPLES:
            self.samples += 1
            self.journal.event("wirealpha_sample", message=json.dumps(msg, default=str)[:700])
        key = data.get("id")
        if key is None or key in self.seen:
            return None
        self.seen.add(key)
        art = to_article(data, self.universe.cache, self.sources, self.max_age_s)
        if not art:
            return None
        received = received or now()
        if (received - parse_ts(art["created_at"])).total_seconds() > 900:
            return None                     # older items, for example replayed after connecting: not news any more
        self.stats["matched"] += 1
        alert = bool(self.is_alert and self.is_alert(art["headline"]))
        self.journal.event("wire_item", id=art["id"], symbol=art["symbols"][0], published=art["created_at"],
                           headline=art["headline"][:140], source=art["source"], company="",
                           sent=msg.get("timestamp"), latency_ms=data.get("latency_ms"),
                           **({"alert": True} if alert else {}))
        if alert:
            return None
        if not art.get("text"):
            # The stream carries the headline only. Whoever rates the release asks for its text;
            # without it the headline still counts (it is the release's own, not a filing's label).
            art["fetch"], art["text_optional"] = (lambda i=data["id"]: self.body(i)), True
        return art

    async def body(self, article_id) -> str:
        """The full text of one release; empty if it cannot be had in time. Never raises."""
        try:
            r = await self.http.get(ARTICLE + str(article_id), headers={"Authorization": f"Bearer {self.key}"})
            if r.status_code != 200:
                return ""
            d = r.json()
            d = d.get("data", d) if isinstance(d, dict) else {}
            text = d.get("body") if isinstance(d, dict) else ""
            text = re.sub(r"<(script|style)\b.*?</\1>", " ", str(text or ""), flags=re.S | re.I)
            return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()[:12000]
        except Exception:
            return ""

    def _error(self, e):
        self.stats["errors"] += 1
        if self.stats["errors"] <= 10 or self.stats["errors"] % 50 == 0:
            # the key is part of the address: never let it into the log
            self.journal.event("error", where="wirealpha", error=repr(e).replace(self.key, "***")[:200])

    async def run(self):
        if not self.cfg.get("enabled"):
            return
        if not self.key:
            log.warning("WIREALPHA_API_KEY fehlt: WireAlpha wird nicht gemessen")
            return
        log.info("wirealpha: listening for press releases from %s", ", ".join(sorted(self.sources)))
        backoff = 1
        while True:
            started = time.monotonic()
            try:
                async with websockets.connect(f"{STREAM}?apiKey={self.key}", ping_interval=30, ping_timeout=30) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "tickers": ["*"]}))
                    self.connected = True
                    async for raw in ws:
                        self.stats["messages"] += 1
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "replace")
                        if raw.strip().strip('"').lower() == "ping":
                            await ws.send("pong")
                            continue
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(msg, dict) and msg.get("type") == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                            continue
                        if not self.universe.cache:
                            continue        # the list of tradable stocks is still loading
                        art = self.take(msg)
                        if art:
                            spawn(self.on_article(art))
                self._error(ConnectionError("the server closed the connection"))
            except Exception as e:
                self._error(e)
            self.connected = False
            # A connection that held for a while is taken up again quickly. One that fails or is closed
            # at once (a key that is not accepted, a service that is down) is tried ever more rarely:
            # asking again every second would help nobody.
            backoff = 2 if time.monotonic() - started > HELD_S else min(backoff * 2, 300)
            await asyncio.sleep(backoff)
