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


def test_day_result_is_measured_from_the_previous_close():
    from bot.main import day_result
    # restarted during the day after a loss: the bot's own first reading (99,650) must not be the start
    r = day_result({"equity": "99600", "last_equity": "100000"}, bot_start_equity=99650.0)
    assert r == {"equity_start": 100000.0, "equity_end": 99600.0, "pnl": -400.0, "basis": "last_close"}
    r = day_result({"equity": "99600"}, bot_start_equity=99650.0)                 # broker gives no previous close
    assert r["pnl"] == -50.0 and r["basis"] == "bot_start"
    assert day_result({"equity": "99600", "last_equity": "0"})["pnl"] == 0.0


def test_weekly_stats_put_right_a_wrong_record_and_a_restarted_day(tmp_path):
    logs = tmp_path / "logs"
    (logs / "daily").mkdir(parents=True)
    (tmp_path / "reports").mkdir()
    base = {"strategy": "orb", "side": "short", "slippage_pct": 0.0, "notional": 100000, "meta": {}}
    (logs / "trades.jsonl").write_text("\n".join(json.dumps(r) for r in [
        # closed by hand at 142.58, but booked by the bot as a stop at an estimated 143.24
        dict(base, id="orb-PEP-1", symbol="PEP", qty=715, signal_price=142.63, entry_price=142.53, stop_price=143.21,
             exit_price=143.24, entry_time="2026-11-17T14:40:55Z", exit_time="2026-11-17T14:50:26Z", hold_min=9.5,
             pnl=-507.65, r=-1.22, exit_reason="stop_sw_unconfirmed"),
        dict(base, id="orb-JPM-1", symbol="JPM", qty=308, signal_price=332.39, entry_price=332.38, stop_price=333.39,
             exit_price=333.56, entry_time="2026-11-17T14:37:47Z", exit_time="2026-11-17T14:38:53Z", hold_min=1.1,
             pnl=-364.06, r=-1.18, exit_reason="stop"),
        dict(base, id="orb-XYZ-1", symbol="XYZ", qty=10, signal_price=50.0, entry_price=50.0, stop_price=51.0,
             exit_price=50.5, entry_time="2026-11-18T15:00:00Z", exit_time="2026-11-18T15:30:00Z", hold_min=30.0,
             pnl=-5.0, r=-0.5, exit_reason="time_unconfirmed"),
    ]))
    (tmp_path / "reports" / "trade-corrections.jsonl").write_text(json.dumps(
        {"id": "orb-PEP-1", "exit_price": 142.58, "exit_time": "2026-11-17T14:41:09Z", "exit_reason": "external",
         "note": "Von Hand geschlossen."}) + "\n" + json.dumps({"id": "orb-JPM-1", "note": "ohne Kurs: wird ignoriert"}))
    (logs / "events-2026-11-17.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"t": "2026-11-17T10:38:17Z", "kind": "orphan", "symbol": "AAPL"},
        {"t": "2026-11-17T10:38:24Z", "kind": "day_start", "equity": 102481.39},
        {"t": "2026-11-17T11:07:10Z", "kind": "orphan", "symbol": "AAPL"},          # same position, next restart
        {"t": "2026-11-17T14:45:10Z", "kind": "day_start", "equity": 102068.67},    # restart after both losses
    ]))
    # written by the old version: measured from the restart, so the day looks flat
    (logs / "daily" / "2026-11-17.json").write_text(json.dumps(
        {"day": "2026-11-17", "equity_start": 102068.67, "equity_end": 102068.67, "pnl": 0.0, "trades": 2, "errors": 0}))
    # written by the new version: measured from the previous close, taken as it is
    (logs / "events-2026-11-18.jsonl").write_text(json.dumps({"t": "2026-11-18T05:00:00Z", "kind": "day_start", "equity": 102070.0}))
    (logs / "daily" / "2026-11-18.json").write_text(json.dumps(
        {"day": "2026-11-18", "equity_start": 102068.67, "equity_end": 102063.67, "pnl": -5.0, "basis": "last_close",
         "trades": 1, "errors": 0}))
    r = build(tmp_path, *week_of(datetime(2026, 11, 17).date()))
    pep = next(t for t in r["worst"] if t["symbol"] == "PEP")
    assert pep["pnl"] == -35.75 and pep["r"] == -0.09 and pep["exit_reason"] == "external" and pep["hold_min"] == 0.2
    assert pep["recorded"]["pnl"] == -507.65 and [t["symbol"] for t in r["corrected"]] == ["PEP"]
    assert r["all"]["pnl"] == round(-35.75 - 364.06 - 5.0, 2) and r["all"]["exit_reasons"]["external"] == 1
    assert [t["symbol"] for t in r["unconfirmed"]] == ["XYZ"]                       # still open to checking
    days = {d["day"]: d["pnl"] for d in r["equity"]["by_day"]}
    assert days == {"2026-11-17": round(102068.67 - 102481.39, 2), "2026-11-18": -5.0}
    assert r["equity"]["start"] == 102481.39 and r["orphans"] == 1
    # a later start on the same day logged the broker's value at the previous close: that is the better start
    with open(logs / "events-2026-11-17.jsonl", "a") as f:
        f.write("\n" + json.dumps({"t": "2026-11-17T21:07:19Z", "kind": "day_start", "equity": 102068.67, "last_equity": 102540.06}))
    again = build(tmp_path, *week_of(datetime(2026, 11, 17).date()))["equity"]
    assert again["start"] == 102540.06 and again["by_day"][0]["pnl"] == round(102068.67 - 102540.06, 2)
    md = markdown(r)
    assert "Nachträglich richtiggestellt: PEP" in md and "verbucht -507.65 $" in md and "tatsächlich -35.75 $" in md
    assert "Ausstieg nicht bestätigt" in md and "XYZ" in md and "2026-11-17: -412.72 $" in md


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


