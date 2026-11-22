"""Only the first report of a piece of news is rated and traded.

The same announcement can arrive several times: over two wires, over several topic feeds of one
wire, and later again over Alpaca's news stream. Whichever source delivers it first wins; every later
copy is dropped and logged (`news_dup`) with how much later it came, which also measures which source
is fastest.

Two reports count as the same news when they concern the same stock, arrive within `window_min`
minutes of each other and either have similar headlines or both report results (the wire's headline
"Acme Reports Third Quarter Results" and Alpaca's "Acme Q3 EPS $1.20 Beats $1.10 Estimate" are the same
news in different words).
"""
import re
from datetime import timedelta

from .common import now, parse_ts

RESULTS = re.compile(r"\b(results|earnings|quarter|q[1-4]\b|fiscal|eps|revenue|sales)\b", re.I)
WORD = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "and", "of", "for", "to", "in", "on", "inc", "corp", "corporation", "ltd", "plc",
        "co", "company", "announces", "reports", "its", "with", "at", "by", "as", "from"}


def words(headline: str) -> set:
    return {w for w in WORD.findall((headline or "").lower()) if w not in STOP and len(w) > 1}


def similar(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class FirstReport:
    def __init__(self, journal, window_min: float = 15, min_similarity: float = 0.5):
        self.journal, self.window = journal, timedelta(minutes=window_min)
        self.min_similarity = min_similarity
        self.recent = {}     # symbol -> [(received, source, id, words, results, published)]
        self.ids = set()

    def claim(self, art: dict) -> bool:
        """True if this is the first report of its news (rate and trade it), False for a later copy."""
        aid = art.get("id")
        if aid in self.ids:
            return False
        t = now()
        head = art.get("headline") or ""
        ws, results = words(head), bool(RESULTS.search(head))
        try:
            published = parse_ts(art["created_at"]) if art.get("created_at") else None
        except (TypeError, ValueError):
            published = None
        for sym in art.get("symbols") or []:
            keep = [r for r in self.recent.get(sym, []) if t - r[0] <= self.window]
            self.recent[sym] = keep
            for received, source, first_id, fws, fresults, fpub in keep:
                sim = similar(ws, fws)
                if sim >= self.min_similarity or (results and fresults):
                    self.ids.add(aid)
                    self.journal.event(
                        "news_dup", article=aid, source=art.get("source"), symbol=sym, headline=head[:140],
                        first_article=first_id, first_source=source, lag_s=round((t - received).total_seconds(), 1),
                        published_lag_s=(round((published - fpub).total_seconds()) if published and fpub else None),
                        similarity=round(sim, 2))
                    return False
        self.ids.add(aid)
        # A roundup naming many stocks is nobody's first report: it must not block a stock's own release.
        for sym in (art.get("symbols") or []) if len(art.get("symbols") or []) <= 2 else []:
            self.recent.setdefault(sym, []).append((t, art.get("source"), aid, ws, results, published))
        if len(self.ids) > 50000:
            self.ids.clear()
        return True
