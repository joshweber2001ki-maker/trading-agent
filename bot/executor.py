"""Order execution, position sizing, risk limits and trade lifecycle.

Two kinds of trades. In the regular session the entry carries its stop and target as orders at
the broker ("bracket"). Before the open and after the close the broker accepts nothing but plain
limit orders, so there the bot keeps the take-profit as a resting limit order and watches the
stop, the time limit and the end of the session itself ("soft"), closing with limit orders
through the market.
"""
import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from .alpaca import AlpacaError
from .common import DATA, iso, log, now, parse_ts

LIVE = ("new", "accepted", "held", "pending_new", "partially_filled", "accepted_for_bidding")
DEAD = ("canceled", "expired", "rejected", "done_for_day", "replaced")


def position_size(equity, price, stop, risk_pct, max_pos_lev, gross_room, buying_power, max_notional=None):
    """Shares so that a move to the stop loses risk_pct of equity, within leverage limits
    and, if given, a fixed upper limit in dollars."""
    dist = abs(price - stop)
    if dist <= 0 or price <= 0:
        return 0
    by_risk = equity * risk_pct / 100 / dist
    room = min(equity * max_pos_lev, gross_room, buying_power)
    if max_notional:
        room = min(room, max_notional)
    return max(0, int(min(by_risk, room / price)))


def is_live(order: dict) -> bool:
    """True while the broker may still fill the order (or has not yet confirmed a cancel)."""
    return order["status"] in LIVE or order["status"].startswith("pending")


def read_quote(snap, at=None) -> dict:
    """Last trade and best bid and offer from a snapshot, each with its age in seconds (None = unknown)."""
    at = at or now()
    out = {"last": 0.0, "last_age_s": None, "bid": 0.0, "ask": 0.0, "quote_age_s": None}
    trade, quote = (snap or {}).get("latestTrade") or {}, (snap or {}).get("latestQuote") or {}
    if trade.get("p"):
        out["last"] = float(trade["p"])
        if trade.get("t"):
            out["last_age_s"] = max(0.0, (at - parse_ts(trade["t"])).total_seconds())
    bid, ask = float(quote.get("bp") or 0), float(quote.get("ap") or 0)
    if bid > 0 and ask >= bid:
        out["bid"], out["ask"] = bid, ask
        if quote.get("t"):
            out["quote_age_s"] = max(0.0, (at - parse_ts(quote["t"])).total_seconds())
    return out


def spread_pct(q: dict):
    """Distance between bid and offer in percent of their middle, or None without both."""
    if q["bid"] <= 0 or q["ask"] < q["bid"]:
        return None
    return (q["ask"] - q["bid"]) / ((q["ask"] + q["bid"]) / 2) * 100


def mark_price(q: dict, trade_fresh_s: float, max_spread_pct: float):
    """The price to judge a position by, or None if the bot sees none it can rely on.

    A trade of the last seconds counts first. Otherwise the middle of bid and offer if they are
    close together: a quote stays valid until it changes, however old it is, but a wide one
    says little about where the stock trades.
    """
    if q["last"] and q["last_age_s"] is not None and q["last_age_s"] <= trade_fresh_s:
        return q["last"]
    sp = spread_pct(q)
    if sp is not None and sp <= max_spread_pct:
        return (q["bid"] + q["ask"]) / 2
    return None


def exit_reference(pos, q: dict, side: str, fallback: float = 0.0) -> float:
    """The price a closing order starts from. First the broker's own price for the position: it
    follows the whole market at every hour. The free price feed comes second - outside regular
    hours its quote can be hours old and far from the market, and a limit set from it might
    never fill, least of all when the price has just collapsed."""
    return float((pos or {}).get("current_price") or 0) \
        or (q["bid"] if side == "long" else q["ask"]) or q["last"] or fallback


def exit_limit(ref: float, side: str, attempt: int, ext: dict):
    """Limit price for closing a position outside regular hours: through the market, and further
    through it with every attempt. side is the side of the position ("long" is closed by selling)."""
    if not ref:
        return None
    off = min(ext["exit_limit_offset_pct"] + ext["exit_offset_step_pct"] * attempt, ext["exit_offset_max_pct"])
    return round(ref * (1 - (1 if side == "long" else -1) * off / 100), 2)


