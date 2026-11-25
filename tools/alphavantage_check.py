#!/usr/bin/env python3
"""What does the Alpha Vantage key on this server deliver?

Run on the server:  bash deploy/run-tool.sh tools/alphavantage_check.py [SYMBOL ...]

The question behind it: Finnhub's free plan gives what analysts expect of the quarter that is
being reported, but not of the NEXT quarter and the year, and those decide how a company's own
outlook is to be read. Alpha Vantage lists such estimates (EARNINGS_ESTIMATES). This shows
whether the free key gets them, for a large and a mid-sized company, and what they look like.

The free plan allows 25 requests a day and one a second; this run uses one per symbol (two by
default) and waits between them. The bot uses the rest of the day's requests (bot/alphavantage.py).
The key is never printed.
"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.alphavantage import Refused, read_answer  # noqa: E402
from bot.common import env  # noqa: E402

URL = "https://www.alphavantage.co/query"
DEFAULT = ["MSFT", "TTC"]       # a large company and a mid-sized one
PAUSE_S = 2.0                   # the free key takes one request a second


def clean(text, key):
    return str(text).replace(key, "***")[:300]


def judge(data):
    """(state, text or rows): "ok" with the records, "fast" or "day" for a notice about the limits
    (one request a second, 25 a day), "none" if the answer carries no estimates."""
    if not isinstance(data, dict):
        return "none", "unerwartete Antwort"
    if data.get("Error Message"):
        return "none", f"Error Message: {data['Error Message']}"
    try:
        rows = read_answer(data)
    except Refused as e:
        return ("day" if e.today else "fast"), " ".join(str(data.get(k) or "") for k in ("Information", "Note")).strip()
    if not rows:
        return "none", f"keine Schätzungen, Felder der Antwort: {sorted(data)[:8]}"
    return "ok", rows


async def ask(http, sym, key):
    r = await http.get(URL, params={"function": "EARNINGS_ESTIMATES", "symbol": sym, "apikey": key})
    return judge(r.json()) if r.status_code == 200 else ("none", f"Antwort {r.status_code}")


async def main(symbols):
    key = (env("ALPHAVANTAGE_API_KEY", "") or "").strip()
    if not key:
        print("Kein Alpha-Vantage-Schlüssel auf dem Server. "
              "Eintragen: bash /opt/trading-agent/deploy/set-keys.sh alphavantage")
        return 1
    print(f"Alpha-Vantage-Prüfung, Schlüssel mit {len(key)} Zeichen. Das kostenlose Konto erlaubt 25 Abfragen am Tag; "
          f"diese Prüfung braucht {len(symbols)}.")
    good, limited = 0, False
    async with httpx.AsyncClient(timeout=20) as http:
        for i, sym in enumerate(symbols):
            if i:
                await asyncio.sleep(PAUSE_S)
            try:
                state, res = await ask(http, sym, key)
                if state == "fast":                 # "not so fast": once more, a little later
                    await asyncio.sleep(PAUSE_S * 2)
                    state, res = await ask(http, sym, key)
            except Exception as e:
                state, res = "none", clean(repr(e), key)
            if state in ("fast", "day"):
                limited = True
                print(f"{sym}: nicht abgefragt, der Dienst verweist auf seine Grenze "
                      + ("von 25 Abfragen am Tag (heute ist sie erreicht)." if state == "day" else "von einer Abfrage je Sekunde.")
                      + " Das sagt nichts über diese Aktie.")
                continue
            if state != "ok":
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
    elif limited:
        print(f"Ergebnis: {good} von {len(symbols)} Aktien abgefragt, der Rest scheiterte an der Grenze des Dienstes, "
              "nicht an den Daten. Später noch einmal mit nur dieser Aktie prüfen.")
    elif good:
        print("Ergebnis: Erwartungen nur für einen Teil der Aktien. Die Ausgabe oben bitte an Claude weitergeben.")
    else:
        print("Ergebnis: Mit diesem Schlüssel gibt es die Erwartungen nicht (Meldung des Dienstes siehe oben).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main([a.upper() for a in sys.argv[1:]] or DEFAULT)))
