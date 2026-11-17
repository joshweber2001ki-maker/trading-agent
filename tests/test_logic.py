"""Logic tests with a simulated broker. Run: python -m pytest tests -q"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from bot import executor as ex_mod
from bot.common import ET, Journal, iso, load_config, parse_ts
from bot.executor import Executor, position_size
from bot.llm import is_noise, parse_verdict
from bot.market import summarize_daily
from bot.news_strategy import confirmation, outcome, trade_plan, vol_factor
from bot.orb_strategy import OrbStrategy, assess, breakout, opening_range, relative_volume
from bot.orb_strategy import stop_distance_pct as orb_stop_pct
from bot.wires import NameIndex, normalize, parse_feed, to_article
from tools.guard import due
from tools.weekly_stats import build, default_week, markdown, week_of

CFG = load_config()
ORB_ON = {**CFG, "orb": {**CFG["orb"], "enabled": True}}   # the strategy's logic is tested whether or not it is switched on


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


def test_verdict_from_damaged_answers():
    cut_off = '{"symbol": "NKE", "direction": "none", "surprise": 2, "impact": 0, "confidence": 6, "category": "product", "reason": "Routine product showcase with upda'
    v = parse_verdict(cut_off, ["NKE"])
    assert v["direction"] == "none" and v["surprise"] == 2 and v["impact"] == 0 and v["confidence"] == 6
    assert v["reason"].startswith("Routine product showcase")
    quotes = '{"symbol": "TRIO", "direction": "up", "surprise": 7, "impact": 2, "confidence": 8, "category": "m&a", "reason": "The "buffered" fund is unrelated"}'
    v = parse_verdict(quotes, ["TRIO"])
    assert v["direction"] == "up" and v["impact"] == 2 and v["category"] == "m&a"
    trailing = '{"symbol": "A", "direction": "down", "surprise": 8, "impact": 1, "confidence": 7} Note: {see above}'
    assert parse_verdict(trailing, ["A"])["direction"] == "down"
    assert parse_verdict('{"symbol": "A", "direction": "up", "surp', ["A"]) is None     # rating itself incomplete
    assert parse_verdict('[1, 2]', ["A"]) is None


def test_verdict_impact():
    v = parse_verdict('{"symbol":"HD","direction":"up","surprise":8,"impact":2.4,"confidence":7}', ["HD"])
    assert v["impact"] == 2
    assert parse_verdict('{"symbol":"HD","direction":"up","impact":9}', ["HD"])["impact"] == 3
    assert parse_verdict('{"symbol":"HD","direction":"up","surprise":8}', ["HD"])["impact"] == 1   # field missing


def test_stock_size_and_swing():
    assert summarize_daily([]) is None
    bars = [{"h": 102.0, "l": 100.0, "c": 100.0, "v": 15_000_000, "vw": 101.0} for _ in range(30)]
    st = summarize_daily(bars)
    assert st["size"] == "very large" and st["daily_range_pct"] == 2.0 and st["dollar_volume"] == 1_515_000_000
    small = summarize_daily([{"h": 5.4, "l": 5.0, "c": 5.0, "v": 100_000} for _ in range(10)])
    assert small["size"] == "small" and small["daily_range_pct"] == 8.0
    n = CFG["news"]
    assert vol_factor(st, n) == pytest.approx(0.8)          # 2.0 % against the 2.5 % reference
    assert vol_factor(small, n) == 2.0                      # capped
    assert vol_factor({"daily_range_pct": 0.5}, n) == 0.6   # floored
    assert vol_factor(None, n) == 1.0


def test_trade_plan_by_impact():
    n = CFG["news"]
    calm = [{"h": 100.1, "l": 100.0}] * 5                   # 0.1 % range: no noise worth mentioning
    assert trade_plan(1, 1.0, calm, 100, n) == (0.5, 2.0, 30)
    assert trade_plan(2, 1.0, calm, 100, n) == (1.0, 3.0, 60)
    assert trade_plan(3, 1.0, calm, 100, n) == (2.0, 3.0, 90)
    assert trade_plan(2, 2.0, calm, 100, n)[0] == 2.0       # wild stock: stop twice as wide
    assert trade_plan(1, 0.6, calm, 100, n)[0] == pytest.approx(0.3)
    noisy = [{"h": 101.6, "l": 100.0}] * 5                  # 1.6 % range in the last minutes
    assert trade_plan(1, 1.0, noisy, 100, n)[0] == pytest.approx(0.8)   # stop moves outside the noise
    wild = [{"h": 106.0, "l": 100.0}] * 5
    assert trade_plan(1, 1.0, wild, 100, n)[0] == 1.0       # but noise at most doubles it
    assert trade_plan(3, 2.0, wild, 100, n)[0] == 4.0       # absolute cap
    assert trade_plan(0, 1.0, calm, 100, n) == trade_plan(1, 1.0, calm, 100, n)   # never called with 0 in practice


def test_outcome_after_news():
    t0 = datetime(2026, 11, 17, 15, 0, 30, tzinfo=timezone.utc)
    def bar(minute, c, h=None, l=None):
        t = datetime(2026, 11, 17, 14, 55, tzinfo=timezone.utc) + timedelta(minutes=minute)
        return {"t": iso(t), "c": c, "h": h or c, "l": l or c}
    bars = [bar(i, 100.0) for i in range(5)]                 # 14:55-14:59, all complete before t0
    bars += [bar(5, 100.5, h=100.8), bar(9, 101.0), bar(19, 102.0, h=103.0), bar(34, 101.5, l=99.0)]
    o = outcome(bars, t0, until=t0 + timedelta(minutes=40))
    assert o["ref_price"] == 100.0
    assert o["ret_5"] == 1.0 and o["ret_15"] == 2.0 and o["ret_30"] == 1.5
    assert o["ret_60"] is None                                # the hour is not over yet
    assert o["max_up"] == 3.0 and o["max_down"] == -1.0
    quiet = outcome(bars[:5], t0, until=t0 + timedelta(minutes=61))
    assert quiet["ret_30"] == 0.0 and quiet["max_up"] == 0.0  # no trades after the news = no move
    assert outcome(bars[5:], t0, until=t0 + timedelta(minutes=61)) is None   # no price from before the news


def test_guard_acts_only_shortly_before_the_close():
    clock = lambda left: {"is_open": True, "timestamp": "2026-11-17T15:00:00-05:00",
                          "next_close": iso(datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc) + timedelta(minutes=left))}
    assert not due(clock(30), 10) and not due(clock(8), 10)
    assert due(clock(7), 10) and due(clock(1), 10)
    assert not due({**clock(5), "is_open": False}, 10)


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

    orb = OrbStrategy(BrokenApi(), ORB_ON, Journal(tmp_path), Sess(), None, None, root=tmp_path)
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


def test_every_matched_release_is_logged_with_its_publication_time(tmp_path):
    from bot.wires import Wires

    class Resp:
        status_code, text, headers = 200, FEED, {}
        def raise_for_status(self): pass

    class Http:
        async def get(self, url, headers=None): return Resp()

    class Uni:
        cache = {"HD": {"name": "Home Depot, Inc."}, "NUTX": {"name": "Nutex Health Inc. Common Stock"}}

    got = []
    async def on_article(a): got.append(a["symbols"][0])

    async def run():
        w = Wires(CFG["wires"], Uni(), on_article, Journal(tmp_path))
        w.http = Http()
        await w.poll("https://example.invalid/feed.rss")
        await w.poll("https://example.invalid/feed.rss")      # the same items again: nothing new
        await asyncio.sleep(0)
        return w.stats
    stats = asyncio.run(run())
    rows = [json.loads(l) for f in (tmp_path / "logs").glob("events-*.jsonl") for l in f.read_text().splitlines()]
    items = [r for r in rows if r["kind"] == "wire_item"]
    assert sorted(got) == ["HD", "NUTX"] and stats["matched"] == 2 and stats["items"] == 4
    assert [(r["symbol"], r["published"]) for r in items][0] == ("HD", "2026-11-17T10:43:00Z") and len(items) == 2


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
        self.gone, self.manual = set(), {}     # positions closed by hand, and the orders that closed them
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
    async def position(self, sym): return None if sym in self.gone else {"symbol": sym}
    async def closed_orders(self, sym, after): return self.manual.get(sym, [])
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


def test_position_closed_by_hand_is_recorded_with_its_real_price(rig):
    api, sess, ex, tmp = rig
    async def go():
        await ex.enter({**SIG, "side": "short", "stop": 101.0})
        api.fill("o0", 100.0, 500)
        await ex.manage()
        for leg in api.orders["o0"]["legs"]:          # closing by hand cancels both legs ...
            leg["status"] = "canceled"
        api.gone.add("ACME")                          # ... and the position is gone
        api.manual["ACME"] = [
            {"id": "x9", "side": "sell", "status": "filled", "filled_avg_price": "99.0", "filled_at": None},   # not a closing order
            {"id": "x1", "side": "buy", "status": "filled", "filled_avg_price": "100.05",
             "filled_at": iso(datetime.now(timezone.utc))},
            {"id": "o0", "side": "sell", "status": "filled", "filled_avg_price": "100.0", "filled_at": None},  # our own entry
        ]
        await ex.manage()
        # legs gone but the position still there (partly filled entry): the software stop stays in charge
        await ex.enter({**SIG, "symbol": "BETA"})
        api.fill("o1", 100.0, 500)
        await ex.manage()
        for leg in api.orders["o1"]["legs"]:
            leg["status"] = "canceled"
        api.price = 98.9
        await ex.manage()
    asyncio.run(go())
    rows = trades_file(tmp)
    assert [(r["symbol"], r["exit_reason"]) for r in rows] == [("ACME", "external"), ("BETA", "stop_sw")]
    assert rows[0]["exit_price"] == 100.05 and rows[0]["pnl"] == pytest.approx(-25.0) and not ex.trades
    assert api.closed == ["BETA"]                     # nothing is sent for a position that no longer exists


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
        {"kind": "news_classified", "llm_s": 2.0, "verdict": {"direction": "up", "surprise": 8, "confidence": 7, "impact": 2}},
        {"kind": "news_outcome", "source": "prnewswire", "direction": "up", "surprise": 8, "confidence": 7, "impact": 2,
         "ret_5": 0.5, "ret_15": 1.0, "ret_30": 2.0, "ret_60": None, "max_up": 3.0, "max_down": -1.0},
        {"kind": "news_outcome", "source": "prnewswire", "direction": "down", "surprise": 8, "confidence": 7, "impact": 2,
         "ret_5": 0.2, "ret_15": 0.4, "ret_30": 1.0, "ret_60": 1.0, "max_up": 1.5, "max_down": -0.2},
        {"kind": "news_outcome", "source": "fiscal.ai", "direction": "none", "surprise": 2, "confidence": 5, "impact": 0,
         "ret_5": 0.0, "ret_15": 0.1, "ret_30": -0.1, "ret_60": 0.0, "max_up": 0.2, "max_down": -0.2},
        {"kind": "guard", "positions": ["ACME 10"], "orders": 2},
    ]))
    (logs / "daily" / "2026-11-17.json").write_text(json.dumps(
        {"day": "2026-11-17", "equity_start": 100000, "equity_end": 100300, "pnl": 300, "trades": 1, "errors": 1}))
    start, end = week_of(datetime(2026, 11, 18).date())
    r = build(tmp_path, start, end)
    assert r["all"]["n"] == 2 and r["all"]["pnl"] == 200 and r["all"]["profit_factor"] == 3.0
    assert r["by_strategy"]["news"]["win_rate_pct"] == 100.0
    assert r["rejections"] == {"confirm:volume": 1} and r["errors"] == {"news_ws": 1}
    assert r["news"]["tradeable_verdicts"] == 1 and r["orb"]["with_gap"]["n"] == 1
    rv = r["ratings_vs_price"]
    big = rv["by_impact"]["groß"]
    assert big["n"] == 2 and big["dir_move_30"] == 0.5 and big["hit_rate_30_pct"] == 50.0   # +2.0 and -1.0 in the predicted direction
    assert big["best_in_direction"] == 1.6 and big["worst_against"] == -1.25
    assert rv["by_impact"]["kaum"]["with_direction"] == 0 and rv["by_impact"]["kaum"]["abs_move_30"] == 0.1
    assert rv["tradeable"]["n"] == 2 and rv["by_source"]["prnewswire"]["n"] == 2
    assert r["guard_interventions"] == 1 and "KI-Bewertung gegen Kursverlauf" in markdown(r)
    assert "Wochenstatistik 2026-11-16 bis 2026-11-20" in markdown(r)
    assert default_week(datetime(2026, 11, 21).date()) == (start, end)          # Saturday -> same week
    assert default_week(datetime(2026, 11, 18).date())[0].isoformat() == "2026-11-09"


def test_outcomes_are_measured_after_the_close_from_the_event_log(tmp_path):
    from bot.news_strategy import NewsStrategy
    t = datetime.now(timezone.utc)

    class Sess:
        day = t.astimezone(ET).strftime("%Y-%m-%d")
        open, close = t - timedelta(hours=4), t - timedelta(minutes=30)

    t0 = (Sess.open + timedelta(minutes=30)).replace(second=30, microsecond=0)   # article received
    first = t0.replace(second=0) - timedelta(minutes=5)

    def bar(minute, c):
        return {"t": iso(first + timedelta(minutes=minute)), "c": c, "h": c, "l": c}
    bars = [bar(i, 100.0) for i in range(5)] + [bar(5, 101.0), bar(20, 102.0), bar(45, 103.0)]

    class Api:
        feed = "iex"
        def __init__(self): self.feeds, self.fail_sip = [], False
        async def bars_multi(self, symbols, timeframe, start, end, feed=None):
            self.feeds.append(feed)
            if feed == "sip" and self.fail_sip:
                raise RuntimeError("subscription does not permit querying recent SIP data")
            return {s: bars for s in symbols if s == "ACME"}

    def run(root, fail_sip=False):
        j, api = Journal(root), Api()
        api.fail_sip = fail_sip
        v = {"direction": "up", "surprise": 8, "impact": 2, "confidence": 7, "category": "contract"}
        j.event("news_classified", article="a1", symbol="ACME", source="prnewswire", phase="live",
                received=iso(t0), verdict=v, size="small", daily_range_pct=3.1)
        j.event("news_classified", article="a2", symbol="ACME", source="fiscal.ai", phase="premarket",
                received=iso(t0), verdict=v)                      # not rated during the session
        j.event("news_classified", article="a3", symbol="QUIET", source="fiscal.ai", phase="live",
                received=iso(t0), verdict=v)                      # no prices at all: nothing to measure
        log = root / "logs" / f"events-{Sess.day}.jsonl"
        with open(log, "a") as f:                                 # a line as the previous version wrote it
            f.write(json.dumps({"t": iso(t0 + timedelta(seconds=2)), "kind": "news_classified", "article": "old",
                                "symbol": "ACME", "source": "fiscal.ai", "phase": "live", "llm_s": 2.0,
                                "verdict": {"direction": "down", "surprise": 7, "confidence": 6}}) + "\n")
            f.write('{"t": "half-written line\n')
        news = NewsStrategy(api, CFG, j, Sess, None, None, None)
        first_run, second_run = asyncio.run(news.log_outcomes()), asyncio.run(news.log_outcomes())
        rows = [json.loads(l) for l in log.read_text().splitlines() if l.startswith('{"t": "2')]
        return api, first_run, second_run, [r for r in rows if r["kind"] == "news_outcome"], rows

    api, n1, n2, out, _ = run(tmp_path / "a")
    assert (n1, n2) == (2, 0) and len(out) == 2                    # a restart measures nothing twice
    o = next(r for r in out if r["article"] == "a1")
    assert o["feed"] == "sip" and set(api.feeds) == {"sip"} and o["received"] == iso(t0)
    assert (o["ref_price"], o["ret_5"], o["ret_15"], o["ret_30"], o["ret_60"]) == (100.0, 1.0, 2.0, 2.0, 3.0)
    assert o["impact"] == 2 and o["size"] == "small" and o["source"] == "prnewswire"
    old = next(r for r in out if r["article"] == "old")
    assert old["ret_60"] == 3.0 and old["impact"] is None and old["direction"] == "down"

    api, n1, _, out, rows = run(tmp_path / "b", fail_sip=True)       # whole-market data refused
    assert n1 == 2 and {r["feed"] for r in out} == {"iex"} and api.feeds[:2] == ["sip", None]
    assert any(r["kind"] == "error" and r["where"] == "news_outcomes_sip" for r in rows)
