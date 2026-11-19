"""Press releases straight from the wire (PR Newswire RSS) as a faster news source.

Alpaca's own news stream delivers summaries in half-hourly batches; a press release is on
the wire's RSS feed within a few minutes. The feed carries the company name but no ticker,
so the name is matched against the names of tradable US stocks. Only exact matches after
normalisation count: a missed release costs nothing, a wrong ticker would cost a trade.
"""
import asyncio
import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx

from .common import iso, log, spawn
from .market import FUND_RE

NS = {"dc": "http://purl.org/dc/elements/1.1/"}
UA = "Mozilla/5.0 (compatible; trading-bot-feed-reader/1.0)"

# Listings that are not the ordinary share of a company.
NOT_COMMON = re.compile(
    r"\b(warrants?|units?|rights?|preferred|preference|notes?\s+due|debentures?|bonds?|%|"
    r"depositary\s+shares?,?\s+each\s+representing\s+a?\s*\d+/\d+)", re.I)
# Everything from the share-class description onwards is not part of the company name.
SHARE_TAIL = re.compile(
    r"\b(common\s+(stock|shares?|units)|ordinary\s+shares?|class\s+[a-z]\b|american\s+deposit[ao]ry|"
    r"depositary\s+shares?|\badss?\b|\badrs?\b|each\s+representing|capital\s+stock|subordinate\s+voting|"
    r"shares?\s+of\s+beneficial|registered\s+shares?|new\s+york\s+registry|sponsored)\b.*", re.I | re.S)
SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies", "ltd", "limited", "plc",
    "llc", "lp", "sa", "nv", "ag", "se", "ab", "asa", "holdings", "holding", "group", "the", "and", "new",
}
TICKER = re.compile(r"\((?:NASDAQ|Nasdaq|NYSE|NYSE American|NYSE Arca|AMEX|NYSEAMERICAN)\s*:\s*([A-Z]{1,5})\)")


def normalize(name: str) -> str:
    s = re.sub(r"\(.*?\)", " ", name or "")
    s = SHARE_TAIL.sub("", s).lower().replace("&", " and ")
    toks = re.sub(r"[^a-z0-9 ]", " ", s).split()
    while toks and toks[-1] in SUFFIXES:
        toks.pop()
    while toks and toks[0] == "the":
        toks.pop(0)
    return " ".join(toks)


class NameIndex:
    """Normalised company name -> ticker, built from the tradable universe."""

    def __init__(self, assets: dict):
        self.names = {}
        for sym, a in assets.items():
            name = a.get("name") or ""
            if NOT_COMMON.search(name) or FUND_RE.search(name):
                continue
            key = normalize(name)
            if len(key) < 3:
                continue
            old = self.names.get(key)
            # Several share classes of one company: keep the shortest, then alphabetically first ticker.
            if old is None or (len(sym), sym) < (len(old), old):
                self.names[key] = sym

    def lookup(self, company: str):
        return self.names.get(normalize(company))


def release_text(page: str, limit: int = 12000) -> str:
    """The running text of a press release from its web page: the paragraphs of the release body,
    without tables, scripts and navigation. Empty if the page has no recognisable body."""
    m = re.search(r'<section[^>]*class="[^"]*release-body[^"]*"[^>]*>(.*?)</section>', page, re.S | re.I)
    if not m:
        return ""
    body = re.sub(r"<(script|style|table)\b.*?</\1>", " ", m.group(1), flags=re.S | re.I)
    paras = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x))).strip()
             for x in re.findall(r"<p\b[^>]*>(.*?)</p>", body, re.S | re.I)]
    return " ".join(x for x in paras if x)[:limit]


def parse_feed(xml_text: str):
    """RSS items as dicts: guid, link, title, summary, company, language, published (datetime)."""
    out = []
    root = ET.fromstring(xml_text)
    for it in root.findall("./channel/item"):
        try:
            published = parsedate_to_datetime(it.findtext("pubDate"))
        except (TypeError, ValueError):
            continue
        out.append({
            "guid": it.findtext("guid") or it.findtext("link") or "",
            "link": (it.findtext("link") or "").strip(),
            "title": (it.findtext("title") or "").strip(),
            "summary": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", it.findtext("description") or "")).strip(),
            "company": (it.findtext("dc:contributor", namespaces=NS) or "").strip(),
            "language": (it.findtext("dc:language", namespaces=NS) or "en").lower(),
            "published": published,
        })
    return out


