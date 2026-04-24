#!/usr/bin/env python3
"""
Guardrails — Pre-trade and portfolio safety checks.
MUST run before every order. Returns JSON with pass/fail.

Usage:
  python scripts/guardrails.py portfolio
  python scripts/guardrails.py trade BUY AAPL 10 150.00
  python scripts/guardrails.py stop_check           # check all positions for stop triggers
"""

import os
import sys
import json
import requests
from datetime import datetime, time, timezone

# ── Alpaca config (same as alpaca.py) ─────────────────────────────────────────
API_KEY    = os.environ.get("APCA_API_KEY_ID", "")
API_SECRET = os.environ.get("APCA_API_SECRET_KEY", "")
BASE_URL   = os.environ.get("APCA_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL   = "https://data.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

# ── Hard Limits (mirror what's in CLAUDE.md) ──────────────────────────────────
MAX_POSITION_PCT    = 0.10   # 10% of portfolio per position
MAX_DAILY_LOSS_PCT  = 0.03   # 3% portfolio daily loss limit
MAX_OPEN_POSITIONS  = 10
STOP_LOSS_PCT       = 0.07   # -7% stop loss
MIN_CASH_RESERVE    = 0.20   # 20% minimum cash
MAX_TRADES_PER_DAY  = 5
MIN_PRICE           = 10.0   # no penny stocks
MIN_AVG_VOLUME      = 500_000
MARKET_OPEN_ET      = time(9, 35)
MARKET_CLOSE_ET     = time(15, 45)

def _get(endpoint, base=None):
    url = (base or BASE_URL) + endpoint
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.json()

def get_account():
    return _get("/v2/account")

def get_positions():
    return _get("/v2/positions")

def get_orders_today():
    return _get("/v2/orders?status=all&limit=50&after=" +
                datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z"))

def get_quote(symbol):
    data = _get(f"/v2/stocks/{symbol}/quotes/latest", base=DATA_URL)
    q = data["quote"]
    return (q.get("ap", 0) + q.get("bp", 0)) / 2

def get_bars(symbol, limit=30):
    data = _get(f"/v2/stocks/{symbol}/bars?timeframe=1Day&limit={limit}&feed=iex", base=DATA_URL)
    return data.get("bars", [])

# ── Portfolio-level checks ────────────────────────────────────────────────────
def check_portfolio():
    checks = []
    passed = True

    account = get_account()
    portfolio_value = float(account["portfolio_value"])
    cash = float(account["cash"])
    cash_pct = cash / portfolio_value

    # 1. Cash reserve
    ok = cash_pct >= MIN_CASH_RESERVE
    if not ok: passed = False
    checks.append({
        "check": "min_cash_reserve",
        "limit": f"{MIN_CASH_RESERVE*100:.0f}%",
        "actual": f"{cash_pct*100:.1f}%",
        "passed": ok,
    })

    # 2. Max open positions
    positions = get_positions()
    n_pos = len(positions)
    ok = n_pos <= MAX_OPEN_POSITIONS
    if not ok: passed = False
    checks.append({
        "check": "max_open_positions",
        "limit": MAX_OPEN_POSITIONS,
        "actual": n_pos,
        "passed": ok,
    })

    # 3. Daily trades count
    orders_today = get_orders_today()
    trades_today = len([o for o in orders_today if o["status"] in ("filled", "partially_filled")])
    ok = trades_today < MAX_TRADES_PER_DAY
    if not ok: passed = False
    checks.append({
        "check": "max_trades_per_day",
        "limit": MAX_TRADES_PER_DAY,
        "actual": trades_today,
        "passed": ok,
    })

    # 4. Account not blocked
    ok = not account.get("trading_blocked", False) and not account.get("account_blocked", False)
    if not ok: passed = False
    checks.append({
        "check": "account_not_blocked",
        "passed": ok,
    })

    result = {
        "portfolio_check": "PASSED" if passed else "FAILED",
        "portfolio_value": portfolio_value,
        "cash": cash,
        "cash_pct": round(cash_pct * 100, 1),
        "checks": checks,
    }
    print(json.dumps(result, indent=2))
    return passed

# ── Pre-trade check ───────────────────────────────────────────────────────────
def check_trade(side, symbol, qty, limit_price):
    checks = []
    passed = True
    qty = float(qty)
    limit_price = float(limit_price)

    account = get_account()
    portfolio_value = float(account["portfolio_value"])
    cash = float(account["cash"])

    # 1. Market hours check (ET) — approximate via UTC offset -4 or -5
    now_utc = datetime.now(timezone.utc)
    # Use simple UTC-4 approximation (EDT). For prod, use pytz.
    now_et_hour = (now_utc.hour - 4) % 24
    now_et_min = now_utc.minute
    now_et_time = time(now_et_hour, now_et_min)
    ok = MARKET_OPEN_ET <= now_et_time <= MARKET_CLOSE_ET
    if not ok: passed = False
    checks.append({
        "check": "market_hours",
        "window": f"{MARKET_OPEN_ET}–{MARKET_CLOSE_ET} ET",
        "current_et_approx": f"{now_et_hour:02d}:{now_et_min:02d}",
        "passed": ok,
    })

    # 2. Min price
    ok = limit_price >= MIN_PRICE
    if not ok: passed = False
    checks.append({
        "check": "min_price",
        "limit": MIN_PRICE,
        "actual": limit_price,
        "passed": ok,
    })

    # 3. Position size check (value of trade vs portfolio)
    trade_value = qty * limit_price
    position_pct = trade_value / portfolio_value
    ok = position_pct <= MAX_POSITION_PCT
    if not ok: passed = False
    checks.append({
        "check": "max_position_size",
        "limit": f"{MAX_POSITION_PCT*100:.0f}%",
        "actual": f"{position_pct*100:.1f}%",
        "trade_value": round(trade_value, 2),
        "passed": ok,
    })

    # 4. Cash available for buys
    if side.upper() == "BUY":
        cash_after = cash - trade_value
        min_cash_needed = portfolio_value * MIN_CASH_RESERVE
        ok = cash_after >= min_cash_needed
        if not ok: passed = False
        checks.append({
            "check": "cash_reserve_after_trade",
            "min_required": round(min_cash_needed, 2),
            "cash_after": round(cash_after, 2),
            "passed": ok,
        })

    # 5. Daily trades limit
    orders_today = get_orders_today()
    trades_today = len([o for o in orders_today if o["status"] in ("filled", "partially_filled")])
    ok = trades_today < MAX_TRADES_PER_DAY
    if not ok: passed = False
    checks.append({
        "check": "max_trades_per_day",
        "limit": MAX_TRADES_PER_DAY,
        "trades_so_far": trades_today,
        "passed": ok,
    })

    # 6. Volume check (use bar data)
    try:
        bars = get_bars(symbol, 30)
        if bars:
            avg_vol = sum(b["v"] for b in bars) / len(bars)
            ok = avg_vol >= MIN_AVG_VOLUME
            if not ok: passed = False
            checks.append({
                "check": "min_avg_volume",
                "limit": MIN_AVG_VOLUME,
                "actual": round(avg_vol),
                "passed": ok,
            })
    except Exception:
        checks.append({"check": "min_avg_volume", "passed": True, "note": "Could not fetch volume data"})

    result = {
        "trade_check": "PASSED" if passed else "FAILED",
        "side": side.upper(),
        "symbol": symbol,
        "qty": qty,
        "limit_price": limit_price,
        "estimated_value": round(trade_value, 2),
        "checks": checks,
    }
    print(json.dumps(result, indent=2))
    return passed

# ── Stop-loss scanner ─────────────────────────────────────────────────────────
def check_stops():
    positions = get_positions()
    stop_triggers = []

    for p in positions:
        symbol = p["symbol"]
        entry = float(p["avg_entry_price"])
        current = float(p["current_price"])
        pnl_pct = (current - entry) / entry
        stop_level = entry * (1 - STOP_LOSS_PCT)

        triggered = current <= stop_level
        stop_triggers.append({
            "symbol": symbol,
            "entry_price": entry,
            "current_price": current,
            "pnl_pct": round(pnl_pct * 100, 2),
            "stop_level": round(stop_level, 4),
            "stop_triggered": triggered,
            "action_required": "SELL NOW" if triggered else "hold",
        })

    result = {
        "stop_check_time": datetime.now(timezone.utc).isoformat(),
        "positions_checked": len(positions),
        "stops_triggered": sum(1 for s in stop_triggers if s["stop_triggered"]),
        "positions": stop_triggers,
    }
    print(json.dumps(result, indent=2))

# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0].lower()

    try:
        if cmd == "portfolio":
            check_portfolio()
        elif cmd == "trade" and len(args) >= 5:
            check_trade(args[1], args[2].upper(), args[3], args[4])
        elif cmd == "stop_check":
            check_stops()
        else:
            print(__doc__)
            sys.exit(1)
    except requests.HTTPError as e:
        print(json.dumps({"error": str(e), "response": e.response.text}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
