"""Press releases straight from the wires (PR Newswire, GlobeNewswire, Business Wire) as news sources.

Alpaca's own news stream delivers summaries late; a press release is on the wire's RSS feed within a
few minutes. Companies use one wire or another, so each wire alone misses most announcements: in the
week of 17.11.2026 only 16 of 99 results reports came over PR Newswire.

Which stock a release belongs to:
- PR Newswire names the company but gives no ticker: the name is matched against the names of
  tradable US stocks; only exact matches after normalisation count.
- GlobeNewswire gives the listing in a category ("Nasdaq:ACME").
- Business Wire gives neither: the ticker is taken from the opening line, where the company names
  itself ("NEW YORK--(BUSINESS WIRE)--Acme Corp (NYSE: ACME) today ...").
A missed release costs nothing, a wrong ticker would cost a trade, so anything unclear is dropped.
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
US_LISTING = re.compile(r"^(NASDAQ|NYSE|NYSE AMERICAN|NYSE MKT|NYSE ARCA|AMEX|CBOE)\s*:\s*([A-Z]{1,5})$", re.I)
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
# Business Wire's opening line: "CITY--(BUSINESS WIRE)--Name (NYSE: ACME) ..." The ticker counts only there.
BW_OPENING = re.compile(r"\(BUSINESS WIRE\)\s*--\s*([^()]{1,90}?)\s*(?:\([^()]{0,60}\)\s*)?\((?:NASDAQ|Nasdaq|NYSE|NYSE American|NYSE Arca|AMEX)\s*:\s*([A-Z]{1,5})\)")
BW_NOT_A_NAME = re.compile(r"\b(today|announc\w*|said|says|with|partner\w*|signed|agreement|deal)\b", re.I)
# Law firms looking for clients among shareholders ("INVESTOR ALERT", "class action", "lead plaintiff
# deadline"). They come by the dozen for stocks that have already fallen and say nothing new about the
# company: a quarter of all matched releases in the week of 17.11.2026, none of them rated with a direction.
ALERT = re.compile(
    r"(investor alert|shareholder alert|\$hareholder|stockholder alert|deadline alert|investor (notice|reminder|deadline)|"
    r"shareholder (notice|reminder|rights law)|class action|securities fraud|lead plaintiff|law firm|\bLLP\b|"
    r"sued for securities|securities law violations|investors? (have|who|with) (opportunity|lost|losses|substantial)|"
    r"investigat\w+ (claims )?on behalf of|encourages .{0,60}investors|notifies investors|suewallst|"
    r"(shareholder|investor) investigation|investigation notice|"
    r"pomerantz|hagens berman|bronstein,? gewirtz|levi & korsinsky|rosen law|faruqi|glancy prongay|kessler topaz|"
    r"schall law|johnson fistel|bragar eagel|kirby mcinerney|halper sadeh|robbins geller|djs law)", re.I)


# A company reporting on a lawsuit of its own is news, not an advertisement.
OWN_CASE = re.compile(r"\b(settle[sd]?|settlement|dismiss(es|ed|al)|prevails?|wins|ruling|verdict|appeal)\b", re.I)


def is_alert(headline: str) -> bool:
    """True for a law firm's call to shareholders (see ALERT)."""
    return bool(ALERT.search(headline or "")) and not OWN_CASE.search(headline or "")


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
    m = re.search(r'<section[^>]*class="[^"]*release-body[^"]*"[^>]*>(.*?)</section>', page, re.S | re.I)  # PR Newswire
    if m:
        return body_text(m.group(1), limit)
    # Business Wire and GlobeNewswire: from the start of the release body to the contact block or the end of the article
    m = re.search(r'<div[^>]*(?:class="[^"]*bw-release-story[^"]*"|itemprop="articleBody")[^>]*>', page, re.I)
    if not m:
        return ""
    rest = page[m.end():m.end() + 60000]
    stop = re.search(r'class="[^"]*(?:bw-release-contact|bw-release-footer|article-contact|main-tags)|</article>', rest, re.I)
    return body_text(rest[:stop.start()] if stop else rest, limit)


