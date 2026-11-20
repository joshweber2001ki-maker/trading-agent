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
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def week_of(d: date):
    mon = d - timedelta(days=d.weekday())
    return mon, mon + timedelta(days=4)


def default_week(today: date):
    """The current week from Friday on, otherwise the previous one."""
    return week_of(today if today.weekday() >= 4 else today - timedelta(days=7))


NIGHT_UNTIL_H = "04"   # UTC. The session after the close ends at 20:00 in New York, which is 0:00 or 1:00 UTC of the next day.


def trading_day(ts):
    """The trading day a UTC time belongs to: what happens after midnight UTC is still the evening before."""
    day = date.fromisoformat(ts[:10])
    return (day - timedelta(days=1) if ts[11:13] < NIGHT_UNTIL_H else day).isoformat()


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


CORRECTIONS = Path("reports") / "trade-corrections.jsonl"


def apply_corrections(trades, corrections):
    """Trades the bot recorded with a wrong exit, put right.

    The logs are written by the server only, so a wrong record is not edited there. Instead one line
    per trade in reports/trade-corrections.jsonl gives the real exit (exit_price, optionally
    exit_time and exit_reason, and a note saying how it is known). Result, R and holding time are
    recomputed from it; what the bot had recorded is kept under "recorded".
    """
    by_id = {c["id"]: c for c in corrections if c.get("id") and c.get("exit_price") is not None}
    out = []
    for t in trades:
        c = by_id.get(t.get("id"))
        if c:
            t = dict(t, recorded={k: t.get(k) for k in ("exit_price", "exit_time", "exit_reason", "pnl", "r")})
            t.update({k: c[k] for k in ("exit_price", "exit_time", "exit_reason") if k in c})
            dirn = 1 if t["side"] == "long" else -1
            t["pnl"] = round((t["exit_price"] - t["entry_price"]) * t["qty"] * dirn, 2)
            risk = abs(t["signal_price"] - t["stop_price"]) * t["qty"]
            t["r"] = round(t["pnl"] / risk, 2) if risk else 0
            ts = lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))
            t["hold_min"] = round((ts(t["exit_time"]) - ts(t["entry_time"])).total_seconds() / 60, 1)
            t["note"] = c.get("note", "")
        out.append(t)
    return out


def day_result(daily, events):
    """Start value and result of one day.

    Summaries written from 2026-11-18 on measure against the broker's value at the previous close.
    Older ones measured from the bot's last start, which hides everything before a restart during
    the day. For those the start is the previous close if a later start of the bot logged it that
    day, otherwise the bot's first reading of the day.
    """
    if daily.get("basis") in ("last_close", "day_start"):
        return daily["equity_start"], daily["pnl"]
    starts = [e for e in events if e["kind"] == "day_start"]
    last_close = next((e["last_equity"] for e in starts if e.get("last_equity")), None)
    first = next((e["equity"] for e in starts if e.get("equity")), None)
    start = last_close or first or daily["equity_start"]
    return start, round(daily["equity_end"] - start, 2)


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
DIRECTION = {"up": "steigt", "down": "fällt"}


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
LIQUID_USD = 100_000  # traded in the 5 minutes after the announcement; below that, a position of 25,000 $ is not realistic
SESSIONS = {"pre": "vorbörslich", "regular": "Handelszeit", "post": "nachbörslich"}


def usd_300(r):
    """Dollars traded in the 5 minutes after the announcement. A stock so busy that the trade
    list was cut off counts as far above any threshold."""
    if r.get("truncated"):
        return float("inf")
    return sum(r.get(k) or 0 for k in ("usd_0_2", "usd_2_10", "usd_10_60", "usd_60_300"))


