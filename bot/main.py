"""Trading bot entry point: python -m bot.main"""
import asyncio
import json
import logging
import signal
import subprocess
import time

import websockets

from .alpaca import Alpaca
from .common import DATA, ROOT, Journal, env, load_config, log, now, spawn
from .edgar import Edgar
from .executor import Executor
from .fast import FastStrategy
from .feedview import FeedView
from .finnhub import Expectations, Finnhub
from .finnhub_news import FinnhubNews
from .llm import Classifier
from .market import Session, StockStats, Universe
from .news_strategy import OUTCOME_DELAY_MIN, NewsStrategy
from .orb_strategy import OrbStrategy
from .speed import Speed
from .first import FirstReport
from .wirealpha import WireAlpha
from .wires import Wires, is_alert

NEWS_WS = "wss://stream.data.alpaca.markets/v1beta1/news"


def day_result(account: dict, bot_start_equity=None, day_start_equity=None) -> dict:
    """Account value at the start and end of the day and the difference.

    The start is the broker's value at yesterday's close. The bot's own first reading of the day
    is only the fallback: after a restart during the day it would hide everything before it.

    With trading after the close that is no longer right: the broker's value is taken at 16:00
    New York time, so the evening's trades would count for the next day as well. Then the start
    is the account value at the beginning of the New York day (day_start_equity), which the bot
    writes down once per day and keeps across restarts.
    """
    equity = float(account["equity"])
    last_close = float(account.get("last_equity") or 0)
    if day_start_equity:
        start, basis = day_start_equity, "day_start"
    else:
        start = last_close or bot_start_equity or equity
        basis = "last_close" if last_close else "bot_start"
    return {"equity_start": start, "equity_end": equity, "pnl": round(equity - start, 2), "basis": basis}