def body_text(fragment: str, limit: int = 12000) -> str:
    """The paragraphs of an HTML fragment as plain running text, without tables and scripts."""
    body = re.sub(r"<(script|style|table)\b.*?</\1>", " ", fragment or "", flags=re.S | re.I)
    paras = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x))).strip()
             for x in re.findall(r"<p\b[^>]*>(.*?)</p>", body, re.S | re.I)]
    return " ".join(x for x in paras if x)[:limit]


def parse_feed(xml_text: str):
    """RSS items as dicts: guid, link, title, summary, company, language, published (datetime),
    listings (US tickers the wire itself names, GlobeNewswire only) and body (the full text, when the
    feed carries it, GlobeNewswire only)."""
    out = []
    root = ET.fromstring(xml_text)
    for it in root.findall("./channel/item"):
        try:
            published = parsedate_to_datetime(it.findtext("pubDate"))
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            continue
        desc = it.findtext("description") or ""
        listings, named = [], 0
        for c in it.findall("category"):
            if (c.get("domain") or "").endswith("/rss/stock"):
                named += 1
                m = US_LISTING.match((c.text or "").strip())
                if m:
                    listings.append(m.group(2).upper())
        plain = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", desc))).strip()
        out.append({
            "guid": it.findtext("guid") or it.findtext("link") or "",
            "link": (it.findtext("link") or "").strip(),
            "title": html.unescape(it.findtext("title") or "").strip(),
            "summary": plain[:1500],
            "body": body_text(desc) if "<p" in desc.lower() and len(plain) > 1500 else "",
            "company": (it.findtext("dc:contributor", namespaces=NS) or "").strip(),
            "language": (it.findtext("dc:language", namespaces=NS) or "en").lower(),
            "published": published,
            "listings": listings,
            "listed_elsewhere": bool(named) and not listings,
        })
    return out


def to_article(item: dict, index: NameIndex, universe: dict, source: str, max_age_s: int):
    """Turn a feed item into the article shape the news strategy expects, or None."""
    if not item["language"].startswith("en") or not item["title"]:
        return None
    sym = None
    listed = sorted({t for t in item.get("listings") or [] if t in universe})
    if listed:
        if len(listed) > 1:
            return None                              # one release, several companies: unclear
        sym = listed[0]
    elif item.get("listings") or item.get("listed_elsewhere"):
        return None                                  # the wire names a listing, but not a tradable US one
    if not sym and source == "businesswire":
        m = BW_OPENING.search(item["summary"])
        if m and m.group(2) in universe and not BW_NOT_A_NAME.search(m.group(1)):
            sym = m.group(2)
        if not sym:
            return None
    if not sym:
        sym = index.lookup(item["company"])
    if not sym:
        for t in TICKER.findall(item["title"] + " " + item["summary"][:400]):
            if t in universe:
                sym = t
                break
    if not sym:
        return None
    art = {
        "id": f"{source}:{item['guid'][-60:]}", "headline": item["title"], "summary": item["summary"][:600],
        "created_at": iso(item["published"]), "symbols": [sym], "source": source,
        "company": item["company"], "max_age_s": max_age_s, "url": item.get("link") or "",
    }
    if item.get("body"):
        art["text"] = item["body"]
    return art


