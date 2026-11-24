"""Company filings with the SEC (form 8-K): which company, accepted at which second, and what it says.

Two uses. Every filing is logged (`sec_item`) to measure how fast prices react to it. And a fresh
filing can be handed on (`on_filing`) to be read, rated and traded: for announcements other than quarterly
results the filing is often the first or the only publication, and prices take a minute or two to
react to it (measured 13. to 23.11.2026).

The SEC's public list of the latest filings shows a filing about half a minute to a minute after it
was accepted. During the hours in which the SEC publishes 8-Ks (6:00 to 17:30 New York time; later
ones appear at 6:00 the next morning) the newest page is asked every few seconds, far below the
SEC's limit of ten requests per second. A complete look further back follows every few minutes.

The SEC asks automated readers to name a contact address in the User-Agent. It comes from the
environment (SEC_CONTACT) and is stored only on the server, never in the repository.
"""
import asyncio
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

from .common import ET as NY, iso, log, now, spawn

FEED = "https://www.sec.gov/cgi-bin/browse-edgar"
TICKERS = "https://www.sec.gov/files/company_tickers.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/"
ATOM = "{http://www.w3.org/2005/Atom}"
TITLE = re.compile(r"^(8-K(?:/A)?) - (.*) \((\d{10})\) \(Filer\)\s*$")
PAGE = 100
STARTUP_WAIT_S = 15     # while the bot's list of tradable stocks is still loading
HAND_ON_MAX_AGE_S = 600  # older filings (a look back after a restart, the morning's backlog) are only logged

# What the items of an 8-K mean, for the headline a reader gets.
ITEMS = {
    "1.01": "Entry into a Material Definitive Agreement", "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership", "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets", "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation", "2.04": "Triggering Events That Accelerate a Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities", "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Listing Rule", "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders", "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements", "5.01": "Changes in Control of Registrant",
    "5.02": "Departure or Appointment of Directors or Officers", "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.07": "Submission of Matters to a Vote of Security Holders", "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events", "9.01": "Financial Statements and Exhibits",
}
# One document of a submission; the last one may be cut off (only the beginning of a large file is read).
DOCUMENT = re.compile(r"<DOCUMENT>\s*<TYPE>([^\s<]+)(.*?)(?:</DOCUMENT>|\Z)", re.S | re.I)
# What the reader of a filing has to know about it (goes in front of the rating as "known beforehand").
NOTE = ("This is a filing with the SEC (form 8-K), accepted at {accepted}. A company may file up to four business "
        "days after the event, and a filing often repeats a press release issued earlier. Compare the dates in the "
        "text with the time of the filing: what the market already knew is no surprise.")


def parse_feed(xml_bytes):
    """Feed entries as dicts: acc, form, company, cik, accepted (datetime), items (e.g. ['2.02', '9.01'])
    and link (the filing's index page)."""
    out = []
    for e in ET.fromstring(xml_bytes).findall(ATOM + "entry"):
        m = TITLE.match((e.findtext(ATOM + "title") or "").strip())
        acc = (e.findtext(ATOM + "id") or "").rsplit("=", 1)[-1]
        try:
            accepted = datetime.fromisoformat(e.findtext(ATOM + "updated") or "")
        except ValueError:
            continue
        if not m or not acc or accepted.tzinfo is None:
            continue
        link = e.find(ATOM + "link")
        out.append({
            "acc": acc, "form": m.group(1), "company": m.group(2), "cik": int(m.group(3)), "accepted": accepted,
            "items": re.findall(r"Item (\d\.\d\d)", e.findtext(ATOM + "summary") or ""),
            "link": (link.get("href") if link is not None else "") or "",
        })
    return out


def submission_url(entry: dict) -> str:
    """Address of the complete submission (every document of the filing in one text file)."""
    link, acc = entry.get("link") or "", entry["acc"]
    if link.startswith(ARCHIVE) and link.endswith("-index.htm"):
        return link[: -len("-index.htm")] + ".txt"
    return f"{ARCHIVE}{entry['cik']}/{acc.replace('-', '')}/{acc}.txt"


