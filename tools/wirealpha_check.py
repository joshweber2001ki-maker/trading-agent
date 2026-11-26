#!/usr/bin/env python3
"""What does the WireAlpha key on this server deliver, and how fast?

Run on the server:  bash deploy/run-tool.sh tools/wirealpha_check.py [SECONDS]

Two looks, both read-only. First the newest press releases over the ordinary interface: does the
key work, which wires are there, is the full text included, and what delay does WireAlpha itself
state. Then the live stream for SECONDS (default 90): how many messages arrive, from which
sources, with which tickers, and how many seconds after their time of publication. The first
messages are printed as they came, to check the format against the documentation.

If one of the two does not answer at the address set in `[wirealpha]`, the tool looks for the
right one: it reads the service's own description of its interface and tries the likely addresses
and ways of sending the key, and prints what works, as the lines to put into config.toml. (The
service's documentation named addresses its server did not know on 26.11.2026.)

The bot logs every release (`wire_item` with source "wa-<wire>") and trades the wires named in
`[wires] trade`; whether WireAlpha is really earlier than the wires' own feeds shows in the weekly
statistics. The key is never printed.
"""
import asyncio
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.common import env, load_config, now, parse_ts  # noqa: E402
from bot.wirealpha import API, ARTICLE, STREAM, SUBSCRIBE, auth, stream_address  # noqa: E402

SHOW = 3
# Where a server built like this one usually keeps the description of its interface.
DESCRIPTIONS = ("/api/v1/openapi.json", "/openapi.json", "/v1/openapi.json", "/api/openapi.json")
LISTS = ("/api/v1/articles/", "/api/v1/articles", "/v1/articles", "/articles")
KEY_STYLES = ("bearer", "header:X-API-Key", "query:apiKey", "query:api_key")
STREAMS = (("/api/v1/ws?apiKey={key}", "none"), ("/v1/ws?apiKey={key}", "none"), ("/api/v1/ws?api_key={key}", "none"),
           ("/api/v1/ws?token={key}", "none"), ("/ws?apiKey={key}", "none"),
           ("/api/v1/ws", "bearer"), ("/api/v1/ws", "header:X-API-Key"))
OF_INTEREST = re.compile(r"article|news|release|stream|/ws|key|webhook|source|feed|subscri", re.I)


def clean(text, key):
    """A text without the key (the key is part of the stream's address)."""
    return str(text).replace(key, "***")[:300]


def delay_s(published, at=None):
    try:
        t = parse_ts(str(published))
    except (TypeError, ValueError):
        return None
    return None if t.tzinfo is None else ((at or now()) - t).total_seconds()


def rows_of(data) -> list:
    """The releases in an answer, whatever it is wrapped in."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("data", "items", "articles", "results"):
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
    return []


def show_rows(rows):
    print(f"  {len(rows)} Meldungen, Quellen {dict(Counter(str(x.get('source')) for x in rows))}")
    if rows:
        print(f"  Felder einer Meldung: {', '.join(sorted(rows[0]))[:300]}")
    with_text = sum(1 for x in rows if x.get("body"))
    lat = sorted(x["latency_ms"] for x in rows if isinstance(x.get("latency_ms"), (int, float)))
    print(f"  mit vollem Text: {with_text} von {len(rows)}"
          + (f"; Verzögerung laut WireAlpha: Median {lat[len(lat) // 2] / 1000:.1f} s, größte {lat[-1] / 1000:.1f} s" if lat else ""))
    for x in rows[:SHOW]:
        tickers = [t.get("ticker") if isinstance(t, dict) else t for t in x.get("tickers") or []]
        print(f"  {x.get('published_at')}  {str(x.get('source')):<14} {tickers}  {(x.get('title') or '')[:80]}")


def list_address(cfg):
    """The address of the list of releases: the one of a single release without its id."""
    return (cfg.get("api") or API).rstrip("/") + (cfg.get("article") or ARTICLE).split("{id}")[0].rstrip("/")


async def newest(cfg, key, sources) -> bool:
    """The newest releases over the address that is set. True if the service answered with them."""
    headers, params = auth(cfg.get("auth", "bearer"), key)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
        r = await http.get(list_address(cfg), params={**params, "source": ",".join(sources), "limit": 20}, headers=headers)
    if r.status_code != 200:
        print(f"Abfrage der neuesten Meldungen: Antwort {r.status_code} {clean(r.text, key)[:160]}")
        return False
    print("Neueste Meldungen (Abfrage):")
    show_rows(rows_of(r.json()))
    return True


async def hear(ws, key, sources, seconds, shown_max=SHOW + 2):
    """Listen on an open connection. Returns (messages, per source, delays per source)."""
    by_source, delays, shown, total = Counter(), {}, 0, 0
    end = time.monotonic() + seconds
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
            print(f"  unlesbar: {clean(raw, key)[:120]}")
            continue
        total += 1
        if not isinstance(msg, dict) or msg.get("type") != "article":
            if shown < shown_max:
                shown += 1
                print(f"  andere Nachricht: {clean(json.dumps(msg, default=str), key)[:240]}")
            continue
        data = msg.get("data") or {}
        src = str(data.get("source") or "?")
        by_source[src] += 1
        d = delay_s(data.get("published_at"))
        if d is not None and src in sources:
            delays.setdefault(src, []).append(d)
        if shown < shown_max and (src in sources or not sources):
            shown += 1
            print(f"  wie empfangen: {clean(json.dumps(msg, default=str), key)[:420]}")
    return total, by_source, delays


def why(e) -> str:
    """Why a connection failed, without anything that could hold the key."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    return f"Antwort {status}" if status else type(e).__name__


