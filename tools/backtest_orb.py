#!/usr/bin/env python3
"""Rough backtest of the opening-range-breakout strategy on Alpaca's 1-minute bars.

  python tools/backtest_orb.py --days 60
  python tools/backtest_orb.py --days 60 --set orb.range_min=15 --set orb.target_r=3

It uses the same selection and stop rules as the live bot (imported from bot/), on the fixed
symbol list from config.toml. Read the result as a sanity check, not as a forecast:
- only the fixed list is tested (the live bot also adds the day's movers and news names),
- prices come from the IEX feed only, fills are assumed at the trigger price plus --slip,
- when stop and target fall into the same minute, the stop is assumed to come first.
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.common import ET, load_config, parse_ts  # noqa: E402
from bot.executor import position_size  # noqa: E402
from bot.orb_strategy import assess, stop_distance_pct  # noqa: E402

DATA = "https://data.alpaca.markets"
CACHE = Path(os.environ.get("BACKTEST_CACHE", "/tmp/orb_backtest_cache"))


def fetch(client, sym, start, end):
    """All 1-minute bars of one symbol, cached on disk."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{sym}_{start[:10]}_{end[:10]}.json"
    if f.exists():
        return json.loads(f.read_text())
    bars, tok = [], None
    while True:
        p = {"timeframe": "1Min", "start": start, "end": end, "feed": "iex", "limit": 10000}
        if tok:
            p["page_token"] = tok
        r = client.get(f"{DATA}/v2/stocks/{sym}/bars", params=p)
        if r.status_code == 429:
            time.sleep(3)
            continue
        r.raise_for_status()
        d = r.json()
        bars += d.get("bars") or []
        tok = d.get("next_page_token")
        if not tok:
            break
    f.write_text(json.dumps(bars))
    return bars


def sessions(bars):
    """day -> regular-session bars as (minute_of_day, o, h, l, c, v), New York time."""
    out = defaultdict(list)
    for b in bars:
        t = parse_ts(b["t"]).astimezone(ET)
        m = t.hour * 60 + t.minute
        if 570 <= m < 960:
            out[t.strftime("%Y-%m-%d")].append((m, b["o"], b["h"], b["l"], b["c"], b["v"]))
    return out


def prepare(data, cfg):
    """Per day: the candidates the live bot would watch, ranked by relative volume."""
    n = cfg["range_min"]
    stats = {}  # (sym, day) -> range info
    for sym, days in data.items():
        order = sorted(days)
        vols = []
        for i, day in enumerate(order):
            rb = [b for b in days[day] if b[0] < 570 + n]
            if not rb:
                continue
            rng = {"high": max(b[2] for b in rb), "low": min(b[3] for b in rb), "volume": sum(b[5] for b in rb),
                   "open": rb[0][1], "close": rb[-1][4]}
            hist = vols[-10:]
            vols.append(rng["volume"])
            if i == 0 or len(hist) < 3:
                continue
            prev_close = days[order[i - 1]][-1][4]
            gap = (rng["open"] - prev_close) / prev_close * 100
            avg = sum(hist) / len(hist)
            rvol = rng["volume"] / avg if avg > 0 else None
            mid = (rng["high"] + rng["low"]) / 2
            rng.update(gap_pct=gap, rvol=rvol, width_pct=(rng["high"] - rng["low"]) / mid * 100)
            if rng["open"] >= cfg["min_price"] and assess(rng, gap, rvol, cfg) is None:
                stats[(sym, day)] = rng
    by_day = defaultdict(list)
    for (sym, day), rng in stats.items():
        by_day[day].append((rng["rvol"], sym, rng))
    return {d: [(s, r) for _, s, r in sorted(v, reverse=True)[: cfg["max_candidates"]]] for d, v in by_day.items()}


