"""Company filings with the SEC (form 8-K): which company, accepted at which second.

This is only a list for measuring how fast prices react to a filing; nothing here is rated or
traded. The SEC's public feed holds the most recent filings (roughly the last day and a half),
so it is enough to look every few minutes and log what is new.

The SEC asks automated readers to name a contact address in the User-Agent. It comes from the
environment (SEC_CONTACT) and is stored only on the server, never in the repository.
"""
import asyncio
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

from .common import ET as NY, iso, log, now

FEED = "https://www.sec.gov/cgi-bin/browse-edgar"
TICKERS = "https://www.sec.gov/files/company_tickers.json"
ATOM = "{http://www.w3.org/2005/Atom}"
TITLE = re.compile(r"^(8-K(?:/A)?) - (.*) \((\d{10})\) \(Filer\)\s*$")
PAGE = 100
STARTUP_WAIT_S = 15     # while the bot's list of tradable stocks is still loading


def parse_feed(xml_bytes):
    """Feed entries as dicts: acc, form, company, cik, accepted (datetime), items (e.g. ['2.02', '9.01'])."""
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
        out.append({
            "acc": acc, "form": m.group(1), "company": m.group(2), "cik": int(m.group(3)), "accepted": accepted,
            "items": re.findall(r"Item (\d\.\d\d)", e.findtext(ATOM + "summary") or ""),
        })
    return out


class Edgar:
    def __init__(self, cfg: dict, contact: str, universe, journal):
        self.cfg, self.universe, self.journal = cfg, universe, journal
        self.contact = (contact or "").strip()
        self.http = httpx.AsyncClient(timeout=20, headers={
            "User-Agent": f"trading-bot-research {self.contact}", "Accept-Encoding": "gzip, deflate"})
        self.seen = None          # accession numbers already logged; filled from the logs on first use
        self.tickers = {}         # SEC company number -> ticker symbols
        self.tickers_day = None
        self.stats = {"polls": 0, "filings": 0, "matched": 0, "errors": 0}

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

    async def collect(self):
        """Log every filing that is new since the last look. Returns how many were logged."""
        if self.seen is None:
            self._load_seen()
        await self._load_tickers()
        new = []
        for page in range(self.cfg["max_pages"]):
            r = await self.http.get(FEED, params={
                "action": "getcurrent", "type": "8-K", "company": "", "dateb": "", "owner": "include",
                "start": page * PAGE, "count": PAGE, "output": "atom"})
            r.raise_for_status()
            self.stats["polls"] += 1
            entries = parse_feed(r.content)
            fresh = [e for e in entries if e["acc"] not in self.seen]
            new += fresh
            if not entries or len(fresh) < len(entries):
                break                       # reached filings that are already logged
            await asyncio.sleep(0.5)        # stay far below the SEC's limit of 10 requests per second
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
        return logged

    def in_hours(self):
        """The SEC accepts filings on weekdays from 6:00 to 22:00 New York time."""
        t = now().astimezone(NY)
        return t.weekday() < 5 and 6 <= t.hour < 23

    def reset_day(self):
        self.stats = {k: 0 for k in self.stats}
        if self.seen and len(self.seen) > 20000:
            self.seen = None

    async def run(self):
        if not self.cfg.get("enabled"):
            return
        if not self.contact:
            log.warning("SEC_CONTACT fehlt: Pflichtmitteilungen der SEC werden nicht mitgeschrieben")
            return
        log.info("edgar: looking for new 8-K filings every %d s", self.cfg["poll_s"])
        while True:
            ready = bool(self.universe.cache)
            try:
                if ready and self.in_hours():
                    await self.collect()
            except Exception as e:
                self.stats["errors"] += 1
                if self.stats["errors"] <= 10 or self.stats["errors"] % 50 == 0:
                    self.journal.event("error", where="edgar", error=repr(e)[:200])
            # Right after the bot starts, the list of tradable stocks is still loading: look again soon.
            await asyncio.sleep(self.cfg["poll_s"] if ready else STARTUP_WAIT_S)


def _demo():   # python -m bot.edgar <contact>  -> prints the newest filings, for a manual check
    import sys

    async def go():
        class U:
            cache = {}
        e = Edgar({"enabled": True, "max_pages": 1, "poll_s": 600}, sys.argv[1], U(), None)
        await e._load_tickers()
        r = await e.http.get(FEED, params={"action": "getcurrent", "type": "8-K", "company": "", "dateb": "",
                                           "owner": "include", "start": 0, "count": 10, "output": "atom"})
        for x in parse_feed(r.content):
            print(json.dumps({**x, "accepted": iso(x["accepted"]), "tickers": e.tickers.get(x["cik"])}))
    asyncio.run(go())


if __name__ == "__main__":
    _demo()