async def listen(cfg, key, sources, seconds) -> bool:
    """The stream at the address that is set. True if the connection came about."""
    print(f"\nStrom: {seconds} s zuhören ...")
    url, headers = stream_address(cfg, key)
    try:
        async with websockets.connect(url, additional_headers=headers or None, ping_interval=30, ping_timeout=30) as ws:
            await ws.send(json.dumps(SUBSCRIBE))
            total, by_source, delays = await hear(ws, key, sources, seconds)
    except Exception as e:
        print(f"Strom: Verbindung nicht zustande gekommen oder abgebrochen ({why(e)}).")
        return False
    print(f"Empfangen: {total} Nachrichten; Meldungen je Quelle: {dict(by_source.most_common())}")
    for src in sorted(delays):
        d = sorted(delays[src])
        print(f"  {src}: {len(d)} Meldungen, Sekunden zwischen Veröffentlichung und Empfang: "
              f"schnellste {d[0]:.1f}, Median {d[len(d) // 2]:.1f}, langsamste {d[-1]:.1f}")
    if not delays:
        print("  Keine Meldung der eingestellten Quellen in dieser Zeit. Außerhalb der US-Handelszeit und an "
              "Feiertagen ist das normal; sonst länger zuhören, zum Beispiel 600.")
    print("Hinweis: Die Zeit der Veröffentlichung ist bei den Verteilern auf die Sekunde, bei manchen auf die Minute "
          "genau. Der verlässliche Vergleich ist der gegen die Feeds der Verteiler in der Wochenstatistik.")
    return True


# ── looking for the addresses that work ──────────────────────────────────────────────────────
def styles_from(spec) -> list:
    """The ways of sending the key that the service's description names, most likely first."""
    out = []
    for s in ((spec.get("components") or {}).get("securitySchemes") or {}).values():
        kind, where, name = s.get("type"), s.get("in"), s.get("name")
        if kind == "apiKey" and where in ("header", "query") and name:
            out.append(f"{where}:{name}")
        elif kind in ("http", "oauth2"):
            out.append("bearer")
    return out


async def description(http, base, key):
    """The service's description of its own interface (OpenAPI), or None."""
    for path in DESCRIPTIONS:
        try:
            r = await http.get(base + path)
            spec = r.json() if r.status_code == 200 else None
        except Exception:
            spec = None
        if isinstance(spec, dict) and isinstance(spec.get("paths"), dict):
            print(f"Beschreibung der Schnittstelle gefunden: {path}, {len(spec['paths'])} Adressen")
            schemes = (spec.get("components") or {}).get("securitySchemes") or {}
            for name, s in schemes.items():
                print(f"  Anmeldung „{name}“: {s.get('type')} {s.get('scheme') or ''} {s.get('in') or ''} {s.get('name') or ''}".rstrip())
            shown = 0
            for p, methods in spec["paths"].items():
                if OF_INTEREST.search(p) and shown < 45:
                    shown += 1
                    print(f"  {'/'.join(m.upper() for m in methods if m in ('get', 'post', 'put', 'delete'))} {p}")
            return spec
    print("Keine Beschreibung der Schnittstelle gefunden (versucht: " + ", ".join(DESCRIPTIONS) + ").")
    return None


