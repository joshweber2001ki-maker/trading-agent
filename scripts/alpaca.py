#!/usr/bin/env python3
"""
Alpaca API helper for the Trading Agent.
All interactions with Alpaca go through this script.

Usage:
  python scripts/alpaca.py account
  python scripts/alpaca.py positions
  python scripts/alpaca.py quote AAPL
  python scripts/alpaca.py bars AAPL 20        # 20 daily bars
  python scripts/alpaca.py order BUY AAPL 10 150.00
  python scripts/alpaca.py order SELL AAPL 5 145.00
  python scripts/alpaca.py cancel <order_id>
  python scripts/alpaca.py open_orders
  python scripts/alpaca.py market_status
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone, timedelta

# ── Config ──────────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("APCA_API_KEY_ID", "")
API_SECRET = os.environ.get("APCA_API_SECRET_KEY", "")
BASE_URL   = os.environ.get("APCA_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL   = "https://data.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json"
}

def _get(endpoint, base=None):
    url = (base or BASE_URL) + endpoint
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.json()

def _post(endpoint, payload):
    url = BASE_URL + endpoint
    r = requests.post(url, headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()

def _delete(endpoint):
    url = BASE_URL + endpoint
    r = requests.delete(url, headers=HEADERS)
    r.raise_for_status()
    return r.status_code

# ── Account ──────────────────────────────────────────────────────────────────
def get_account():
    data = _get("/v2/account")
    print(json.dumps({
        "portfolio_value": float(data["portfolio_value"]),
        "cash": float(data["cash"]),
        "buying_power": float(data["buying_power"]),
        "equity": float(data["equity"]),
        "daytrade_count": data.get("daytrade_count", 0),
        "pattern_day_trader": data.get("pattern_day_trader", False),
        "trading_blocked": data.get("trading_blocked", False),
        "account_blocked": data.get("account_blocked", False),
    }, indent=2))

# ── Positions ────────────────────────────────────────────────────────────────
def get_positions():
    data = _get("/v2/positions")
    positions = []
    for p in data:
        positions.append({
            "symbol": p["symbol"],
            "qty": float(p["qty"]),
            "avg_entry_price": float(p["avg_entry_price"]),
            "current_price": float(p["current_price"]),
            "market_value": float(p["market_value"]),
            "unrealized_pl": float(p["unrealized_pl"]),
            "unrealized_plpc": round(float(p["unrealized_plpc"]) * 100, 2),
            "side": p["side"],
        })
    print(json.dumps(positions, indent=2))

# ── Quote ────────────────────────────────────────────────────────────────────
def get_quote(symbol):
    data = _get(f"/v2/stocks/{symbol}/quotes/latest", base=DATA_URL)
    q = data["quote"]
    print(json.dumps({
        "symbol": symbol,
        "ask": q.get("ap"),
        "bid": q.get("bp"),
        "mid": round((q.get("ap", 0) + q.get("bp", 0)) / 2, 4),
        "timestamp": q.get("t"),
    }, indent=2))

# ── Bars (OHLCV) ──────────────────────────────────────────────────────────────
def get_bars(symbol, limit=50):
    start = (datetime.now(timezone.utc) - timedelta(days=limit * 2)).strftime("%Y-%m-%d")
    feed = "sip" if symbol in ("SPY", "QQQ", "IWM", "DIA") else "iex"
    params = f"?timeframe=1Day&limit={limit}&feed={feed}&start={start}"
    data = _get(f"/v2/stocks/{symbol}/bars{params}", base=DATA_URL)
    bars = []
    for b in (data.get("bars") or []):
        bars.append({
            "date": b["t"][:10],
            "open": b["o"],
            "high": b["h"],
            "low": b["l"],
            "close": b["c"],
            "volume": b["v"],
        })
    # Add simple SMAs
    closes = [b["close"] for b in bars]
    if len(closes) >= 20:
        bars[-1]["sma20"] = round(sum(closes[-20:]) / 20, 4)
    if len(closes) >= 50:
        bars[-1]["sma50"] = round(sum(closes[-50:]) / 50, 4)
    print(json.dumps({"symbol": symbol, "bars": bars[-10:]}, indent=2))  # print last 10

# ── Orders ────────────────────────────────────────────────────────────────────
def place_order(side, symbol, qty, limit_price):
    """Place a limit order. side = BUY or SELL"""
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side.lower(),
        "type": "limit",
        "time_in_force": "day",
        "limit_price": str(limit_price),
    }
    data = _post("/v2/orders", payload)
    print(json.dumps({
        "order_id": data["id"],
        "symbol": data["symbol"],
        "side": data["side"],
        "qty": data["qty"],
        "limit_price": data.get("limit_price"),
        "status": data["status"],
        "created_at": data["created_at"],
    }, indent=2))

def cancel_order(order_id):
    status = _delete(f"/v2/orders/{order_id}")
    print(json.dumps({"cancelled": True, "order_id": order_id, "http_status": status}))

def get_open_orders():
    data = _get("/v2/orders?status=open")
    orders = []
    for o in data:
        orders.append({
            "order_id": o["id"],
            "symbol": o["symbol"],
            "side": o["side"],
            "qty": o["qty"],
            "filled_qty": o["filled_qty"],
            "limit_price": o.get("limit_price"),
            "status": o["status"],
            "created_at": o["created_at"],
        })
    print(json.dumps(orders, indent=2))

# ── Market Status ─────────────────────────────────────────────────────────────
def market_status():
    data = _get("/v2/clock")
    print(json.dumps({
        "is_open": data["is_open"],
        "next_open": data["next_open"],
        "next_close": data["next_close"],
        "timestamp": data["timestamp"],
    }, indent=2))

# ── CLI dispatcher ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print(json.dumps({"error": "Missing APCA_API_KEY_ID or APCA_API_SECRET_KEY env vars"}))
        sys.exit(1)

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0].lower()

    try:
        if cmd == "account":
            get_account()
        elif cmd == "positions":
            get_positions()
        elif cmd == "quote" and len(args) >= 2:
            get_quote(args[1].upper())
        elif cmd == "bars" and len(args) >= 2:
            limit = int(args[2]) if len(args) >= 3 else 50
            get_bars(args[1].upper(), limit)
        elif cmd == "order" and len(args) >= 5:
            place_order(args[1], args[2].upper(), args[3], args[4])
        elif cmd == "cancel" and len(args) >= 2:
            cancel_order(args[1])
        elif cmd == "open_orders":
            get_open_orders()
        elif cmd == "market_status":
            market_status()
        else:
            print(__doc__)
            sys.exit(1)
    except requests.HTTPError as e:
        print(json.dumps({"error": str(e), "response": e.response.text}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