def speed_stats(rows):
    """How much of the move after an announcement is left for someone who gets in later.

    Looks only at announcements after which the price had moved by MOVER_PCT within 5 minutes,
    and assumes the direction is known. That makes every number an upper bound. Most of these
    moves happen in thin trading before the open or after the close, so the same numbers are
    given again for the movers with at least LIQUID_USD traded in those 5 minutes.
    """
    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        return round(xs[len(xs) // 2], 2) if xs else None

    sign = lambda r: 1 if r["r300"] > 0 else -1

    def core(movers):
        out = {"movers": len(movers),
               "move_300": med(abs(r["r300"]) for r in movers),
               "before_publication": med(r["pre_move"] * sign(r) for r in movers if r.get("pre_move") is not None),
               "after_30min": med(r["r1800"] * sign(r) for r in movers if r.get("r1800") is not None),
               "usd_first_10s": med((r.get("usd_0_2") or 0) + (r.get("usd_2_10") or 0) for r in movers),
               "usd_5min": med(usd_300(r) for r in movers if not r.get("truncated"))}
        for h in (2, 10, 60, 120):
            out[f"left_after_{h}s"] = med((r["r300"] - r[f"r{h}"]) * sign(r) for r in movers if r.get(f"r{h}") is not None)
        return out

    measured = [r for r in rows if not r.get("skipped") and r.get("r300") is not None]
    # Only stocks that were trading right before the announcement: with a reference price that is
    # hours old, the first trade afterwards is a gap, not a move anyone could have followed.
    ok = [r for r in measured if (r.get("ref_age_s") or 0) <= FRESH_REF_S]
    movers = [r for r in ok if abs(r["r300"]) >= MOVER_PCT]
    return {"n": len(ok), "thin": len(measured) - len(ok), "skipped": sum(1 for r in rows if r.get("skipped")),
            **core(movers), "liquid": core([r for r in movers if usd_300(r) >= LIQUID_USD])}


# List prices in $ per million tokens (input, output), as of 2026-11-17.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}


def news_limits(root):
    """The thresholds a rating must reach to be traded, from config.toml."""
    try:
        import tomllib
        n = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))["news"]
        return n["min_surprise"], n["min_confidence"], n["min_impact"]
    except Exception:
        return 7, 6, 1


def model_stats(classified, shadows, fallbacks, outcomes, limits):
    """The same articles as rated by each model, against what the price did afterwards.

    Since 2026-11-18 one model decides and others rate along. Everything that compares the models
    looks only at articles that all of them rated, so that a model is not judged on easier news.
    """
    min_surprise, min_confidence, min_impact = limits

    def med(xs, q=0.5):
        xs = sorted(x for x in xs if x is not None)
        return round(xs[min(len(xs) - 1, int(len(xs) * q))], 1) if xs else None

    def candidate(v):
        return bool(v) and v.get("direction") in ("up", "down") and (v.get("surprise") or 0) >= min_surprise \
            and (v.get("confidence") or 0) >= min_confidence and (v.get("impact") or 0) >= min_impact

    decided = [e for e in classified if e.get("model") and e.get("phase") == "live"]
    if not decided:
        return {"models": {}, "common": 0}
    rows = defaultdict(list)                       # model -> every rating it gave
    by_article = defaultdict(dict)                 # article -> model -> verdict
    info = {}                                      # article -> headline and the symbol the outcome was measured for
    for e in decided:
        rows[e["model"]].append(e)
        by_article[e["article"]][e["model"]] = e["verdict"]
        info[e["article"]] = (e.get("headline", ""), e["symbol"])
    failed = Counter(f.get("failed") for f in fallbacks)
    for e in shadows:
        if e.get("verdict"):
            rows[e["model"]].append(e)
            by_article[e["article"]].setdefault(e["model"], e["verdict"])
        else:
            failed[e.get("model")] += 1
    models = sorted(rows, key=lambda m: (-sum(1 for e in decided if e["model"] == m), m))
    common = [a for a, vs in by_article.items() if a in info and all(m in vs for m in models)]
    price = {(o.get("article"), o.get("symbol")): o for o in outcomes}

    def after(article, verdict):
        """The measured price path, if the model rated the stock it was measured for."""
        o = price.get((article, info[article][1]))
        return o if o and verdict.get("symbol") == info[article][1] else None

    movers = [a for a in common if (price.get((a, info[a][1])) or {}).get("ret_30") is not None
              and abs(price[(a, info[a][1])]["ret_30"]) >= MOVER_PCT]
    out = {"models": {}, "common": len(common), "movers": len(movers), "limits": list(limits)}
    for m in models:
        mine = rows[m]
        tin, tout = [e.get("tokens_in") or 0 for e in mine], [e.get("tokens_out") or 0 for e in mine]
        pin, pout = PRICES.get(m, (None, None))
        cost = (sum(tin) * pin + sum(tout) * pout) / 1e6 if pin is not None else None
        cands = [a for a in common if candidate(by_article[a][m])]
        seen = [dict(o, direction=by_article[a][m]["direction"]) for a in cands if (o := after(a, by_article[a][m]))]
        out["models"][m] = {
            "decided": sum(1 for e in decided if e["model"] == m), "ratings": len(mine), "failed": failed.get(m, 0),
            "seconds_median": med(e.get("model_s") for e in mine), "seconds_p90": med((e.get("model_s") for e in mine), 0.9),
            "tokens_in": round(sum(tin) / len(mine)), "tokens_out": round(sum(tout) / len(mine)),
            "cost": round(cost, 2) if cost is not None else None,
            "cents_per_rating": round(cost / len(mine) * 100, 3) if cost is not None else None,
            "with_direction": sum(1 for a in common if by_article[a][m].get("direction") in ("up", "down")),
            "candidates": len(cands), "candidates_vs_price": outcome_stats(seen),
            "movers_caught": sum(1 for a in movers if a in cands and after(a, by_article[a][m])
                                 and (price[(a, info[a][1])]["ret_30"] > 0) == (by_article[a][m]["direction"] == "up")),
        }
    differ = [a for a in common if len({candidate(by_article[a][m]) for m in models}) > 1]
    move = lambda a: (price.get((a, info[a][1])) or {}).get("ret_30")
    differ.sort(key=lambda a: -abs(move(a) or 0))
    out["differences"] = len(differ)
    out["examples"] = [{
        "symbol": info[a][1], "headline": info[a][0], "ret_30": move(a),
        "verdicts": {m: "{direction} {surprise}/{impact}/{confidence}".format(**{"impact": "?", **by_article[a][m]}) for m in models},
    } for a in differ[:12]]
    return out


