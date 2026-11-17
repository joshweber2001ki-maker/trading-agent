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
            ),
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
        },
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
            f'- KI-Antwortzeit: {r["news"]["avg_llm_s"]} s, Meldung bis Signal: {r["news"]["avg_news_to_signal_s"]} s',
            f'- übersprungen: {r["news"]["skipped"]}']
    for k, v in r["news"]["by_category"].items():
        out.append(f"- Kategorie {k}: {fmt(v)}")
    out += ["", "## Ausbruchs-Strategie",
            f'- in Richtung der Kurslücke: {fmt(r["orb"]["with_gap"])}',
            f'- gegen die Kurslücke: {fmt(r["orb"]["against_gap"])}',
            "", "## Abgelehnte Signale (Stufe:Grund)"]
    out += [f"- {k}: {v}" for k, v in r["rejections"].items()] or ["- keine"]
    out += ["", "## Probleme",
            f'- Fehler nach Bereich: {r["errors"] or "keine"}',
            f'- Tagesverlust-Stopps: {r["halts"]}, verwaiste Positionen: {r["orphans"]}, nicht ausgeführte Einstiege: {r["no_fills"]}',
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
