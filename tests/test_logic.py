"""Logic tests with a simulated broker. Run: python -m pytest tests -q"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from bot import executor as ex_mod
from bot.common import ET, Journal, iso, load_config, parse_ts
from bot.executor import Executor, position_size
from bot.llm import is_noise, parse_verdict
from bot.news_strategy import confirmation, stop_distance_pct
from bot.orb_strategy import OrbStrategy, assess, breakout, opening_range, relative_volume
from bot.orb_strategy import stop_distance_pct as orb_stop_pct
from bot.wires import NameIndex, normalize, parse_feed, to_article
from tools.weekly_stats import build, default_week, markdown, week_of

CFG = load_config()


def test_parse_ts_nanoseconds():
    assert parse_ts("2026-11-16T21:37:10.639981148Z").microsecond == 639981
    assert parse_ts("2026-11-16T21:37:00Z").tzinfo is not None


def test_position_size_risk_and_caps():
    # 0.5 % of 100k = 500 $ risk; 1 $ stop distance -> 500 shares (50k notional, under the cap)
    assert position_size(100_000, 100, 99, 0.5, 1.0, 300_000, 400_000) == 500
    # tight stop: capped by max position notional (1x equity)
    assert position_size(100_000, 100, 99.9, 0.5, 1.0, 300_000, 400_000) == 1000
    # capped by remaining gross room
    assert position_size(100_000, 100, 99.9, 0.5, 1.0, 20_000, 400_000) == 200
    assert position_size(100_000, 100, 100, 0.5, 1.0, 300_000, 400_000) == 0


def test_verdict_parsing():
    v = parse_verdict('Sure: {"symbol":"nvda","direction":"UP","surprise":8.6,"confidence":"7","category":"earnings","reason":"beat"}', ["NVDA"])
    assert v["symbol"] == "NVDA" and v["direction"] == "up" and v["surprise"] == 9 and v["confidence"] == 7
    assert parse_verdict("no json here", ["A"]) is None
    assert parse_verdict('{"symbol":"XYZ","direction":"sideways"}', ["A"])["symbol"] == "A"
    assert parse_verdict('{"symbol":"XYZ","direction":"sideways"}', ["A"])["direction"] == "none"


def test_noise_filter():
    assert is_noise("Why Is Tesla Stock Moving Today?")
    assert is_noise("Capri Holdings Insider Sold Shares Worth $46.9 Million")
    assert not is_noise("Acme raises full-year guidance")


def test_confirmation():
    c = CFG["news"]
    assert confirmation(100, 100.1, 1, 5000, 1000, 0.05, c)[0] == "wait"        # move too small
    assert confirmation(100, 100.6, 1, 5000, 1000, 0.05, c)[0] == "confirmed"
    assert confirmation(100, 100.6, 1, 500, 1000, 0.05, c) == ("wait", "volume")
    assert confirmation(100, 100.6, 1, 5000, 1000, 0.9, c) == ("wait", "spread")
    assert confirmation(100, 104, 1, 5000, 1000, 0.05, c) == ("abort", "too_late")
    assert confirmation(100, 99.4, -1, 5000, 1000, 0.05, c)[0] == "confirmed"   # short side
    assert confirmation(100, 102, -1, 5000, 1000, 0.05, c) == ("abort", "moved_against")


def test_stop_distance_clamped():
    bars = [{"h": 100.2, "l": 99.8}] * 10
    assert stop_distance_pct(bars, 100, 0.75, 0.4, 1.5) == pytest.approx(0.4)   # 0.3 % -> floor
    wide = [{"h": 103, "l": 99}] * 10
    assert stop_distance_pct(wide, 100, 0.75, 0.4, 1.5) == 1.5
    assert stop_distance_pct([], 100, 0.75, 0.4, 1.5) == 0.4


def test_opening_range_and_breakout():
    open_dt = datetime(2026, 11, 17, 9, 30, tzinfo=ET)
    bars = [
        {"t": iso(open_dt + timedelta(minutes=i)), "h": 100 + i * 0.1, "l": 99 - i * 0.05, "v": 1000}
        for i in range(20)
    ]
    rng = opening_range(bars, open_dt, 15)
    assert rng["bars"] == 15 and rng["high"] == pytest.approx(101.4) and rng["low"] == pytest.approx(98.3)
    assert rng["volume"] == 15000
    assert breakout(101.5, rng, 0.05) == "long"
    assert breakout(101.41, rng, 0.05) is None
    assert breakout(98.2, rng, 0.05) == "short"


def test_orb_assess_and_stop():
    c = CFG["orb"]
    rng = {"high": 101.0, "low": 100.0, "volume": 50_000}
    assert assess(rng, 2.0, 1.4, c) is None
    assert assess(rng, 0.2, 1.4, c) == "gap_too_small"
    assert assess(rng, 0.2, 1.4, c, is_news=True) is None
    assert assess(rng, 80.0, 1.4, c) == "gap_implausible"
    assert assess(rng, 2.0, 0.7, c) == "low_rvol"
    assert assess(rng, 2.0, None, c) == "no_volume_history"
    assert assess({**rng, "volume": 500}, 2.0, 1.4, c) == "illiquid"
    assert assess({**rng, "high": 104.0}, 2.0, 1.4, c) == "range_too_wide"
    assert assess({**rng, "high": 100.1}, 2.0, 1.4, c) == "range_too_narrow"
    assert orb_stop_pct(1.0, 0.5, 0.3, 1.0) == 0.5
    assert orb_stop_pct(0.4, 0.5, 0.3, 1.0) == 0.3
    assert orb_stop_pct(2.4, 0.5, 0.3, 1.0) == 1.0


def test_relative_volume():
    assert relative_volume(25_000, [10_000] * 8) == pytest.approx(2.5)
    assert relative_volume(25_000, [10_000, 10_000]) is None            # too little history
    assert relative_volume(30_000, [1] * 5 + [10_000] * 10) == pytest.approx(3.0)   # only the last 10 days count


def test_orb_keeps_only_fresh_news_candidates_overnight(tmp_path):
    orb = OrbStrategy(None, CFG, None, None, None, None, root=tmp_path)
    orb.add_candidate("NVDA", source="static")
    orb.add_candidate("HD", source="news", verdict={"surprise": 8})
    orb.add_candidate("OLD", source="news", verdict={"surprise": 9})
    orb.pool["OLD"]["added"] -= timedelta(hours=30)
    orb.add_candidate("HD", source="static")                 # must not replace the news entry
    assert orb.pool["HD"]["source"] == "news"
    orb.reset_day()
    assert list(orb.pool) == ["HD"] and orb.stage == "idle"
    again = OrbStrategy(None, CFG, None, None, None, None, root=tmp_path)      # restart: news candidates are still there
    assert list(again.pool) == ["HD"] and again.pool["HD"]["verdict"]["surprise"] == 8


def test_orb_gives_up_after_repeated_failures(tmp_path):
    class Sess:
        trading_day, day = True, "2026-11-17"
        open = datetime.now(timezone.utc) - timedelta(minutes=10)

    class BrokenApi:
        async def snapshots(self, syms): raise RuntimeError("boom")

    orb = OrbStrategy(BrokenApi(), CFG, Journal(tmp_path), Sess(), None, None, root=tmp_path)
    orb.stage = "pooled"
    orb.pool = {"NVDA": {"source": "static"}}
    for _ in range(5):
        asyncio.run(orb.step())
    assert orb.stage == "done" and orb.failures == 3


def test_company_name_matching():
    assets = {
        "HD": {"name": "Home Depot, Inc."}, "NUTX": {"name": "Nutex Health Inc. Common Stock"},
        "GOOGL": {"name": "Alphabet Inc. Class A Common Stock"}, "GOOG": {"name": "Alphabet Inc. Class C Capital Stock"},
        "T": {"name": "AT&T Inc."}, "JD": {"name": "JD.com, Inc. American Depositary Shares"},
        "SOXL": {"name": "Direxion Daily Semiconductor Bull 3X ETF"},
        "XYZW": {"name": "Xyz Acquisition Corp. Warrant"}, "TME": {
            "name": "Tencent Music Entertainment Group American Depositary Shares, each representing two Class A Ordinary Shares"},
    }
    idx = NameIndex(assets)
    assert idx.lookup("The Home Depot") == "HD"
    assert idx.lookup("Nutex Health, Inc.") == "NUTX"
    assert idx.lookup("Alphabet Inc.") == "GOOG"            # one ticker per company
    assert idx.lookup("AT&T Inc.") == "T"
    assert idx.lookup("Tencent Music Entertainment Group") == "TME"
    assert idx.lookup("Home Depot Supply Partners LLC") is None     # no partial matches
    assert idx.lookup("Xyz Acquisition Corp.") is None              # warrants are not the company's share
    assert idx.lookup("Direxion") is None and idx.lookup("") is None
    assert normalize("S&P Global Inc.") == "s and p global"


FEED = """<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item><title>The Home Depot Names New CEO</title><guid>https://x/hd-1.html</guid>
<pubDate>Tue, 17 Nov 2026 10:43:00 +0000</pubDate><description>&lt;p&gt;ATLANTA -- The Home   Depot today announced...&lt;/p&gt;</description>
<dc:contributor>The Home Depot</dc:contributor><dc:language>en-US</dc:language></item>
<item><title>Empresa anuncia resultados</title><guid>https://x/es-1.html</guid>
<pubDate>Tue, 17 Nov 2026 10:40:00 +0000</pubDate><description>x</description>
<dc:contributor>The Home Depot</dc:contributor><dc:language>es</dc:language></item>
<item><title>Law firm reminds investors of Nutex Health (NASDAQ: NUTX) deadline</title><guid>https://x/law-1.html</guid>
<pubDate>Tue, 17 Nov 2026 10:30:00 +0000</pubDate><description>x</description>
<dc:contributor>Some Law Firm LLP</dc:contributor><dc:language>en-US</dc:language></item>
<item><title>Private company opens office</title><guid>https://x/p-1.html</guid>
<pubDate>Tue, 17 Nov 2026 10:20:00 +0000</pubDate><description>x</description>
<dc:contributor>Costero Brokers LLC</dc:contributor><dc:language>en-US</dc:language></item>
<item><title>Broken date</title><guid>https://x/b-1.html</guid><pubDate>not a date</pubDate></item>
</channel></rss>"""


def test_wire_feed_to_articles():
    assets = {"HD": {"name": "Home Depot, Inc."}, "NUTX": {"name": "Nutex Health Inc. Common Stock"}}
    idx = NameIndex(assets)
    items = parse_feed(FEED)
    assert len(items) == 4 and items[0]["summary"] == "ATLANTA -- The Home Depot today announced..."
    arts = [to_article(i, idx, assets, "prnewswire", 420) for i in items]
    assert [a["symbols"][0] if a else None for a in arts] == ["HD", None, "NUTX", None]   # es: skipped; law firm: ticker in title
    a = arts[0]
    assert a["created_at"] == "2026-11-17T10:43:00Z" and a["source"] == "prnewswire" and a["max_age_s"] == 420
    assert a["id"].startswith("prnewswire:") and a["headline"] == "The Home Depot Names New CEO"


# ── simulated broker ─────────────────────────────────────────────────────────
class FakeSession:
    def __init__(self):
        self.mins = 200
    def is_open(self): return True
    def to_close_min(self): return self.mins


class FakeUniverse:
    async def shortable(self, s): return True


class FakeApi:
    def __init__(self):
        self.orders, self.price, self.cancelled, self.closed = {}, 100.0, [], []
        self.acct = {"equity": "100000", "last_equity": "100000", "buying_power": "400000"}
    async def account(self): return self.acct
    async def submit(self, p):
        oid = f"o{len(self.orders)}"
        self.orders[oid] = {
            "id": oid, "status": "new", "filled_qty": "0", "filled_avg_price": None, "filled_at": None,
            "payload": p, "legs": [
                {"id": oid + "-tp", "type": "limit", "status": "held", "filled_avg_price": None, "filled_at": None},
                {"id": oid + "-sl", "type": "stop", "status": "held", "filled_avg_price": None, "filled_at": None},
            ],
        }
        return self.orders[oid]
    async def order(self, oid): return self.orders[oid]
    async def cancel(self, oid):
        self.cancelled.append(oid)
        for o in self.orders.values():
            if o["id"] == oid and o["status"] == "new":
                o["status"] = "canceled"
            for l in o.get("legs", []):
                if l["id"] == oid:
                    l["status"] = "canceled"
    async def close_position(self, sym):
        self.closed.append(sym)
        oid = f"c{len(self.orders)}"
        self.orders[oid] = {"id": oid, "status": "filled", "filled_avg_price": str(self.price),
                            "filled_at": iso(datetime.now(timezone.utc)), "legs": []}
        return self.orders[oid]
    async def snapshot(self, sym): return {"latestTrade": {"p": self.price}}
    async def positions(self): return []
    async def open_orders(self): return []
    def fill(self, oid, price, qty):
        o = self.orders[oid]
        o.update(status="filled", filled_qty=str(qty), filled_avg_price=str(price),
                 filled_at=iso(datetime.now(timezone.utc)))
        for l in o["legs"]:
            l["status"] = "new" if l["type"] == "limit" else "held"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    async def no_sleep(_): return None
    monkeypatch.setattr(ex_mod.asyncio, "sleep", no_sleep)
    api, sess = FakeApi(), FakeSession()
    ex = Executor(api, CFG, Journal(tmp_path), sess, FakeUniverse(), root=tmp_path)
    return api, sess, ex, tmp_path


SIG = {"strategy": "news", "symbol": "ACME", "side": "long", "price": 100.0, "stop": 99.0,
       "target_r": 2.0, "max_hold_min": 30, "meta": {"verdict": {"category": "earnings"}}}


def trades_file(tmp):
    p = tmp / "logs" / "trades.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_bracket_payload_and_target_exit(rig):
    api, sess, ex, tmp = rig
    async def go():
        assert await ex.enter(dict(SIG))
        p = api.orders["o0"]["payload"]
        assert p["order_class"] == "bracket" and p["qty"] == "500" and p["side"] == "buy"
        assert p["limit_price"] == "100.15" and p["take_profit"]["limit_price"] == "102.0"
        assert p["stop_loss"]["stop_price"] == "99.0"
        api.fill("o0", 100.1, 500)
        await ex.manage()
        assert ex.trades[next(iter(ex.trades))].status == "open"
        api.orders["o0"]["legs"][0].update(status="filled", filled_avg_price="102.0",
                                           filled_at=iso(datetime.now(timezone.utc)))
        await ex.manage()
    asyncio.run(go())
    t = trades_file(tmp)[0]
    assert not ex.trades and t["exit_reason"] == "target" and t["pnl"] == pytest.approx(950.0)
    assert t["r"] == pytest.approx(1.9) and t["slippage_pct"] == pytest.approx(0.1)


def test_short_stop_exit(rig):
    api, sess, ex, tmp = rig
    async def go():
        assert await ex.enter({**SIG, "side": "short", "stop": 101.0})
        p = api.orders["o0"]["payload"]
        assert p["side"] == "sell" and p["limit_price"] == "99.85" and p["take_profit"]["limit_price"] == "98.0"
        api.fill("o0", 99.9, 500)
        await ex.manage()
        api.orders["o0"]["legs"][1].update(status="filled", filled_avg_price="101.0",
                                           filled_at=iso(datetime.now(timezone.utc)))
        await ex.manage()
    asyncio.run(go())
    t = trades_file(tmp)[0]
    assert t["exit_reason"] == "stop" and t["pnl"] == pytest.approx(-550.0)


def test_unfilled_entry_is_cancelled(rig):
    api, sess, ex, tmp = rig
    async def go():
        await ex.enter(dict(SIG))
        t = next(iter(ex.trades.values()))
        t.submitted_at = iso(datetime.now(timezone.utc) - timedelta(seconds=60))
        await ex.manage()      # times out -> cancel
        await ex.manage()      # sees cancelled -> dropped
    asyncio.run(go())
    assert not ex.trades and "o0" in api.cancelled and not trades_file(tmp) and ex.trades_today == 0


def test_time_stop_and_eod_flatten(rig):
    api, sess, ex, tmp = rig
    async def go():
        await ex.enter(dict(SIG))
        api.fill("o0", 100.0, 500)
        await ex.manage()
        t = next(iter(ex.trades.values()))
        t.entry_time = iso(datetime.now(timezone.utc) - timedelta(minutes=31))
        api.price = 100.4
        await ex.manage()
        await ex.enter({**SIG, "symbol": "BETA", "strategy": "orb", "max_hold_min": 0})
        api.fill("o2", 100.0, 500)
        await ex.manage()
        sess.mins = 5          # inside the flatten window
        api.price = 99.7
        await ex.manage()
    asyncio.run(go())
    rows = trades_file(tmp)
    assert [r["exit_reason"] for r in rows] == ["time", "eod"]
    assert rows[0]["pnl"] == pytest.approx(200.0) and rows[1]["pnl"] == pytest.approx(-150.0)
    assert set(api.closed) == {"ACME", "BETA"} and not ex.trades


def test_risk_limits(rig):
    api, sess, ex, tmp = rig
    async def go():
        assert await ex.enter(dict(SIG))
        assert not await ex.enter(dict(SIG))                       # same symbol
        api.acct["equity"] = "97900"                               # -2.1 % on the day
        assert not await ex.enter({**SIG, "symbol": "BETA"})
        api.acct["equity"] = "100000"
        ex._acct = (0, None)
        (tmp / "HALT").write_text("x")
        assert not await ex.enter({**SIG, "symbol": "GAMMA"})
    asyncio.run(go())
    reasons = [json.loads(l)["reason"] for l in next((tmp / "logs").glob("events-*.jsonl")).read_text().splitlines()
               if json.loads(l)["kind"] == "rejected"]
    assert reasons == ["already_in_symbol", "daily_loss_limit", "halt_file"]


def test_state_survives_restart(rig):
    api, sess, ex, tmp = rig
    asyncio.run(ex.enter(dict(SIG)))
    ex2 = Executor(api, CFG, Journal(tmp), sess, FakeUniverse(), root=tmp)
    ex2.load()
    assert list(ex2.trades) == list(ex.trades)
    assert ex2.trades_today == 1 and ("news", "ACME") in ex2.done_today
    ex2.start_day(ex.day)                       # restart on the same day keeps the counters
    assert ex2.trades_today == 1
    ex2.start_day("2099-01-01")                 # a new day resets them
    assert ex2.trades_today == 0 and not ex2.done_today


def test_weekly_stats(tmp_path):
    logs = tmp_path / "logs"
    (logs / "daily").mkdir(parents=True)
    rows = [
        {"strategy": "news", "symbol": "A", "side": "long", "pnl": 300, "r": 1.5, "hold_min": 12,
         "slippage_pct": 0.05, "notional": 50000, "exit_reason": "target", "entry_time": "2026-11-17T15:00:00Z",
         "meta": {"verdict": {"category": "earnings"}, "news_to_signal_s": 40}},
        {"strategy": "orb", "symbol": "B", "side": "short", "pnl": -100, "r": -1.0, "hold_min": 50,
         "slippage_pct": 0.1, "notional": 40000, "exit_reason": "stop", "entry_time": "2026-11-18T15:00:00Z",
         "meta": {"with_gap": True}},
        {"strategy": "orb", "symbol": "C", "side": "long", "pnl": 50, "r": 0.5, "hold_min": 5,
         "slippage_pct": 0.0, "notional": 1000, "exit_reason": "eod", "entry_time": "2026-11-25T15:00:00Z", "meta": {}},
    ]
    (logs / "trades.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (logs / "events-2026-11-17.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"kind": "rejected", "stage": "confirm", "reason": "volume"},
        {"kind": "error", "where": "news_ws"},
        {"kind": "news_classified", "llm_s": 2.0, "verdict": {"direction": "up", "surprise": 8}},
    ]))
    (logs / "daily" / "2026-11-17.json").write_text(json.dumps(
        {"day": "2026-11-17", "equity_start": 100000, "equity_end": 100300, "pnl": 300, "trades": 1, "errors": 1}))
    start, end = week_of(datetime(2026, 11, 18).date())
    r = build(tmp_path, start, end)
    assert r["all"]["n"] == 2 and r["all"]["pnl"] == 200 and r["all"]["profit_factor"] == 3.0
    assert r["by_strategy"]["news"]["win_rate_pct"] == 100.0
    assert r["rejections"] == {"confirm:volume": 1} and r["errors"] == {"news_ws": 1}
    assert r["news"]["tradeable_verdicts"] == 1 and r["orb"]["with_gap"]["n"] == 1
    assert "Wochenstatistik 2026-11-16 bis 2026-11-20" in markdown(r)
    assert default_week(datetime(2026, 11, 21).date()) == (start, end)          # Saturday -> same week
    assert default_week(datetime(2026, 11, 18).date())[0].isoformat() == "2026-11-09"