def to_article(item: dict, index: NameIndex, universe: dict, source: str, max_age_s: int):
    """Turn a feed item into the article shape the news strategy expects, or None."""
    if not item["language"].startswith("en") or not item["title"]:
        return None
    sym = index.lookup(item["company"])
    if not sym:
        for t in TICKER.findall(item["title"] + " " + item["summary"]):
            if t in universe:
                sym = t
                break
    if not sym:
        return None
    return {
        "id": f"{source}:{item['guid'][-60:]}", "headline": item["title"], "summary": item["summary"],
        "created_at": iso(item["published"]), "symbols": [sym], "source": source,
        "company": item["company"], "max_age_s": max_age_s, "url": item.get("link") or "",
    }


class Wires:
    def __init__(self, cfg: dict, universe, on_article, journal):
        self.cfg, self.universe, self.on_article, self.journal = cfg, universe, on_article, journal
        self.http = httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}, follow_redirects=True)
        self.seen = set()
        self.modified = {}     # feed url -> Last-Modified
        self.index = None
        self.index_size = 0
        self.stats = {"polls": 0, "items": 0, "matched": 0, "errors": 0}

    def reset_day(self):
        self.stats = {k: 0 for k in self.stats}
        self.index = None      # rebuilt from the day's fresh universe on the next poll
        if len(self.seen) > 20000:
            self.seen.clear()

    async def poll(self, url: str):
        if self.index is None or self.index_size != len(self.universe.cache):
            self.index = NameIndex(self.universe.cache)
            self.index_size = len(self.universe.cache)
        hdr = {"If-Modified-Since": self.modified[url]} if url in self.modified else {}
        r = await self.http.get(url, headers=hdr)
        self.stats["polls"] += 1
        if r.status_code == 304:
            return
        r.raise_for_status()
        if r.headers.get("last-modified"):
            self.modified[url] = r.headers["last-modified"]
        for item in parse_feed(r.text):
            if item["guid"] in self.seen:
                continue
            self.seen.add(item["guid"])
            self.stats["items"] += 1
            art = to_article(item, self.index, self.universe.cache, "prnewswire", self.cfg["max_age_s"])
            if art:
                self.stats["matched"] += 1
                # Every matched release with its publication time, at any hour and whether or not
                # it is rated: the list cannot be rebuilt later (the feed only holds the newest
                # items), and it is what a study of how fast prices react needs.
                self.journal.event("wire_item", id=art["id"], symbol=art["symbols"][0], published=art["created_at"],
                                   headline=art["headline"][:140], source=art["source"], company=art["company"][:60])
                spawn(self.on_article(art))

    async def fetch_text(self, url: str, timeout_s: float = 4.0) -> str:
        """The full text of a release from the wire's own page; empty if that fails or takes too long."""
        if not url.startswith("https://www.prnewswire.com/"):
            return ""
        try:
            r = await asyncio.wait_for(self.http.get(url), timeout=timeout_s)
            return release_text(r.text) if r.status_code == 200 else ""
        except Exception:
            return ""

    async def run(self):
        """Poll all feeds round-robin, one request every poll_s / len(feeds) seconds."""
        feeds = self.cfg["feeds"]
        if not self.cfg["enabled"] or not feeds:
            return
        gap = max(2.0, self.cfg["poll_s"] / len(feeds))
        log.info("wires: polling %d feeds, one request every %.0f s", len(feeds), gap)
        while True:
            for url in feeds:
                try:
                    if self.universe.cache:
                        await self.poll(url)
                except Exception as e:
                    self.stats["errors"] += 1
                    if self.stats["errors"] <= 20 or self.stats["errors"] % 100 == 0:
                        self.journal.event("error", where="wires", feed=url[-60:], error=repr(e)[:200])
                await asyncio.sleep(gap)