def plain(fragment: str) -> str:
    """Running text of an HTML document from a filing: no hidden data, no tags. Tables stay as
    text: many filings set the headings of their items ("Item 1.01") in a table."""
    s = re.sub(r"<(script|style|ix:header)\b.*?</\1>", " ", fragment or "", flags=re.S | re.I)
    s = re.sub(r'<[^>]+style="[^"]*display:\s*none[^"]*"[^>]*>.*?</[^>]+>', " ", s, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def decode(buf: bytes) -> str:
    """Filings are plain ASCII, Latin-1 or UTF-8, and nothing says which."""
    try:
        return buf.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.reason == "unexpected end of data":    # a character cut in two where the reading stopped
            return buf[:e.start].decode("utf-8", "replace")
        return buf.decode("latin-1")


def filing_text(submission: str, main_chars: int = 2000, exhibit_chars: int = 9000) -> str:
    """What an 8-K says, from its complete submission: the report itself from its first item on
    (the cover page before it says nothing), then the press release or other text attached as
    Exhibit 99. Graphics, data files and other exhibits are left out."""
    main, exhibits = "", []
    for m in DOCUMENT.finditer(submission or ""):
        kind, rest = m.group(1).upper(), m.group(2)
        body = rest.split("<TEXT>", 1)[-1]
        if kind == "8-K" and not main:
            text = plain(body)
            first = re.search(r"\bItem\s+\d\.\d\d", text)
            main = text[first.start():] if first else text
        elif kind.startswith("EX-99"):
            text = plain(body)
            if text:
                exhibits.append(text)
    parts = ([main[:main_chars]] if main else []) + [("Exhibit 99: " + x) for x in exhibits]
    return " ".join(parts)[: main_chars + exhibit_chars].strip()


class Edgar:
    def __init__(self, cfg: dict, contact: str, universe, journal, on_filing=None):
        self.cfg, self.universe, self.journal = cfg, universe, journal
        self.on_filing = on_filing      # called with (entry, symbol) for every fresh filing of a tradable stock
        self.contact = (contact or "").strip()
        self.http = httpx.AsyncClient(timeout=20, headers={
            "User-Agent": f"trading-bot-research {self.contact}", "Accept-Encoding": "gzip, deflate"})
        self.seen = None          # accession numbers already logged; filled from the logs on first use
        self.tickers = {}         # SEC company number -> ticker symbols
        self.tickers_day = None
        self.stats = {"polls": 0, "filings": 0, "matched": 0, "errors": 0}
        self.handed = 0           # filings handed on to be read today

    @property
    def active(self):
        return bool(self.cfg.get("enabled") and self.contact)

    def _load_seen(self):
        """Filings logged before a restart must not be logged again."""
        self.seen = set()
        for row in self.journal.recent_events(days=4):
            if row.get("kind") == "sec_item" and row.get("acc"):
                self.seen.add(row["acc"])

    async def _load_tickers(self):
        day = now().strftime("%Y-%m-%d")
        if day == self.tickers_day and self.tickers:
            return
        r = await self.http.get(TICKERS)
        r.raise_for_status()
        tickers = {}
        for row in r.json().values():
            tickers.setdefault(int(row["cik_str"]), []).append(str(row["ticker"]).upper())
        self.tickers, self.tickers_day = tickers, day

    def symbol_for(self, cik: int):
        """The company's ticker among the stocks the bot can trade, or None."""
        for t in self.tickers.get(cik, []):
            if t in self.universe.cache:
                return t
        return None

    async def _page(self, start: int, count: int, timeout=None):
        params = {"action": "getcurrent", "type": "8-K", "company": "", "dateb": "", "owner": "include",
                  "start": start, "count": count, "output": "atom"}
        r = await (self.http.get(FEED, params=params, timeout=timeout) if timeout else self.http.get(FEED, params=params))
        r.raise_for_status()
        self.stats["polls"] += 1
        return parse_feed(r.content)

    def _take(self, new) -> int:
        """Log the filings in `new` that belong to a tradable stock and hand the fresh ones on."""
        # A filing lists every co-registrant as its own entry; the first with a tradable ticker counts.
        by_acc = {}
        for e in new:
            sym = self.symbol_for(e["cik"])
            if e["acc"] not in by_acc or (sym and not by_acc[e["acc"]][1]):
                by_acc[e["acc"]] = (e, sym)
        logged = 0
        for acc, (e, sym) in sorted(by_acc.items(), key=lambda kv: kv[1][0]["accepted"]):
            self.seen.add(acc)
            self.stats["filings"] += 1
            if not sym or e["form"] != "8-K":
                continue                    # no listed stock behind it, or only an amendment
            self.stats["matched"] += 1
            logged += 1
            self.journal.event("sec_item", acc=acc, symbol=sym, company=e["company"][:60], cik=e["cik"],
                               items=e["items"], accepted=iso(e["accepted"]))
            if self.on_filing and (now() - e["accepted"]).total_seconds() <= HAND_ON_MAX_AGE_S:
                self.handed += 1
                spawn(self.on_filing(e, sym))
        return logged

    async def collect(self):
        """Log every filing that is new since the last look. Returns how many were logged."""
        if self.seen is None:
            self._load_seen()
        await self._load_tickers()
        new = []
        for page in range(self.cfg["max_pages"]):
            entries = await self._page(page * PAGE, PAGE)
            fresh = [e for e in entries if e["acc"] not in self.seen]
            new += fresh
            if not entries or len(fresh) < len(entries):
                break                       # reached filings that are already logged
            await asyncio.sleep(0.5)        # stay far below the SEC's limit of 10 requests per second
        return self._take(new)

    async def latest(self):
        """The quick look: only the newest filings, one small request. Returns how many were logged."""
        if self.seen is None or not self.tickers:
            return await self.collect()     # the first look of a run goes all the way back
        entries = await self._page(0, self.cfg.get("fast_count", 40), timeout=self.cfg.get("fast_timeout_s", 6))
        return self._take([e for e in entries if e["acc"] not in self.seen])

    def article(self, e: dict, sym: str) -> dict:
        """A filing in the shape the immediate entry expects. The headline names the company and
        what kind of news the filing reports; what it says has to be fetched (`fetch`)."""
        kinds = [ITEMS.get(i, "Item " + i) for i in e["items"] if i != "9.01"] or ["Form 8-K"]
        return {
            "id": e["acc"], "source": "sec", "symbols": [sym], "created_at": iso(e["accepted"]),
            "headline": f"{e['company']} files 8-K with the SEC: {'; '.join(kinds)}"[:200],
            "summary": "", "company": e["company"][:60], "items": e["items"], "url": e.get("link") or "",
            "note": NOTE.format(accepted=iso(e["accepted"])),
            "results": "2.02" in e["items"],        # only then is it the same news as a results press release
            "dedupe_key": e["acc"], "fetch": lambda: self.text(e),
        }

    async def text(self, e: dict) -> str:
        """What the filing says (see filing_text); empty if it cannot be fetched in time. The
        submission file can be large (graphics): only its beginning is read."""
        url, limit, buf = submission_url(e), self.cfg.get("text_max_bytes", 600_000), b""
        for pause in (0, 1.0, 2.0):         # a filing can be listed a moment before its file is there: two more tries
            if pause:
                await asyncio.sleep(pause)
            try:
                async with self.http.stream("GET", url, timeout=self.cfg.get("text_timeout_s", 6)) as r:
                    if r.status_code == 404:
                        continue
                    if r.status_code != 200:
                        return ""
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) >= limit:
                            break
            except Exception:
                pass                        # what was read until then still counts
            break
        return filing_text(decode(buf)) if buf else ""

    def in_hours(self):
        """The SEC accepts filings on weekdays from 6:00 to 22:00 New York time."""
        t = now().astimezone(NY)
        return t.weekday() < 5 and 6 <= t.hour < 23

    def fast_hours(self):
        """When the quick look is worth it: the SEC publishes 8-Ks from 6:00 to 17:30 New York time;
        what is accepted later appears at 6:00 the next morning."""
        t = now().astimezone(NY)
        return bool(self.cfg.get("fast_poll_s")) and t.weekday() < 5 and 6 * 60 <= t.hour * 60 + t.minute < 17 * 60 + 40

    def reset_day(self):
        self.stats = {k: 0 for k in self.stats}
        self.handed = 0
        if self.seen and len(self.seen) > 20000:
            self.seen = None

    async def run(self):
        if not self.cfg.get("enabled"):
            return
        if not self.contact:
            log.warning("SEC_CONTACT fehlt: Pflichtmitteilungen der SEC werden nicht mitgeschrieben")
            return
        log.info("edgar: looking for new 8-K filings every %s s, completely every %d s",
                 self.cfg.get("fast_poll_s") or self.cfg["poll_s"], self.cfg["poll_s"])
        next_full, fails = 0.0, 0
        while True:
            ready = bool(self.universe.cache)
            quick = ready and self.fast_hours()
            try:
                if ready and self.in_hours():
                    if quick and time.monotonic() < next_full:
                        await self.latest()
                    else:
                        await self.collect()
                        next_full = time.monotonic() + self.cfg["poll_s"]
                fails = 0
            except Exception as e:
                # The SEC refuses those who ask too much (403, 429): then be quiet for a whole interval.
                refused = isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (403, 429)
                fails = 99 if refused else fails + 1
                self.stats["errors"] += 1
                if self.stats["errors"] <= 10 or self.stats["errors"] % 50 == 0:
                    self.journal.event("error", where="edgar", error=repr(e)[:200])
            # Right after the bot starts, the list of tradable stocks is still loading: look again soon.
            if not ready:
                await asyncio.sleep(STARTUP_WAIT_S)
            elif quick:
                # After a failed look wait longer, and longer still while it keeps failing: the list is
                # sometimes slow or away for hours, and asking more does not help.
                quiet = max(1.0, self.cfg["fast_poll_s"])
                await asyncio.sleep(min(self.cfg["poll_s"], quiet * 5 * 2 ** min(fails - 1, 10)) if fails else quiet)
            else:
                await asyncio.sleep(self.cfg["poll_s"])


def _demo():   # python -m bot.edgar <contact>  -> prints the newest filings, for a manual check
    import sys

    async def go():
        class U:
            cache = {}
        e = Edgar({"enabled": True, "max_pages": 1, "poll_s": 600}, sys.argv[1], U(), None)
        await e._load_tickers()
        for x in (await e._page(0, 10)):
            print(json.dumps({**x, "accepted": iso(x["accepted"]), "tickers": e.tickers.get(x["cik"])}))
            if len(sys.argv) > 2:           # any second argument: also show what the filing says
                print("   ", (await e.text(x))[:600])
    asyncio.run(go())


if __name__ == "__main__":
    _demo()