def simulate(data, cands, cfg, risk, slip_pct, direction="both"):
    n = cfg["range_min"]
    h, m = map(int, cfg["entry_until"].split(":"))
    deadline = h * 60 + m
    equity, trades = 100_000.0, []
    for day in sorted(cands):
        start_eq, open_pos, fired, n_today = equity, [], set(), 0
        bars = {s: {b[0]: b for b in data[s][day]} for s, _ in cands[day]}
        for minute in range(570 + n, 960):
            # exits first
            for p in list(open_pos):
                b = bars[p["sym"]].get(minute)
                if not b:
                    continue
                d = p["dirn"]
                hit_stop = (b[3] <= p["stop"]) if d > 0 else (b[2] >= p["stop"])
                hit_tgt = (b[2] >= p["target"]) if d > 0 else (b[3] <= p["target"])
                exit_px = reason = None
                if hit_stop:
                    exit_px, reason = p["stop"], "stop"
                elif hit_tgt:
                    exit_px, reason = p["target"], "target"
                elif cfg["max_hold_min"] and minute - p["t"] >= cfg["max_hold_min"]:
                    exit_px, reason = b[4], "time"
                elif minute >= 950:
                    exit_px, reason = b[4], "eod"
                if exit_px is not None:
                    exit_px *= 1 - d * slip_pct / 100
                    pnl = (exit_px - p["entry"]) * p["qty"] * d
                    equity += pnl
                    trades.append({**p, "day": day, "exit": exit_px, "reason": reason, "pnl": pnl,
                                   "r": pnl / p["risk_usd"], "hold": minute - p["t"]})
                    open_pos.remove(p)
            if minute >= deadline:
                continue
            if (equity - start_eq) / start_eq * 100 <= -risk["daily_loss_limit_pct"]:
                continue
            for sym, rng in cands[day]:
                b = bars[sym].get(minute)
                if not b or sym in fired:
                    continue
                up = rng["high"] * (1 + cfg["breakout_buffer_pct"] / 100)
                dn = rng["low"] * (1 - cfg["breakout_buffer_pct"] / 100)
                side = 1 if b[2] > up else (-1 if b[3] < dn else 0)
                if b[2] > up and b[3] < dn:
                    fired.add(sym)  # both sides in one minute: unreadable, skip the symbol
                    continue
                if not side:
                    continue
                fired.add(sym)
                if direction == "gap" and (rng["gap_pct"] > 0) != (side > 0):
                    continue
                if direction == "range" and (rng["close"] > rng["open"]) != (side > 0):
                    continue
                if len(open_pos) >= risk["max_open_positions"] or n_today >= risk["max_trades_per_day"]:
                    continue
                level = up if side > 0 else dn
                entry = (max(level, b[1]) if side > 0 else min(level, b[1])) * (1 + side * slip_pct / 100)
                dist = stop_distance_pct(rng["width_pct"], cfg["stop_range_frac"], cfg["stop_min_pct"], cfg["stop_max_pct"])
                stop = entry * (1 - side * dist / 100)
                gross = sum(p["qty"] * p["entry"] for p in open_pos)
                qty = position_size(equity, entry, stop, risk["risk_per_trade_pct"], risk["max_position_leverage"],
                                    equity * risk["max_gross_leverage"] - gross, equity * 4 * 0.95 - gross)
                if qty < 1:
                    continue
                n_today += 1
                pos = {"sym": sym, "dirn": side, "entry": entry, "stop": stop, "qty": qty, "t": minute,
                       "target": entry + side * cfg["target_r"] * abs(entry - stop),
                       "risk_usd": abs(entry - stop) * qty, "with_gap": (rng["gap_pct"] > 0) == (side > 0),
                       "rvol": rng["rvol"], "notional": qty * entry}
                # the entry minute itself can already hit the stop (conservative: checked, target is not)
                if (b[3] <= stop) if side > 0 else (b[2] >= stop):
                    px = stop * (1 - side * slip_pct / 100)
                    pnl = (px - entry) * qty * side
                    equity += pnl
                    trades.append({**pos, "day": day, "exit": px, "reason": "stop", "pnl": pnl,
                                   "r": pnl / pos["risk_usd"], "hold": 0})
                else:
                    open_pos.append(pos)
    return equity, trades


def summarize(label, equity, trades, n_days):
    if not trades:
        print(f"{label:34} keine Trades")
        return
    wins = [t for t in trades if t["pnl"] > 0]
    gp, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    reasons = defaultdict(int)
    for t in trades:
        reasons[t["reason"]] += 1
    daily = defaultdict(float)
    for t in trades:
        daily[t["day"]] += t["pnl"]
    peak = dd = 0.0
    run = 0.0
    for d in sorted(daily):
        run += daily[d]
        peak = max(peak, run)
        dd = min(dd, run - peak)
    print(f"{label:34} {len(trades):4} Trades ({len(trades)/n_days:.1f}/Tag) | Treffer {len(wins)/len(trades)*100:4.1f} % | "
          f"Schnitt {sum(t['r'] for t in trades)/len(trades):+.3f} R | PF {gp/gl if gl else float('inf'):.2f} | "
          f"Ergebnis {(equity/100000-1)*100:+6.2f} % | max. Rückgang {dd/1000:+.1f} % | "
          f"Halten {sum(t['hold'] for t in trades)/len(trades):.0f} min | Position {sum(t['notional'] for t in trades)/len(trades)/1000:.0f}k | {dict(reasons)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="calendar days back")
    ap.add_argument("--slip", type=float, default=0.02, help="slippage per side in percent")
    ap.add_argument("--set", action="append", default=[], help="override, e.g. orb.range_min=15")
    ap.add_argument("--direction", default="both", choices=["both", "gap", "range"])
    ap.add_argument("--label", default="")
    a = ap.parse_args()
    cfg = load_config()
    for s in a.set:
        key, val = s.split("=", 1)
        sec, name = key.split(".")
        cfg[sec][name] = type(cfg[sec][name])(val) if not isinstance(cfg[sec][name], bool) else val == "true"
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=a.days + 20)  # extra days to warm up the volume average
    hdr = {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"]}
    data = {}
    with httpx.Client(headers=hdr, timeout=60) as c:
        for sym in cfg["orb"]["static_pool"]:
            data[sym] = sessions(fetch(c, sym, start.strftime("%Y-%m-%dT00:00:00Z"), end.strftime("%Y-%m-%dT00:00:00Z")))
    cands = prepare(data, cfg["orb"])
    first = (end - timedelta(days=a.days)).strftime("%Y-%m-%d")
    cands = {d: v for d, v in cands.items() if d >= first}
    all_days = sorted({d for s in data.values() for d in s if d >= first})
    equity, trades = simulate(data, cands, cfg["orb"], cfg["risk"], a.slip, a.direction)
    summarize(a.label or "aktuelle Einstellungen", equity, trades, len(all_days))
    return trades


if __name__ == "__main__":
    main()