class Wires:
    def __init__(self, cfg: dict, universe, on_article, journal):
        self.cfg, self.universe, self.on_article, self.journal = cfg, universe, on_article, journal
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=True, headers={
            "User-Agent": UA, "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"})
        self.seen = set()
        self.modified = {}     # feed url -> Last-Modified
        self.index = None
        self.index_size = 0
        self.stats = {"polls": 0, "items": 0, "matched": 0, "errors": 0}
        self.by_source = {}    # source -> the same counts, per wire

    def feeds(self):
        """(source, url, every_s) for every feed. The first feed of each wire is its main feed and is
        asked more often; the others are topic feeds that catch what the main feed's short list misses."""
        cfg, out = self.cfg, []
        wires = {"prnewswire": cfg.get("feeds") or [], **(cfg.get("more") or {})}
        for source, urls in wires.items():
            for i, url in enumerate(urls):
                out.append((source, url, cfg.get("fast_poll_s", cfg["poll_s"]) if i == 0 else cfg["poll_s"]))
        return out

    def _count(self, source, key):
        self.stats[key] += 1
        s = self.by_source.setdefault(source, {"polls": 0, "items": 0, "matched": 0, "errors": 0})
        s[key] += 1

    def reset_day(self):
        self.stats = {k: 0 for k in self.stats}
        self.by_source = {}
        self.index = None      # rebuilt from the day's fresh universe on the next poll
        if len(self.seen) > 20000:
            self.seen.clear()

    async def poll(self, url: str, source: str = "prnewswire"):
        if self.index is None or self.index_size != len(self.universe.cache):
            self.index = NameIndex(self.universe.cache)
            self.index_size = len(self.universe.cache)
        hdr = {"If-Modified-Since": self.modified[url]} if url in self.modified else {}
        r = await self.http.get(url, headers=hdr)
        self._count(source, "polls")
        if r.status_code == 304:
            return
        r.raise_for_status()
        if r.headers.get("last-modified"):
            self.modified[url] = r.headers["last-modified"]
        for item in parse_feed(r.text):
            key = (source, item["guid"])
            if key in self.seen:
                continue
            self.seen.add(key)
            self._count(source, "items")
            art = to_article(item, self.index, self.universe.cache, source, self.cfg["max_age_s"])
            if art:
                self._count(source, "matched")
                # Every matched release with its publication time, at any hour and whether or not
                # it is rated: the list cannot be rebuilt later (the feed only holds the newest
                # items), and it is what a study of how fast prices react needs.
                alert = is_alert(art["headline"])
                self.journal.event("wire_item", id=art["id"], symbol=art["symbols"][0], published=art["created_at"],
                                   headline=art["headline"][:140], source=art["source"], company=art["company"][:60],
                                   **({"alert": True} if alert else {}))
                if not alert:               # a law firm's call to shareholders is listed, but neither rated nor measured
                    spawn(self.on_article(art))

    async def fetch_text(self, url: str, timeout_s: float = 4.0) -> str:
        """The full text of a release from the wire's own page; empty if that fails or takes too long."""
        if not url.startswith(("https://www.prnewswire.com/", "https://www.businesswire.com/",
                               "http://www.businesswire.com/", "https://www.globenewswire.com/")):
            return ""
        url = url.replace("http://www.businesswire.com/", "https://www.businesswire.com/")
        try:
            r = await asyncio.wait_for(self.http.get(url), timeout=timeout_s)
            return release_text(r.text) if r.status_code == 200 else ""
        except Exception:
            return ""

    async def run(self):
        """Poll every feed on its own clock: main feeds every fast_poll_s, topic feeds every poll_s.
        The starts are spread out so the requests do not come in bursts."""
        feeds = self.feeds()
        if not self.cfg["enabled"] or not feeds:
            return
        log.info("wires: polling %d feeds of %d wires", len(feeds), len({f[0] for f in feeds}))
        await asyncio.gather(*(self._loop(src, url, every, i * 1.3) for i, (src, url, every) in enumerate(feeds)))

    async def _loop(self, source, url, every_s, delay_s):
        await asyncio.sleep(delay_s)
        while True:
            try:
                if self.universe.cache:
                    await self.poll(url, source)
            except Exception as e:
                self._count(source, "errors")
                n = self.by_source[source]["errors"]
                if n <= 20 or n % 100 == 0:
                    self.journal.event("error", where="wires", source=source, feed=url[-60:], error=repr(e)[:200])
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (403, 429):
                    # refused or too often: do not keep knocking; try again in ten minutes
                    await asyncio.sleep(max(600.0, every_s))
                    continue
            await asyncio.sleep(max(2.0, every_s))