async def close_with_limit(api, symbol, ext: dict, journal=None, tries=4) -> bool:
    """Close whatever position there is in `symbol` with limit orders that are valid outside
    regular hours. Used for positions nobody is managing (leftovers, the guard). True once flat."""
    for attempt in range(tries):
        pos = await api.position(symbol)
        if pos is None:
            return True
        qty = abs(int(float(pos["qty"])))
        side = pos.get("side") or ("short" if float(pos["qty"]) < 0 else "long")
        ref = exit_reference(pos, read_quote(await api.snapshot(symbol)), side)
        limit = exit_limit(ref, side, attempt, ext)
        if not limit or qty < 1:
            return False
        o = await api.submit({
            "symbol": symbol, "qty": str(qty), "side": "sell" if side == "long" else "buy", "type": "limit",
            "limit_price": str(limit), "time_in_force": "day", "extended_hours": True,
        })
        if journal:
            journal.event("exit_order", symbol=symbol, reason="unmanaged", qty=qty, limit=limit, ref=ref, attempt=attempt + 1)
        for _ in range(max(1, int(ext["exit_retry_s"] / 0.5))):
            await asyncio.sleep(0.5)
            o = await api.order(o["id"])
            if not is_live(o):
                break
        if is_live(o):
            await api.cancel(o["id"])
            await asyncio.sleep(1)
    return await api.position(symbol) is None


@dataclass
class Trade:
    id: str
    strategy: str
    symbol: str
    side: str            # "long" | "short"
    qty: int
    signal_price: float
    stop_price: float
    target_price: float
    order_id: str
    submitted_at: str
    max_hold_min: float = 0
    status: str = "pending"   # pending -> open (-> closing, for trades the bot closes itself)
    entry_price: float = 0
    entry_time: str = ""
    meta: dict = field(default_factory=dict)
    style: str = "bracket"    # "bracket": stop and target are orders at the broker; "soft": the bot watches them
    phase: str = ""           # session the trade was entered in: pre, regular, post
    entry_timeout_s: float = 0
    exit_request: str = ""    # somebody asked for the trade to be closed (reason), e.g. the second opinion
    # soft trades only:
    target_order_id: str = ""
    exit_reason: str = ""     # set once the bot has decided to close
    exit_order_id: str = ""   # the closing order that is working right now
    exit_orders: list = field(default_factory=list)
    exit_tries: int = 0
    last_price_at: str = ""   # when the price of the position last changed (or the feed last showed the market)
    last_mark: float = 0      # the broker's price for the position at the last check
    price_changes: int = 0    # how often that price changed while the position was open
    missing: int = 0          # checks in a row at which the broker showed no position
    # Entries made without a live price feed: the limit starts from a price before the news, and
    # stop and target are set from the price the order was really filled at.
    stop_pct: float = 0       # distance of the stop from the fill, in percent
    target_pct: float = 0     # distance of the target from the fill, in percent
    ref_price: float = 0      # price before the news
    max_adverse_pct: float = 0  # filled this far on the wrong side of ref_price: close at once
    ticks: int = 0            # price checks while the position was open ...
    blind_ticks: int = 0      # ... and how many of them found no usable price

    @property
    def dirn(self):
        return 1 if self.side == "long" else -1