def fast_limits(root):
    """The thresholds a rating of the immediate entry must reach to be traded, from config.toml."""
    try:
        import tomllib
        f = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))["fast"]
        return f["min_surprise"], f["min_confidence"], f["min_impact"]
    except Exception:
        return 7, 7, 1


def fast_stats(rated, speed, skipped, rejected, limits):
    """The ratings of the immediate entry against what the price did after the same announcement.

    Every rating is logged, with or without a trade. The price path is the nightly measurement of
    the same press release (news_speed carries the same id). "Right" means: after 5 minutes the
    price was on the predicted side of the price before the announcement. That says whether the KI
    reads announcements correctly. It does not say that a trade would have paid: what a late order
    gets, and what the spread costs, is another matter (memory/strategy.md, "Dritte Auswertung").
    """
    min_surprise, min_confidence, min_impact = limits

    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        return round(xs[len(xs) // 2], 1) if xs else None

    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 2) if xs else None

    path = {(r.get("src"), r.get("ref")): r for r in speed if not r.get("skipped") and r.get("r300") is not None}
    after = lambda e: path.get((e.get("source"), e.get("article")))
    direction = lambda e: (e.get("verdict") or {}).get("direction")
    sign = lambda e: 1 if direction(e) == "up" else -1
    # a move someone could have traded: the stock was trading before, moved, and had volume (as in the speed section)
    mover = lambda r: (r.get("ref_age_s") or 0) <= FRESH_REF_S and abs(r["r300"]) >= MOVER_PCT and usd_300(r) >= LIQUID_USD

    def candidate(e):
        v = e.get("verdict") or {}
        return v.get("direction") in ("up", "down") and (v.get("surprise") or 0) >= min_surprise \
            and (v.get("confidence") or 0) >= min_confidence and (v.get("impact") or 0) >= min_impact

    def group(rows):
        seen = [(e, after(e)) for e in rows if after(e)]
        moved = [(e, r) for e, r in seen if mover(r)]
        later = [(e, r) for e, r in seen if r.get("r900") is not None]
        return {
            "n": len(rows), "measured": len(seen),
            "right_5": sum(1 for e, r in seen if r["r300"] * sign(e) > 0),
            "wrong_5": sum(1 for e, r in seen if r["r300"] * sign(e) < 0),
            "flat_5": sum(1 for e, r in seen if r["r300"] == 0),
            "dir_move_5": avg(r["r300"] * sign(e) for e, r in seen),
            "measured_15": len(later),
            "right_15": sum(1 for e, r in later if r["r900"] * sign(e) > 0),
            "dir_move_15": avg(r["r900"] * sign(e) for e, r in later),
            "movers": len(moved), "movers_right": sum(1 for e, r in moved if r["r300"] * sign(e) > 0),
            "examples": [{"symbol": e.get("symbol"), "direction": direction(e), "r60": r.get("r60"), "r300": r["r300"],
                          "r900": r.get("r900"), "headline": (e.get("headline") or "")[:90]}
                         for e, r in sorted(seen, key=lambda p: -abs(p[1]["r300"]))[:8]],
        }

    directed = [e for e in rated if direction(e) in ("up", "down")]
    quiet = [e for e in rated if direction(e) == "none" and after(e)]
    cost = 0.0
    for e in rated:
        pin, pout = PRICES.get(e.get("model"), (0.0, 0.0))
        cost += ((e.get("tokens_in") or 0) * pin + (e.get("tokens_out") or 0) * pout) / 1e6
    return {
        "rated": len(rated), "limits": list(limits),
        "with_text": sum(1 for e in rated if e.get("with_text")),
        "with_expectations": sum(1 for e in rated if e.get("with_expectations")),
        "age_s_median": med(e.get("age_s") for e in rated), "seconds_median": med(e.get("model_s") for e in rated),
        "cost": round(cost, 2),
        "by_session": dict(Counter(SESSIONS.get(e.get("phase"), e.get("phase") or "?") for e in rated)),
        "directed": group(directed), "candidates": group([e for e in directed if candidate(e)]),
        "none": {"n": sum(1 for e in rated if direction(e) == "none"), "measured": len(quiet),
                 "moved_anyway": sum(1 for e in quiet if mover(after(e)))},
        "skipped": dict(Counter(e.get("reason", "?") for e in skipped).most_common()),
        "rejected": dict(Counter(e.get("reason", "?") for e in rejected).most_common()),
    }


def build(root: Path, start: date, end: date):
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    trades = apply_corrections(
        [t for t in read_jsonl(root / "logs" / "trades.jsonl")
         if start.isoformat() <= t["entry_time"][:10] <= end.isoformat() or trading_day(t["entry_time"]) == end.isoformat()],
        read_jsonl(root / CORRECTIONS),
    )
    events, daily = [], []
    for d in days:
        of_day = read_jsonl(root / "logs" / f"events-{d.isoformat()}.jsonl")
        events += of_day
        p = root / "logs" / "daily" / f"{d.isoformat()}.json"
        if p.exists():
            row = json.loads(p.read_text())
            row["equity_start"], row["pnl"] = day_result(row, of_day)
            daily.append(row)
    # Trading runs into the evening: the last hour of Friday's session and the measurement of
    # Friday's announcements are written after midnight UTC, into Saturday's file.
    later = [read_jsonl(root / "logs" / f"events-{(end + timedelta(days=i)).isoformat()}.jsonl") for i in (1, 2)]
    night = lambda e: (e.get("t") or "9")[11:13] < NIGHT_UNTIL_H
    events += [e for e in later[0] if night(e)]

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
    # a measurement that ran late (it gives up for the night when the data source refuses) still belongs to this week
    speed += [e for e in [x for x in later[0] if not night(x)] + later[1]
              if e.get("kind") == "news_speed" and e.get("t0") and trading_day(e["t0"]) <= end.isoformat()]
    shadows = [e for e in events if e["kind"] == "news_shadow"]
    fallbacks = [e for e in events if e["kind"] == "news_fallback"]
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
        "models": model_stats(classified, shadows, fallbacks, outcomes, news_limits(root)),
        "fast": {
            **fast_stats([e for e in events if e["kind"] == "fast_rated"], speed,
                         [e for e in events if e["kind"] == "fast_skipped"],
                         [e for e in events if e["kind"] == "fast_rejected"], fast_limits(root)),
            "trades_by_session": {SESSIONS.get(k, k): trade_stats([t for t in by_strategy.get("fast", []) if t.get("phase") == k])
                                  for k in sorted({t.get("phase") or "?" for t in by_strategy.get("fast", [])})},
        },
        "news_by_model": {
            m: trade_stats([t for t in news_trades if (t.get("meta", {}).get("model") or "?") == m])
            for m in sorted({t.get("meta", {}).get("model") or "?" for t in news_trades})
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
        # a position found before the open is reported again at every restart until it is sold
        "orphans": len({(e["t"][:10], e.get("symbol")) for e in events if e["kind"] == "orphan"}),
        "no_fills": sum(1 for e in events if e["kind"] == "no_fill"),
        "corrected": [t for t in trades if t.get("recorded")],
        "unconfirmed": [t for t in trades if t["exit_reason"].endswith("_unconfirmed")],
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
    ms = r["models"]
    if ms["models"]:
        lim = ms["limits"]
        out += ["", "## Modellvergleich: dieselben Meldungen, von jedem Modell bewertet",
                "(Das zuerst genannte Modell hat entschieden, die anderen liefen ohne Einfluss auf den Handel mit.)"]
        for m, v in ms["models"].items():
            out.append(
                f'- {m}: {v["ratings"]} Bewertungen, davon {v["decided"]} entscheidend, {v["failed"]} Ausfälle; Antwortzeit '
                f'{v["seconds_median"]} s (90 % unter {v["seconds_p90"]} s); {v["tokens_in"]}+{v["tokens_out"]} Token je Bewertung; '
                f'Kosten {v["cost"]} $ ({v["cents_per_rating"]} Cent je Bewertung)')
        out.append(f'- Von allen Modellen bewertet: {ms["common"]} Meldungen. Die folgenden Zahlen beziehen sich nur auf diese.')
        out.append(f'- Kandidaten (Richtung, Überraschung >= {lim[0]}, Sicherheit >= {lim[1]}, Wirkung >= {lim[2]}): '
                   + ", ".join(f'{m} {v["candidates"]}' for m, v in ms["models"].items())
                   + f'; uneinig bei {ms["differences"]} Meldungen')
        out += [f'- Kurs nach den Kandidaten von {m}: {fo(v["candidates_vs_price"])}' for m, v in ms["models"].items()]
        out.append(f'- Meldungen mit mindestens {MOVER_PCT:g} % Bewegung nach 30 min: {ms["movers"]}; davon als Kandidat mit '
                   'richtiger Richtung erkannt: ' + ", ".join(f'{m} {v["movers_caught"]}' for m, v in ms["models"].items()))
        if ms["examples"]:
            out.append("- Uneinige Fälle, größte Kursbewegung zuerst (Richtung Überraschung/Wirkung/Sicherheit):")
            for x in ms["examples"]:
                mv = "nicht gemessen" if x["ret_30"] is None else f'{x["ret_30"]:+.2f} % nach 30 min'
                out.append(f'  - {x["symbol"]} ({mv}): {x["headline"][:90]} | '
                           + " | ".join(f"{m}: {v}" for m, v in x["verdicts"].items()))
        out += [f"- Trades nach Entscheidung von {k}: {fmt(v)}" for k, v in r["news_by_model"].items()]

    fa = r["fast"]
    if fa["rated"]:
        lim = fa["limits"]

        def fg(g):
            if not g["n"]:
                return "keine"
            if not g["measured"]:
                return f'{g["n"]}, Kursverlauf noch nicht gemessen'
            late = (f'; nach 15 Minuten richtig {g["right_15"]} von {g["measured_15"]} (im Schnitt {g["dir_move_15"]:+.2f} %)'
                    if g["measured_15"] else "")
            return (f'{g["n"]}, davon {g["measured"]} mit gemessenem Kursverlauf: Richtung nach 5 Minuten richtig {g["right_5"]}, '
                    f'falsch {g["wrong_5"]}, ohne Bewegung {g["flat_5"]} (im Schnitt {g["dir_move_5"]:+.2f} % in vorhergesagter '
                    f'Richtung){late}; bei handelbaren Ausschlägen richtig {g["movers_right"]} von {g["movers"]}')
        out += ["", "## Sofort-Einstieg: Bewertungen gegen den Kursverlauf (jede Bewertung, auch ohne Trade)",
                "(Richtig heißt: Der Kurs lag danach auf der vorhergesagten Seite des Kurses vor der Meldung. Ob ein Trade sich "
                f"gelohnt hätte, sagt das nicht. Handelbarer Ausschlag: mindestens {MOVER_PCT:g} % und "
                + f"{LIQUID_USD:,} $ Umsatz in 5 Minuten.)".replace(",", "."),
                f'- bewertet: {fa["rated"]} Pressemitteilungen {fa["by_session"]}, {fa["with_text"]} mit vollem Text, '
                f'{fa["with_expectations"]} mit Erwartungen; Alter beim Eintreffen {fa["age_s_median"]} s, Antwortzeit '
                f'{fa["seconds_median"]} s, Kosten {fa["cost"]} $',
                f'- Bewertungen mit Richtung: {fg(fa["directed"])}',
                f'- Kandidaten (Überraschung >= {lim[0]}, Sicherheit >= {lim[1]}, Wirkung >= {lim[2]}): {fg(fa["candidates"])}',
                f'- ohne Richtung bewertet: {fa["none"]["n"]}, davon {fa["none"]["measured"]} gemessen; trotzdem ein '
                f'handelbarer Ausschlag bei {fa["none"]["moved_anyway"]}',
                f'- nicht bewertet (Grund): {fa["skipped"] or "keine"}',
                f'- Kandidaten ohne Einstieg (Grund): {fa["rejected"] or "keine"}']
        out += [f'- Trades {k}: {fmt(v)}' for k, v in fa["trades_by_session"].items()]
        if fa["directed"]["examples"]:
            out.append("- Bewertungen mit Richtung, größte Bewegung zuerst (Kurs nach 1 / 5 / 15 Minuten):")
            pc = lambda v: "?" if v is None else f"{v:+.2f} %"
            out += [f'  - {e["symbol"]} {DIRECTION.get(e["direction"], e["direction"])}: {pc(e["r60"])} / {pc(e["r300"])} / '
                    f'{pc(e["r900"])} | {e["headline"]}' for e in fa["directed"]["examples"]]

    def fs(o):
        thin = f' (dazu {o["thin"]} ohne Handel in den 10 Minuten davor)' if o.get("thin") else ""
        if not o["n"]:
            return "keine Daten" + thin
        if not o["movers"]:
            return f'{o["n"]} Meldungen gemessen{thin}, keine mit mindestens {MOVER_PCT:g} % Bewegung in 5 Minuten'
        def path(x):
            return (f'Bewegung nach 5 min {x["move_300"]} %; davon noch zu holen bei Einstieg nach 2 s / 10 s / 60 s / 120 s = '
                    f'{x["left_after_2s"]} / {x["left_after_10s"]} / {x["left_after_60s"]} / {x["left_after_120s"]} Prozentpunkte; '
                    f'schon vor der Veröffentlichung gelaufen {x["before_publication"]} %; Stand nach 30 min {x["after_30min"]} %; '
                    f'Umsatz in den ersten 10 s {x["usd_first_10s"]} $, in 5 min {x["usd_5min"]} $')
        liq = o["liquid"]
        liquid = (f' Davon mit mindestens {LIQUID_USD:,} $ Umsatz in 5 Minuten (nur dort passt eine Position hinein): '.replace(",", ".")
                  + (f'{liq["movers"]} Meldungen. Bei diesen: {path(liq)}' if liq["movers"] else "keine"))
        return (f'{o["n"]} Meldungen gemessen{thin}, {o["movers"]} mit mindestens {MOVER_PCT:g} % Bewegung in 5 Minuten. '
                f'Bei diesen: {path(o)}.{liquid}')

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
            f'- Eingriffe des Feierabend-Wächters (er musste vor Börsenschluss selbst glattstellen): {r["guard_interventions"]}']
    out += [f'- Ausstieg nicht bestätigt, Ergebnis geschätzt (bitte am Konto prüfen und in {CORRECTIONS} richtigstellen): '
            f'{t["symbol"]} {t["strategy"]} vom {t["entry_time"][:10]}, verbucht {t["pnl"]:+.2f} $ ({t["exit_reason"]})'
            for t in r["unconfirmed"]]
    out += [f'- Nachträglich richtiggestellt: {t["symbol"]} {t["strategy"]} vom {t["entry_time"][:10]}: vom Bot verbucht '
            f'{t["recorded"]["pnl"]:+.2f} $ ({t["recorded"]["exit_reason"]}), tatsächlich {t["pnl"]:+.2f} $ '
            f'({t["exit_reason"]}). {t["note"]}'.rstrip() for t in r["corrected"]]
    out += ["", "## Tage"]
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
