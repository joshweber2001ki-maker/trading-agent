#!/usr/bin/env python3
"""Weekly statistics from the bot's logs.

Usage:
  python tools/weekly_stats.py                 # last completed Mon-Fri week
  python tools/weekly_stats.py 2026-11-16      # the week containing that date
  python tools/weekly_stats.py --json          # machine-readable
"""
import json
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def week_of(d: date):
    mon = d - timedelta(days=d.weekday())
    return mon, mon + timedelta(days=4)


def default_week(today: date):
    """The current week from Friday on, otherwise the previous one."""
    return week_of(today if today.weekday() >= 4 else today - timedelta(days=7))


def read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def trade_stats(trades):
    if not trades:
        return {"n": 0}
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    return {
        "n": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "pnl": round(sum(t["pnl"] for t in trades), 2),
        "avg_r": round(sum(t["r"] for t in trades) / len(trades), 2),
        "avg_win_r": round(sum(t["r"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss_r": round(sum(t["r"] for t in losses) / len(losses), 2) if losses else 0,
        "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "avg_hold_min": round(sum(t["hold_min"] for t in trades) / len(trades), 1),
        "avg_slippage_pct": round(sum(t["slippage_pct"] for t in trades) / len(trades), 3),
        "avg_notional": round(sum(t["notional"] for t in trades) / len(trades)),
        "exit_reasons": dict(Counter(t["exit_reason"] for t in trades)),
        "long": sum(1 for t in trades if t["side"] == "long"),
        "short": sum(1 for t in trades if t["side"] == "short"),
    }


IMPACT = {0: "kaum", 1: "mittel", 2: "groß", 3: "sehr groß"}


def outcome_stats(rows):
    """How the price moved after rated articles. Moves are counted in the direction the KI predicted."""
    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 2) if xs else None

    directed = [r for r in rows if r.get("direction") in ("up", "down")]
    sign = lambda r: 1 if r["direction"] == "up" else -1
    out = {"n": len(rows), "with_direction": len(directed)}
    for h in (5, 15, 30, 60):
        out[f"dir_move_{h}"] = avg([r[f"ret_{h}"] * sign(r) for r in directed if r.get(f"ret_{h}") is not None])
    hits = [r["ret_30"] * sign(r) > 0 for r in directed if r.get("ret_30") is not None]
    out["hit_rate_30_pct"] = round(sum(hits) / len(hits) * 100, 1) if hits else None
    out["abs_move_30"] = avg([abs(r["ret_30"]) for r in rows if r.get("ret_30") is not None])
    out["best_in_direction"] = avg([r["max_up"] if sign(r) > 0 else -r["max_down"] for r in directed])
    out["worst_against"] = avg([r["max_down"] if sign(r) > 0 else -r["max_up"] for r in directed])
    return out


MOVER_PCT = 1.0     # an announcement counts as a mover if the price is at least this far away after 5 minutes
FRESH_REF_S = 600   # the stock must have traded within this many seconds before the announcement
SESSIONS = {"pre": "vorbörslich", "regular": "Handelszeit", "post": "nachbörslich"}


def speed_stats(rows):
    """How much of the move after an announcement is left for someone who gets in later.

    Looks only at announcements after which the price had moved by MOVER_PCT within 5 minutes,
    and assumes the direction is known. That makes every number an upper bound.
    """
    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        return round(xs[len(xs) // 2], 2) if xs else None

    measured = [r for r in rows if not r.get("skipped") and r.get("r300") is not None]
    # Only stocks that were trading right before the announcement: with a reference price that is
    # hours old, the first trade afterwards is a gap, not a move anyone could have followed.
    ok = [r for r in measured if (r.get("ref_age_s") or 0) <= FRESH_REF_S]
    movers = [r for r in ok if abs(r["r300"]) >= MOVER_PCT]
    sign = lambda r: 1 if r["r300"] > 0 else -1
    out = {"n": len(ok), "thin": len(measured) - len(ok), "skipped": sum(1 for r in rows if r.get("skipped")),
           "movers": len(movers),
           "move_300": med(abs(r["r300"]) for r in movers),
           "before_publication": med(r["pre_move"] * sign(r) for r in movers if r.get("pre_move") is not None),
           "after_30min": med(r["r1800"] * sign(r) for r in movers if r.get("r1800") is not None),
           "usd_first_10s": med((r.get("usd_0_2") or 0) + (r.get("usd_2_10") or 0) for r in movers)}
    for h in (2, 10, 60, 120):
        out[f"left_after_{h}s"] = med((r["r300"] - r[f"r{h}"]) * sign(r) for r in movers if r.get(f"r{h}") is not None)
    return out


def build(root: Path, start: date, end: date):
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    trades = [
        t for t in read_jsonl(root / "logs" / "trades.jsonl")
        if start.isoformat() <= t["entry_time"][:10] <= end.isoformat()
    ]
    events = []
    for d in days:
        events += read_jsonl(root / "logs" / f"events-{d.isoformat()}.jsonl")
    daily = []
    for d in days:
        p = root / "logs" / "daily" / f"{d.isoformat()}.json"
        if p.exists():
            daily.append(json.loads(p.read_text()))

    by_strategy = defaultdict(list)
    for t in trades:
        by_strategy[t["strategy"]].append(t)
    rejections = Counter(
        f'{e.get("stage", "?")}:{e.get("reason", "?")}' for e in events if e["kind"] == "rejected"
    )
    skipped = Counter(e.get("reason", "?").split(":")[0] for e in events if e["kind"] == "news_skipped")
    errors = Counter(e.get("where", "?") for e in events if e["kind"] == "error")
    classified = [e for e in events if e["kind"] == "news_classified"]
    outcomes = [e for e in events if e["kind"] == "news_outcome"]
    speed = [e for e in events if e["kind"] == "news_speed"]
    news_trades = by_strategy.get("news", [])
    by_category = defaultdict(list)
    for t in news_trades:
        by_category[(t.get("meta", {}).get("verdict") or {}).get("category", "?")].append(t)
    orb = by_strategy.get("orb", [])

    return {
        "week": {"start": start.isoformat(), "end": end.isoformat()},
        "days_with_data": len(daily),
        "equity": {
            "start": daily[0]["equity_start"] if daily else None,
            "end": daily[-1]["equity_end"] if daily else None,
            "by_day": [{"day": d["day"], "pnl": d["pnl"], "trades": d["trades"], "errors": d["errors"]} for d in daily],
        },
        "all": trade_stats(trades),
        "by_strategy": {k: trade_stats(v) for k, v in by_strategy.items()},
        "news": {
            "classified": len(classified),
            "tradeable_verdicts": sum(
                1 for e in classified
                if e["verdict"]["direction"] != "none" and e["verdict"]["surprise"] >= 7
                and e["verdict"].get("confidence", 0) >= 6 and e["verdict"].get("impact", 1) >= 1
            ),
            "by_impact": dict(sorted(Counter(IMPACT.get(e["verdict"].get("impact"), "?") for e in classified).items())),
            "avg_llm_s": round(sum(e.get("llm_s", 0) for e in classified) / len(classified), 1) if classified else None,
            "avg_news_to_signal_s": round(
                sum(t["meta"].get("news_to_signal_s", 0) for t in news_trades) / len(news_trades), 1
            ) if news_trades else None,
            "by_category": {k: trade_stats(v) for k, v in by_category.items()},
            "skipped": dict(skipped),
        },
        "orb": {
            "with_gap": trade_stats([t for t in orb if t.get("meta", {}).get("with_gap")]),
            "against_gap": trade_stats([t for t in orb if not t.get("meta", {}).get("with_gap")]),
            "with_range_dir": trade_stats([t for t in orb if t.get("meta", {}).get("with_range_dir")]),
            "against_range_dir": trade_stats([t for t in orb if not t.get("meta", {}).get("with_range_dir")]),
        },
        "news_by_source": {
            src: trade_stats([t for t in news_trades if t.get("meta", {}).get("source") == src])
            for src in sorted({t.get("meta", {}).get("source") or "?" for t in news_trades})
        },
        "ratings_vs_price": {
            "all": outcome_stats(outcomes),
            "by_impact": {IMPACT.get(k, str(k)): outcome_stats([o for o in outcomes if o.get("impact") == k])
                          for k in sorted({o.get("impact") for o in outcomes if o.get("impact") is not None})},
            "tradeable": outcome_stats([o for o in outcomes if o.get("direction") in ("up", "down")
                                        and (o.get("impact") or 0) >= 1 and (o.get("surprise") or 0) >= 7
                                        and (o.get("confidence") or 0) >= 6]),
            "by_source": {src: outcome_stats([o for o in outcomes if o.get("source") == src])
                          for src in sorted({o.get("source") or "?" for o in outcomes})},
        },
        "speed": {
            "all": speed_stats(speed),
            "by_source": {k: speed_stats([r for r in speed if r.get("src") == k])
                          for k in sorted({r.get("src") or "?" for r in speed})},
            "by_session": {SESSIONS.get(k, k): speed_stats([r for r in speed if r.get("session") == k])
                           for k in ("pre", "regular", "post") if any(r.get("session") == k for r in speed)},
            "by_size": {k: speed_stats([r for r in speed if r.get("size") == k])
                        for k in ("small", "mid-sized", "large", "very large") if any(r.get("size") == k for r in speed)},
        },
        "guard_interventions": sum(1 for e in events if e["kind"] == "guard"),
        "rejections": dict(rejections.most_common()),
        "errors": dict(errors),
        "halts": sum(1 for e in events if e["kind"] == "halt"),
        "orphans": sum(1 for e in events if e["kind"] == "orphan"),
        "no_fills": sum(1 for e in events if e["kind"] == "no_fill"),
        "best": sorted(trades, key=lambda t: -t["pnl"])[:3],
        "worst": sorted(trades, key=lambda t: t["pnl"])[:3],
    }


def fmt(s):
    if s.get("n", 0) == 0:
        return "keine Trades"
    return (
        f'{s["n"]} Trades, Trefferquote {s["win_rate_pct"]} %, Ergebnis {s["pnl"]:+.2f} $, '
        f'Schnitt {s["avg_r"]:+.2f} R, Profitfaktor {s["profit_factor"]}, '
        f'Haltedauer {s["avg_hold_min"]} min, Slippage {s["avg_slippage_pct"]} %, Ausstiege {s["exit_reasons"]}'
    )


def markdown(r):
    out = [f'# Wochenstatistik {r["week"]["start"]} bis {r["week"]["end"]}', ""]
    out.append(f'Handelstage mit Daten: {r["days_with_data"]}')
    if r["equity"]["start"] is not None:
        out.append(f'Depot: {r["equity"]["start"]:.2f} $ -> {r["equity"]["end"]:.2f} $')
    out += ["", "## Gesamt", fmt(r["all"]), "", "## Nach Strategie"]
    for k, v in r["by_strategy"].items():
        out.append(f"- {k}: {fmt(v)}")
    out += ["", "## News-Strategie",
            f'- bewertete Meldungen: {r["news"]["classified"]}, davon handelbar eingestuft: {r["news"]["tradeable_verdicts"]}',
            f'- Verteilung der Wirkungs-Stufen: {r["news"]["by_impact"]}',
            f'- KI-Antwortzeit: {r["news"]["avg_llm_s"]} s, Meldung bis Signal: {r["news"]["avg_news_to_signal_s"]} s',
            f'- übersprungen: {r["news"]["skipped"]}']
    for k, v in r["news"]["by_category"].items():
        out.append(f"- Kategorie {k}: {fmt(v)}")
    for k, v in r["news_by_source"].items():
        out.append(f"- Quelle {k}: {fmt(v)}")
    def fo(o):
        if not o["n"]:
            return "keine Daten"
        return (f'{o["n"]} Meldungen ({o["with_direction"]} mit Richtung): Bewegung in vorhergesagter Richtung nach '
                f'5/15/30/60 min = {o["dir_move_5"]} / {o["dir_move_15"]} / {o["dir_move_30"]} / {o["dir_move_60"]} %, '
                f'Trefferquote nach 30 min {o["hit_rate_30_pct"]} %, Betrag der Bewegung nach 30 min {o["abs_move_30"]} %, '
                f'bestenfalls {o["best_in_direction"]} %, schlimmstenfalls {o["worst_against"]} %')

    rv = r["ratings_vs_price"]
    out += ["", "## KI-Bewertung gegen Kursverlauf (alle bewerteten Meldungen in der Handelszeit, auch ohne Trade)",
            f"- gesamt: {fo(rv['all'])}", f"- wie gehandelt würde (Richtung, Wirkung >= 1, Überraschung >= 7, Sicherheit >= 6): {fo(rv['tradeable'])}"]
    out += [f"- Wirkung {k}: {fo(v)}" for k, v in rv["by_impact"].items()]
    out += [f"- Quelle {k}: {fo(v)}" for k, v in rv["by_source"].items()]
    def fs(o):
        thin = f' (dazu {o["thin"]} ohne Handel in den 10 Minuten davor)' if o.get("thin") else ""
        if not o["n"]:
            return "keine Daten" + thin
        if not o["movers"]:
            return f'{o["n"]} Meldungen gemessen{thin}, keine mit mindestens {MOVER_PCT:g} % Bewegung in 5 Minuten'
        return (f'{o["n"]} Meldungen gemessen{thin}, {o["movers"]} mit mindestens {MOVER_PCT:g} % Bewegung in 5 Minuten. Bei diesen: '
                f'Bewegung nach 5 min {o["move_300"]} %; davon noch zu holen bei Einstieg nach 2 s / 10 s / 60 s / 120 s = '
                f'{o["left_after_2s"]} / {o["left_after_10s"]} / {o["left_after_60s"]} / {o["left_after_120s"]} Prozentpunkte; '
                f'schon vor der Veröffentlichung gelaufen {o["before_publication"]} %; Stand nach 30 min {o["after_30min"]} %; '
                f'Umsatz in den ersten 10 s {o["usd_first_10s"]} $')

    sp = r["speed"]
    out += ["", "## Tempo: Kursverlauf im Sekundentakt nach Pressemitteilungen und SEC-Meldungen",
            "(Mittelwerte sind Mediane. Richtung und Auswahl gelten als bekannt, die Zahlen sind also Obergrenzen.)",
            f"- gesamt: {fs(sp['all'])}"]
    out += [f"- Quelle {k}: {fs(v)}" for k, v in sp["by_source"].items()]
    out += [f"- {k}: {fs(v)}" for k, v in sp["by_session"].items()]
    out += [f"- Firmengröße {k}: {fs(v)}" for k, v in sp["by_size"].items()]
    out += ["", "## Ausbruchs-Strategie",
            f'- in Richtung der Kurslücke: {fmt(r["orb"]["with_gap"])}',
            f'- gegen die Kurslücke: {fmt(r["orb"]["against_gap"])}',
            f'- in Richtung der Eröffnungsspanne: {fmt(r["orb"]["with_range_dir"])}',
            f'- gegen die Richtung der Eröffnungsspanne: {fmt(r["orb"]["against_range_dir"])}',
            "", "## Abgelehnte Signale (Stufe:Grund)"]
    out += [f"- {k}: {v}" for k, v in r["rejections"].items()] or ["- keine"]
    out += ["", "## Probleme",
            f'- Fehler nach Bereich: {r["errors"] or "keine"}',
            f'- Tagesverlust-Stopps: {r["halts"]}, verwaiste Positionen: {r["orphans"]}, nicht ausgeführte Einstiege: {r["no_fills"]}',
            f'- Eingriffe des Feierabend-Wächters (er musste vor Börsenschluss selbst glattstellen): {r["guard_interventions"]}',
            "", "## Tage"]
    out += [f'- {d["day"]}: {d["pnl"]:+.2f} $, {d["trades"]} Trades, {d["errors"]} Fehler' for d in r["equity"]["by_day"]] or ["- keine Daten"]
    for title, key in (("Beste Trades", "best"), ("Schlechteste Trades", "worst")):
        out += ["", f"## {title}"]
        out += [f'- {t["symbol"]} {t["strategy"]} {t["side"]}: {t["pnl"]:+.2f} $ ({t["r"]:+.2f} R, {t["exit_reason"]})' for t in r[key]] or ["- keine"]
    return "\n".join(out)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    start, end = week_of(date.fromisoformat(args[0])) if args else default_week(date.today())
    rep = build(ROOT, start, end)
    print(json.dumps(rep, indent=2, default=str) if "--json" in sys.argv else markdown(rep))