# ── measuring how fast prices react (no trading involved) ─────────────────────
def _sec_entry(acc, title, updated, items):
    summary = " &lt;b&gt;Filed:&lt;/b&gt; 2026-11-17 &lt;b&gt;AccNo:&lt;/b&gt; %s" % acc + "".join(
        "\n&lt;br&gt;Item %s: Something" % i for i in items)
    return ('<entry><title>%s</title><summary type="html">%s</summary><updated>%s</updated>'
            '<id>urn:tag:sec.gov,2008:accession-number=%s</id></entry>' % (title, summary, updated, acc))


SEC_FEED = ('<?xml version="1.0" encoding="ISO-8859-1" ?><feed xmlns="http://www.w3.org/2005/Atom">' + "".join([
    _sec_entry("A1", "8-K - ACME CORP (0000000011) (Filer)", "2026-11-17T09:25:18-05:00", ["2.02", "9.01"]),
    _sec_entry("A1", "8-K - ACME FINANCE LLC (0000000012) (Filer)", "2026-11-17T09:25:18-05:00", ["2.02", "9.01"]),
    _sec_entry("A2", "8-K/A - BETA INC (0000000013) (Filer)", "2026-11-17T09:20:00-05:00", ["5.02"]),
    _sec_entry("A3", "8-K - SOME LOAN TRUST 2026-1 (0000000014) (Filer)", "2026-11-17T09:10:00-05:00", ["8.01"]),
    _sec_entry("A4", "8-K - GAMMA &amp; CO (0000000015) (Filer)", "2026-11-17T09:05:00-05:00", ["1.01"]),
    "<entry><title>something else entirely</title><updated>2026-11-17T09:00:00-05:00</updated><id>x=A5</id></entry>",
]) + "</feed>").encode("latin-1")


def test_sec_filings_are_listed_once_with_ticker_and_acceptance_time(tmp_path, monkeypatch):
    from bot import edgar as edgar_mod
    from bot.edgar import Edgar, parse_feed as parse_sec

    entries = parse_sec(SEC_FEED)
    assert [e["acc"] for e in entries] == ["A1", "A1", "A2", "A3", "A4"]
    assert entries[0]["cik"] == 11 and entries[0]["items"] == ["2.02", "9.01"] and entries[2]["form"] == "8-K/A"
    assert entries[4]["company"] == "GAMMA & CO" and iso(entries[0]["accepted"]) == "2026-11-17T14:25:18Z"

    async def no_sleep(_): return None
    monkeypatch.setattr(edgar_mod.asyncio, "sleep", no_sleep)

    class Resp:
        def __init__(self, content=b"", data=None): self.content, self.data = content, data
        def raise_for_status(self): pass
        def json(self): return self.data

    class Http:
        def __init__(self): self.calls = []
        async def get(self, url, params=None):
            self.calls.append((url, (params or {}).get("start")))
            if url.endswith("company_tickers.json"):
                return Resp(data={"0": {"cik_str": 11, "ticker": "ACME"}, "1": {"cik_str": 13, "ticker": "BETA"},
                                  "2": {"cik_str": 15, "ticker": "GAM"}, "3": {"cik_str": 15, "ticker": "GAMMA"}})
            empty = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
            return Resp(content=SEC_FEED if params["start"] == 0 else empty)

    class Uni:
        cache = {"ACME": {}, "BETA": {}, "GAMMA": {}}      # GAM is not tradable

    def make(journal):
        e = Edgar(CFG["edgar"], "someone@example.org", Uni(), journal)
        e.http = Http()
        return e

    j = Journal(tmp_path)
    first = make(j)
    assert first.active and not Edgar(CFG["edgar"], "", Uni(), j).active       # no contact address: switched off
    assert asyncio.run(first.collect()) == 2 and asyncio.run(first.collect()) == 0
    assert first.stats["filings"] == 4 and first.stats["matched"] == 2
    rows = [r for r in j.recent_events(1) if r["kind"] == "sec_item"]
    assert [(r["symbol"], r["acc"], r["accepted"], r["items"]) for r in rows] == [
        ("GAMMA", "A4", "2026-11-17T14:05:00Z", ["1.01"]), ("ACME", "A1", "2026-11-17T14:25:18Z", ["2.02", "9.01"])]
    again = make(j)                                                             # after a restart
    assert asyncio.run(again.collect()) == 0
    assert len([r for r in j.recent_events(1) if r["kind"] == "sec_item"]) == 2