class Executor:
    def __init__(self, api, cfg, journal, session, universe, root=None):
        self.api, self.journal, self.session, self.universe = api, journal, session, universe
        self.risk, self.ex = cfg["risk"], cfg["execution"]
        self.ext = cfg.get("extended") or {}     # trading before the open and after the close
        self.cooldown_min = cfg.get("news", {}).get("symbol_cooldown_min", 60)
        self.root = root or DATA
        self.state_file = self.root / "state" / "open_trades.json"
        self.trades = {}
        self.day = None
        self.trades_today = 0
        self.done_today = set()   # (strategy, symbol) that already traded today
        self.cooldown = {}        # symbol -> datetime until which news entries are blocked
        self.halt_logged = False
        self._acct = (0, None)

    # ── state ────────────────────────────────────────────────────────────────
    def save(self):
        self.state_file.parent.mkdir(exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "day": self.day, "trades_today": self.trades_today,
            "done_today": sorted(map(list, self.done_today)),
            "trades": [asdict(t) for t in self.trades.values()],
        }, indent=1))
        tmp.replace(self.state_file)

    def load(self):
        """Restore open trades and the day's counters after a restart."""
        if not self.state_file.exists():
            return
        try:
            d = json.loads(self.state_file.read_text())
            for t in d.get("trades", []):
                self.trades[t["id"]] = Trade(**t)
            self.day = d.get("day")
            self.trades_today = d.get("trades_today", 0)
            self.done_today = {tuple(x) for x in d.get("done_today", [])}
        except (ValueError, TypeError, AttributeError) as e:
            log.warning("state file unreadable: %s", e)

    def start_day(self, day):
        """Reset the per-day limits, unless this is a restart on the same day."""
        if day == self.day:
            return
        self.day = day
        self.trades_today = 0
        self.done_today.clear()
        self.cooldown.clear()
        self.halt_logged = False
        self.save()

    async def account(self, max_age=20):
        t, a = self._acct
        if a is None or time.monotonic() - t > max_age:
            a = await self.api.account()
            self._acct = (time.monotonic(), a)
        return a

    def gross(self):
        return sum(t.qty * t.signal_price for t in self.trades.values())

    def flatten_due(self):
        m = self.session.to_close_min()
        return m is not None and m <= self.ex["flatten_before_close_min"]

    def ext_enabled(self):
        return bool(self.ext.get("enabled"))

    def ext_flatten_due(self, extra_min=0):
        """True in the last minutes of the pre-market or after-hours session."""
        phase = self.session.phase()
        if phase not in ("pre", "post"):
            return False
        left = (self.session.phase_end(phase) - now()).total_seconds() / 60
        return left <= self.ext[f"flatten_before_end_min_{phase}"] + extra_min

    def has_soft(self):
        return any(t.style == "soft" for t in self.trades.values())

    async def quote(self, symbol) -> dict:
        """What the bot can see of the current price (see read_quote)."""
        return read_quote(await self.api.live_snapshot(symbol))

    def mark(self, q: dict):
        return mark_price(q, self.ext["trade_fresh_s"], self.ext["max_mark_spread_pct"])

    def request_exit(self, symbol, reason) -> bool:
        """Ask for the trade in this symbol to be closed at the next check. True if there is one."""
        for t in self.trades.values():
            if t.symbol == symbol and not t.exit_reason and not t.exit_request:
                t.exit_request = reason
                self.save()
                return True
        return False

    # ── entry ────────────────────────────────────────────────────────────────
    async def can_enter(self, strategy, symbol, extended=False, max_open=None):
        """extended: this strategy may also enter before the open and after the close.
        max_open: at most this many trades of the strategy at a time."""
        if (self.root / "HALT").exists():
            return False, "halt_file"
        phase = self.session.phase()
        if phase == "regular":
            if self.flatten_due():
                return False, "session"
        elif phase in ("pre", "post") and extended and self.ext_enabled():
            if self.ext_flatten_due(self.ext["no_entry_before_flatten_min"]):
                return False, "session"
        else:
            return False, "session"
        if any(t.symbol == symbol for t in self.trades.values()):
            return False, "already_in_symbol"
        if max_open and sum(1 for t in self.trades.values() if t.strategy == strategy) >= max_open:
            return False, "max_open_strategy"
        if (strategy, symbol) in self.done_today:
            return False, "already_traded_today"
        if strategy == "news" and self.cooldown.get(symbol, now()) > now():
            return False, "cooldown"
        if len(self.trades) >= self.risk["max_open_positions"]:
            return False, "max_open_positions"
        if self.trades_today >= self.risk["max_trades_per_day"]:
            return False, "max_trades_per_day"
        a = await self.account()
        eq, last = float(a["equity"]), float(a["last_equity"])
        if last > 0 and (eq - last) / last * 100 <= -self.risk["daily_loss_limit_pct"]:
            if not self.halt_logged:
                self.journal.event("halt", reason="daily_loss_limit", equity=eq, last_equity=last)
                self.halt_logged = True
            return False, "daily_loss_limit"
        if a.get("trading_blocked") or a.get("account_blocked"):
            return False, "account_blocked"
        return True, ""

    async def enter(self, sig: dict) -> bool:
        """sig: strategy, symbol, side, price, stop, target_r, max_hold_min, meta.

        Optional: extended (may be entered before the open and after the close), risk_pct and
        max_notional (smaller size than the general limits), max_open (trades of this strategy at
        a time), limit_offset_pct and entry_timeout_s (how far through the price the entry limit
        sits and how long it may wait).

        Without a live price (before the open, after the close, free data): give limit_price,
        stop_pct and target_pct instead of stop and target_r. price is then the last price known
        from before the news; the order is filled at the real market or not at all, and stop and
        target are set from the fill. ref_price and max_adverse_pct: if the fill shows that the
        market has already run that far against the trade, it is closed at once.
        """
        strategy, symbol, side = sig["strategy"], sig["symbol"], sig["side"]
        dirn = 1 if side == "long" else -1
        price = float(sig["price"])
        from_fill = bool(sig.get("stop_pct"))
        # Size and risk are worked out for the worst case: filled exactly at the limit.
        basis = float(sig.get("limit_price") or price) if from_fill else price
        stop = basis * (1 - dirn * sig["stop_pct"] / 100) if from_fill else float(sig["stop"])
        base = dict(strategy=strategy, symbol=symbol, side=side, price=price)
        phase = self.session.phase()
        soft = phase in ("pre", "post")      # outside regular hours the broker takes no stop and no bracket

        def reject(reason, **kw):
            self.journal.event("rejected", stage="risk", reason=reason, **base, **kw)
            return False

        ok, why = await self.can_enter(strategy, symbol, sig.get("extended", False), sig.get("max_open"))
        if not ok:
            return reject(why)
        if price < self.risk["min_price"]:
            return reject("min_price")
        if side == "short" and not await self.universe.shortable(symbol):
            return reject("not_shortable")
        a = await self.account(max_age=5)
        equity = float(a["equity"])
        # A short is filled at the limit or above it: count its size from the higher price.
        size_price = max(basis, price) if from_fill else price
        size_stop = size_price * (1 - dirn * sig["stop_pct"] / 100) if from_fill else stop
        qty = position_size(
            equity, size_price, size_stop, sig.get("risk_pct") or self.risk["risk_per_trade_pct"],
            self.risk["max_position_leverage"],
            equity * self.risk["max_gross_leverage"] - self.gross(),
            float(a["buying_power"]) * 0.95, sig.get("max_notional"),
        )
        if qty < 1:
            return reject("size_zero", equity=equity, stop=stop)

        dist = abs(price - stop)
        offset = sig.get("limit_offset_pct", self.ex["entry_limit_offset_pct"])
        if sig.get("limit_price"):
            limit = round(float(sig["limit_price"]), 2)
        else:
            limit = round(price * (1 + dirn * offset / 100), 2)
        if from_fill:
            target = round(basis * (1 + dirn * sig["target_pct"] / 100), 2)   # provisional, reset at the fill
        else:
            target = round(price + dirn * sig["target_r"] * dist, 2)
        stop = round(stop, 2)
        tid = f"{strategy}-{symbol}-{uuid.uuid4().hex[:8]}"
        payload = {
            "symbol": symbol, "qty": str(qty), "side": "buy" if side == "long" else "sell",
            "type": "limit", "limit_price": str(limit), "time_in_force": "day", "client_order_id": tid,
        }
        if soft:
            payload["extended_hours"] = True
        else:
            payload.update(order_class="bracket", take_profit={"limit_price": str(target)},
                           stop_loss={"stop_price": str(stop)})
        try:
            o = await self.api.submit(payload)
        except AlpacaError as e:
            self.journal.event("error", where="submit", error=str(e), **base)
            return False
        t = Trade(
            id=tid, strategy=strategy, symbol=symbol, side=side, qty=qty, signal_price=price,
            # the broker's own time stamp where there is one: later it separates this trade's orders from earlier ones
            stop_price=stop, target_price=target, order_id=o["id"],
            submitted_at=o.get("submitted_at") or o.get("created_at") or iso(now()),
            max_hold_min=sig.get("max_hold_min") or 0, meta=sig.get("meta") or {},
            style="soft" if soft else "bracket", phase=phase or "",
            entry_timeout_s=sig.get("entry_timeout_s") or 0,
            stop_pct=sig.get("stop_pct") or 0, target_pct=sig.get("target_pct") or 0,
            ref_price=sig.get("ref_price") or 0, max_adverse_pct=sig.get("max_adverse_pct") or 0,
        )
        self.trades[tid] = t
        self.trades_today += 1
        self.done_today.add((strategy, symbol))
        self.save()
        self.journal.event(
            "trade_submitted", id=tid, qty=qty, limit=limit, stop=stop, target=target,
            notional=round(qty * price, 2), equity=equity, style=t.style, phase=t.phase, **base,
        )
        return True

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def manage(self):
        for t in list(self.trades.values()):
            try:
                await self._manage_one(t)
            except Exception as e:  # keep the loop alive; one bad trade must not stop the rest
                self.journal.event("error", where="manage", id=t.id, error=repr(e)[:300])

    async def _manage_one(self, t: Trade):
        if t.style == "soft":
            return await self._manage_soft(t)
        o = await self.api.order(t.order_id)
        filled = float(o.get("filled_qty") or 0)
        legs = o.get("legs") or []

        if t.status == "pending":
            if o["status"] == "filled" or (o["status"] in DEAD and filled > 0):
                t.status, t.qty = "open", int(filled)
                t.entry_price = float(o["filled_avg_price"])
                t.entry_time = iso(parse_ts(o["filled_at"])) if o.get("filled_at") else iso(now())
                self.save()
                self.journal.event(
                    "trade_open", id=t.id, symbol=t.symbol, side=t.side, qty=t.qty,
                    entry=t.entry_price, partial=o["status"] != "filled",
                )
            elif o["status"] in DEAD:
                self.journal.event("no_fill", id=t.id, symbol=t.symbol, status=o["status"])
                self._drop(t, count=False)
                return
            else:
                age = (now() - parse_ts(t.submitted_at)).total_seconds()
                if age > (t.entry_timeout_s or self.ex["entry_timeout_s"]) or t.exit_request:
                    await self.api.cancel(t.order_id)
                return

        for leg in legs:
            if leg["status"] == "filled":
                reason = "target" if leg["type"] == "limit" else "stop"
                await self._record(t, float(leg["filled_avg_price"]), reason, leg.get("filled_at"))
                return

        unprotected = not any(l["status"] in LIVE for l in legs)
        if unprotected and await self.api.position(t.symbol) is None:
            # The legs are gone and so is the position: somebody closed it outside the bot
            # (by hand in the broker's dashboard, which also cancels the legs).
            price, when = await self._external_exit(t)
            await self._record(t, price, "external" if when else "external_unconfirmed", when)
            return

        held_min = (now() - parse_ts(t.entry_time)).total_seconds() / 60
        if self.flatten_due():
            await self.exit_now(t, "eod", legs)
        elif t.max_hold_min and held_min >= t.max_hold_min:
            await self.exit_now(t, "time", legs)
        elif t.exit_request:
            await self.exit_now(t, t.exit_request, legs)
        elif unprotected:
            # No protective orders at the broker (e.g. partial fill then cancel): guard in software.
            snap = await self.api.snapshot(t.symbol)
            p = float(snap["latestTrade"]["p"]) if snap and snap.get("latestTrade") else 0
            if p and (p - t.stop_price) * t.dirn <= 0:
                await self.exit_now(t, "stop_sw", legs)
            elif p and (p - t.target_price) * t.dirn >= 0:
                await self.exit_now(t, "target_sw", legs)

    # ── trades outside regular hours ("soft") ────────────────────────────────
    async def _manage_soft(self, t: Trade):
        if t.status == "pending":
            o = await self.api.order(t.order_id)
            filled = float(o.get("filled_qty") or 0)
            if o["status"] == "filled" or (o["status"] in DEAD and filled > 0):
                t.status, t.qty = "open", int(filled)
                t.entry_price = float(o["filled_avg_price"])
                t.entry_time = iso(parse_ts(o["filled_at"])) if o.get("filled_at") else iso(now())
                t.last_price_at = iso(now())
                if t.stop_pct:
                    t.stop_price = round(t.entry_price * (1 - t.dirn * t.stop_pct / 100), 2)
                    t.target_price = round(t.entry_price * (1 + t.dirn * t.target_pct / 100), 2)
                moved = (t.entry_price - t.ref_price) / t.ref_price * 100 * t.dirn if t.ref_price else 0.0
                self.save()
                self.journal.event(
                    "trade_open", id=t.id, symbol=t.symbol, side=t.side, qty=t.qty,
                    entry=t.entry_price, partial=o["status"] != "filled", style=t.style,
                    stop=t.stop_price, target=t.target_price, moved_from_ref_pct=round(moved, 3),
                )
                if t.max_adverse_pct and moved < -t.max_adverse_pct:
                    # The fill is the first real price the bot sees: the market had already run
                    # against the trade before it got in. Out again, without waiting for the stop.
                    t.exit_reason, t.status = "moved_against", "closing"
                    self.save()
                    self.journal.event("trade_closing", id=t.id, symbol=t.symbol, reason=t.exit_reason)
                else:
                    await self._place_target(t)
            elif o["status"] in DEAD:
                self.journal.event("no_fill", id=t.id, symbol=t.symbol, status=o["status"])
                self._drop(t, count=False)
                return
            else:
                age = (now() - parse_ts(t.submitted_at)).total_seconds()
                if age > (t.entry_timeout_s or self.ex["entry_timeout_s"]) or t.exit_request:
                    await self.api.cancel(t.order_id)
                return
        if not t.exit_reason:
            reason = await self._soft_exit_reason(t)
            if not reason:
                return
            t.exit_reason, t.status = reason, "closing"
            self.save()
            self.journal.event("trade_closing", id=t.id, symbol=t.symbol, reason=reason)
        await self._close_soft(t)

    async def _place_target(self, t: Trade):
        """Leave the take-profit at the broker as a resting limit order."""
        try:
            o = await self.api.submit({
                "symbol": t.symbol, "qty": str(t.qty), "side": "sell" if t.side == "long" else "buy",
                "type": "limit", "limit_price": str(t.target_price), "time_in_force": "day",
                "extended_hours": True, "client_order_id": f"{t.id}-tp",
            })
            t.target_order_id = o["id"]
            self.save()
        except AlpacaError as e:   # the target is then watched like the stop
            self.journal.event("error", where="target_order", id=t.id, error=str(e)[:300])

    async def _soft_exit_reason(self, t: Trade):
        """Why an open soft trade has to be closed now, or None."""
        if t.target_order_id and (await self.api.order(t.target_order_id))["status"] == "filled":
            return "target"
        held_min = (now() - parse_ts(t.entry_time)).total_seconds() / 60
        pos = await self.api.position(t.symbol)
        if pos is None:
            # Gone without us: closed by hand, or the target filled this instant. One empty answer
            # right after the entry is not enough to give the position up.
            t.missing += 1
            return "external" if t.missing >= 2 and held_min * 60 >= 3 else None
        t.missing = 0
        if self.ext_flatten_due() or (self.session.phase() == "regular" and self.flatten_due()):
            return "session_end"
        if t.max_hold_min and held_min >= t.max_hold_min:
            return "time"
        if t.exit_request:
            return t.exit_request
        # The price to judge the position by is the broker's own valuation of it: it follows the
        # whole market also before the open and after the close, where the free price feed shows
        # next to nothing (measured on 19.11.2026).
        price = float(pos.get("current_price") or 0)
        t.ticks += 1
        if price and price != t.last_mark:
            t.last_mark, t.last_price_at = price, iso(now())
            t.price_changes += 1
        quiet_s = (now() - parse_ts(t.last_price_at or t.entry_time)).total_seconds()
        if not price or quiet_s >= self.ext["blind_exit_s"]:
            # No price, or one that has not moved for a long while: is it the market that is quiet,
            # or does the bot not see it? Ask the price feed as a second opinion.
            seen = self.mark(await self.quote(t.symbol))
            if seen is None:
                # A stop that sees nothing protects nothing: close the position.
                t.blind_ticks += 1
                if quiet_s >= self.ext["blind_exit_s"]:
                    return "blind"
                return None
            price, t.last_price_at = seen, iso(now())
        if (price - t.stop_price) * t.dirn <= 0:
            return "stop"
        if (price - t.target_price) * t.dirn >= 0:
            return "target_sw"     # the resting order should have taken it; close by hand then
        return None

    async def _released(self, order_id) -> bool:
        """Cancel an order if it is still working and wait until the broker has let go of the shares."""
        o = await self.api.order(order_id)
        if not is_live(o):
            return True
        await self.api.cancel(order_id)
        for _ in range(6):
            await asyncio.sleep(0.5)
            if not is_live(await self.api.order(order_id)):
                return True
        return False

    async def _close_soft(self, t: Trade):
        """One attempt to close what is left of the position. Called on every check until it is flat."""
        for oid in (t.target_order_id, t.exit_order_id):
            if oid and not await self._released(oid):
                return                      # still held by that order: again at the next check
        pos = await self.api.position(t.symbol)
        left = abs(int(float(pos["qty"]))) if pos else 0
        if left < 1:
            return await self._record_soft(t)
        if self.session.phase() == "regular" and t.exit_tries >= 2:
            # Still not out when the regular session has started: a market order works again.
            co = await self.api.close_position(t.symbol)
            if co:
                t.exit_orders.append(co["id"])
                self.save()
            return
        ref = exit_reference(pos, await self.quote(t.symbol), t.side, t.entry_price)
        limit = exit_limit(ref, t.side, t.exit_tries, self.ext)
        try:
            o = await self.api.submit({
                "symbol": t.symbol, "qty": str(left), "side": "sell" if t.side == "long" else "buy",
                "type": "limit", "limit_price": str(limit), "time_in_force": "day",
                "extended_hours": True, "client_order_id": f"{t.id}-x{t.exit_tries + 1}",
            })
        except AlpacaError as e:
            self.journal.event("error", where="exit_order", id=t.id, error=str(e)[:300])
            return
        t.exit_order_id, t.exit_tries = o["id"], t.exit_tries + 1
        t.exit_orders.append(o["id"])
        self.save()
        self.journal.event("exit_order", id=t.id, symbol=t.symbol, reason=t.exit_reason, qty=left,
                           limit=limit, ref=ref, attempt=t.exit_tries)
        for _ in range(max(1, int(self.ext["exit_retry_s"] / 0.5))):
            await asyncio.sleep(0.5)
            o = await self.api.order(t.exit_order_id)
            if o["status"] == "filled":
                return await self._record_soft(t)
            if not is_live(o):
                break
        # Not filled: the next check cancels this order and tries again further through the market.

    async def _record_soft(self, t: Trade):
        """The position is gone: work out the exit price from every order that closed it and book the trade."""
        closing = "sell" if t.side == "long" else "buy"
        own = [oid for oid in (t.target_order_id, *t.exit_orders) if oid]
        fills = {}                          # order id -> (shares, price, time)

        def note(o):
            filled = float(o.get("filled_qty") or 0)
            if filled > 0 and o.get("filled_avg_price"):
                fills[o["id"]] = (filled, float(o["filled_avg_price"]), o.get("filled_at") or o.get("updated_at"))
        try:
            for oid in own:                 # the bot's own orders, asked one by one: always up to date
                note(await self.api.order(oid))
            for o in await self.api.closed_orders(t.symbol, t.submitted_at):
                if o.get("id") not in fills and o.get("side") == closing and o.get("id") != t.order_id:
                    note(o)                 # an order the bot did not send
        except Exception as e:
            self.journal.event("error", where="soft_exit_price", id=t.id, error=repr(e)[:200])
        qty = sum(f for f, _, _ in fills.values())
        reason, when = t.exit_reason or "external", None
        if qty <= 0:
            q = await self.quote(t.symbol)
            price = q["last"] or t.entry_price
            reason += "_unconfirmed"
        else:
            price = round(sum(f * p for f, p, _ in fills.values()) / qty, 4)
            stamps = [w for _, _, w in fills.values() if w]
            when = max(stamps, key=parse_ts) if stamps else None
            if set(fills) - set(own):
                reason = "external"         # an order the bot did not send closed (part of) it
            elif set(fills) == {t.target_order_id}:
                reason = "target"           # the resting take-profit did it, whatever the bot had decided meanwhile
        await self._record(t, price, reason, when)

    async def _external_exit(self, t: Trade):
        """Price and time of the order that closed this position outside the bot, if it can be found."""
        closing_side = "sell" if t.side == "long" else "buy"
        try:
            for o in await self.api.closed_orders(t.symbol, t.entry_time):
                if o.get("side") == closing_side and o.get("status") == "filled" and o.get("id") != t.order_id \
                        and o.get("filled_avg_price"):
                    return float(o["filled_avg_price"]), o.get("filled_at")
        except Exception as e:
            self.journal.event("error", where="external_exit", id=t.id, error=repr(e)[:200])
        snap = await self.api.snapshot(t.symbol)
        return (float(snap["latestTrade"]["p"]) if snap and snap.get("latestTrade") else t.entry_price), None

    async def exit_now(self, t: Trade, reason: str, legs=None):
        if legs is None:
            legs = (await self.api.order(t.order_id)).get("legs") or []
        for leg in legs:
            if leg["status"] in LIVE:
                await self.api.cancel(leg["id"])
        # Cancels are asynchronous at the broker: wait until the legs have released the shares.
        for _ in range(10):
            await asyncio.sleep(0.5)
            legs = (await self.api.order(t.order_id)).get("legs") or []
            for leg in legs:
                if leg["status"] == "filled":
                    reason = "target" if leg["type"] == "limit" else "stop"
                    await self._record(t, float(leg["filled_avg_price"]), reason, leg.get("filled_at"))
                    return
            if not any(l["status"] in LIVE or l["status"].startswith("pending") for l in legs):
                break
        co = None
        for attempt in range(3):
            try:
                co = await self.api.close_position(t.symbol)
                break
            except AlpacaError as e:
                if e.status not in (403, 422) or attempt == 2:
                    raise  # logged by the caller; the trade stays tracked and is retried
                await asyncio.sleep(1)
        price, when = 0.0, None
        if co:
            for _ in range(15):
                o = await self.api.order(co["id"])
                if o["status"] == "filled":
                    price, when = float(o["filled_avg_price"]), o.get("filled_at")
                    break
                await asyncio.sleep(1)
        else:  # position already gone: a leg filled while we were cancelling
            for leg in (await self.api.order(t.order_id)).get("legs") or []:
                if leg["status"] == "filled":
                    price, when = float(leg["filled_avg_price"]), leg.get("filled_at")
                    reason = "target" if leg["type"] == "limit" else "stop"
        if not price:
            snap = await self.api.snapshot(t.symbol)
            price = float(snap["latestTrade"]["p"]) if snap and snap.get("latestTrade") else t.entry_price
            reason += "_unconfirmed"
        await self._record(t, price, reason, when)

    async def _record(self, t: Trade, exit_price: float, reason: str, when=None):
        exit_time = parse_ts(when) if when else now()
        pnl = (exit_price - t.entry_price) * t.qty * t.dirn
        risk = (t.entry_price * t.stop_pct / 100 if t.stop_pct else abs(t.signal_price - t.stop_price)) * t.qty
        row = {
            "id": t.id, "strategy": t.strategy, "symbol": t.symbol, "side": t.side, "qty": t.qty,
            "signal_price": t.signal_price, "entry_price": t.entry_price, "exit_price": exit_price,
            "stop_price": t.stop_price, "target_price": t.target_price,
            "entry_time": t.entry_time, "exit_time": iso(exit_time),
            "hold_min": round((exit_time - parse_ts(t.entry_time)).total_seconds() / 60, 1),
            "pnl": round(pnl, 2), "r": round(pnl / risk, 2) if risk else 0,
            "slippage_pct": round((t.entry_price - t.signal_price) / t.signal_price * 100 * t.dirn, 3),
            "notional": round(t.qty * t.entry_price, 2), "exit_reason": reason, "meta": t.meta,
            "style": t.style, "phase": t.phase,
        }
        if t.style == "soft":
            row.update(ticks=t.ticks, blind_ticks=t.blind_ticks, price_changes=t.price_changes, exit_tries=t.exit_tries)
        self.journal.trade(row)
        self.journal.event("trade_closed", id=t.id, symbol=t.symbol, pnl=row["pnl"], r=row["r"], reason=reason)
        self._drop(t)

    def _drop(self, t: Trade, count=True):
        self.trades.pop(t.id, None)
        if not count:
            self.trades_today = max(0, self.trades_today - 1)
        self.cooldown[t.symbol] = now() + timedelta(minutes=self.cooldown_min)
        self.save()

    async def flatten(self, reason="eod"):
        """Close everything the bot holds, then anything left in the account."""
        for t in list(self.trades.values()):
            try:
                if t.status == "pending":
                    await self.api.cancel(t.order_id)
                    await asyncio.sleep(1)
                    await self._manage_one(t)       # dropped, or open with what was filled
                    if t.id not in self.trades or t.status == "pending" or t.style != "soft":
                        continue                    # (a cancel still on its way is settled at the next check)
                if t.style == "soft":
                    if not t.exit_reason:
                        t.exit_reason, t.status = reason, "closing"
                        self.save()
                    for _ in range(4):               # each round goes further through the market
                        await self._close_soft(t)
                        if t.id not in self.trades:
                            break
                else:
                    await self.exit_now(t, reason)
            except Exception as e:
                self.journal.event("error", where="flatten", id=t.id, error=repr(e)[:300])
        await self.close_orphans()

    async def close_orphans(self):
        """Close positions in the account that the bot is not tracking. Never raises."""
        tracked = {t.symbol for t in self.trades.values()}
        try:
            positions = await self.api.positions()
        except Exception as e:
            self.journal.event("error", where="orphans", error=repr(e)[:300])
            return
        for p in positions:
            sym = p["symbol"]
            if sym in tracked:
                continue
            try:
                # Cancels are asynchronous: wait until the symbol's orders have released the shares.
                for _ in range(10):
                    mine = [o for o in await self.api.open_orders() if o["symbol"] == sym]
                    if not mine:
                        break
                    for o in mine:
                        await self.api.cancel(o["id"])
                    await asyncio.sleep(1)
                if self.ext_enabled() and self.session.phase() in ("pre", "post"):
                    # A market order would wait for the next regular open: use limit orders instead.
                    done = await close_with_limit(self.api, sym, self.ext, self.journal)
                    self.journal.event("orphan", symbol=sym, qty=p["qty"], action="closed" if done else "not_closed")
                    continue
                await self.api.close_position(sym)
                self.journal.event("orphan", symbol=sym, qty=p["qty"], action="closed")
            except Exception as e:
                self.journal.event("error", where="orphans", symbol=sym, error=repr(e)[:300])
