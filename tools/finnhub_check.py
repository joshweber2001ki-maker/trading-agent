#!/usr/bin/env python3
"""What does the Finnhub key on this server deliver?

Run on the server:  bash deploy/run-tool.sh tools/finnhub_check.py [--json]

Shows whether the earnings calendar carries the analysts' expectations (profit per share and
revenue) and the time of day of each report, how many of those companies the bot can trade,
and which further data the plan includes or refuses. The key itself is never printed.
"""
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.alpaca import Alpaca  # noqa: E402
from bot.common import ET, env, now  # noqa: E402
from bot.finnhub import Finnhub, FinnhubError, calendar_summary  # noqa: E402

DAYS_AHEAD = 7
PROBE = "AAPL"
# (label, path, parameters) - one call each, to see what the plan includes.
EXTRAS = [
    ("letzte Quartale: erwartet und gemeldet", "/stock/earnings", {"symbol": PROBE}),
    ("Kennzahlen der Firma", "/stock/metric", {"symbol": PROBE, "metric": "all"}),
    ("Firmenprofil mit Börsenwert", "/stock/profile2", {"symbol": PROBE}),
    ("Analysten-Empfehlungen", "/stock/recommendation", {"symbol": PROBE}),
    ("Gewinn-Schätzungen kommender Quartale", "/stock/eps-estimate", {"symbol": PROBE, "freq": "quarterly"}),
    ("Umsatz-Schätzungen kommender Quartale", "/stock/revenue-estimate", {"symbol": PROBE, "freq": "quarterly"}),
    ("Kursziele der Analysten", "/stock/price-target", {"symbol": PROBE}),
    ("Hoch- und Herabstufungen", "/stock/upgrade-downgrade", {"symbol": PROBE}),
]


def size(d):
    if isinstance(d, list):
        return len(d)
    if isinstance(d, dict):
        for key in ("data", "metric", "earningsCalendar"):
            if isinstance(d.get(key), (list, dict)):
                return len(d[key])
        return len(d)
    return 0


async def tradable_symbols():
    """Symbols the bot can trade, or None without Alpaca keys."""
    key, secret = env("APCA_API_KEY_ID", ""), env("APCA_API_SECRET_KEY", "")
    if not key or not secret:
        return None
    from bot.market import Universe
    u = Universe(Alpaca(key, secret, env("APCA_BASE_URL", "https://paper-api.alpaca.markets")))
    await u.load()
    return set(u.cache)


async def main(as_json=False):
    fh = Finnhub(env("FINNHUB_API_KEY", ""))
    if not fh.available:
        print("Kein Finnhub-Schlüssel auf dem Server. Eintragen: bash /opt/trading-agent/deploy/set-keys.sh finnhub")
        return 1
    today = now().astimezone(ET).date()
    start, end = today.isoformat(), (today + timedelta(days=DAYS_AHEAD)).isoformat()
    out = {"from": start, "to": end}
    try:
        rows = await fh.earnings_calendar(start, end)
    except FinnhubError as e:
        print(f"Ergebniskalender nicht abrufbar: {e}")
        if e.status == 401:
            print("Der Schlüssel wird nicht angenommen. Bitte neu eintragen: bash /opt/trading-agent/deploy/set-keys.sh finnhub")
        return 1
    out["calendar"] = calendar_summary(rows)
    try:
        universe = await tradable_symbols()
    except Exception as e:
        universe, out["universe_error"] = None, repr(e)[:120]
    if universe is not None:
        mine = [r for r in rows if r.get("symbol") in universe]
        out["calendar_tradable"] = calendar_summary(mine)
    else:
        mine = rows
    best = sorted((r for r in mine if r.get("revenueEstimate")), key=lambda r: -r["revenueEstimate"])[:6]
    out["examples"] = [{k: r.get(k) for k in ("symbol", "date", "hour", "epsEstimate", "revenueEstimate")} for r in best]
    out["extras"] = {}
    for label, path, params in EXTRAS:
        try:
            out["extras"][path] = {"label": label, "ok": True, "size": size(await fh.get(path, **params))}
        except FinnhubError as e:
            out["extras"][path] = {"label": label, "ok": False, "status": e.status}
    out["calls_per_minute"] = fh.plan_limit
    if as_json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0

    def line(name, c):
        return (f"{name}: {c['entries']} Berichte, {c['with_eps']} mit Gewinn-Erwartung, "
                f"{c['with_revenue']} mit Umsatz-Erwartung, {c['with_hour']} mit Tageszeit {c['hours']}")
    print(f"Finnhub-Prüfung, Ergebniskalender {start} bis {end}")
    print(line("alle", out["calendar"]))
    if "calendar_tradable" in out:
        print(line("handelbar", out["calendar_tradable"]))
    for x in out["examples"]:
        print(f"  {x['symbol']:<6}{x['date']} {x['hour'] or '?':<4} Gewinn je Aktie {x['epsEstimate']}  "
              f"Umsatz {x['revenueEstimate'] / 1e9:.2f} Mrd")
    for path, x in out["extras"].items():
        state = f"ja ({x['size']} Einträge)" if x["ok"] else f"nein ({x['status']})"
        print(f"{x['label']}: {state}")
    print(f"Anfragen pro Minute laut Finnhub: {out['calls_per_minute']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(as_json="--json" in sys.argv)))