def test_sec_list_starts_as_soon_as_the_stock_list_is_loaded(tmp_path, monkeypatch):
    from bot import edgar as edgar_mod
    from bot.edgar import Edgar

    class Uni:
        cache = {}                                         # still loading when the bot has just started

    e = Edgar(CFG["edgar"], "someone@example.org", Uni(), Journal(tmp_path))
    e.in_hours = lambda: True
    looks, waits = [], []

    async def collect():
        looks.append(len(waits))

    async def sleep(seconds):
        waits.append(seconds)
        Uni.cache = {"ACME": {}}                           # loaded in the meantime
        if len(waits) == 3:
            raise asyncio.CancelledError

    e.collect = collect
    monkeypatch.setattr(edgar_mod.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(e.run())
    assert waits == [edgar_mod.STARTUP_WAIT_S, CFG["edgar"]["poll_s"], CFG["edgar"]["poll_s"]] and looks == [1, 2]


def test_price_path_around_an_announcement():
    from bot.speed import price_path, session_of
    t0 = datetime(2026, 11, 17, 15, 0, 0, tzinfo=timezone.utc)                  # 10:00 in New York
    at = lambda s, p, n: (t0 + timedelta(seconds=s), p, n)
    pre = [at(-2, 100.0, 100), at(-30, 99.5, 200)]
    post = [at(0.5, 100.2, 100), at(1.5, 100.5, 300), at(8, 101.0, 100), at(50, 102.0, 1000), at(200, 101.5, 100)]
    p = price_path(pre, post, t0)
    assert (p["price"], p["ref_age_s"], p["pre_move"], p["pre_span_s"], p["first_trade_s"]) == (100.0, 2, 0.503, 30, 0.5)
    assert [p[f"r{h}"] for h in (1, 2, 5, 10, 30, 60, 120, 300)] == [0.2, 0.5, 0.5, 1.0, 1.0, 2.0, 2.0, 1.5]
    assert (p["n_0_2"], p["usd_0_2"], p["n_2_10"], p["usd_10_60"], p["n_60_300"]) == (2, 40170, 1, 102000, 1)
    quiet = price_path(pre, [], t0)
    assert quiet["r300"] == 0.0 and quiet["first_trade_s"] is None              # nobody traded: no move
    assert price_path([], post, t0) is None                                     # never traded before: nothing to compare with
    assert price_path([at(-7000, 100.0, 1)], post, t0)["ref_age_s"] == 7000     # thin stock: old reference, flagged by its age
    part = price_path(pre, post[:3], t0, covered_s=9)                           # busy stock, data cut off after 9 s
    assert part["r5"] == 0.5 and part["r10"] is None and part["usd_0_2"] == 40170 and part["n_2_10"] is None
    ny = lambda h, m, d=17: session_of(datetime(2026, 11, d, h, m, tzinfo=ET))
    assert [ny(3, 59), ny(4, 0), ny(9, 29), ny(9, 30), ny(15, 59), ny(16, 0), ny(19, 59), ny(20, 0), ny(10, 0, 21)] == [
        "closed", "pre", "pre", "regular", "regular", "post", "post", "closed", "closed"]


def test_speed_measurement_runs_once_per_announcement(tmp_path):
    from bot.speed import Speed
    t0 = datetime.now(timezone.utc).replace(hour=15, minute=0, second=0, microsecond=0)   # 10:00 or 11:00 New York
    while t0.weekday() >= 5 or t0 > datetime.now(timezone.utc) - timedelta(hours=2):
        t0 -= timedelta(days=1)
    night = t0.replace(hour=6)                                                  # 1:00 or 2:00 New York: no trading

    class Api:
        def __init__(self): self.calls = 0
        async def trades(self, sym, start, end, limit=10000, sort="asc", feed="sip"):
            self.calls += 1
            a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
            if sym == "DEAD":
                return [], False
            tr = lambda s, p: {"t": (t0 + timedelta(seconds=s)).isoformat().replace("+00:00", "Z"), "p": p, "s": 100, "c": ["@"]}
            every = [tr(-5, 50.0), tr(1, 50.5), tr(40, 51.0), {**tr(41, 99.0), "c": ["Z"]}, tr(600, 51.5)]
            hit = [x for x in every if a <= parse_ts(x["t"]) <= b]
            return (hit[::-1] if sort == "desc" else hit)[:limit], False

        async def bars_multi(self, symbols, timeframe, start, end, feed=None):
            self.calls += 1
            bar = lambda minute, lo, hi, c: {"t": iso(t0 + timedelta(minutes=minute)), "l": lo, "h": hi, "c": c}
            return {"ACME": [bar(-1, 49.0, 50.0, 50.0), bar(1, 50.5, 51.0, 51.0), bar(10, 51.0, 52.0, 51.5)]}

        async def quotes(self, sym, start, end, limit=1, sort="desc", feed="sip"):
            self.calls += 1
            if self.no_quotes:
                raise RuntimeError("403 subscription does not permit querying quotes")
            at = datetime.fromisoformat(end)
            return [{"t": iso(at - timedelta(seconds=2)), "bp": 50.4, "ap": 50.6, "bs": 3, "as": 1}]

        no_quotes = False

    class Uni:
        cache = {"ACME": {}, "DEAD": {}}

    class Stats:
        async def get(self, symbols): return {"ACME": {"size": "small", "dollar_volume": 5000000}}

    j, api = Journal(tmp_path), Api()
    j.event("wire_item", id="w1", symbol="ACME", published=iso(t0), headline="Acme wins contract", source="prnewswire")
    j.event("wire_item", id="w1", symbol="ACME", published=iso(t0), headline="Acme wins contract", source="prnewswire")  # logged again after a restart
    j.event("wire_item", id="w2", symbol="DEAD", published=iso(t0), headline="x", source="prnewswire")
    j.event("wire_item", id="w3", symbol="GONE", published=iso(t0), headline="x", source="prnewswire")   # not tradable
    j.event("sec_item", acc="S1", symbol="ACME", accepted=iso(night), items=["2.02"])
    j.event("sec_item", acc="S2", symbol="ACME", accepted=iso(datetime.now(timezone.utc)), items=["8.01"])  # too fresh
    sp = Speed(api, CFG["speed"], j, Uni(), Stats())
    assert asyncio.run(sp.run()) == 1
    calls = api.calls
    assert asyncio.run(sp.run()) == 0 and api.calls == calls                    # nothing is measured twice
    rows = {r["ref"]: r for r in j.recent_events(1) if r["kind"] == "news_speed"}
    assert set(rows) == {"w1", "w2", "S1"}
    r = rows["w1"]
    assert (r["src"], r["symbol"], r["session"], r["size"], r["price"]) == ("prnewswire", "ACME", "regular", "small", 50.0)
    assert (r["r1"], r["r30"], r["r60"], r["r300"], r["r900"], r["r1800"]) == (1.0, 1.0, 2.0, 2.0, 3.0, 3.0)
    assert rows["w2"]["skipped"] == "no_trade_before" and rows["S1"]["skipped"] == "market_closed"
    done = [x for x in j.recent_events(1) if x["kind"] == "speed_done"]
    assert len(done) == 1 and (done[0]["due"], done[0]["measured"], done[0]["skipped"], done[0]["errors"]) == (3, 1, 2, 0)
    # the detailed path, once, for the one announcement in a stock that was trading
    paths = [x for x in j.recent_events(1) if x["kind"] == "news_path"]
    assert len(paths) == 1 and (paths[0]["ref"], paths[0]["price"]) == ("w1", 50.0)
    p5 = paths[0]["p5"]
    assert len(p5) == 60 and (p5[0], p5[6], p5[7], p5[-1]) == (1.0, 1.0, 2.0, 2.0)      # after 5 s, 35 s, 40 s, 300 s
    assert paths[0]["bars"] == [[60, 1.0, 2.0, 2.0], [600, 2.0, 4.0, 3.0]]             # the minute before t0 is left out
    assert (paths[0]["bid_5"], paths[0]["ask_5"], paths[0]["ask_size_60"], paths[0]["quote_age_5"]) == (50.4, 50.6, 1, 2.0)
    pd = [x for x in j.recent_events(1) if x["kind"] == "path_done"]
    assert len(pd) == 1 and (pd[0]["due"], pd[0]["done"], pd[0]["errors"], pd[0]["quotes"]) == (1, 1, 0, True)

    # quote history refused: one error is logged, the path is kept without bid and offer
    j2, api2 = Journal(tmp_path / "noquotes"), Api()
    api2.no_quotes = True
    j2.event("wire_item", id="w1", symbol="ACME", published=iso(t0), headline="Acme wins contract", source="prnewswire")
    assert asyncio.run(Speed(api2, CFG["speed"], j2, Uni(), Stats()).run()) == 1
    rows2 = list(j2.recent_events(1))
    path = next(x for x in rows2 if x["kind"] == "news_path")
    assert len(path["p5"]) == 60 and "bid_5" not in path
    assert [x["where"] for x in rows2 if x["kind"] == "error"] == ["speed_quotes"]
    assert next(x for x in rows2 if x["kind"] == "path_done")["quotes"] is False


def test_speed_statistics_show_what_is_left_for_a_late_entry():
    from tools.weekly_stats import speed_stats
    rows = [
        {"r300": 2.0, "r2": 0.1, "r10": 0.5, "r60": 1.5, "r120": 1.8, "r1800": 2.5, "pre_move": 0.1, "usd_0_2": 1000, "usd_2_10": 4000},
        {"r300": -3.0, "r2": -1.0, "r10": -2.0, "r60": -3.5, "r120": -3.0, "r1800": -1.0, "pre_move": -0.4, "usd_0_2": 0, "usd_2_10": 2000},
        {"r300": 0.3, "r2": 0.0, "r10": 0.1, "r60": 0.2, "r120": 0.3, "r1800": 0.1, "pre_move": 0.0, "ref_age_s": 12},
        {"r300": 9.0, "r2": 9.0, "r10": 9.0, "r60": 9.0, "r120": 9.0, "r1800": 9.0, "pre_move": 0.0, "ref_age_s": 40000},  # gap in a thin stock
        {"skipped": "no_trade_before"},
    ]
    s = speed_stats(rows)
    assert (s["n"], s["thin"], s["skipped"], s["movers"], s["move_300"]) == (3, 1, 1, 2, 3.0)
    assert (s["left_after_2s"], s["left_after_10s"], s["left_after_60s"], s["left_after_120s"]) == (2.0, 1.5, 0.5, 0.2)
    assert s["before_publication"] == 0.4 and s["after_30min"] == 2.5 and s["usd_first_10s"] == 5000
    assert s["liquid"]["movers"] == 0                              # 5,000 $ and 2,000 $ in five minutes: nothing to trade in
    # the same two movers with real volume: one by its dollars, one so busy that the trade list was cut off
    rows[0].update(usd_60_300=400000)
    rows[1].update(truncated=True)
    liquid = speed_stats(rows)["liquid"]
    assert (liquid["movers"], liquid["move_300"], liquid["left_after_60s"], liquid["usd_5min"]) == (2, 3.0, 0.5, 405000)
    assert speed_stats([])["n"] == 0 and speed_stats([])["liquid"]["movers"] == 0


# ── which model rates, and the models that rate along ────────────────────────
class ModelHttp:
    """Stands in for the Anthropic API: answers each call with the next prepared response."""
    def __init__(self, *responses):
        self.responses, self.bodies = list(responses), []

    async def post(self, url, headers=None, json=None):
        self.bodies.append(json)
        status, data, hdrs = self.responses.pop(0)

        class R:
            status_code, headers, text = status, hdrs, str(data)
            def json(self): return data
        return R()


ANSWER = '{"symbol": "ACME", "direction": "up", "surprise": 8, "impact": 2, "confidence": 7, "category": "guidance", "reason": "raised"}'


def test_every_model_gets_a_request_it_accepts():
    from bot.llm import Classifier
    llm = CFG["llm"]
    by_model = {m: Classifier("k", llm, model=m).request("x") for m in [llm["model"], *llm["shadow"]]}
    # Opus always thinks: "disabled" would be refused with an HTTP 400, and the thinking needs room.
    opus = by_model["claude-opus-5-5"]
    assert opus["thinking"] == {"type": "adaptive"} and opus["output_config"] == {"effort": "medium"}
    assert opus["max_tokens"] >= 2000
    # Sonnet and Haiku answer without thinking, each switched off in the way that model accepts.
    assert by_model["claude-sonnet-5-5"]["thinking"] == {"type": "between_tools"}
    assert by_model["claude-haiku-5-5"]["thinking"] == {"type": "disabled"}
    for m in ("claude-sonnet-5-5", "claude-haiku-5-5"):
        assert by_model[m]["max_tokens"] == 300 and "output_config" not in by_model[m]
    assert llm["fallback"] in llm["shadow"] and llm["model"] not in llm["shadow"]
    with pytest.raises(ValueError):                                # Opus cannot be told not to think
        Classifier("k", {**llm, "effort": {"claude-opus-5-5": "off"}}, model="claude-opus-5-5")
    with pytest.raises(ValueError):
        Classifier("k", {**llm, "effort": {"claude-opus-5-5": "maximal"}}, model="claude-opus-5-5")


def test_rating_reads_the_answer_behind_the_thinking_and_reports_time_and_tokens(monkeypatch):
    from bot import llm as llm_mod
    from bot.llm import Classifier
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
    monkeypatch.setattr(llm_mod.asyncio, "sleep", sleep)
    art = {"headline": "Acme raises guidance", "summary": "", "source": "test", "created_at": ""}
    ok = (200, {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": ANSWER}],
                "usage": {"input_tokens": 900, "output_tokens": 420}, "stop_reason": "end_turn"},
          {"anthropic-ratelimit-requests-limit": "4000"})

    async def go(*responses):
        c = Classifier("k", CFG["llm"], model="claude-opus-5-5")
        c.http = ModelHttp(*responses)
        return c, await c.rate(art, ["ACME"])

    c, res = asyncio.run(go(ok))
    assert res["verdict"]["direction"] == "up" and res["error"] is None and res["model"] == "claude-opus-5-5"
    assert (res["tokens_in"], res["tokens_out"]) == (900, 420) and res["limits"]["requests"] == "4000"
    assert res["seconds"] is not None and c.day_calls == 1
    # busy server: one more try after the wait the server asks for (capped), then the answer counts
    c, res = asyncio.run(go((529, {"error": "overloaded"}, {"retry-after": "30"}), ok))
    assert res["verdict"] and len(c.http.bodies) == 2 and waits == [5.0]
    # refused twice: an error, no verdict, and no third try
    c, res = asyncio.run(go((429, {"error": "rate"}, {}), (429, {"error": "rate"}, {})))
    assert res["verdict"] is None and res["error"].startswith("llm_http_429") and len(c.http.bodies) == 2
    # a wrong request is not retried
    c, res = asyncio.run(go((400, {"error": "thinking.type.disabled is not supported"}, {})))
    assert res["error"].startswith("llm_http_400") and len(c.http.bodies) == 1
    # the thinking used up all the room: no answer
    c, res = asyncio.run(go((200, {"content": [{"type": "thinking", "thinking": ""}], "stop_reason": "max_tokens"}, {})))
    assert res["verdict"] is None and res["error"].startswith("llm_cut_off")
    # the old interface still works
    async def old_way():
        c = Classifier("k", CFG["llm"])
        c.http = ModelHttp(ok)
        return await c.classify(art, ["ACME"])
    verdict, err = asyncio.run(old_way())
    assert verdict["surprise"] == 8 and err is None


class FakeModel:
    """A model with a fixed opinion (or a fixed failure)."""
    def __init__(self, model, direction="up", error=None, surprise=8):
        self.model, self.direction, self.error, self.surprise, self.day_calls = model, direction, error, surprise, 0

    async def rate(self, art, symbols, names=None, stats=None):
        self.day_calls += 1
        v = None if self.error else {"symbol": symbols[0], "direction": self.direction, "surprise": self.surprise,
                                     "impact": 2, "confidence": 7, "category": "guidance", "reason": ""}
        return {"verdict": v, "error": self.error, "model": self.model, "seconds": 1.5, "tokens_in": 900, "tokens_out": 80}


def news_rig(tmp_path, decider, shadows=(), fallback=None):
    from bot.news_strategy import NewsStrategy

    class Sess:
        def is_open(self): return True
        def since_open_min(self): return 60
        def to_close_min(self): return 200

    class Uni:
        async def asset(self, s): return {"symbol": s}
        async def name(self, s): return "Acme Corp"

    class Api:
        async def snapshots(self, syms): return {s: {"latestTrade": {"p": 100.0}} for s in syms}

    class Ex:
        async def can_enter(self, strategy, sym): return True, ""

    j = Journal(tmp_path)
    news = NewsStrategy(Api(), CFG, j, Sess(), Uni(), Ex(), decider, shadows=shadows, fallback=fallback)
    art = {"id": "a1", "headline": "Acme raises guidance", "summary": "", "source": "prnewswire",
           "symbols": ["ACME"], "created_at": iso(datetime.now(timezone.utc))}

    async def go():
        plan = await news._classify(art, datetime.now(timezone.utc))
        await asyncio.sleep(0.01)                                  # let the models that rate along finish
        return plan
    plan = asyncio.run(go())
    rows = list(j.recent_events(1))
    return plan, {k: [r for r in rows if r["kind"] == k] for k in
                  ("news_classified", "news_shadow", "news_fallback", "news_skipped")}


def test_models_in_the_shadow_rate_along_but_never_decide(tmp_path):
    haiku, sonnet = FakeModel("claude-haiku-5-5", "up"), FakeModel("claude-sonnet-5-5", "down")
    plan, ev = news_rig(tmp_path, FakeModel("claude-opus-5-5", "none"), [haiku, sonnet], "claude-haiku-5-5")
    assert plan is None                                            # the deciding model saw nothing to trade
    c = ev["news_classified"][0]
    assert (c["model"], c["verdict"]["direction"], c["model_s"], c["tokens_out"]) == ("claude-opus-5-5", "none", 1.5, 80)
    shadow = {r["model"]: r for r in ev["news_shadow"]}
    assert set(shadow) == {"claude-haiku-5-5", "claude-sonnet-5-5"} and not ev["news_fallback"]
    assert shadow["claude-haiku-5-5"]["verdict"]["direction"] == "up" and shadow["claude-sonnet-5-5"]["symbol"] == "ACME"
    assert all(r["article"] == "a1" and "error" not in r for r in ev["news_shadow"])
    # and the other way round: only the deciding model's rating makes a candidate
    plan, ev = news_rig(tmp_path / "b", FakeModel("claude-opus-5-5", "up"), [FakeModel("claude-haiku-5-5", "none")])
    assert plan[0] == "ACME" and plan[1]["direction"] == "up" and plan[4]["model"] == "claude-opus-5-5"


def test_fallback_model_decides_only_when_the_main_model_fails(tmp_path):
    broken = FakeModel("claude-opus-5-5", error="llm_http_529: overloaded")
    plan, ev = news_rig(tmp_path, broken, [FakeModel("claude-haiku-5-5", "up"), FakeModel("claude-sonnet-5-5", "down")],
                        "claude-haiku-5-5")
    assert plan[1]["direction"] == "up" and plan[4]["model"] == "claude-haiku-5-5"
    fb = ev["news_fallback"][0]
    assert (fb["failed"], fb["used"]) == ("claude-opus-5-5", "claude-haiku-5-5") and "529" in fb["error"]
    assert ev["news_classified"][0]["model"] == "claude-haiku-5-5" and len(ev["news_shadow"]) == 2
    # no fallback named, or the fallback failed as well: the article is skipped with the error
    plan, ev = news_rig(tmp_path / "b", broken, [FakeModel("claude-haiku-5-5", "up")])
    assert plan is None and "529" in ev["news_skipped"][0]["reason"] and not ev["news_classified"]
    plan, ev = news_rig(tmp_path / "c", broken, [FakeModel("claude-haiku-5-5", error="llm_network: timeout")], "claude-haiku-5-5")
    assert plan is None and ev["news_skipped"] and ev["news_shadow"][0]["error"].startswith("llm_network")


def test_weekly_stats_compare_the_models_on_the_same_articles(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (tmp_path / "config.toml").write_text("[news]\nmin_surprise = 7\nmin_confidence = 6\nmin_impact = 1\n")
    O, H, S = "claude-opus-5-5", "claude-haiku-5-5", "claude-sonnet-5-5"
    v = lambda d, s=8, i=2, c=7: {"symbol": "ACME", "direction": d, "surprise": s, "impact": i, "confidence": c}
    ev = []

    def rated(article, model, verdict, kind="news_shadow", tokens=(1000, 200), secs=3.0, **kw):
        ev.append({"t": "2026-11-18T15:00:00Z", "kind": kind, "article": article, "headline": "Headline " + article,
                   "phase": "live", "model": model, "symbol": verdict["symbol"] if verdict else None, "verdict": verdict,
                   "model_s": secs, "tokens_in": tokens[0], "tokens_out": tokens[1], **kw})

    for article, opus, haiku, sonnet, ret in (("a1", v("none", 0, 0), v("up"), v("up"), -1.5),      # two false alarms
                                              ("a2", v("up"), v("up", 7, 1, 6), v("none", 3, 0), 2.0),
                                              ("a3", v("none", 0, 0), v("none", 0, 0), v("none", 0, 0), 0.1)):
        rated(article, O, opus, "news_classified")
        rated(article, H, haiku, tokens=(1000, 90), secs=0.9)
        rated(article, S, sonnet, tokens=(1000, 100), secs=1.5)
        ev.append({"t": "2026-11-18T21:20:00Z", "kind": "news_outcome", "article": article, "symbol": "ACME",
                   "direction": opus["direction"], "ret_5": ret / 2, "ret_15": ret, "ret_30": ret, "ret_60": ret,
                   "max_up": abs(ret), "max_down": -abs(ret)})
    # Opus did not answer: Haiku's rating decided. Not an article that all models rated.
    ev.append({"t": "2026-11-18T15:05:00Z", "kind": "news_fallback", "article": "a4", "failed": O, "used": H, "error": "llm_http_529"})
    rated("a4", H, v("up"), "news_classified", tokens=(1000, 90), secs=0.9)
    rated("a4", H, v("up"), tokens=(1000, 90), secs=0.9)
    rated("a4", S, v("up"), tokens=(1000, 100), secs=1.5)
    # Sonnet failed on one: again not rated by all.
    rated("a5", O, v("none", 0, 0), "news_classified")
    rated("a5", H, v("none", 0, 0), tokens=(1000, 90), secs=0.9)
    rated("a5", S, None, tokens=(None, None), secs=20.0, error="llm_network: timeout")
    # a line from before the switch (no model named) is left out
    ev.append({"t": "2026-11-17T15:00:00Z", "kind": "news_classified", "article": "old", "phase": "live", "symbol": "ACME",
               "verdict": v("up"), "llm_s": 2.0})
    (logs / "events-2026-11-18.jsonl").write_text("\n".join(json.dumps(e) for e in ev))
    m = build(tmp_path, *week_of(datetime(2026, 11, 18).date()))["models"]
    assert list(m["models"]) == [O, H, S] and m["common"] == 3 and m["movers"] == 2 and m["differences"] == 2
    opus, haiku, sonnet = (m["models"][k] for k in (O, H, S))
    assert (opus["decided"], opus["ratings"], opus["failed"]) == (4, 4, 1)
    assert (haiku["decided"], haiku["ratings"], haiku["failed"]) == (1, 6, 0) and sonnet["failed"] == 1
    assert (opus["candidates"], haiku["candidates"], sonnet["candidates"]) == (1, 2, 1)
    assert (opus["movers_caught"], haiku["movers_caught"], sonnet["movers_caught"]) == (1, 1, 0)
    assert opus["candidates_vs_price"]["hit_rate_30_pct"] == 100.0 and haiku["candidates_vs_price"]["hit_rate_30_pct"] == 50.0
    assert sonnet["candidates_vs_price"]["dir_move_30"] == -1.5
    assert (opus["seconds_median"], haiku["seconds_median"], opus["tokens_out"]) == (3.0, 0.9, 200)
    assert opus["cents_per_rating"] == 0.8 and opus["cost"] == 0.03
    assert haiku["cents_per_rating"] == pytest.approx(0.0145, abs=0.001)      # 1000 x 0.10 $ + 90 x 0.50 $ per million
    assert [x["symbol"] for x in m["examples"]] == ["ACME", "ACME"] and m["examples"][0]["ret_30"] == 2.0
    assert m["examples"][1]["verdicts"] == {O: "none 0/0/7", H: "up 8/2/7", S: "up 8/2/7"}
    md = markdown(build(tmp_path, *week_of(datetime(2026, 11, 18).date())))
    assert "## Modellvergleich" in md and "uneinig bei 2 Meldungen" in md and f"{O} 1, {H} 2, {S} 1" in md


# ── the whole program against a simulated broker ─────────────────────────────
def test_bot_starts_lists_filings_and_measures_after_the_close_without_trading(tmp_path, monkeypatch):
    """Start, begin the day, list SEC filings, run both measurements after the close, write the
    status file. No strategy has anything to do here, so the account must not be touched."""
    from collections import Counter
    from bot import common as common_mod, main as main_mod, orb_strategy as orb_mod
    from bot.news_strategy import OUTCOME_DELAY_MIN

    for mod in (common_mod, main_mod, ex_mod, orb_mod):                   # logs and state go to the test folder
        monkeypatch.setattr(mod, "DATA", tmp_path)
    for name, value in (("APCA_API_KEY_ID", "test"), ("APCA_API_SECRET_KEY", "test"), ("SEC_CONTACT", "someone@example.org")):
        monkeypatch.setenv(name, value)
    for name in ("ANTHROPIC_API_KEY", "APCA_BASE_URL", "APCA_DATA_FEED"):
        monkeypatch.delenv(name, raising=False)

    t_now = datetime.now(timezone.utc)
    t0 = t_now.replace(hour=15, minute=0, second=0, microsecond=0)        # a filing during a regular session,
    while t0 > t_now - timedelta(hours=1) or t0.weekday() >= 5:           # old enough to be measured
        t0 -= timedelta(days=1)
    calls = []

    async def req(method, url, ok404=False, **kw):
        params = kw.get("params") or {}
        calls.append(method)
        if method != "GET":
            raise AssertionError(f"the bot tried to change the account: {method} {url}")
        if url.endswith("/calendar"):
            return [{"date": params["start"], "open": "09:30", "close": "16:00"}]
        if url.endswith("/account"):
            return {"equity": "100000", "last_equity": "100400", "buying_power": "400000"}
        if url.endswith("/assets"):
            return [{"symbol": sym, "class": "us_equity", "status": "active", "tradable": True, "exchange": "NYSE",
                     "name": sym.title() + " Corp", "shortable": True, "easy_to_borrow": True} for sym in ("ACME", "GAMMA")]
        if url.endswith("/positions") or url.endswith("/orders"):
            return []
        if url.endswith("/stocks/bars"):
            return {"bars": {}}
        if url.endswith("/quotes"):
            return {"quotes": [{"t": params["end"], "bp": 101.9, "ap": 102.1, "bs": 2, "as": 2}]}
        if url.endswith("/trades"):                                       # 100 before the filing, 102 from one second after
            start, end = parse_ts(params["start"]), parse_ts(params["end"])
            xs = [{"t": iso(t0 + timedelta(seconds=sec)), "p": 102.0 if sec > 0 else 100.0, "s": 100, "c": ["@"]}
                  for sec in (-30, -5, 1, 20, 200, 1000)]
            xs = [x for x in xs if start <= parse_ts(x["t"]) <= end]
            return {"trades": (xs[::-1] if params["sort"] == "desc" else xs)[:params["limit"]], "next_page_token": None}
        raise AssertionError(f"unexpected request: {method} {url}")

    stamp = t0.astimezone(ET).isoformat(timespec="seconds")
    feed = ('<?xml version="1.0" encoding="ISO-8859-1" ?><feed xmlns="http://www.w3.org/2005/Atom">'
            + _sec_entry("B1", "8-K - ACME CORP (0000000011) (Filer)", stamp, ["2.02"])
            + _sec_entry("B2", "8-K - GAMMA &amp; CO (0000000015) (Filer)", stamp, ["1.01"]) + "</feed>").encode("latin-1")

    class Resp:
        def __init__(self, content=b"", data=None): self.content, self.data = content, data
        def raise_for_status(self): pass
        def json(self): return self.data

    class SecHttp:
        async def get(self, url, params=None):
            if url.endswith("company_tickers.json"):
                return Resp(data={"0": {"cik_str": 11, "ticker": "ACME"}, "1": {"cik_str": 15, "ticker": "GAMMA"}})
            empty = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
            return Resp(content=feed if params["start"] == 0 else empty)

    async def scenario():
        b = main_mod.Bot()
        b.api._req = req
        b.edgar.http, b.edgar.in_hours = SecHttp(), lambda: True
        b.cfg["execution"]["poll_s"] = 0.02

        async def idle():
            await asyncio.sleep(3600)
        b.news_loop = b.wires.run = idle                                  # no news stream, no feed polling, no KI

        def seen(kind):
            return sum(1 for r in b.journal.recent_events(1) if r["kind"] == kind)

        async def until(kind, n):
            for _ in range(200):
                if seen(kind) >= n:
                    return
                await asyncio.sleep(0.05)
            raise AssertionError(f"no {kind} after 10 s: {[r for r in b.journal.recent_events(1)]}")

        await b.session.refresh()
        b.session.open = datetime.now(timezone.utc) - timedelta(hours=7)   # pretend: the session ended 21 minutes ago
        b.session.close = datetime.now(timezone.utc) - timedelta(minutes=OUTCOME_DELAY_MIN + 1)
        task = asyncio.ensure_future(b.run())
        try:
            await until("sec_item", 2)
            b.session.close -= timedelta(minutes=b.cfg["speed"]["after_close_min"])   # now the speed measurement is due
            await until("speed_done", 1)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return b

    b = asyncio.run(scenario())
    rows = list(b.journal.recent_events(1))
    kinds = Counter(r["kind"] for r in rows)
    assert kinds["error"] == 0, [r for r in rows if r["kind"] == "error"]
    assert (kinds["day_start"], kinds["sec_item"], kinds["news_speed"], kinds["speed_done"]) == (1, 2, 2, 1)
    assert next(r for r in rows if r["kind"] == "day_start")["last_equity"] == 100400.0
    done = next(r for r in rows if r["kind"] == "speed_done")
    assert (done["due"], done["measured"], done["skipped"], done["errors"]) == (2, 2, 0, 0)
    acme = next(r for r in rows if r["kind"] == "news_speed" and r["symbol"] == "ACME")
    assert (acme["src"], acme["ref"], acme["session"], acme["items"]) == ("sec", "B1", "regular", ["2.02"])
    assert (acme["price"], acme["ref_age_s"], acme["r1"], acme["r300"], acme["r1800"]) == (100.0, 5, 2.0, 2.0, 2.0)
    status = json.loads((tmp_path / "logs" / "status.json").read_text())
    assert status["open_trades"] == [] and status["trades_today"] == 0 and "edgar" in status
    assert b.outcomes_day == b.day and not b.executor.trades and set(calls) == {"GET"}
    assert not (tmp_path / "logs" / "trades.jsonl").exists()



# ── analysts' expectations (Finnhub) ─────────────────────────────────────────
class FinnhubHttp:
    """Stands in for Finnhub: answers each call with the next prepared (status, data, headers)."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        status, data, hdrs = self.responses.pop(0)

        class R:
            status_code, headers, text = status, hdrs, json.dumps(data)
            def json(self): return data
        return R()


def test_expectations_come_from_the_earnings_calendar_and_the_key_stays_out_of_addresses(monkeypatch):
    from bot import finnhub as fh_mod
    from bot.finnhub import Finnhub, FinnhubError, calendar_summary

    async def no_sleep(_): return None
    monkeypatch.setattr(fh_mod.asyncio, "sleep", no_sleep)
    rows = [
        {"symbol": "ACME", "date": "2026-11-24", "hour": "amc", "epsEstimate": 1.2, "revenueEstimate": 5.0e9},
        {"symbol": "TINY", "date": "2026-11-24", "hour": "", "epsEstimate": None, "revenueEstimate": None},
        {"symbol": "BIGG", "date": "2026-11-25", "hour": "bmo", "epsEstimate": 0.4, "revenueEstimate": None},
    ]

    async def go(*responses, key="geheim-123"):
        fh = Finnhub(key, http=FinnhubHttp(*responses))
        try:
            return fh, await fh.earnings_calendar("2026-11-24", "2026-11-25"), None
        except FinnhubError as e:
            return fh, None, e

    fh, got, err = asyncio.run(go((200, {"earningsCalendar": rows}, {"x-ratelimit-limit": "60"})))
    assert got == rows and err is None and fh.plan_limit == 60
    url, params, headers = fh.http.calls[0]
    assert url.endswith("/calendar/earnings") and params == {"from": "2026-11-24", "to": "2026-11-25"}
    assert headers == {"X-Finnhub-Token": "geheim-123"} and "geheim" not in url + json.dumps(params)
    assert calendar_summary(got) == {"entries": 3, "with_eps": 2, "with_revenue": 1, "with_hour": 2,
                                     "hours": {"amc": 1, "bmo": 1, "unbekannt": 1}}
    # over the limit once: one more try, then the answer counts
    fh, got, err = asyncio.run(go((429, {"error": "limit"}, {}), (200, {"earningsCalendar": rows[:1]}, {})))
    assert got == rows[:1] and len(fh.http.calls) == 2
    # data that the plan does not include: a clear error that names the call but never the key
    fh, got, err = asyncio.run(go((403, {"error": "You don't have access to this resource."}, {})))
    assert got is None and err.status == 403 and "/calendar/earnings" in str(err) and "geheim" not in str(err)
    # without a key nothing is sent at all
    fh, got, err = asyncio.run(go(key=""))
    assert err.status == "no_key" and fh.http.calls == [] and not fh.available
