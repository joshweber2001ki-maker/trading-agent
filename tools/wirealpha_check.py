#!/usr/bin/env python3
"""What does the WireAlpha key on this server deliver, and how fast?

Run on the server:  bash deploy/run-tool.sh tools/wirealpha_check.py [SECONDS]

Two looks, both read-only. First the newest press releases over the ordinary interface: does the
key work, which wires are there, is the full text included, and what delay does WireAlpha itself
state. Then the live stream for SECONDS (default 90): how many messages arrive, from which
sources, with which tickers, and how many seconds after their time of publication. The first
messages are printed as they came, to check the format against the documentation.

The bot logs every release (`wire_item` with source "wa-<wire>") and trades the wires named in
`[wires] trade`; whether WireAlpha is really earlier than the wires' own feeds shows in the weekly
statistics. The key is never printed.
"""
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.common import env, load_config, now, parse_ts  # noqa: E402
from bot.wirealpha import STREAM  # noqa: E402

REST = "https://api.wirealpha.com/v1/articles"
SHOW = 3


def clean(text, key):
    """An error text without the key (the key is part of the stream's address)."""
    return str(text).replace(key, "***")[:300]


def delay_s(published, at=None):
    try:
        t = parse_ts(str(published))
    except (TypeError, ValueError):
        return None
    return None if t.tzinfo is None else ((at or now()) - t).total_seconds()


async def newest(key, sources):
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(REST, params={"source": ",".join(sources), "limit": 20},
                           headers={"Authorization": f"Bearer {key}"})
    if r.status_code in (401, 403):
        print(f"Der Schlüssel wird nicht angenommen ({r.status_code}). "
              "Neu eintragen: bash /opt/trading-agent/deploy/set-keys.sh wirealpha")
        return False
    if r.status_code != 200:
        print(f"Abfrage der neuesten Meldungen: Antwort {r.status_code} {clean(r.text, key)[:160]}")
        return True          # the stream may still work
    rows = (r.json() or {}).get("data") or []
    print(f"Neueste Meldungen (Abfrage): {len(rows)} Stück, Quellen {dict(Counter(x.get('source') for x in rows))}")
    with_text = sum(1 for x in rows if x.get("body"))
    lat = sorted(x["latency_ms"] for x in rows if isinstance(x.get("latency_ms"), (int, float)))
    print(f"  mit vollem Text: {with_text} von {len(rows)}"
          + (f"; Verzögerung laut WireAlpha: Median {lat[len(lat) // 2] / 1000:.1f} s, größte {lat[-1] / 1000:.1f} s" if lat else ""))
    for x in rows[:SHOW]:
        tickers = [t.get("ticker") if isinstance(t, dict) else t for t in x.get("tickers") or []]
        print(f"  {x.get('published_at')}  {x.get('source'):<14} {tickers}  {(x.get('title') or '')[:80]}")
    return True


async def listen(key, sources, seconds):
    print(f"\nStrom: {seconds} s zuhören ...")
    by_source, delays, shown, total = Counter(), {}, 0, 0
    end = time.monotonic() + seconds
    try:
        async with websockets.connect(f"{STREAM}?apiKey={key}", ping_interval=30, ping_timeout=30) as ws:
            await ws.send(json.dumps({"action": "subscribe", "tickers": ["*"]}))
            while time.monotonic() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                if raw.strip().strip('"').lower() == "ping":
                    await ws.send("pong")
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    print(f"  unlesbar: {raw[:120]}")
                    continue
                total += 1
                if not isinstance(msg, dict) or msg.get("type") != "article":
                    if shown < SHOW + 2:
                        shown += 1
                        print(f"  andere Nachricht: {json.dumps(msg, default=str)[:200]}")
                    continue
                data = msg.get("data") or {}
                src = str(data.get("source") or "?")
                by_source[src] += 1
                d = delay_s(data.get("published_at"))
                if d is not None and src in sources:
                    delays.setdefault(src, []).append(d)
                if shown < SHOW + 2 and src in sources:
                    shown += 1
                    print(f"  wie empfangen: {json.dumps(msg, default=str)[:420]}")
    except Exception as e:
        print(f"Strom: Verbindung fehlgeschlagen oder abgebrochen: {clean(repr(e), key)}")
    print(f"Empfangen: {total} Nachrichten; Meldungen je Quelle: {dict(by_source.most_common())}")
    for src in sorted(delays):
        d = sorted(delays[src])
        print(f"  {src}: {len(d)} Meldungen, Sekunden zwischen Veröffentlichung und Empfang: "
              f"schnellste {d[0]:.1f}, Median {d[len(d) // 2]:.1f}, langsamste {d[-1]:.1f}")
    if not delays:
        print("  Keine Meldung der eingestellten Quellen in dieser Zeit. Außerhalb der US-Handelszeit ist das "
              "normal; sonst länger zuhören, zum Beispiel 600.")
    print("Hinweis: Die Zeit der Veröffentlichung ist bei den Verteilern auf die Sekunde, bei manchen auf die Minute "
          "genau. Der verlässliche Vergleich ist der gegen die Feeds der Verteiler in der Wochenstatistik.")


async def main(seconds):
    key = (env("WIREALPHA_API_KEY", "") or "").strip()
    if not key:
        print("Kein WireAlpha-Schlüssel auf dem Server. Eintragen: bash /opt/trading-agent/deploy/set-keys.sh wirealpha")
        return 1
    sources = load_config().get("wirealpha", {}).get("sources") or []
    print(f"WireAlpha-Prüfung, Schlüssel mit {len(key)} Zeichen, Quellen: {', '.join(sources)}")
    try:
        if not await newest(key, sources):
            return 1
    except Exception as e:
        print(f"Abfrage der neuesten Meldungen fehlgeschlagen: {clean(repr(e), key)}")
    await listen(key, set(sources), seconds)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 90)))
