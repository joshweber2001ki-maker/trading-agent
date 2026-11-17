#!/usr/bin/env python3
"""Replay one past session of Alpaca's news feed through the news strategy.

  python tools/replay_news.py 2026-11-16

Shows how many articles the KI would have classified, how many it would have rated as
tradeable, how many the price would have confirmed, and how those trades would have ended.
It costs one KI call per classified article (same as a live day). Needs APCA_* and
ANTHROPIC_API_KEY in the environment. Rough by design: 1-minute bars from the IEX feed,
entry at the close of the confirming minute, no bid/ask check.
"""
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.alpaca import DATA, Alpaca  # noqa: E402
from bot.common import ET, iso, load_config, parse_ts  # noqa: E402
from bot.llm import Classifier, is_noise  # noqa: E402
from bot.market import StockStats, Universe  # noqa: E402
from bot.news_strategy import confirmation, outcome, trade_plan, vol_factor  # noqa: E402


async def articles(api, start, end):
    out, tok = [], None
    while True:
        p = {"limit": 50, "sort": "asc", "start": iso(start), "end": iso(end)}
        if tok:
            p["page_token"] = tok
        d = await api._req("GET", f"{DATA}/v1beta1/news", params=p)
        out += d.get("news") or []
        tok = d.get("next_page_token")
        if not tok:
            return out


def simulate(bars, t0, dirn, cfg, impact=1, factor=1.0):
    """Returns (stage, detail) where stage is 'no_price', 'unconfirmed' or 'trade'."""
    base_cfg = cfg
    cfg = {**cfg, "min_move_pct": cfg["min_move_pct"] * factor, "max_chase_pct": cfg["max_chase_pct"] * factor}
    before = [b for b in bars if parse_ts(b["t"]) + timedelta(minutes=1) <= t0]
    after = [b for b in bars if parse_ts(b["t"]) + timedelta(minutes=1) > t0]
    if len(before) < 3 or not after:
        return "no_price", None
    ref = before[-1]["c"]
    hist = before[-30:]
    avg_vol = sum(b["v"] for b in hist) / len(hist)
    last = "timeout"
    for i, b in enumerate(after):
        if parse_ts(b["t"]) >= t0 + timedelta(seconds=cfg["confirm_window_s"]):
            break
        state, last = confirmation(ref, b["c"], dirn, b["v"], avg_vol, None, cfg)
        if state == "abort":
            break
        if state != "confirmed":
            continue
        entry = b["c"]
        seen = before + after[: i + 1]
        dist, target_r, max_hold = trade_plan(impact, factor, seen, entry, base_cfg)
        stop = entry * (1 - dirn * dist / 100)
        target = entry + dirn * target_r * abs(entry - stop)
        t_in = parse_ts(b["t"])
        for nb in after[i + 1:]:
            if (nb["l"] <= stop) if dirn > 0 else (nb["h"] >= stop):
                return "trade", {"r": -1.0, "exit": "stop", "dist": dist}
            if (nb["h"] >= target) if dirn > 0 else (nb["l"] <= target):
                return "trade", {"r": target_r, "exit": "target", "dist": dist}
            if parse_ts(nb["t"]) - t_in >= timedelta(minutes=max_hold):
                return "trade", {"r": (nb["c"] - entry) * dirn / abs(entry - stop), "exit": "time", "dist": dist}
        return "trade", {"r": (after[-1]["c"] - entry) * dirn / abs(entry - stop), "exit": "end_of_data", "dist": dist}
    return "unconfirmed", last


