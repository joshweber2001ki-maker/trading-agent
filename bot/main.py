"""Trading bot entry point: python -m bot.main"""
import asyncio
import json
import logging
import signal
import subprocess
import time

import websockets

from .alpaca import Alpaca
from .common import DATA, ROOT, Journal, env, load_config, log, now
from .executor import Executor
from .llm import Classifier
from .market import Session, Universe
from .news_strategy import NewsStrategy
from .orb_strategy import OrbStrategy

NEWS_WS = "wss://stream.data.alpaca.markets/v1beta1/news"


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
        self.llm = Classifier(env("ANTHROPIC_API_KEY", ""), self.cfg["llm"])
        self.orb = OrbStrategy(self.api, self.cfg, self.journal, self.session, self.universe, self.executor)
        self.news = NewsStrategy(
            self.api, self.cfg, self.journal, self.session, self.universe, self.executor, self.llm, self.orb
        )
        self.day = None
        self.flattened = False
        self.start_equity = None
        self.errors = 0
        self.ws_connected = False
        self.last_status = 0
        try:
            self.version = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
            ).stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            self.version = "unknown"

    # ── loops ────────────────────────────────────────────────────────────────
    async def news_loop(self):
        backoff = 2
        while True:
            try:
                async with websockets.connect(
                    NEWS_WS, ping_interval=20,
                    additional_headers={"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret},
                ) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
                    log.info("news stream connected")
                    backoff, self.ws_connected = 2, True
                    async for raw in ws:
                        for m in json.loads(raw):
                            if m.get("T") == "n":
                                asyncio.create_task(self.news.on_article(m))
                            elif m.get("T") == "error":
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
                if self.session.is_open():
                    await self.executor.manage()
                    if self.executor.flatten_due() and not self.flattened:
                        await self.end_of_day()
                    elif not self.flattened:
                        await self.orb.step()
                elif self.session.trading_day and now() < self.session.open:
                    await self.orb.step()
            except Exception as e:
                self.errors += 1
                self.journal.event("error", where="control", error=repr(e)[:300])
            if time.monotonic() - self.last_status > 60:
                self.last_status = time.monotonic()
                self.write_status()
            await asyncio.sleep(poll)

    def write_status(self):
        s = self.session
        self.journal.status({
            "version": self.version, "day": self.day, "trading_day": s.trading_day,
            "market_open": s.is_open(), "news_stream": self.ws_connected,
            "llm_available": self.llm.available, "llm_calls_today": self.llm.day_calls,
            "news": self.news.stats, "orb_stage": self.orb.stage, "orb_watching": sorted(self.orb.ranges),
            "open_trades": [
                {"symbol": t.symbol, "strategy": t.strategy, "side": t.side, "qty": t.qty, "status": t.status}
                for t in self.executor.trades.values()
            ],
            "trades_today": self.executor.trades_today, "errors_today": self.errors,
            "flattened": self.flattened,
        })

    async def new_day(self):
        self.day = self.session.day
        self.flattened = False
        self.errors = 0
        self.executor.start_day(self.day)
        self.news.reset_day()
        self.orb.reset_day()
        self.llm.reset_day()
        try:
            universe = await self.universe.load()
        except Exception as e:  # keep yesterday's list (or per-symbol lookups) if this fails
            universe = len(self.universe.cache)
            self.journal.event("error", where="universe", error=repr(e)[:200])
        a = await self.api.account()
        self.start_equity = float(a["equity"])
        self.journal.event(
            "day_start", day=self.day, trading_day=self.session.trading_day, universe=universe,
            equity=self.start_equity, llm=self.llm.available,
            news=self.cfg["news"]["enabled"], orb=self.cfg["orb"]["enabled"],
        )
        if self.session.trading_day and now() >= self.session.open:
            # Started mid-session: the opening range is gone, skip ORB today.
            self.orb.stage = "done"

    async def end_of_day(self):
        await self.executor.flatten("eod")
        await self.api.cancel_all()
        self.flattened = True
        a = await self.api.account()
        equity = float(a["equity"])
        summary = {
            "day": self.day, "equity_start": self.start_equity, "equity_end": equity,
            "pnl": round(equity - (self.start_equity or equity), 2),
            "trades": self.executor.trades_today, "news": self.news.stats,
            "llm_calls": self.llm.day_calls, "errors": self.errors,
            "orb_watched": sorted(self.orb.ranges), "orb_traded": sorted(self.orb.fired),
        }
        if not (self.journal.dir / "daily" / f"{self.day}.json").exists():  # keep the first summary after a restart
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
            log.warning("ANTHROPIC_API_KEY fehlt: News-Strategie inaktiv, nur Ausbruchs-Strategie läuft")
        await asyncio.gather(self.news_loop(), self.control_loop())


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