class Bot:
    def __init__(self):
        self.cfg = load_config()
        base = env("APCA_BASE_URL", "https://paper-api.alpaca.markets")
        if "paper" not in base and env("ALLOW_LIVE") != "yes":
            raise SystemExit("Nur Paper-Trading erlaubt. APCA_BASE_URL zeigt nicht auf das Paper-Konto.")
        self.key, self.secret = env("APCA_API_KEY_ID", required=True), env("APCA_API_SECRET_KEY", required=True)
        self.api = Alpaca(self.key, self.secret, base, feed=env("APCA_DATA_FEED", "iex"))
        self.journal = Journal()
        self.session = Session(self.api)
        self.universe = Universe(self.api)
        self.executor = Executor(self.api, self.cfg, self.journal, self.session, self.universe)
        llm_key = env("ANTHROPIC_API_KEY", "")
        self.llm = Classifier(llm_key, self.cfg["llm"])                      # this model's rating decides
        self.shadows = [Classifier(llm_key, self.cfg["llm"], model=m)        # these only rate along for comparison
                        for m in self.cfg["llm"].get("shadow", []) if m != self.llm.model]
        self.orb = OrbStrategy(self.api, self.cfg, self.journal, self.session, self.universe, self.executor)
        self.news = NewsStrategy(
            self.api, self.cfg, self.journal, self.session, self.universe, self.executor, self.llm, self.orb,
            stats=StockStats(self.api), shadows=self.shadows, fallback=self.cfg["llm"].get("fallback"),
        )
        self.wires = Wires(self.cfg["wires"], self.universe, self.on_wire, self.journal)
        # Several sources report the same news: only the first report is rated and traded.
        self.first = FirstReport(self.journal, self.cfg["wires"].get("same_news_min", 15))
        # Immediate entry before the open and after the close, with what analysts expect of today's results.
        # Finnhub allows 60 calls a minute per key: 30 for the expectations, 28 for its news (measured, not traded).
        self.expectations = Expectations(Finnhub(env("FINNHUB_API_KEY", ""), max_per_min=30), self.journal)
        self.fh_news = FinnhubNews(Finnhub(env("FINNHUB_API_KEY", ""), max_per_min=28), self.cfg.get("finnhub_news"),
                                   self.expectations, self.universe, self.on_wire, self.journal,
                                   self.cfg["wires"]["max_age_s"])
        self.fast = FastStrategy(
            self.api, self.cfg, self.journal, self.session, self.universe, self.executor, self.llm,
            stats=self.news.stock_stats, expectations=self.expectations, fetch_text=self.wires.fetch_text,
            shadows=self.shadows,
        )
        # Filings with the SEC: every one is logged (measurement); a fresh one is also read and rated.
        self.edgar = Edgar(self.cfg["edgar"], env("SEC_CONTACT", ""), self.universe, self.journal,
                           on_filing=self.on_filing)
        # One more source of press releases, measured only until it is named in [wires] trade.
        self.wirealpha = WireAlpha(self.cfg.get("wirealpha"), env("WIREALPHA_API_KEY", ""), self.universe,
                                   self.on_wire, self.journal, self.cfg["wires"]["max_age_s"], is_alert=is_alert)
        self.speed = Speed(self.api, self.cfg["speed"], self.journal, self.universe, self.news.stock_stats)
        self.feedview = FeedView(self.api, self.cfg.get("feedview") or {}, self.cfg.get("extended") or {}, self.journal,
                                 self.session, self.universe, self.cfg["orb"]["static_pool"])
        self.ext = self.cfg.get("extended") or {}
        self.day = None
        self.outcomes_day = None
        self.speed_day = None
        self.flattened = False
        self.ext_flattened = set()     # pre-market / after-hours sessions already closed out today
        self.start_equity = None
        self.day_start_equity = None   # account value at the start of the New York day (kept across restarts)
        self.errors = 0
        self.ws_connected = False
        self.last_status = None     # time of the last status file (None: none yet, write at once)
        try:
            self.version = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
            ).stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            self.version = "unknown"

    async def on_wire(self, art: dict):
        """A press release from a wire: the immediate entry takes it in every session it is switched
        on for, the old rule with price confirmation during the regular session if it still is on.
        Each decides by itself whether it is its turn."""
        trade = self.cfg["wires"].get("trade")
        traded = trade is None or art.get("source") in trade
        if not self.first.claim(art, traded) or not traded:
            return                      # a later copy, or a source that is only measured (wire_item)
        spawn(self.fast.on_article(art))
        await self.news.on_article(art)

    async def on_filing(self, entry: dict, sym: str):
        """A fresh filing with the SEC: read and rated like a press release. It is traded only with
        [edgar] trade; until then every rating is logged, to measure how often it is right."""
        cfg = self.cfg["edgar"]
        items = set(entry["items"])
        if not cfg.get("rate") or items & set(cfg.get("skip_with_items") or []) \
                or (items and items <= set(cfg.get("skip_items") or [])):
            return                      # quarterly results (the press release was there minutes earlier), formalities
        try:
            art = self.edgar.article(entry, sym)
            traded = bool(cfg.get("trade"))
            if not self.first.claim(art, traded):
                return                  # the company's press release with the same news was here first
            if not traded:
                art["rate_only"] = True
            await self.fast.on_article(art)
        except Exception as e:
            self.errors += 1
            self.journal.event("error", where="filing", acc=entry.get("acc"), error=repr(e)[:200])

    # ── loops ────────────────────────────────────────────────────────────────
    async def news_loop(self):
        """Alpaca's news stream. Off with [news] stream = false: it arrives hours late and feeds
        only the old rule with price confirmation."""
        if not self.cfg["news"].get("stream", True):
            return
        backoff = 2
        while True:
            try:
                async with websockets.connect(
                    NEWS_WS, ping_interval=20,
                    additional_headers={"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret},
                ) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
                    async for raw in ws:
                        for m in json.loads(raw):
                            if m.get("T") == "n":
                                if self.first.claim({**m, "source": m.get("source") or "alpaca"}):
                                    spawn(self.news.on_article(m))
                            elif m.get("T") == "subscription":
                                log.info("news stream connected")
                                backoff, self.ws_connected = 2, True
                            elif m.get("T") == "error":
                                # 406 = another process already holds the account's single stream connection
                                self.ws_connected = False
                                self.journal.event("error", where="news_ws", error=str(m))
            except Exception as e:
                self.errors += 1
                self.journal.event("error", where="news_ws", error=repr(e)[:200])
            self.ws_connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def control_loop(self):
        """Session bookkeeping, trade management, ORB state machine."""
        poll = self.cfg["execution"]["poll_s"]
        while True:
            try:
                await self.session.refresh()
                if self.session.day != self.day:
                    await self.new_day()
                phase = self.session.phase()
                if phase == "regular":
                    await self.executor.manage()
                    if self.executor.flatten_due() and not self.flattened:
                        await self.end_of_day()
                    elif not self.flattened:
                        await self.orb.step()
                else:
                    # Before the open and after the close: trades there are watched by the bot itself.
                    # A position that could not be closed in time is looked after until it is gone.
                    if (phase and self.ext.get("enabled")) or self.executor.has_soft():
                        await self.executor.manage()
                        if phase and self.executor.ext_flatten_due() and phase not in self.ext_flattened:
                            await self.end_of_session(phase)
                    if self.session.trading_day and now() < self.session.open:
                        await self.orb.step()
                    elif self.session.trading_day and now() >= self.session.close:
                        self.after_close()
                await self.feedview.step()
            except Exception as e:
                self.errors += 1
                self.journal.event("error", where="control", error=repr(e)[:300])
            if self.last_status is None or time.monotonic() - self.last_status > 60:
                self.last_status = time.monotonic()
                try:
                    self.write_status()
                except Exception as e:   # e.g. a full disk: must not end trade management
                    log.warning("status not written: %r", e)
            await asyncio.sleep(self.ext.get("poll_s", poll) if self.executor.has_soft() else poll)

    def after_close(self):
        """Measurements that need whole-market data, which is free once it is 15 minutes old.
        Each runs once per day (again after a restart; what is already measured is skipped)."""
        since = (now() - self.session.close).total_seconds() / 60
        if self.outcomes_day != self.day and since >= OUTCOME_DELAY_MIN:
            self.outcomes_day = self.day
            spawn(self.news.log_outcomes())
        if self.ext.get("enabled"):
            # With trading after the close, the long measurement waits until that session is over:
            # it needs many price requests, and nothing may slow down an open position.
            since = (now() - self.session.ext_close).total_seconds() / 60
        if self.speed_day != self.day and self.cfg["speed"]["enabled"] and since >= self.cfg["speed"]["after_close_min"]:
            self.speed_day = self.day
            spawn(self.speed.run())

    def write_status(self):
        s = self.session
        self.journal.status({
            "version": self.version, "day": self.day, "trading_day": s.trading_day,
            "market_open": s.is_open(), "news_stream": self.ws_connected,
            "llm_available": self.llm.available, "llm_model": self.llm.model, "llm_calls_today": self.llm.day_calls,
            "llm_shadow_calls": {c.model: c.day_calls for c in self.shadows},
            "news": self.news.stats, "wires": self.wires.stats,
            "wire_sources": {**self.wires.by_source, "finnhub": self.fh_news.stats,
                             "wirealpha": {**self.wirealpha.stats, "connected": self.wirealpha.connected}},
            "edgar": {**self.edgar.stats, "handed_on": self.edgar.handed},
            "fast": self.fast.stats, "fast_enabled": bool(self.cfg["fast"]["enabled"]),
            "expectations": len(self.expectations.rows),
            "orb_stage": self.orb.stage, "orb_pool": len(self.orb.pool), "orb_watching": sorted(self.orb.ranges),
            "open_trades": [
                {"symbol": t.symbol, "strategy": t.strategy, "side": t.side, "qty": t.qty, "status": t.status}
                for t in self.executor.trades.values()
            ],
            "trades_today": self.executor.trades_today, "errors_today": self.errors,
            "flattened": self.flattened, "phase": s.phase(), "extended": bool(self.ext.get("enabled")),
        })

    def _day_start_equity(self, equity):
        """The account value at the start of the New York day: written down at the first look of
        the day, read again after a restart. Returns None if this day's value was never taken at
        its start (the bot was first started in the middle of it)."""
        path = DATA / "state" / "day_start.json"
        try:
            d = json.loads(path.read_text())
            if d.get("day") == self.day:
                return d.get("equity")
        except (OSError, ValueError):
            pass
        fresh = self.session.ext_open is None or now() < self.session.ext_open   # nothing can have traded yet
        value = equity if fresh else None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"day": self.day, "equity": value}))
        except OSError as e:
            log.warning("day start not written: %r", e)
        return value

    async def new_day(self):
        self.day = self.session.day
        self.flattened = False
        self.ext_flattened = set()
        self.errors = 0
        self.executor.start_day(self.day)
        self.news.reset_day()
        self.fast.reset_day()
        self.orb.reset_day()
        self.wires.reset_day()
        self.fh_news.reset_day()
        self.wirealpha.reset_day()
        self.edgar.reset_day()
        for c in [self.llm, *self.shadows]:
            c.reset_day()
        try:
            universe = await self.universe.load()
        except Exception as e:  # keep yesterday's list (or per-symbol lookups) if this fails
            universe = len(self.universe.cache)
            self.journal.event("error", where="universe", error=repr(e)[:200])
        a = await self.api.account()
        self.start_equity = float(a["equity"])
        self.day_start_equity = self._day_start_equity(self.start_equity)
        self.journal.event(
            "day_start", day=self.day, trading_day=self.session.trading_day, universe=universe,
            equity=self.start_equity, last_equity=float(a.get("last_equity") or 0) or None,
            llm=self.llm.available, llm_model=self.llm.model, llm_shadow=[c.model for c in self.shadows],
            news=self.cfg["news"]["enabled"], orb=self.cfg["orb"]["enabled"],
            extended=bool(self.ext.get("enabled")), fast=bool(self.cfg["fast"]["enabled"]),
            fast_sessions=self.cfg["fast"]["sessions"], look=bool((self.cfg["fast"].get("look") or {}).get("enabled")),
            sec_rated=bool(self.cfg["edgar"].get("rate")), sec_traded=bool(self.cfg["edgar"].get("trade")),
            wirealpha=self.wirealpha.active,
        )
        if self.cfg["fast"]["enabled"]:
            spawn(self.expectations.refresh(force=True))
        if self.session.trading_day and now() >= self.session.open:
            # Started mid-session: the opening range is gone, skip ORB today.
            self.orb.stage = "done"

    async def end_of_day(self):
        """Shortly before the regular close: everything flat. Without trading after the close this
        also ends the day; with it, the day's summary is written when that session is over."""
        await self.executor.flatten("eod")
        await self.api.cancel_all()
        self.flattened = True
        if not self.ext.get("enabled"):
            await self.write_summary()

    async def end_of_session(self, phase):
        """Shortly before the pre-market or the after-hours session ends: close what was opened in it."""
        await self.executor.flatten("session_end")
        await self.api.cancel_all()
        self.ext_flattened.add(phase)
        self.journal.event("session_end", phase=phase, still_open=sorted(t.symbol for t in self.executor.trades.values()))
        if phase == "post":
            await self.write_summary()

    async def write_summary(self):
        ext = bool(self.ext.get("enabled"))
        summary = {
            "day": self.day,
            **day_result(await self.api.account(), self.start_equity, self.day_start_equity if ext else None),
            "trades": self.executor.trades_today, "news": self.news.stats, "wires": self.wires.stats,
            "llm_model": self.llm.model, "llm_calls": self.llm.day_calls,
            "llm_shadow_calls": {c.model: c.day_calls for c in self.shadows}, "errors": self.errors,
            "orb_watched": sorted(self.orb.ranges), "orb_traded": sorted(self.orb.fired), "extended": ext,
            "fast": self.fast.stats,
        }
        path = self.journal.dir / "daily" / f"{self.day}.json"
        try:
            before = json.loads(path.read_text()) if path.exists() else None
        except ValueError:
            before = None
        # Keep the first summary after a restart - unless it was written at the regular close by a
        # version without trading after the close: then the evening is still missing in it.
        if before is None or (ext and not before.get("extended")):
            self.journal.daily(self.day, summary)
        self.journal.event("day_end", **summary)

    async def run(self):
        (DATA / "state").mkdir(parents=True, exist_ok=True)
        (DATA / "state" / "running_commit").write_text(self.version)
        await self.session.refresh()
        self.executor.load()
        await self.executor.close_orphans()
        log.info("bot started; llm=%s day=%s trading_day=%s", self.llm.available, self.session.day, self.session.trading_day)
        if not self.llm.available:
            log.warning("ANTHROPIC_API_KEY fehlt: Meldungen werden nicht bewertet, also auch nicht gehandelt")
        await asyncio.gather(self.news_loop(), self.control_loop(), self.wires.run(), self.edgar.run(),
                             self.fh_news.run(), self.wirealpha.run())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    loop = asyncio.new_event_loop()
    task = loop.create_task(Bot().run())
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        log.info("stopped")


if __name__ == "__main__":
    main()
