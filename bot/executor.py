"""Order execution, position sizing, risk limits and trade lifecycle."""
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


def position_size(equity, price, stop, risk_pct, max_pos_lev, gross_room, buying_power):
    """Shares so that a move to the stop loses risk_pct of equity, within leverage limits."""
    dist = abs(price - stop)
    if dist <= 0 or price <= 0:
        return 0
    by_risk = equity * risk_pct / 100 / dist
    by_notional = min(equity * max_pos_lev, gross_room, buying_power) / price
    return max(0, int(min(by_risk, by_notional)))


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
    status: str = "pending"   # pending -> open
    entry_price: float = 0
    entry_time: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def dirn(self):
        return 1 if self.side == "long" else -1


class Executor:
    def __init__(self, api, cfg, journal, session, universe, root=None):
        self.api, self.journal, self.session, self.universe = api, journal, session, universe
        self.risk, self.ex = cfg["risk"], cfg["execution"]
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

    # ── entry ────────────────────────────────────────────────────────────────
    async def can_enter(self, strategy, symbol):
        if (self.root / "HALT").exists():
            return False, "halt_file"
        if not self.session.is_open() or self.flatten_due():
            return False, "session"
        if any(t.symbol == symbol for t in self.trades.values()):
            return False, "already_in_symbol"
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
        """sig: strategy, symbol, side, price, stop, target_r, max_hold_min, meta"""
        strategy, symbol, side = sig["strategy"], sig["symbol"], sig["side"]
        price, stop = float(sig["price"]), float(sig["stop"])
        base = dict(strategy=strategy, symbol=symbol, side=side, price=price)

        def reject(reason, **kw):
            self.journal.event("rejected", stage="risk", reason=reason, **base, **kw)
            return False

        ok, why = await self.can_enter(strategy, symbol)
        if not ok:
            return reject(why)
        if price < self.risk["min_price"]:
            return reject("min_price")
        if side == "short" and not await self.universe.shortable(symbol):
            return reject("not_shortable")
        a = await self.account(max_age=5)
        equity = float(a["equity"])
        qty = position_size(
            equity, price, stop, self.risk["risk_per_trade_pct"],
            self.risk["max_position_leverage"],
            equity * self.risk["max_gross_leverage"] - self.gross(),
            float(a["buying_power"]) * 0.95,
        )
        if qty < 1:
            return reject("size_zero", equity=equity, stop=stop)

        dirn = 1 if side == "long" else -1
        dist = abs(price - stop)
        limit = round(price * (1 + dirn * self.ex["entry_limit_offset_pct"] / 100), 2)
        target = round(price + dirn * sig["target_r"] * dist, 2)
        stop = round(stop, 2)
        tid = f"{strategy}-{symbol}-{uuid.uuid4().hex[:8]}"
        payload = {
            "symbol": symbol, "qty": str(qty), "side": "buy" if side == "long" else "sell",
            "type": "limit", "limit_price": str(limit), "time_in_force": "day",
            "order_class": "bracket", "client_order_id": tid,
            "take_profit": {"limit_price": str(target)},
            "stop_loss": {"stop_price": str(stop)},
        }
        try:
            o = await self.api.submit(payload)
        except AlpacaError as e:
            self.journal.event("error", where="submit", error=str(e), **base)
            return False
        t = Trade(
            id=tid, strategy=strategy, symbol=symbol, side=side, qty=qty, signal_price=price,
            stop_price=stop, target_price=target, order_id=o["id"], submitted_at=iso(now()),
            max_hold_min=sig.get("max_hold_min") or 0, meta=sig.get("meta") or {},
        )
        self.trades[tid] = t
        self.trades_today += 1
        self.done_today.add((strategy, symbol))
        self.save()
        self.journal.event(
            "trade_submitted", id=tid, qty=qty, limit=limit, stop=stop, target=target,
            notional=round(qty * price, 2), equity=equity, **base,
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
                if age > self.ex["entry_timeout_s"]:
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
        elif unprotected:
            # No protective orders at the broker (e.g. partial fill then cancel): guard in software.
            snap = await self.api.snapshot(t.symbol)
            p = float(snap["latestTrade"]["p"]) if snap and snap.get("latestTrade") else 0
            if p and (p - t.stop_price) * t.dirn <= 0:
                await self.exit_now(t, "stop_sw", legs)
            elif p and (p - t.target_price) * t.dirn >= 0:
                await self.exit_now(t, "target_sw", legs)

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
        risk = abs(t.signal_price - t.stop_price) * t.qty
        row = {
            "id": t.id, "strategy": t.strategy, "symbol": t.symbol, "side": t.side, "qty": t.qty,
            "signal_price": t.signal_price, "entry_price": t.entry_price, "exit_price": exit_price,
            "stop_price": t.stop_price, "target_price": t.target_price,
            "entry_time": t.entry_time, "exit_time": iso(exit_time),
            "hold_min": round((exit_time - parse_ts(t.entry_time)).total_seconds() / 60, 1),
            "pnl": round(pnl, 2), "r": round(pnl / risk, 2) if risk else 0,
            "slippage_pct": round((t.entry_price - t.signal_price) / t.signal_price * 100 * t.dirn, 3),
            "notional": round(t.qty * t.entry_price, 2), "exit_reason": reason, "meta": t.meta,
        }
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
                if t.status == "open":
                    await self.exit_now(t, reason)
                else:
                    await self.api.cancel(t.order_id)
                    await asyncio.sleep(1)
                    await self._manage_one(t)
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
                await self.api.close_position(sym)
                self.journal.event("orphan", symbol=sym, qty=p["qty"], action="closed")
            except Exception as e:
                self.journal.event("error", where="orphans", symbol=sym, error=repr(e)[:300])