async def find(cfg, key):
    """Try the likely addresses and ways of sending the key; print what works."""
    base = (cfg.get("api") or API).rstrip("/")
    print(f"\nSuche die Adressen, die funktionieren (Server {base}) ...")
    found = {}
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as http:
        spec = await description(http, base, key) or {}
        paths = spec.get("paths") or {}
        lists = [p for p, m in paths.items() if re.search(r"/articles/?$", p) and "get" in m] + list(LISTS)
        single = next((p for p in paths if re.search(r"/articles/\{[^}/]+\}$", p)), None)
        styles = list(dict.fromkeys(styles_from(spec) + list(KEY_STYLES)))
        print("Abfrage der Meldungen:")
        for path in dict.fromkeys(lists):
            for style in styles:
                headers, params = auth(style, key)
                try:
                    r = await http.get(base + path, params={**params, "limit": 3}, headers=headers)
                except Exception as e:
                    print(f"  {path} mit {style}: {type(e).__name__}")
                    continue
                print(f"  {path} mit {style}: Antwort {r.status_code}"
                      + ("" if r.status_code == 200 else f" {clean(r.text, key)[:100]}"))
                if r.status_code == 200:
                    show_rows(rows_of(r.json()))
                    article = re.sub(r"\{[^}]+\}", "{id}", single) if single else path.rstrip("/") + "/{id}"
                    found.update(article=article, auth=style)
                    break
                if r.status_code == 404:
                    break               # no such address: the other ways of sending the key will not change that
            if found:
                break
    print("Strom:")
    prefer = "/api/v1" if found.get("article", "").startswith("/api/v1") else ""
    for path, style in sorted(STREAMS, key=lambda c: not c[0].startswith(prefer)) if prefer else STREAMS:
        url, headers = stream_address({"api": base, "stream": path, "stream_auth": style}, key)
        label = f"{path}" + ("" if style == "none" else f" mit {style}")
        try:
            async with websockets.connect(url, additional_headers=headers or None, open_timeout=8,
                                          ping_interval=30, ping_timeout=30) as ws:
                print(f"  {label}: verbunden. 20 s zuhören ...")
                await ws.send(json.dumps(SUBSCRIBE))
                total, by_source, _ = await hear(ws, key, set(), 20)
                print(f"  empfangen: {total} Nachrichten {dict(by_source.most_common())}")
                found.update(stream=path, stream_auth=style)
                break
        except Exception as e:
            print(f"  {label}: {why(e)}")
    print("\nErgebnis der Suche:")
    if not found:
        print("  Nichts gefunden: Weder die Abfrage noch der Strom antwortet an einer der versuchten Adressen. "
              "Die Ausgabe oben bitte an Claude weitergeben.")
        return
    for name, what in (("article", "Abfrage"), ("stream", "Strom")):
        print(f"  {what}: " + ("gefunden" if name in found else "NICHT gefunden"))
    print("  Einzustellen in config.toml unter [wirealpha] (bitte an Claude weitergeben):")
    for k in ("article", "auth", "stream", "stream_auth"):
        if k in found:
            print(f'    {k} = "{found[k]}"')


async def main(seconds):
    key = (env("WIREALPHA_API_KEY", "") or "").strip()
    if not key:
        print("Kein WireAlpha-Schlüssel auf dem Server. Eintragen: bash /opt/trading-agent/deploy/set-keys.sh wirealpha")
        return 1
    cfg = load_config().get("wirealpha", {})
    sources = cfg.get("sources") or []
    print(f"WireAlpha-Prüfung, Schlüssel mit {len(key)} Zeichen, Quellen: {', '.join(sources)}")
    print(f"Eingestellt: Abfrage {list_address(cfg)} ({cfg.get('auth', 'bearer')}), "
          f"Strom {(cfg.get('stream') or STREAM).split('?')[0]}")
    try:
        answered = await newest(cfg, key, sources)
    except Exception as e:
        answered = False
        print(f"Abfrage der neuesten Meldungen fehlgeschlagen ({type(e).__name__}).")
    connected = await listen(cfg, key, set(sources), seconds)
    if not (answered and connected):
        await find(cfg, key)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 90)))
