#!/usr/bin/env python3
"""What does the Alpha Vantage key on this server deliver?

Run on the server:  bash deploy/run-tool.sh tools/alphavantage_check.py [SYMBOL ...]

The question behind it: Finnhub's free plan gives what analysts expect of the quarter that is
being reported, but not of the NEXT quarter and the year, and those decide how a company's own
outlook is to be read. Alpha Vantage lists such estimates (EARNINGS_ESTIMATES). This shows
whether the free key gets them, for a large and a mid-sized company, and what they look like.

The free plan allows 25 requests a day; this run uses one per symbol (two by default). The bot
does not use this key yet. The key is never printed.
"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.common import env  # noqa: E402

URL = "https://www.alphavantage.co/query"
DEFAULT = ["MSFT", "TTC"]       # a large company and a mid-sized one


def clean(text, key):
    return str(text).replace(key, "***")[:300]


def judge(data):
    """(ok, text): whether the answer carries estimates, and what else it says."""
    if not isinstance(data, dict):
        return False, "unerwartete Antwort"
    for k in ("Information", "Note", "Error Message"):
        if data.get(k):
            # the service repeats the key in some of these texts
            return False, f"{k}: {data[k]}"
    # documented as a list of records, one per fiscal quarter or year; take it under whatever name it comes
    rows = next((v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)), None)
    if not rows:
        return False, f"keine Schätzungen, Felder der Antwort: {sorted(data)[:8]}"
    return True, rows


async def main(symbols):
    key = (env("ALPHAVANTAGE_API_KEY", "") or "").strip()
    if not key:
        print("Kein Alpha-Vantage-Schlüssel auf dem Server. "
              "Eintragen: bash /opt/trading-agent/deploy/set-keys.sh alphavantage")
        return 1
    print(f"Alpha-Vantage-Prüfung, Schlüssel mit {len(key)} Zeichen. Das kostenlose Konto erlaubt 25 Abfragen am Tag; "
          f"diese Prüfung braucht {len(symbols)}.")
    good = 0
    async with httpx.AsyncClient(timeout=20) as http:
        for sym in symbols:
            try:
                r = await http.get(URL, params={"function": "EARNINGS_ESTIMATES", "symbol": sym, "apikey": key})
                ok, res = judge(r.json()) if r.status_code == 200 else (False, f"Antwort {r.status_code}")
            except Exception as e:
                ok, res = False, clean(repr(e), key)
            if not ok:
                print(f"{sym}: keine Erwartungen. {clean(res, key)}")
                continue
            good += 1
            print(f"{sym}: {len(res)} Zeilen. Felder: {', '.join(sorted(res[0]))[:230]}")
            for row in res[:4]:
                print("   " + "  ".join(f"{k}={row.get(k)}" for k in (
                    "date", "horizon", "eps_estimate_average", "eps_estimate_analyst_count",
                    "revenue_estimate_average", "revenue_estimate_analyst_count") if k in row))
    if good == len(symbols):
        print("Ergebnis: Das kostenlose Konto liefert die Erwartungen für kommende Quartale und das Jahr.")
    elif good:
        print("Ergebnis: Erwartungen nur für einen Teil der Aktien. Die Ausgabe oben bitte an Claude weitergeben.")
    else:
        print("Ergebnis: Mit diesem Schlüssel gibt es die Erwartungen nicht (Meldung des Dienstes siehe oben).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main([a.upper() for a in sys.argv[1:]] or DEFAULT)))