async def main(day):
    cfg = load_config()
    n = cfg["news"]
    api = Alpaca(os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"],
                 os.environ.get("APCA_BASE_URL", "https://paper-api.alpaca.markets"))
    uni = Universe(api)
    await uni.load()
    llm = Classifier(os.environ["ANTHROPIC_API_KEY"], {**cfg["llm"], "max_calls_per_hour": 10**6, "max_calls_per_day": 10**6})
    d = datetime.strptime(day, "%Y-%m-%d")
    open_dt, close_dt = d.replace(hour=9, minute=30, tzinfo=ET), d.replace(hour=16, tzinfo=ET)
    arts = await articles(api, open_dt + timedelta(minutes=n["start_after_open_min"]),
                          close_dt - timedelta(minutes=n["no_entry_before_close_min"]))
    todo = []
    for a in arts:
        syms = a.get("symbols") or []
        if not syms or len(syms) > n["max_symbols"] or is_noise(a.get("headline", "")):
            continue
        tr = [s for s in syms if await uni.asset(s)]
        if tr:
            todo.append((a, tr))
    sem = asyncio.Semaphore(6)

    stock_stats = StockStats(api)

    async def classify(a, tr):
        async with sem:
            st = await stock_stats.get(tr)
            v, err = await llm.classify(a, tr, {s: await uni.name(s) for s in tr}, st)
            return a, v, err

    results = await asyncio.gather(*(classify(a, tr) for a, tr in todo))
    verdicts = [(a, v) for a, v, err in results if v]
    errors = Counter((err or "").split(":")[0] for _, v, err in results if not v)
    tradeable = [(a, v) for a, v in verdicts if v["direction"] != "none" and v["impact"] >= n["min_impact"]
                 and v["surprise"] >= n["min_surprise"] and v["confidence"] >= n["min_confidence"]]
    names = {0: "kaum", 1: "mittel", 2: "groß", 3: "sehr groß"}
    print("Wirkung:", {names[k]: c for k, c in sorted(Counter(v["impact"] for _, v in verdicts).items())})
    # What did the price do in the hour after each rated article? (uses the bot's own outcome logic)
    by_impact = {}
    sem2 = asyncio.Semaphore(5)

    async def follow(a, v):
        t0 = parse_ts(a["created_at"])
        async with sem2:
            bars = await api.bars(v["symbol"], "1Min", iso(t0 - timedelta(minutes=30)), iso(t0 + timedelta(minutes=61)))
        o = outcome(bars, t0, min(t0 + timedelta(minutes=60), close_dt))
        if o and o["ret_30"] is not None:
            by_impact.setdefault(v["impact"], []).append((v, o))

    await asyncio.gather(*(follow(a, v) for a, v in verdicts))
    print("Kursbewegung in den 30 Minuten nach der Meldung, nach Wirkungs-Stufe (Betrag, Median):")
    for k in sorted(by_impact):
        moves = sorted(abs(o["ret_30"]) for _, o in by_impact[k])
        swings = sorted(max(o["max_up"], -o["max_down"]) for _, o in by_impact[k])
        print(f"  {names[k]:10} n={len(moves):3}  nach 30 min {moves[len(moves)//2]:.2f} %   größter Ausschlag in 60 min {swings[len(swings)//2]:.2f} %")
    print(f"{day}: {len(arts)} Meldungen in der Handelszeit, {len(todo)} an die KI, {len(verdicts)} bewertet, Fehler {dict(errors)}")
    print("Überraschung (0-10):", dict(sorted(Counter(v["surprise"] for _, v in verdicts).items())))
    print("Richtung:", dict(Counter(v["direction"] for _, v in verdicts)),
          "| Minute der Stunde:", dict(Counter(a["created_at"][14:16] for a, _ in verdicts).most_common(4)))
    print(f"Als handelbar eingestuft (Überraschung >= {n['min_surprise']}, Wirkung >= {n['min_impact']}, Sicherheit >= {n['min_confidence']}): {len(tradeable)}")
    outcomes = Counter()
    rs = []
    for a, v in tradeable:
        t0 = parse_ts(a["created_at"])
        dirn = 1 if v["direction"] == "up" else -1
        bars = await api.bars(v["symbol"], "1Min", iso(t0 - timedelta(minutes=40)), iso(t0 + timedelta(minutes=45)))
        st = (await stock_stats.get([v["symbol"]])).get(v["symbol"])
        stage, detail = simulate(bars, t0, dirn, n, v["impact"], vol_factor(st, n))
        outcomes[stage if stage != "unconfirmed" else f"unconfirmed:{detail}"] += 1
        tail = ""
        if stage == "trade":
            rs.append(detail["r"])
            tail = f" -> {detail['r']:+.2f} R ({detail['exit']}, Stop {detail['dist']:.2f} %)"
        print(f"  {a['created_at'][11:16]} {v['symbol']:5} {v['direction']:4} Ü{v['surprise']} W{v['impact']} S{v['confidence']} "
              f"{v['category']:10} {stage if stage == 'trade' else stage + ':' + str(detail)}{tail} | {a['headline'][:70]}")
    print("Ergebnis:", dict(outcomes), "| Trades:", len(rs), "| Summe:", f"{sum(rs):+.2f} R" if rs else "-")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
