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
# Everything that trades before the open and after the close, switched off - for tests of the rest.
OFF = {**CFG, "extended": {**CFG["extended"], "enabled": False}, "fast": {**CFG["fast"], "enabled": False}}
ORB_ON = {**CFG, "orb": {**CFG["orb"], "enabled": True}}   # the strategy's logic is tested whether or not it is switched on
NEWS_ON = {**CFG, "news": {**CFG["news"], "enabled": True}}   # the old rule with price confirmation: off since 24.11.2026


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
    # the law firm's call to Nutex shareholders is listed (marked), but not handed on to be rated
    assert got == ["HD"] and stats["matched"] == 2 and stats["items"] == 4
    assert [(r["symbol"], r["published"]) for r in items][0] == ("HD", "2026-11-17T10:43:00Z") and len(items) == 2
    assert [(r["symbol"], r.get("alert")) for r in items] == [("HD", None), ("NUTX", True)]


def test_a_law_firms_call_to_shareholders_is_not_an_announcement_of_the_company():
    from bot.wires import is_alert
    calls = ["INVESTOR ALERT: Pomerantz Law Firm Investigates Claims On Behalf of Investors of Acme Corp - ACME",
             "ACME DEADLINE ALERT: Bragar Eagel & Squire, P.C. Reminds Investors of Class Action Deadline",
             "Shareholders that lost money on Acme Corp (ACME) should contact Some Firm LLP",
             "SueWallSt Notifies Investors of an Investigation Notice Involving Acme Corp",
             "Lead Plaintiff Deadline Approaching for Acme Corp Securities Fraud Lawsuit"]
    news = ["Acme Corp Reports Third Quarter Results", "Acme to Acquire Beta Inc. for $2.1 Billion",
            "Acme Announces Settlement of Securities Class Action",          # the company's own case is news
            "Acme Prevails as Court Dismisses Shareholder Class Action", "Acme Names New Chief Executive", ""]
    assert all(is_alert(h) for h in calls) and not any(is_alert(h) for h in news)


GNW = """<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item><guid isPermaLink="true">https://www.globenewswire.com/news-release/2026/11/23/3408001/0/en/acme.html</guid>
<link>https://www.globenewswire.com/news-release/2026/11/23/3408001/0/en/acme.html</link>
<category domain="https://www.globenewswire.com/rss/stock">Nasdaq:ACME</category>
<category domain="https://www.globenewswire.com/rss/ISIN">US0000000001</category>
<title>Acme Reports Third Quarter 2026 Results &amp; Raises Outlook</title>
<description><![CDATA[<p>BOSTON, Nov. 23, 2026 (GLOBE NEWSWIRE) -- Acme Corp (Nasdaq: ACME) today reported revenue of
$5.2&nbsp;billion. """ + "Long body. " * 200 + """</p><table><tr><td>1,234</td></tr></table><p>Outlook raised.</p>]]></description>
<pubDate>Mon, 23 Nov 2026 12:01 GMT</pubDate><dc:language>en</dc:language><dc:contributor>Acme Corp</dc:contributor></item>
<item><guid>https://www.globenewswire.com/news-release/2026/11/23/3408002/0/en/hk.html</guid>
<category domain="https://www.globenewswire.com/rss/stock">HKSE:9973.HK</category>
<title>Chery launches a car</title><description>x</description>
<pubDate>Mon, 23 Nov 2026 12:00 GMT</pubDate><dc:language>en</dc:language><dc:contributor>Acme Corp</dc:contributor></item>
<item><guid>https://www.globenewswire.com/news-release/2026/11/23/3408003/0/en/two.html</guid>
<category domain="https://www.globenewswire.com/rss/stock">Nasdaq:ACME</category>
<category domain="https://www.globenewswire.com/rss/stock">NYSE:BETA</category>
<title>Acme and Beta merge</title><description>x</description>
<pubDate>Mon, 23 Nov 2026 11:59 GMT</pubDate><dc:language>en</dc:language></item>
</channel></rss>"""

BW = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Beta Inc. Reports Third Quarter Results</title>
<link>http://www.businesswire.com/news/home/20261123000001/en/Beta-Reports/?feedref=x</link>
<guid>20261123000001en</guid><pubDate>Mon, 23 Nov 2026 21:05:00 UT</pubDate>
<description>NEW YORK--(BUSINESS WIRE)--Beta Inc. (NYSE: BETA) today announced results; its partner Acme (Nasdaq: ACME) ...</description></item>
<item><title>Gamma partners with Acme</title>
<link>http://www.businesswire.com/news/home/20261123000002/en/</link>
<guid>20261123000002en</guid><pubDate>Mon, 23 Nov 2026 21:04:00 UT</pubDate>
<description>AUSTIN, Texas--(BUSINESS WIRE)--Gamma, a private company, today announced a deal with Acme Corp (Nasdaq: ACME).</description></item>
</channel></rss>"""


def test_globenewswire_and_business_wire_name_their_stocks_differently():
    assets = {"ACME": {"name": "Acme Corp"}, "BETA": {"name": "Beta Inc."}}
    idx = NameIndex(assets)
    g = parse_feed(GNW)
    assert g[0]["listings"] == ["ACME"] and g[1]["listings"] == [] and g[2]["listings"] == ["ACME", "BETA"]
    arts = [to_article(i, idx, assets, "globenewswire", 420) for i in g]
    a = arts[0]
    assert a["symbols"] == ["ACME"] and a["headline"] == "Acme Reports Third Quarter 2026 Results & Raises Outlook"
    assert a["created_at"] == "2026-11-23T12:01:00Z" and a["text"].endswith("Outlook raised.") and "1,234" not in a["text"]
    assert len(a["summary"]) <= 600
    # a Hong Kong listing is not ours, even though the contributor's name matches a US stock; two companies: unclear
    assert arts[1] is None and arts[2] is None
    b = [to_article(i, idx, assets, "businesswire", 420) for i in parse_feed(BW)]
    assert b[0]["symbols"] == ["BETA"] and b[0]["created_at"] == "2026-11-23T21:05:00Z" and "text" not in b[0]
    assert b[1] is None                      # the ticker is the partner's, not the company's that speaks
    from bot.wires import BW_OPENING, BW_NOT_A_NAME
    m = BW_OPENING.search('SEATTLE--(BUSINESS WIRE)--Zeta Holdings, Inc. ("Zeta" or the "Company") (NYSE: ZETA), the leader')
    assert m.group(2) == "ZETA" and not BW_NOT_A_NAME.search(m.group(1))


def test_full_text_from_business_wire_and_globenewswire_pages():
    from bot.wires import release_text
    bw = """<html><nav><p>Menu</p></nav><div class="bw-release-story"><p>NEW YORK--(BUSINESS WIRE)--Beta Inc.
      (NYSE: BETA) today reported results.</p><div><p>Revenue rose 9%.</p></div></div>
      <div class="bw-release-contact"><p>Contact: press@beta</p></div></html>"""
    assert release_text(bw) == "NEW YORK--(BUSINESS WIRE)--Beta Inc. (NYSE: BETA) today reported results. Revenue rose 9%."
    gnw = """<div itemprop="articleBody"><p>Acme today reported.</p><p>Outlook raised.</p></div></article><p>Footer</p>"""
    assert release_text(gnw) == "Acme today reported. Outlook raised."


def test_every_wire_has_its_own_clock(tmp_path):
    from bot.wires import Wires
    cfg = {**CFG["wires"], "feeds": ["p1", "p2"], "more": {"globenewswire": ["g1"], "businesswire": ["b1", "b2"]},
           "fast_poll_s": 10, "poll_s": 60}
    w = Wires(cfg, None, None, Journal(tmp_path))
    assert w.feeds() == [("prnewswire", "p1", 10), ("prnewswire", "p2", 60), ("globenewswire", "g1", 10),
                         ("businesswire", "b1", 10), ("businesswire", "b2", 60)]


def test_only_the_first_report_of_a_piece_of_news_counts(tmp_path, monkeypatch):
    from bot import first as first_mod
    from bot.first import FirstReport
    clock = [datetime(2026, 11, 23, 21, 6, tzinfo=timezone.utc)]
    monkeypatch.setattr(first_mod, "now", lambda: clock[0])
    f = FirstReport(Journal(tmp_path), window_min=15)
    art = lambda i, src, sym, head, pub="2026-11-23T21:05:00Z", syms=None: {
        "id": i, "source": src, "symbols": syms or [sym], "headline": head, "created_at": pub}
    assert f.claim(art("bw:1", "businesswire", "BETA", "Beta Inc. Reports Third Quarter Results"))
    clock[0] += timedelta(seconds=40)
    assert not f.claim(art("bw:1", "businesswire", "BETA", "Beta Inc. Reports Third Quarter Results"))   # same item again
    assert not f.claim(art("gnw:9", "globenewswire", "BETA", "Beta Inc. Reports Third-Quarter Results", "2026-11-23T21:06:00Z"))
    clock[0] += timedelta(minutes=5)
    assert not f.claim(art(77, "benzinga", "BETA", "Beta Q3 EPS $1.20 Beats $1.10 Estimate"))         # both report results
    assert f.claim(art(78, "benzinga", "BETA", "Beta Names New Chief Financial Officer"))             # other news
    assert f.claim(art("prn:5", "prnewswire", "ACME", "Acme Reports Third Quarter Results"))          # other stock
    clock[0] += timedelta(minutes=16)
    assert f.claim(art("prn:6", "prnewswire", "BETA", "Beta Inc. Reports Third Quarter Results Conference Call Replay"))
    # a roundup of many stocks blocks nobody's own release
    assert f.claim(art(90, "benzinga", "X", "Earnings roundup: results from five companies", syms=["C1", "C2", "C3"]))
    assert f.claim(art("gnw:10", "globenewswire", "C1", "C1 Reports Fourth Quarter Results"))
    rows = [json.loads(l) for p in (tmp_path / "logs").glob("events-*.jsonl") for l in p.read_text().splitlines()]
    dups = [r for r in rows if r["kind"] == "news_dup"]
    assert [(d["source"], d["first_source"], d["lag_s"]) for d in dups] == [("globenewswire", "businesswire", 40.0),
                                                                          ("benzinga", "businesswire", 340.0)]
    assert dups[0]["published_lag_s"] == 60


def test_a_copy_from_another_wire_is_neither_rated_nor_traded(tmp_path):
    from bot.main import Bot
    got = []

    class Strat:
        async def on_article(self, art): got.append(art["id"])

    b = Bot.__new__(Bot)
    from bot.first import FirstReport
    b.first, b.fast, b.news = FirstReport(Journal(tmp_path)), Strat(), Strat()
    b.cfg = {"wires": {"trade": ["businesswire", "prnewswire"]}}

    async def run():
        await b.on_wire({"id": "bw:1", "source": "businesswire", "symbols": ["BETA"], "headline": "Beta Reports Q3 Results",
                         "created_at": "2026-11-23T21:05:00Z"})
        await b.on_wire({"id": "prn:1", "source": "prnewswire", "symbols": ["BETA"], "headline": "Beta Reports Q3 Results",
                         "created_at": "2026-11-23T21:05:00Z"})
        await b.on_wire({"id": "gnw:1", "source": "globenewswire", "symbols": ["ACME"], "headline": "Acme Reports Q3 Results",
                         "created_at": "2026-11-23T21:05:00Z"})          # a wire that is only measured
        await asyncio.sleep(0)
    asyncio.run(run())
    assert got == ["bw:1", "bw:1"]                 # the fast and the old strategy each saw the first report once


def test_finnhub_news_is_measured_and_never_blocks_a_traded_wire(tmp_path, monkeypatch):
    from bot import finnhub_news as fn_mod, first as first_mod
    from bot.finnhub_news import FinnhubNews, to_article as fh_article
    from bot.first import FirstReport
    t0 = datetime(2026, 11, 23, 21, 6, tzinfo=timezone.utc)
    clock = [t0]
    monkeypatch.setattr(fn_mod, "now", lambda: clock[0])
    monkeypatch.setattr(first_mod, "now", lambda: clock[0])
    uni = {"ACME": {}, "BETA": {}}
    item = lambda i, head, related="ACME", ago=60: {"id": i, "headline": head, "related": related, "source": "Yahoo",
                                                  "datetime": int((t0 - timedelta(seconds=ago)).timestamp()), "summary": "s"}
    a = fh_article(item(1, "Acme Q3 Results Top Estimates"), None, uni, 420)
    assert a["symbols"] == ["ACME"] and a["source"] == "finnhub" and a["created_at"] == "2026-11-23T21:05:00Z"
    assert fh_article(item(2, "x", "ACME,BETA"), None, uni, 420) is None           # two of ours: unclear
    assert fh_article(item(3, "x", "BETA"), "ACME", uni, 420) is None              # asked for Acme, about Beta
    assert fh_article(item(4, "x", ""), "ACME", uni, 420)["symbols"] == ["ACME"]

    class Fh:
        available = True
        def __init__(self): self.answers = {}
        async def get(self, path, **p): return self.answers.get((path, p.get("category") or p.get("symbol")), [])

    class Uni: cache = uni
    class Exp: rows = {"ACME": {"date": "2026-11-23"}, "BETA": {"date": "2026-11-24"}}
    got = []
    async def on_article(x): got.append(x["id"])
    fh = Fh()
    news = FinnhubNews(fh, {"enabled": True}, Exp(), Uni(), on_article, Journal(tmp_path))
    assert news.reporters() == ["ACME"]

    async def run():
        fh.answers[("/news", "general")] = [item(10, "Old news", ago=30)]
        await news.market("general")                       # first call: what is there is not news
        fh.answers[("/news", "general")] = [item(10, "Old news", ago=30), item(11, "Beta Wins Contract", "BETA")]
        await news.market("general")
        fh.answers[("/company-news", "ACME")] = [item(12, "Acme Reports Q3 Results"), item(13, "Acme last week", ago=86400)]
        await news.company("ACME")
        await asyncio.sleep(0)
    asyncio.run(run())
    assert got == ["finnhub:11", "finnhub:12"] and news.min_id["general"] == 11

    # Finnhub first, the wire 30 s later: the wire is still traded, and the row says Finnhub was earlier
    f = FirstReport(Journal(tmp_path))
    assert f.claim({"id": "finnhub:12", "source": "finnhub", "symbols": ["ACME"], "headline": "Acme Reports Q3 Results"}, traded=False)
    clock[0] += timedelta(seconds=30)
    assert f.claim({"id": "gnw:1", "source": "globenewswire", "symbols": ["ACME"], "headline": "Acme Reports Third Quarter Results"})
    clock[0] += timedelta(seconds=30)
    assert not f.claim({"id": "finnhub:14", "source": "finnhub", "symbols": ["ACME"], "headline": "Acme reports Q3 results"}, traded=False)
    rows = [json.loads(l) for p in (tmp_path / "logs").glob("events-*.jsonl") for l in p.read_text().splitlines()]
    dups = [(r["source"], r["first_source"], r["blocked"]) for r in rows if r["kind"] == "news_dup"]
    assert dups == [("globenewswire", "finnhub", False), ("finnhub", "finnhub", False), ("finnhub", "globenewswire", True)]
    assert sum(1 for r in rows if r["kind"] == "wire_item" and r["source"] == "finnhub") == 2


# ── simulated broker ─────────────────────────────────────────────────────────
class FakeSession:
    def __init__(self):
        self.mins = 200          # minutes to the regular close
        self.ph = "regular"      # which session is running: pre, regular, post or None
        self.ext_mins = 120      # minutes to the end of the pre-market / after-hours session
        self.since = 60          # minutes since the regular open
    def is_open(self): return self.ph == "regular"
    def to_close_min(self): return self.mins
    def since_open_min(self): return self.since
    def phase(self): return self.ph
    def phase_end(self, phase): return ex_mod.now() + timedelta(minutes=self.ext_mins)


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
    async def live_snapshot(self, sym): return await self.snapshot(sym)
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
    e.in_hours, e.fast_hours = (lambda: True), (lambda: False)     # the evening: complete looks only
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
    def __init__(self, model, direction="up", error=None, surprise=8, confidence=7, impact=2):
        self.model, self.direction, self.error, self.surprise, self.day_calls = model, direction, error, surprise, 0
        self.confidence, self.impact, self.asked = confidence, impact, []

    async def rate(self, art, symbols, names=None, stats=None, context=None, text_chars=1200):
        self.day_calls += 1
        self.asked.append({"article": art, "context": context, "text_chars": text_chars})
        v = None if self.error else {"symbol": symbols[0], "direction": self.direction, "surprise": self.surprise,
                                     "impact": self.impact, "confidence": self.confidence, "category": "guidance", "reason": ""}
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
    news = NewsStrategy(Api(), NEWS_ON, j, Sess(), Uni(), Ex(), decider, shadows=shadows, fallback=fallback)
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


def test_weekly_stats_set_the_ratings_of_the_immediate_entry_against_the_price(tmp_path):
    """Every rating of the immediate entry is compared with the price path measured for the same press
    release in the night. Friday's evening session and its measurement end up in Saturday's file."""
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (tmp_path / "config.toml").write_text("[news]\nmin_surprise = 7\nmin_confidence = 6\nmin_impact = 1\n"
                                          "[fast]\nmin_surprise = 7\nmin_confidence = 7\nmin_impact = 1\n")
    v = lambda d, s=8, i=2, c=8: {"symbol": "ACME", "direction": d, "surprise": s, "impact": i, "confidence": c}

    def rated(t, article, verdict, **kw):
        return {"t": t, "kind": "fast_rated", "article": article, "source": "prnewswire", "symbol": "ACME",
                "headline": "Headline " + article, "phase": "post", "age_s": 120, "verdict": verdict,
                "model": "claude-opus-5-5", "model_s": 1.5, "tokens_in": 2000, "tokens_out": 100, "with_text": True, **kw}

    def path(t, article, t0, r300, r900=None, usd=500000, **kw):
        return {"t": t, "kind": "news_speed", "src": "prnewswire", "ref": article, "symbol": "ACME", "t0": t0,
                "session": "post", "ref_age_s": 30, "r60": r300 / 2, "r300": r300, "r900": r900, "usd_60_300": usd, **kw}

    friday = [rated("2026-11-20T21:10:00Z", "a", v("down"), with_expectations=True),       # a candidate, and right
              rated("2026-11-20T21:11:00Z", "b", v("up", 5, 1, 5)),                         # a direction, below the thresholds
              rated("2026-11-20T21:12:00Z", "c", v("none", 1, 0, 3)),                       # nothing expected, and it moved
              rated("2026-11-20T21:13:00Z", "e", v("up")),                                  # never measured
              {"t": "2026-11-20T21:14:00Z", "kind": "fast_skipped", "reason": "stale"},
              {"t": "2026-11-20T21:14:01Z", "kind": "fast_skipped", "reason": "stale"},
              {"t": "2026-11-20T21:15:00Z", "kind": "fast_rejected", "reason": "no_reference"}]
    saturday = [rated("2026-11-21T00:20:00Z", "d", v("up")),                               # 19:20 in New York, still Friday
                {"t": "2026-11-21T00:30:00Z", "kind": "error", "where": "fast"},
                path("2026-11-21T01:30:00Z", "a", "2026-11-20T21:08:00Z", -2.0, -1.0),
                path("2026-11-21T01:30:01Z", "b", "2026-11-20T21:09:00Z", -0.5, 0.2, usd=2000),
                path("2026-11-21T01:30:02Z", "c", "2026-11-20T21:10:00Z", 3.0, 3.0),
                path("2026-11-21T01:30:03Z", "d", "2026-11-21T00:18:00Z", 0.0),
                {"t": "2026-11-21T01:30:04Z", "kind": "news_speed", "src": "prnewswire", "ref": "e", "skipped": "market_closed"},
                # Saturday proper: not part of the week
                {"t": "2026-11-21T09:00:00Z", "kind": "error", "where": "wires"},
                rated("2026-11-21T09:00:00Z", "weekend", v("up")),
                # a measurement that ran late still counts, by the time of the announcement
                path("2026-11-21T09:30:00Z", "late", "2026-11-20T15:00:00Z", 1.5),
                path("2026-11-21T09:30:01Z", "next", "2026-11-21T08:00:00Z", 1.5)]
    (logs / "events-2026-11-20.jsonl").write_text("\n".join(json.dumps(e) for e in friday))
    (logs / "events-2026-11-21.jsonl").write_text("\n".join(json.dumps(e) for e in saturday))
    trade = {"strategy": "fast", "symbol": "ACME", "side": "long", "pnl": -40, "r": -0.4, "hold_min": 3, "slippage_pct": 0.3,
             "notional": 9000, "exit_reason": "stop", "style": "soft", "phase": "post", "meta": {}}
    (logs / "trades.jsonl").write_text("\n".join(json.dumps(dict(trade, entry_time=t)) for t in
                                                  ("2026-11-21T00:21:00Z", "2026-11-21T09:00:00Z", "2026-11-14T00:21:00Z")))
    r = build(tmp_path, *week_of(datetime(2026, 11, 20).date()))
    fa = r["fast"]
    assert (fa["rated"], fa["with_text"], fa["with_expectations"], fa["by_session"]) == (5, 5, 1, {"nachbörslich": 5})
    assert (fa["age_s_median"], fa["seconds_median"], fa["cost"], fa["limits"]) == (120, 1.5, 0.05, [7, 7, 1])
    d = fa["directed"]
    assert (d["n"], d["measured"], d["right_5"], d["wrong_5"], d["flat_5"], d["dir_move_5"]) == (4, 3, 1, 1, 1, 0.5)
    assert (d["measured_15"], d["right_15"], d["dir_move_15"]) == (2, 2, 0.6)
    assert (d["movers"], d["movers_right"]) == (1, 1)                 # b fell without volume: nothing anyone could trade
    assert [x["symbol"] for x in d["examples"]] == ["ACME"] * 3 and d["examples"][0]["r300"] == -2.0
    c = fa["candidates"]
    assert (c["n"], c["measured"], c["right_5"], c["wrong_5"], c["flat_5"]) == (3, 2, 1, 0, 1)
    assert fa["none"] == {"n": 1, "measured": 1, "moved_anyway": 1}
    assert fa["skipped"] == {"stale": 2} and fa["rejected"] == {"no_reference": 1}
    assert r["errors"] == {"fast": 1}                                  # Saturday morning's error belongs to no trading day
    assert r["by_strategy"]["fast"]["n"] == 1 and fa["trades_by_session"]["nachbörslich"]["pnl"] == -40
    assert r["speed"]["all"]["n"] == 5                                 # a, b, c, d and the one measured late; not Saturday's own
    md = markdown(r)
    assert "## Sofort-Einstieg: Bewertungen gegen den Kursverlauf" in md
    assert "Richtung nach 5 Minuten richtig 1, falsch 1, ohne Bewegung 1" in md and "ACME fällt: -1.00 % / -2.00 % / -1.00 %" in md
    # a week without the immediate entry: no section, and nothing breaks
    empty = build(tmp_path, *week_of(datetime(2026, 11, 11).date()))
    assert empty["fast"]["rated"] == 0 and "## Sofort-Einstieg" not in markdown(empty)
    assert empty["by_strategy"]["fast"]["n"] == 1                      # entered on the 14th at 0:21 UTC: Friday evening of that week


def test_weekly_stats_tell_press_releases_from_filings_and_show_the_look_at_the_market(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    v = lambda d, cat="m&a": {"symbol": "ACME", "direction": d, "surprise": 8, "impact": 2, "confidence": 8, "category": cat}
    ev = lambda kind, sec, **kw: {"t": f"2026-11-24T15:{sec // 60:02d}:{sec % 60:02d}Z", "kind": kind, **kw}
    rated = lambda sec, src, art, verdict: ev("fast_rated", sec, article=art, source=src, symbol="ACME", headline="H " + art,
                                              phase="regular", age_s=40, verdict=verdict, model="claude-opus-5-5", model_s=2.5,
                                              tokens_in=2000, tokens_out=100, with_text=True)
    shadow = lambda sec, src, art, verdict: ev("fast_shadow", sec, article=art, source=src, symbol="ACME", verdict=verdict,
                                               model="claude-haiku-5-5", model_s=0.6)
    path = lambda src, art, r300: ev("news_speed", 3000, src=src, ref=art, symbol="ACME", t0="2026-11-24T15:00:00Z",
                                     session="regular", ref_age_s=10, r60=r300 / 4, r300=r300, r900=r300, usd_60_300=900000)
    rows = [rated(60, "wa-prnewswire", "wa-prnewswire:p1", v("up", "contract")) | {"published": "2026-11-24T15:00:20Z"},
            path("prnewswire", "p1", 2.0),          # measured once, under the wire's own copy of the same minute
            rated(61, "sec", "0001-26-1", v("down")), path("sec", "0001-26-1", -1.5),
            rated(62, "sec", "0001-26-2", v("up")), path("sec", "0001-26-2", -1.0),          # wrong
            shadow(60, "wa-prnewswire", "wa-prnewswire:p1", v("up")) | {"published": "2026-11-24T15:00:20Z"},
            shadow(61, "sec", "0001-26-1", v("up")),                                           # disagrees, and is wrong
            ev("fast_shadow", 62, article="0001-26-2", source="sec", model="claude-haiku-5-5", verdict=None, error="llm_http_529"),
            ev("fast_probe", 63, symbol="ACME", ok=True, seconds=0.8, bid=99.95, ask=100.05, spread_pct=0.1),
            ev("fast_probe", 64, symbol="BETA", ok=False, seconds=3.4, reason="spread"),
            ev("fast_rejected", 64, reason="spread", how="probe", symbol="BETA"),
            ev("fast_rejected", 65, reason="moved_against", how="feed", spread_pct=0.2, symbol="GAMMA"),
            ev("fast_rejected", 66, reason="source_off", symbol="ACME"),
            ev("wire_item", 70, id="prnewswire:1", source="prnewswire", symbol="ACME", published="2026-11-24T15:00:00Z", alert=True),
            ev("wire_item", 5, id="wa-prnewswire:1", source="wa-prnewswire", symbol="ACME", published="2026-11-24T15:00:00Z"),
            ev("sec_item", 45, acc="0001-26-1", symbol="ACME", accepted="2026-11-24T15:00:00Z"),
            ev("sec_item", 50, acc="0001-26-0", symbol="ACME", accepted="2026-11-23T23:10:00Z"),   # last night's, published in the morning
            ev("news_dup", 70, source="prnewswire", first_source="wa-prnewswire", lag_s=65.0, blocked=False)]
    (logs / "events-2026-11-24.jsonl").write_text("\n".join(json.dumps(e) for e in rows))
    trade = {"strategy": "fast", "symbol": "ACME", "side": "long", "pnl": 80, "r": 0.5, "hold_min": 6, "slippage_pct": 0.4,
             "notional": 9900, "exit_reason": "target", "style": "bracket", "phase": "regular", "entry_time": "2026-11-24T15:01:05Z",
             "meta": {"market": {"how": "probe", "bid": 99.95, "ask": 100.05, "spread_pct": 0.1}}}
    (logs / "trades.jsonl").write_text(json.dumps(trade))
    r = build(tmp_path, *week_of(datetime(2026, 11, 24).date()))
    fa = r["fast"]
    kinds = fa["by_kind"]
    assert kinds["Pressemitteilungen"]["rated"] == 1 and kinds["SEC-Pflichtmitteilungen"]["rated"] == 2
    assert kinds["SEC-Pflichtmitteilungen"]["candidates"] == {"n": 2, "measured": 2, "right_5": 1, "wrong_5": 1, "flat_5": 0,
                                                              "movers": 2, "movers_right": 1}
    assert fa["by_category"]["m&a"]["n"] == 2 and fa["by_category"]["contract"]["right_5"] == 1
    haiku = fa["shadows"]["claude-haiku-5-5"]
    assert (haiku["n"], haiku["same_direction"], haiku["of"], haiku["seconds_median"]) == (2, 1, 2, 0.6)
    assert (haiku["directed"]["right_5"], haiku["directed"]["wrong_5"]) == (1, 1)
    assert fa["look"] == {"probes": 2, "probes_ok": 1, "probe_seconds_median": 0.8, "probe_failed": {"spread": 1},
                          "looks": 2, "by_feed": 1, "spread_median": 0.2, "narrow": 2}
    src = r["sources"]["by_source"]
    assert (src["sec"]["n"], src["sec"]["live"], src["sec"]["lag_median_s"]) == (2, 1, 45)   # the night's backlog is not a delay
    assert (src["prnewswire"]["alerts"], src["prnewswire"]["lag_median_s"], src["wa-prnewswire"]["lag_median_s"]) == (1, 70, 5)
    assert r["sources"]["dup_pairs"] == {"wa-prnewswire vor prnewswire": 1} and r["sources"]["dup_lag_s"] == {"wa-prnewswire vor prnewswire": 65}
    md = markdown(r)
    assert "- SEC-Pflichtmitteilungen: bewertet 2; mit Richtung 2, gemessen 2: richtig 1, falsch 1" in md
    assert "Proben: 2, davon 1 hin und zurück ausgeführt (Dauer im Median 0.8 s); nicht gelungen: {'spread': 1}" in md
    assert "- mitbewertet von claude-haiku-5-5: 2 Meldungen (Antwortzeit 0.6 s), gleiche Richtung wie das entscheidende Modell bei 1 von 2" in md
    assert "wa-prnewswire vor prnewswire: 1 (65 s)" in md and "| sec | 2 | 1 | 45 s (45–45) | 0 | 0 | 0 |" in md
    assert "Trades Handelszeit: 1 Trades" in md


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
    for name in ("ANTHROPIC_API_KEY", "APCA_BASE_URL", "APCA_DATA_FEED", "WIREALPHA_API_KEY", "ALPHAVANTAGE_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(main_mod, "load_config", lambda: json.loads(json.dumps(OFF)))   # nothing here trades out of hours
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
        if url.endswith("/stocks/snapshots"):                              # the feed measurement, if a session is running
            return {}
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
        async def get(self, url, params=None, timeout=None):
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


# ── trading before the open and after the close ─────────────────────────────
EXT_ON = {**CFG, "extended": {**CFG["extended"], "enabled": True}}


def test_trading_runs_with_the_values_josh_approved_on_19_and_24_november():
    ext, fast = CFG["extended"], CFG["fast"]
    assert ext["enabled"] is True and fast["enabled"] is True
    assert fast["max_notional"] == 10000 and fast["max_notional_small"] == 5000 and fast["max_open"] == 1
    # 24.11.: the immediate entry also during the session, with a look at bid and offer before every entry
    assert fast["max_hold_min"] == {"pre": 15, "regular": 15, "post": 10} and fast["sessions"] == ["pre", "regular", "post"]
    assert (fast["regular_start_after_open_min"], fast["regular_no_entry_before_close_min"]) == (5, 30)
    assert fast["look"] == {"enabled": True, "quote_fresh_s": 10, "max_spread_pct": 0.5, "max_against_pct": 0.5,
                            "limit_offset_pct": 0.2, "probe_wait_s": 3, "probe_back_pct": 0.5}
    # the old rule with price confirmation and Alpaca's news stream are off; the model is still the one Josh chose
    assert CFG["news"]["enabled"] is False and CFG["news"]["stream"] is False and CFG["orb"]["enabled"] is False
    assert CFG["llm"]["model"] == "claude-opus-5-5"
    # Josh on 24.11.: trade everything at once, no measuring first. Filings with the SEC are traded, and so are the
    # four wires that come over WireAlpha; the filings WireAlpha reports are a copy of the SEC's list and only measured
    assert CFG["edgar"]["rate"] is True and CFG["edgar"]["trade"] is True and CFG["edgar"]["fast_poll_s"] >= 1
    assert sorted(s for s in CFG["wires"]["trade"] if s.startswith("wa-")) == [
        "wa-accesswire", "wa-businesswire", "wa-globenewswire", "wa-prnewswire"]
    assert "sec_edgar" in CFG["wirealpha"]["sources"] and CFG["finnhub_news"]["enabled"] is False
    assert (fast["min_surprise"], fast["min_confidence"], fast["min_impact"], fast["max_age_s"]) == (7, 7, 1, 300)
    assert fast["expected_move_pct"] == {"1": 2.0, "2": 5.0, "3": 10.0} and fast["chase_frac"] == 0.5
    assert (fast["stop_mult"], fast["entry_timeout_s"], fast["risk_pct"]) == (1.5, 15, 0.25)
    assert (ext["flatten_before_end_min_pre"], ext["flatten_before_end_min_post"], ext["no_entry_before_flatten_min"]) == (10, 15, 10)


def test_a_trading_day_has_three_sessions_also_on_a_half_day(monkeypatch):
    from bot import market as mk

    class Cal:
        def __init__(self, **row): self.row = row
        async def calendar(self, day, end=None): return [self.row]

    clock = {}
    monkeypatch.setattr(mk, "now", lambda: clock["t"].astimezone(timezone.utc))

    def phases(day, times, **row):
        at = lambda hm: datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=ET)
        clock["t"] = at("03:00")
        s = mk.Session(Cal(date=day, **row))
        asyncio.run(s.refresh())
        out = []
        for hm in times:
            clock["t"] = at(hm)
            out.append(s.phase())
        return s, out

    s, got = phases("2026-11-23", ("03:59", "04:00", "09:29", "09:30", "15:59", "16:00", "19:59", "20:00"),
                    open="09:30", close="16:00", session_open="0400", session_close="2000")
    assert got == [None, "pre", "pre", "regular", "regular", "post", "post", None]
    assert s.phase_end("pre") == s.open and s.phase_end("post").hour == 20 and s.is_open() is False
    # the Friday after Thanksgiving: the exchange closes at 13:00, after-hours trading ends four hours
    # later, whatever the calendar says about the usual end
    s, got = phases("2026-11-27", ("12:59", "13:00", "16:59", "17:00"),
                    open="09:30", close="13:00", session_open="0400", session_close="2000")
    assert got == ["regular", "post", "post", None] and s.ext_close.hour == 17
    # a calendar without the two fields, or with nonsense in them: 4:00 and four hours after the close
    s, got = phases("2026-11-23", ("04:00", "19:59"), open="09:30", close="16:00")
    assert got == ["pre", "post"] and (s.ext_open.hour, s.ext_close.hour) == (4, 20)
    s, got = phases("2026-11-23", ("04:00",), open="09:30", close="16:00", session_open="1000", session_close="x")
    assert got == ["pre"] and (s.ext_open.hour, s.ext_close.hour) == (4, 20)
    # a holiday has no session at all
    clock["t"] = datetime.fromisoformat("2026-11-26T10:00:00").replace(tzinfo=ET)
    s = mk.Session(Cal(date="2026-11-27", open="09:30", close="13:00"))     # the calendar answers with the next trading day
    asyncio.run(s.refresh())
    assert s.phase() is None and not s.trading_day


def test_which_price_counts_for_a_position_nobody_else_protects():
    from bot.executor import exit_limit, exit_reference, mark_price, read_quote, spread_pct
    at = datetime(2026, 11, 23, 13, 0, 30, tzinfo=timezone.utc)
    snap = {"latestTrade": {"p": 50.0, "t": "2026-11-23T13:00:27.123456789Z"},
            "latestQuote": {"bp": 49.9, "ap": 50.1, "t": "2026-11-23T12:58:30Z"}}
    q = read_quote(snap, at)
    assert (q["last"], round(q["last_age_s"]), q["bid"], q["ask"], round(q["quote_age_s"])) == (50.0, 3, 49.9, 50.1, 120)
    assert round(spread_pct(q), 2) == 0.4
    assert mark_price(q, 10, 0.6) == 50.0                         # a trade of the last seconds counts first
    old = read_quote({**snap, "latestTrade": {"p": 48.0, "t": "2026-11-23T12:40:00Z"}}, at)
    assert mark_price(old, 10, 0.6) == 50.0                       # otherwise the middle of a narrow quote, however old it is
    wide = read_quote({"latestTrade": snap["latestTrade"] | {"t": "2026-11-23T12:40:00Z"},
                       "latestQuote": {"bp": 49.0, "ap": 51.0, "t": "2026-11-23T13:00:29Z"}}, at)
    assert mark_price(wide, 10, 0.6) is None                      # a wide quote and an old trade: the bot is blind
    for nothing in ({}, None, {"latestTrade": {"p": 50.0}}, {"latestQuote": {"bp": 0, "ap": 50.1}},
                    {"latestQuote": {"bp": 50.2, "ap": 50.1}}):   # no time on the trade, one-sided or crossed quote
        assert mark_price(read_quote(nothing, at), 10, 0.6) is None
    # closing starts from the broker's own price for the position: the feed's quote can be hours old out of hours
    ext = CFG["extended"]
    stale = read_quote({"latestTrade": {"p": 131.0, "t": "2026-11-22T21:00:00Z"},
                        "latestQuote": {"bp": 130.0, "ap": 132.0, "t": "2026-11-22T21:00:00Z"}}, at)
    assert exit_reference({"current_price": "124.10"}, stale, "long") == 124.10      # not the stale bid of 130
    assert exit_reference(None, q, "long") == 49.9 and exit_reference({}, q, "short") == 50.1   # without it: bid / offer
    assert exit_reference(None, read_quote({"latestTrade": {"p": 48.0, "t": "2026-11-23T12:40:00Z"}}, at), "long") == 48.0
    assert exit_reference(None, read_quote({}, at), "long", 47.0) == 47.0 and exit_limit(0.0, "long", 0, ext) is None
    # through the market, and further with every attempt, but never further than the maximum
    assert exit_limit(124.10, "long", 0, ext) == round(124.10 * 0.995, 2)
    assert exit_limit(124.10, "short", 0, ext) == round(124.10 * 1.005, 2)
    assert exit_limit(124.10, "long", 2, ext) == round(124.10 * 0.985, 2)
    assert exit_limit(124.10, "long", 99, ext) == round(124.10 * 0.95, 2)


class ExtBroker(FakeApi):
    """A broker outside regular hours: it takes nothing but plain limit orders marked for extended
    hours, fills them when they cross the quote (like the paper account), and its positions follow the fills."""
    def __init__(self, clock):
        super().__init__()
        self.clock, self.pos = clock, {}
        self.bid, self.ask, self.last, self.last_age_s = 99.95, 100.05, 100.0, 1
        self.quote_age_s = 30     # the free feed's quote is rarely fresh out of hours
        self.auto = True          # fill orders that cross the quote at once
        self.market_closes = 0    # market orders sent (not possible out of hours)
        self.seq = 0              # every order gets a later time stamp than the one before

    def _stamp(self, age_s=0):
        return (self.clock["t"] - timedelta(seconds=age_s)).isoformat().replace("+00:00", "Z")

    async def submit(self, p):
        assert p["type"] == "limit" and p.get("extended_hours") is True and p["time_in_force"] == "day", p
        assert "order_class" not in p and "stop_loss" not in p, p
        oid = f"o{len(self.orders)}"
        self.orders[oid] = {"id": oid, "status": "new", "side": p["side"], "symbol": p["symbol"], "qty": p["qty"],
                            "filled_qty": "0", "filled_avg_price": None, "filled_at": None, "payload": p, "legs": [],
                            "submitted_at": self._order_stamp()}
        limit = float(p["limit_price"])
        if self.auto and (limit >= self.ask if p["side"] == "buy" else limit <= self.bid):
            self.fill(oid, self.ask if p["side"] == "buy" else self.bid)
        return self.orders[oid]

    def _order_stamp(self):
        self.seq += 1
        return (self.clock["t"] + timedelta(microseconds=self.seq)).isoformat().replace("+00:00", "Z")

    def fill(self, oid, price, qty=None):
        o = self.orders[oid]
        qty = int(qty or o["qty"])
        o.update(status="filled" if qty == int(o["qty"]) else "partially_filled", filled_qty=str(qty),
                 filled_avg_price=str(price), filled_at=self._stamp())
        self.pos[o["symbol"]] = self.pos.get(o["symbol"], 0) + (qty if o["side"] == "buy" else -qty)

    async def cancel(self, oid):
        self.cancelled.append(oid)
        if self.orders[oid]["status"] in ("new", "partially_filled"):
            self.orders[oid]["status"] = "canceled"

    async def close_position(self, sym):
        self.market_closes += 1
        raise AssertionError("a market order outside regular hours would wait for the next open")

    async def position(self, sym):
        q = self.pos.get(sym, 0)
        return {"symbol": sym, "qty": str(q), "side": "long" if q > 0 else "short", "current_price": str(self.last)} if q else None

    async def positions(self):
        return [p for p in [await self.position(s) for s in self.pos] if p]

    async def open_orders(self):
        return [o for o in self.orders.values() if o["status"] in ("new", "partially_filled")]

    async def closed_orders(self, sym, after):
        return [o for o in reversed(list(self.orders.values()))
                if o["symbol"] == sym and o["status"] not in ("new", "partially_filled")
                and parse_ts(o["submitted_at"]) > parse_ts(after)]      # like the broker: only orders sent later

    async def snapshot(self, sym):
        return {"latestTrade": {"p": self.last, "t": self._stamp(self.last_age_s)},
                "latestQuote": {"bp": self.bid, "ap": self.ask, "t": self._stamp(self.quote_age_s)}}

    def move(self, price, spread=0.10, last_age_s=1):
        self.last, self.bid, self.ask, self.last_age_s = price, round(price - spread / 2, 2), round(price + spread / 2, 2), last_age_s

    ref = 99.0                # the last price of the whole market that is old enough to be free

    async def last_bar(self, sym, start, end, feed="sip"):
        self.bar_asked = (start, end, feed)
        return {"t": self._stamp(20 * 60), "c": self.ref} if self.ref else None


@pytest.fixture
def ext_rig(tmp_path, monkeypatch):
    async def no_sleep(_): return None
    monkeypatch.setattr(ex_mod.asyncio, "sleep", no_sleep)
    clock = {"t": datetime(2026, 11, 23, 13, 0, tzinfo=timezone.utc)}     # 8:00 in New York: before the open
    monkeypatch.setattr(ex_mod, "now", lambda: clock["t"])
    api, sess = ExtBroker(clock), FakeSession()
    sess.ph = "pre"
    ex = Executor(api, EXT_ON, Journal(tmp_path), sess, FakeUniverse(), root=tmp_path)

    def events(kind):
        return [r for r in ex.journal.recent_events(1) if r["kind"] == kind]
    return api, sess, ex, tmp_path, clock, events


FAST = {"strategy": "fast", "symbol": "ACME", "side": "long", "price": 100.05, "stop": 98.55, "target_r": 1.0,
        "max_hold_min": 15, "extended": True, "risk_pct": 0.25, "max_notional": 10000, "max_open": 1,
        "limit_offset_pct": 0.3, "entry_timeout_s": 15, "meta": {"source": "sec"}}


def test_before_the_open_the_entry_is_a_plain_limit_order_and_the_bot_keeps_the_stop_itself(ext_rig):
    api, sess, ex, tmp, clock, events = ext_rig

    async def go():
        assert await ex.enter(dict(FAST))
        t = next(iter(ex.trades.values()))
        entry = api.orders[t.order_id]["payload"]
        # at most 10,000 $: 99 shares, although 0.25 % risk with this stop would allow 170
        assert (entry["qty"], entry["limit_price"], entry["extended_hours"]) == ("99", str(round(100.05 * 1.003, 2)), True)
        assert (t.style, t.phase, t.status) == ("soft", "pre", "pending")
        await ex.manage()                                      # filled at the offer: the take-profit goes to the broker
        assert t.status == "open" and t.entry_price == 100.05
        tp = api.orders[t.target_order_id]["payload"]
        assert (tp["side"], tp["qty"], tp["limit_price"], tp["extended_hours"]) == ("sell", "99", "101.55", True)
        await ex.manage()                                      # price unchanged: nothing happens
        assert t.id in ex.trades and not t.exit_reason and (t.ticks, t.blind_ticks) == (2, 0)
        api.move(98.50)                                        # through the stop at 98.55
        clock["t"] += timedelta(seconds=30)
        await ex.manage()
        return t
    t = asyncio.run(go())
    assert not ex.trades and t.target_order_id in api.cancelled          # the take-profit is taken away first
    exit_order = api.orders[t.exit_orders[-1]]["payload"]
    assert (exit_order["side"], exit_order["qty"], exit_order["extended_hours"]) == ("sell", "99", True)
    assert exit_order["limit_price"] == str(round(98.5 * 0.995, 2))       # half a percent below the price of the position
    row = trades_file(tmp)[0]
    assert (row["exit_reason"], row["exit_price"], row["style"], row["phase"]) == ("stop", 98.45, "soft", "pre")
    assert row["pnl"] == round((98.45 - 100.05) * 99, 2) and row["exit_tries"] == 1 and api.market_closes == 0
    assert [e["reason"] for e in events("trade_closing")] == ["stop"] and len(events("exit_order")) == 1


def test_take_profit_resting_at_the_broker_and_the_time_limit(ext_rig):
    api, sess, ex, tmp, clock, events = ext_rig

    async def go():
        # 1. the price runs to the target: the resting order fills at the broker, the bot only books it
        assert await ex.enter(dict(FAST))
        await ex.manage()
        t = next(iter(ex.trades.values()))
        api.move(101.70)
        api.fill(t.target_order_id, 101.55)
        await ex.manage()
        assert not ex.trades
        # 2. a short position that goes nowhere: closed after 15 minutes, bought back above the offer
        ex.cooldown.clear()
        ex.done_today.clear()
        clock["t"] += timedelta(minutes=5)
        api.move(100.0)
        assert await ex.enter({**FAST, "side": "short", "price": 99.95, "stop": 101.45})
        await ex.manage()
        t2 = next(iter(ex.trades.values()))
        assert api.orders[t2.target_order_id]["payload"]["side"] == "buy" and t2.target_price == 98.45
        clock["t"] += timedelta(minutes=14)
        await ex.manage()
        assert t2.id in ex.trades
        clock["t"] += timedelta(minutes=1, seconds=5)
        await ex.manage()
        return t2
    t2 = asyncio.run(go())
    rows = trades_file(tmp)
    assert [(r["side"], r["exit_reason"]) for r in rows] == [("long", "target"), ("short", "time")]
    assert rows[0]["exit_price"] == 101.55 and rows[0]["pnl"] == round(1.5 * 99, 2)
    assert rows[1]["exit_price"] == 100.05 and rows[1]["pnl"] == round((99.95 - 100.05) * 100, 2)
    assert api.orders[t2.exit_orders[-1]]["payload"]["limit_price"] == str(round(100.0 * 1.005, 2))


def test_an_exit_that_does_not_fill_goes_further_through_the_market(ext_rig):
    api, sess, ex, tmp, clock, events = ext_rig

    async def go():
        assert await ex.enter(dict(FAST))
        await ex.manage()
        t = next(iter(ex.trades.values()))
        api.move(98.0)
        api.auto = False                       # nobody takes the other side at first
        await ex.manage()                      # decides "stop", sends the first exit order
        assert t.status == "closing" and t.exit_tries == 1 and t.id in ex.trades
        await ex.manage()                      # cancels it and sends the next one, lower
        assert t.exit_tries == 2 and t.exit_orders[0] in api.cancelled
        api.auto = True
        await ex.manage()                      # this one fills
        return t
    t = asyncio.run(go())
    limits = [float(api.orders[o]["payload"]["limit_price"]) for o in t.exit_orders]
    assert limits == [round(98.0 * 0.995, 2), round(98.0 * 0.99, 2), round(98.0 * 0.985, 2)]
    row = trades_file(tmp)[0]
    assert (row["exit_reason"], row["exit_tries"], row["exit_price"]) == ("stop", 3, 97.95) and not ex.trades


def test_the_position_is_judged_by_the_brokers_own_price_and_closed_when_nothing_is_seen_for_a_minute(ext_rig):
    api, sess, ex, tmp, clock, events = ext_rig

    async def go():
        assert await ex.enter(dict(FAST))
        await ex.manage()
        t = next(iter(ex.trades.values()))
        # The free feed is useless here (last trade minutes old, bid and offer far apart). The broker's
        # price for the position moves all the same, and that is what counts.
        api.move(100.4, spread=2.0, last_age_s=300)
        clock["t"] += timedelta(seconds=10)
        await ex.manage()
        assert (t.price_changes, t.last_mark, t.blind_ticks) == (2, 100.4, 0) and not t.exit_reason
        for _ in range(5):                                   # then nothing moves for 50 seconds
            clock["t"] += timedelta(seconds=10)
            await ex.manage()
        assert t.id in ex.trades and t.blind_ticks == 0
        api.move(100.4, spread=0.10, last_age_s=300)         # a minute: the feed is asked, and a narrow quote
        clock["t"] += timedelta(seconds=10)                  # there says the market is just quiet
        await ex.manage()
        assert not t.exit_reason and t.blind_ticks == 0
        api.move(100.4, spread=2.0, last_age_s=300)          # another minute, and now the feed shows nothing either
        for _ in range(6):
            clock["t"] += timedelta(seconds=10)
            await ex.manage()
        assert t.exit_reason == "blind" and t.exit_tries == 1 and t.id in ex.trades
        await ex.manage()                                    # the bid is a full percent below: the second, lower order gets there
        return t
    t = asyncio.run(go())
    row = trades_file(tmp)[0]
    assert (row["exit_reason"], row["blind_ticks"], row["price_changes"]) == ("blind", 1, 2) and not ex.trades
    assert (row["exit_price"], row["exit_tries"]) == (99.4, 2)   # sold at the (wide) bid


FROM_FILL = {"strategy": "fast", "symbol": "ACME", "side": "long", "price": 99.0, "limit_price": 101.475,
             "stop_pct": 1.5, "target_pct": 2.0, "ref_price": 99.0, "max_adverse_pct": 2.5, "max_hold_min": 10,
             "extended": True, "risk_pct": 0.25, "max_notional": 10000, "max_open": 1, "entry_timeout_s": 15,
             "meta": {"source": "prnewswire"}}


def test_entry_without_a_price_feed_limit_from_before_the_news_stop_and_target_from_the_fill(ext_rig):
    api, sess, ex, tmp, clock, events = ext_rig

    async def fresh(sig, market):
        ex.cooldown.clear()
        ex.done_today.clear()
        clock["t"] += timedelta(minutes=5)
        api.move(market)
        assert await ex.enter(dict(sig))
        t = next(iter(ex.trades.values()))
        await ex.manage()
        return t

    async def go():
        # 1. The stock stood at 99.00 before the news; the bot may pay up to 2.5 % more. The market is at
        #    100.05: filled there, and stop and target are measured from that price, not from the old one.
        t = await fresh(FROM_FILL, 100.0)
        order = api.orders[t.order_id]["payload"]
        assert (order["limit_price"], order["qty"]) == ("101.47", "98")          # sized for the worst case, the limit
        assert (t.entry_price, t.stop_price, t.target_price) == (100.05, 98.55, 102.05)
        assert api.orders[t.target_order_id]["payload"]["limit_price"] == "102.05"
        assert events("trade_open")[-1]["moved_from_ref_pct"] == round(1.05 / 99 * 100, 3)
        api.move(98.4)
        await ex.manage()
        # 2. The market has already run past the limit: no fill, and the order is withdrawn after 15 seconds.
        t = await fresh(FROM_FILL, 102.0)
        assert t.status == "pending"
        clock["t"] += timedelta(seconds=16)
        await ex.manage()
        await ex.manage()
        assert not ex.trades and events("no_fill")
        # 3. The market has fallen 3 % since the news although the rating said "up": the fill shows it,
        #    and the bot gets out at once instead of waiting for the stop.
        t = await fresh(FROM_FILL, 96.0)
        assert not ex.trades and not t.target_order_id and t.exit_reason == "moved_against"
        # 4. Short: sold at the bid, stop above, target below.
        t = await fresh({**FROM_FILL, "side": "short", "limit_price": 96.525}, 99.4)
        assert (t.entry_price, t.stop_price, t.target_price) == (99.35, 100.84, 97.36)
        api.fill(t.target_order_id, 97.36)
        await ex.manage()
    asyncio.run(go())
    rows = trades_file(tmp)
    assert [(r["side"], r["exit_reason"]) for r in rows] == [("long", "stop"), ("long", "moved_against"), ("short", "target")]
    assert rows[0]["r"] == round((98.35 - 100.05) * 98 / (100.05 * 0.015 * 98), 2)        # risk counts from the fill
    assert rows[1]["exit_price"] == 95.95 and rows[1]["signal_price"] == 99.0 and rows[2]["pnl"] == round((99.35 - 97.36) * 101, 2)
    assert rows[2]["qty"] == 101                             # a short is sized from the price before the news, not from its lower limit


def test_session_end_second_opinion_and_a_position_closed_by_hand(ext_rig):
    api, sess, ex, tmp, clock, events = ext_rig

    async def one(then):
        ex.cooldown.clear()
        ex.done_today.clear()
        sess.ext_mins = 120
        clock["t"] += timedelta(minutes=5)
        assert await ex.enter(dict(FAST))
        await ex.manage()
        t = next(iter(ex.trades.values()))
        await then(t)
        clock["t"] += timedelta(seconds=10)
        await ex.manage()
        if then is by_hand:                   # one empty answer from the broker is not enough to give a position up
            assert t.id in ex.trades and t.missing == 1
            await ex.manage()
        assert not ex.trades

    async def session_end(t):
        sess.ext_mins = 9                     # pre-market trades are flat ten minutes before the open

    async def recheck(t):
        assert ex.request_exit("ACME", "recheck") and not ex.request_exit("NONE", "recheck")

    async def by_hand(t):                     # sold in the broker's dashboard: the take-profit is gone, so is the position
        await api.cancel(t.target_order_id)
        api.orders["hand"] = {"id": "hand", "status": "filled", "side": "sell", "symbol": "ACME", "qty": "99",
                              "filled_qty": "99", "filled_avg_price": "100.40", "filled_at": api._stamp(), "legs": [],
                              "submitted_at": api._order_stamp()}
        api.pos["ACME"] = 0

    async def go():
        for then in (session_end, recheck, by_hand):
            await one(then)
    asyncio.run(go())
    rows = trades_file(tmp)
    assert [r["exit_reason"] for r in rows] == ["session_end", "recheck", "external"]
    assert rows[2]["exit_price"] == 100.4 and rows[2]["pnl"] == round(0.35 * 99, 2)


def test_entries_out_of_hours_need_the_switch_the_permission_and_enough_time(ext_rig, tmp_path):
    api, sess, ex, tmp, clock, events = ext_rig

    async def go():
        out = {}
        out["news"] = await ex.can_enter("news", "ACME")                       # the old strategy stays in regular hours
        out["fast"] = await ex.can_enter("fast", "ACME", extended=True, max_open=1)
        sess.ext_mins = 19                                                     # 10 min to the flatten + 10 min margin
        out["late"] = await ex.can_enter("fast", "ACME", extended=True)
        sess.ext_mins, sess.ph = 120, None                                     # overnight
        out["night"] = await ex.can_enter("fast", "ACME", extended=True)
        sess.ph = "post"
        off = Executor(api, OFF, Journal(tmp_path / "off"), sess, FakeUniverse(), root=tmp_path / "off")
        out["switched_off"] = await off.can_enter("fast", "ACME", extended=True)
        # one position at a time for this strategy
        assert await ex.enter(dict(FAST))
        out["second"] = await ex.can_enter("fast", "BETA", extended=True, max_open=1)
        out["other_strategy"] = await ex.can_enter("other", "BETA", extended=True, max_open=1)
        return out
    out = asyncio.run(go())
    assert out == {"news": (False, "session"), "fast": (True, ""), "late": (False, "session"),
                   "night": (False, "session"), "switched_off": (False, "session"),
                   "second": (False, "max_open_strategy"), "other_strategy": (True, "")}


def test_in_regular_hours_a_fast_entry_still_gets_its_stop_at_the_broker(rig):
    api, sess, ex, tmp = rig

    async def go():
        assert await ex.enter(dict(FAST))
        t = next(iter(ex.trades.values()))
        p = api.orders[t.order_id]["payload"]
        assert p["order_class"] == "bracket" and "extended_hours" not in p and p["qty"] == "99"
        assert (t.style, t.phase, t.entry_timeout_s) == ("bracket", "regular", 15)
        api.fill(t.order_id, 100.1, 99)
        await ex.manage()
        assert ex.request_exit("ACME", "recheck")          # the second opinion disagrees
        await ex.manage()
    asyncio.run(go())
    row = trades_file(tmp)[0]
    assert (row["exit_reason"], row["style"]) == ("recheck", "bracket") and api.closed == ["ACME"]


def test_leftovers_and_the_guard_close_with_limit_orders_out_of_hours(ext_rig):
    from tools.guard import ext_due, status_age_s, worth_a_look
    api, sess, ex, tmp, clock, events = ext_rig
    ext = EXT_ON["extended"]

    async def go():
        api.pos["ZZZ"] = -40                                   # a short position nobody is managing
        await ex.close_orphans()
        return await api.positions()
    assert asyncio.run(go()) == [] and api.market_closes == 0
    order = next(o for o in api.orders.values() if o["symbol"] == "ZZZ")["payload"]
    assert (order["side"], order["qty"], order["extended_hours"]) == ("buy", "40", True)
    assert events("orphan")[0]["action"] == "closed"
    # the guard: after the bot's own closing time at the end of both sessions, or when the bot is silent
    assert ext_due("pre", 30, ext, 20) is None and ext_due("pre", 7, ext, 20) == "session_end"
    assert ext_due("pre", 8, ext, 20) is None                  # the bot goes first (10 minutes), the guard 3 minutes later
    assert ext_due("post", 12, ext, 20) == "session_end" and ext_due("post", 13, ext, 20) is None
    assert ext_due("post", 60, ext, 181) == "bot_silent" and ext_due("post", 60, ext, None) == "bot_silent"
    assert ext_due("regular", 60, ext, None) is None and ext_due(None, 60, ext, None) is None
    assert ext_due("pre", 1, OFF["extended"], None) is None    # switched off: the guard leaves these hours alone
    assert status_age_s(tmp) is None
    Journal(tmp).status({"x": 1})
    assert 0 <= status_age_s(tmp) < 5
    night = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)
    assert worth_a_look(True, night) and not worth_a_look(False, night)
    assert worth_a_look(False, night.replace(hour=20)) and not worth_a_look(False, datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc))


def test_day_result_with_trading_after_the_close_starts_at_the_beginning_of_the_day():
    from bot.main import day_result
    acct = {"equity": "101000", "last_equity": "100500"}
    # the broker's value from 16:00 the day before would count yesterday evening's trades a second time
    assert day_result(acct, 100000.0, 100800.0) == {"equity_start": 100800.0, "equity_end": 101000.0, "pnl": 200.0,
                                                    "basis": "day_start"}
    assert day_result(acct, 100000.0, None)["basis"] == "last_close" and day_result(acct, 100000.0)["pnl"] == 500.0


def test_a_whole_morning_and_evening_out_of_hours_through_the_running_bot(tmp_path, monkeypatch):
    """The running program before the open and after the close: a trade is stopped out, a second one
    is closed when the session nears its end, and the day's summary is written after the evening
    session. Not a single market order, because the broker would hold it until the next open."""
    from collections import Counter
    from bot import common as common_mod, main as main_mod, orb_strategy as orb_mod

    for mod in (common_mod, main_mod, ex_mod, orb_mod):
        monkeypatch.setattr(mod, "DATA", tmp_path)
    for name, value in (("APCA_API_KEY_ID", "test"), ("APCA_API_SECRET_KEY", "test")):
        monkeypatch.setenv(name, value)
    for name in ("ANTHROPIC_API_KEY", "APCA_BASE_URL", "APCA_DATA_FEED", "SEC_CONTACT", "FINNHUB_API_KEY",
                 "WIREALPHA_API_KEY", "ALPHAVANTAGE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    real_sleep = asyncio.sleep

    async def quick(seconds):                       # waits still give way to the other tasks, only shorter
        await real_sleep(min(seconds, 0.01))
    monkeypatch.setattr(asyncio, "sleep", quick)

    class Now(dict):
        def __getitem__(self, key): return datetime.now(timezone.utc)

    class Broker(ExtBroker):
        cancel_alls = 0
        async def calendar(self, day, end=None):
            return [{"date": day, "open": "09:30", "close": "16:00", "session_open": "0400", "session_close": "2000"}]
        async def assets(self):
            return [{"symbol": "ACME", "class": "us_equity", "status": "active", "tradable": True, "exchange": "NYSE",
                     "name": "Acme Corp", "shortable": True, "easy_to_borrow": True}]
        async def cancel_all(self):
            self.cancel_alls += 1
            for o in await self.open_orders():
                await self.cancel(o["id"])
        async def snapshots(self, symbols):
            return {s: await self.snapshot(s) for s in symbols}

    broker = Broker(Now())
    cfg = json.loads(json.dumps(FAST_V2))            # the second version: a look at bid and offer before the entry
    cfg["execution"]["poll_s"] = cfg["extended"]["poll_s"] = 0.02
    cfg["fast"]["fetch_text"] = False                # no request to the wire's website from a test
    for part in ("edgar", "wires", "speed"):
        cfg[part]["enabled"] = False
    monkeypatch.setattr(main_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(main_mod, "Alpaca", lambda *a, **k: broker)
    day = datetime.now(timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "day_start.json").write_text(json.dumps({"day": day, "equity": 100000.0}))

    async def scenario():
        b = main_mod.Bot()

        async def idle():
            await real_sleep(3600)
        b.news_loop = idle

        def seen(kind):
            return sum(1 for r in b.journal.recent_events(1) if r["kind"] == kind)

        async def until(what, check):
            for _ in range(400):
                if check():
                    return
                await real_sleep(0.02)
            raise AssertionError(f"not reached: {what}; events: {[r for r in b.journal.recent_events(1)][-12:]}")

        def holding():
            return any(t.status == "open" and t.target_order_id for t in b.executor.trades.values())

        s = b.session
        await s.refresh()
        t = datetime.now(timezone.utc)              # pretend: pre-market trading started two hours ago
        s.ext_open, s.open, s.close, s.ext_close = (t + timedelta(hours=h) for h in (-2, 2, 8, 12))
        task = asyncio.ensure_future(b.run())
        try:
            await until("day start", lambda: seen("day_start"))
            assert await b.executor.enter(dict(FAST))                     # what the fast strategy will send
            await until("position with take-profit at the broker", holding)
            broker.move(98.0)                                             # through the stop
            await until("stopped out", lambda: not b.executor.trades)
            b.executor.cooldown.clear()
            b.executor.done_today.clear()
            broker.move(100.0)
            assert await b.executor.enter(dict(FAST))
            await until("second position", holding)
            s.open = datetime.now(timezone.utc) + timedelta(minutes=9)    # ten minutes before the open everything is closed
            await until("pre-market closed out", lambda: seen("session_end") == 1)
            assert not b.executor.trades and not await broker.positions()
            assert not await b.executor.enter(dict(FAST))                 # and nothing new is opened
            # Evening, an hour after the close: a press release arrives the way the wire delivers it.
            t = datetime.now(timezone.utc)
            s.open, s.close, s.ext_close = t - timedelta(hours=8), t - timedelta(hours=1), t + timedelta(hours=3)
            b.executor.cooldown.clear()
            b.executor.done_today.clear()

            class Stats:
                async def get(self, syms):
                    return {x: {"price": 100.0, "dollar_volume": 5e7, "daily_range_pct": 2.5, "size": "mid-sized"} for x in syms}
            b.fast.llm, b.fast.stock_stats = FakeModel("claude-opus-5-5", "up"), Stats()
            # A filing with the SEC, seen 50 seconds after it was accepted: read, rated and traded.
            async def what_it_says(entry): return "Item 8.01 Other Events. Acme won a large contract."
            b.edgar.text = what_it_says
            await b.on_filing({"acc": "0000000011-26-000001", "form": "8-K", "company": "ACME CORP", "cik": 11,
                               "accepted": t - timedelta(seconds=50), "items": ["8.01"], "link": ""}, "ACME")
            await until("position from the filing", holding)
            fast_trade = next(iter(b.executor.trades.values()))
            assert (fast_trade.strategy, fast_trade.phase, fast_trade.entry_price) == ("fast", "post", 100.05)
            assert fast_trade.meta["source"] == "sec" and fast_trade.meta["article"] == "0000000011-26-000001"
            # one share there and back showed bid and offer; the order then went out just above the offer
            assert fast_trade.meta["market"]["how"] == "probe" and fast_trade.meta["market"]["spread_pct"] == 0.1
            assert broker.orders[fast_trade.order_id]["payload"]["limit_price"] == "100.25" and broker.pos["ACME"] == fast_trade.qty
            # The company's press release follows: rated (the model is told what was seen before), no second position.
            await b.on_wire({"id": "prnewswire:e2e", "headline": "Acme wins large contract", "summary": "", "source": "prnewswire",
                             "symbols": ["ACME"], "url": "", "created_at": iso(t - timedelta(seconds=20))})
            await until("press release rated and turned away", lambda: seen("fast_rejected") == 1)
            assert len(b.executor.trades) == 1 and broker.pos["ACME"] == fast_trade.qty
            assert "files 8-K with the SEC" in b.fast.llm.asked[-1]["context"]
            s.ext_close = datetime.now(timezone.utc) + timedelta(minutes=14)   # the last minutes after the close
            await until("day's summary", lambda: seen("day_end") >= 1)
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
    trades = trades_file(tmp_path)
    assert [(r["exit_reason"], r["style"], r["phase"], r["strategy"]) for r in trades] == [
        ("stop", "soft", "pre", "fast"), ("session_end", "soft", "pre", "fast"), ("session_end", "soft", "post", "fast")]
    assert kinds["fast_rated"] == 2 and trades[2]["meta"]["ref_price"] == 99.0 and trades[2]["price_changes"] >= 1
    assert [(r["source"], r["reason"]) for r in rows if r["kind"] == "fast_rejected"] == [("prnewswire", "already_in_symbol")]
    assert kinds["fast_probe"] == 1 and kinds["fast_shadow"] == 4      # two more models are asked about both announcements
    start = next(r for r in rows if r["kind"] == "day_start")
    assert (start["news"], start["look"], start["sec_rated"], start["sec_traded"], start["wirealpha"]) == (False, True, True, True, False)
    assert [r["phase"] for r in rows if r["kind"] == "session_end"] == ["pre", "post"]
    assert broker.market_closes == 0 and broker.cancel_alls >= 2 and not await_none(broker.open_orders())
    daily = json.loads((tmp_path / "logs" / "daily" / f"{day}.json").read_text())
    assert (daily["basis"], daily["equity_start"], daily["trades"], daily["extended"]) == ("day_start", 100000.0, 3, True)
    assert daily["fast"] == {"received": 2, "rated": 2, "candidates": 2, "entered": 1}
    status = json.loads((tmp_path / "logs" / "status.json").read_text())
    assert status["extended"] is True and status["open_trades"] == [] and status["news_stream"] is False
    assert status["wire_sources"]["wirealpha"]["connected"] is False and "handed_on" in status["edgar"]
    assert kinds["feed_view"] >= 1                                        # the feed measurement runs alongside


def await_none(coro):
    """Run a small coroutine of a fake outside the event loop."""
    return asyncio.run(coro)


def test_the_bot_notes_how_much_of_the_market_its_price_feed_shows(tmp_path, monkeypatch):
    from bot import feedview as fv_mod
    from bot.feedview import FeedView
    at = datetime.now(timezone.utc)
    stamp = lambda age_s: (at - timedelta(seconds=age_s)).isoformat().replace("+00:00", "Z")
    snaps = {
        "NVDA": {"latestTrade": {"p": 150.0, "t": stamp(3)}, "latestQuote": {"bp": 149.9, "ap": 150.1, "t": stamp(2)}},
        "TSLA": {"latestTrade": {"p": 390.0, "t": stamp(1300)}, "latestQuote": {"bp": 365.0, "ap": 404.0, "t": stamp(60000)}},
        "WMT": {"latestTrade": {"p": 127.0, "t": stamp(61000)}, "latestQuote": {"bp": 119.6, "ap": 131.1, "t": stamp(61000)}},
        "LZB": {"latestTrade": {"p": 31.4, "t": stamp(40)}, "latestQuote": {"bp": 0, "ap": 0}},
    }

    class Api:
        feed, asked = "iex", []
        async def snapshots(self, symbols):
            self.asked.append(list(symbols))
            return {s: snaps[s] for s in symbols if s in snaps}

    class U:
        cache = {"WMT": {}, "LZB": {}, "NVDA": {}, "TSLA": {}}

    sess = FakeSession()
    sess.ph = "pre"
    j = Journal(tmp_path)
    j.event("wire_item", symbol="WMT", id="w1")           # announcements of the last hour ...
    j.event("sec_item", symbol="LZB", acc="s1")
    j.event("wire_item", symbol="WMT", id="w2")
    j.event("wire_item", symbol="NVDA", id="w3")          # ... one of them in the liquid list already
    j.event("sec_item", symbol="GONE", acc="s2")          # ... and one the bot cannot trade
    api = Api()
    fv = FeedView(api, CFG["feedview"], CFG["extended"], j, sess, U(), ["NVDA", "TSLA"])

    async def go():
        await fv.step()
        await fv.step()                                    # too soon: nothing happens
        sess.ph = None
        fv.last = None
        await fv.step()                                    # no session running: nothing happens
    asyncio.run(go())
    assert api.asked == [["NVDA", "TSLA", "WMT", "LZB"]]
    row = [r for r in j.recent_events(1) if r["kind"] == "feed_view"]
    assert len(row) == 1 and (row[0]["phase"], row[0]["feed"]) == ("pre", "iex")
    assert row[0]["liquid"] == {"n": 2, "seeing": 1, "trade_60s": 1, "quote_ok": 1}
    assert row[0]["news"] == {"n": 2, "seeing": 0, "trade_60s": 1, "quote_ok": 0}
    assert CFG["feedview"]["enabled"] and len(CFG["orb"]["static_pool"]) == 20


# ── immediate entry on a fresh announcement ─────────────────────────────────
# The first version of the immediate entry (19.11.): before the open and after the close only, the limit from
# the price before the news, no look at the market. Its tests stay as they were; the second version (24.11.)
# adds the look at bid and offer and the regular session, and has tests of its own further down.
FAST_ON = {**EXT_ON, "fast": {**CFG["fast"], "enabled": True, "sessions": ["pre", "post"], "shadows": False,
                               "look": {**CFG["fast"]["look"], "enabled": False}}}
FAST_V2 = {**EXT_ON, "fast": {**CFG["fast"], "enabled": True}}


def test_full_text_of_a_press_release():
    from bot.wires import parse_feed, release_text, to_article
    page = """<html><head><script>var x = "<p>not this</p>";</script></head><body><nav><p>Menu</p></nav>
      <section class="release-body container"><div class="col">
        <p><span class="xn-location">NEW YORK</span>, Nov. 19, 2026 /PRNewswire/ -- Acme Corp (NYSE: ACME) today
           reported third-quarter revenue of $5.2&nbsp;billion.</p>
        <table><tr><td><p>1,234</p></td></tr></table>
        <p>The company raised its full-year outlook.</p><script>track()</script><p> </p>
      </div></section><footer><p>Cookies</p></footer></body></html>"""
    text = release_text(page)
    assert text == ("NEW YORK , Nov. 19, 2026 /PRNewswire/ -- Acme Corp (NYSE: ACME) today reported third-quarter "
                    "revenue of $5.2 billion. The company raised its full-year outlook.")
    assert release_text("<html><body><p>Access denied</p></body></html>") == "" and len(release_text(page, 20)) == 20
    rss = """<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item><title>Acme Reports</title>
      <link>https://www.prnewswire.com/news-releases/acme-reports-302000001.html</link>
      <guid>https://www.prnewswire.com/news-releases/acme-reports-302000001.html</guid>
      <pubDate>Thu, 19 Nov 2026 21:05:00 +0000</pubDate><description>Acme today...</description>
      <dc:contributor>Acme Corp</dc:contributor><dc:language>en</dc:language></item></channel></rss>"""
    art = to_article(parse_feed(rss)[0], NameIndex({"ACME": {"name": "Acme Corp"}}), {"ACME": {}}, "prnewswire", 420)
    assert art["url"].endswith("acme-reports-302000001.html") and art["symbols"] == ["ACME"]


def test_what_analysts_expect_is_put_in_front_of_the_rating(monkeypatch):
    from bot import finnhub as fh_mod
    from bot.finnhub import Expectations, Finnhub, expectation_text
    from bot.llm import Classifier
    today = datetime(2026, 11, 19).date()
    cal = {"earningsCalendar": [
        {"symbol": "ACME", "date": "2026-11-19", "hour": "amc", "quarter": 3, "year": 2026, "epsEstimate": 1.2499, "revenueEstimate": 5.2e9},
        {"symbol": "ACME", "date": "2026-11-20", "hour": "bmo", "epsEstimate": 9.9, "revenueEstimate": None},
        {"symbol": "TINY", "date": "2026-11-18", "hour": "", "epsEstimate": None, "revenueEstimate": 41.5e6}]}
    past = [{"period": "2026-06-30", "actual": 1.1, "estimate": 1.02}, {"period": "2026-03-31", "actual": None, "estimate": 0.9},
            {"period": "2025-12-31", "actual": 0.8, "estimate": 0.95}]

    async def go():
        fh = Finnhub("k", http=FinnhubHttp((200, cal, {}), (200, past, {})))
        exp = Expectations(fh)
        assert await exp.refresh(today) == 2
        first = await exp.context("ACME")
        again = await exp.context("ACME")                       # the earlier quarters are asked for only once
        return fh, first, again, await exp.context("NONE"), expectation_text(exp.rows["TINY"])
    fh, first, again, none, tiny = asyncio.run(go())
    assert first == again and none is None and len(fh.http.calls) == 2
    assert fh.http.calls[0][1] == {"from": "2026-11-18", "to": "2026-11-20"}
    assert "ACME reports quarterly results on 2026-11-19 (after the market close), fiscal Q3 2026." in first
    assert "earnings per share of 1.25 and revenue of 5.20 billion" in first       # today's report, not tomorrow's row
    assert "2026-06-30: 1.10 against 1.02; 2025-12-31: 0.80 against 0.95." in first and "2026-03-31" not in first
    assert "revenue of 41.5 million" in tiny and "earnings per share" not in tiny.split("\n")[1] and "time of day not known" in tiny
    assert asyncio.run(Expectations(Finnhub("")).context("ACME")) is None           # no key: no expectations, no call

    # the rating: expectations and the full text reach the model, and the usual request stays as it was
    ok = (200, {"content": [{"type": "text", "text": ANSWER}], "usage": {"input_tokens": 1, "output_tokens": 1}}, {})
    art = {"headline": "Acme Reports", "summary": "Acme today...", "source": "prnewswire", "created_at": "", "text": "T" * 9000}

    async def ask(**kw):
        c = Classifier("k", CFG["llm"], model="claude-haiku-5-5")
        c.http = ModelHttp(ok)
        await c.rate(kw.pop("article", art), ["ACME"], **kw)
        return c.http.bodies[0]["messages"][0]["content"]
    plain = asyncio.run(ask(article={k: v for k, v in art.items() if k != "text"}))
    assert plain.endswith("Headline: Acme Reports\nSummary: Acme today...") and "Known beforehand" not in plain
    rich = asyncio.run(ask(context=first, text_chars=6000))
    assert "Known beforehand:\nACME reports quarterly results" in rich and rich.endswith("Text: " + "T" * 6000)
    assert rich.index("Known beforehand") < rich.index("Headline:")


@pytest.fixture
def fast_rig(ext_rig):
    from bot.fast import FastStrategy
    api, sess, ex, tmp, clock, events = ext_rig
    from bot import fast as fast_mod

    class Uni(FakeUniverse):
        async def asset(self, s): return {"symbol": s}
        async def name(self, s): return "Acme Corp"
        async def is_fund(self, s): return s == "FUND"

    class Stats:
        size, dollar_volume = "mid-sized", 5e7
        async def get(self, syms):
            return {s: {"price": 100.0, "dollar_volume": self.dollar_volume, "daily_range_pct": 2.5, "size": self.size} for s in syms}

    def make(model, cfg=FAST_ON, **kw):
        return FastStrategy(api, cfg, ex.journal, sess, Uni(), ex, model, stats=Stats(), **kw)

    def article(n=1, age_s=120, sym="ACME", head=None):
        return {"id": f"prnewswire:{n}", "headline": head or f"Acme raises outlook {n}", "summary": "Acme today...",
                "source": "prnewswire", "symbols": [sym], "url": "https://www.prnewswire.com/news-releases/x.html",
                "created_at": iso(clock["t"] - timedelta(seconds=age_s))}
    return api, sess, ex, tmp, clock, events, make, article, fast_mod


def test_fresh_announcement_before_the_open_is_rated_and_bought_at_once(fast_rig, monkeypatch):
    api, sess, ex, tmp, clock, events, make, article, fast_mod = fast_rig
    monkeypatch.setattr(fast_mod, "now", lambda: clock["t"])
    model = FakeModel("claude-opus-5-5", "up")

    class Exp:
        async def context(self, sym): return "ACME reports quarterly results today."

    async def text(url): return "Full text of the release."
    fast = make(model, expectations=Exp(), fetch_text=text)

    async def go():
        await fast.on_article(article())
        t = next(iter(ex.trades.values()))
        await ex.manage()
        return t
    t = asyncio.run(go())
    # the model saw the expectations and the full text
    asked = model.asked[0]
    assert asked["context"].startswith("ACME reports") and asked["article"]["text"].startswith("Full text") and asked["text_chars"] == 6000
    # the price before the news was 99.00; "large" news is expected to move the stock 5 %, half of that may be gone
    order = api.orders[t.order_id]["payload"]
    assert (order["limit_price"], order["qty"], order["extended_hours"]) == ("101.47", "98", True)
    assert api.bar_asked[2] == "sip" and parse_ts(api.bar_asked[1]) == clock["t"] - timedelta(minutes=16)
    # filled at the real offer; stop one and a half times the usual 1 %, target three times 1 %, counted from the fill
    assert (t.entry_price, t.stop_price, t.target_price, t.max_hold_min, t.strategy) == (100.05, 98.55, 103.05, 15, "fast")
    assert t.meta["ref_price"] == 99.0 and t.meta["news_to_order_s"] == 120.0 and t.meta["with_expectations"] is True
    rated = events("fast_rated")[0]
    assert (rated["symbol"], rated["age_s"], rated["with_text"], rated["model"]) == ("ACME", 120, True, "claude-opus-5-5")
    assert fast.stats == {"received": 1, "rated": 1, "candidates": 1, "entered": 1}


def test_what_the_immediate_entry_leaves_alone(fast_rig, monkeypatch):
    api, sess, ex, tmp, clock, events, make, article, fast_mod = fast_rig
    monkeypatch.setattr(fast_mod, "now", lambda: clock["t"])
    model = FakeModel("claude-opus-5-5", "up")
    fast = make(model)

    async def go():
        out = {}
        async def one(name, art):
            before = (model.day_calls, len(ex.trades))
            await fast.on_article(art)
            out[name] = (model.day_calls - before[0], len(ex.trades) - before[1])
        await one("stale", article(1, age_s=301))                       # older than five minutes: not even rated
        await one("fund", article(2, sym="FUND"))
        await one("noise", article(3, head="Why is Acme stock moving today?"))
        await one("two_stocks", {**article(4), "symbols": ["ACME", "BETA"]})
        fast.stock_stats.dollar_volume = 1e6
        await one("too_small", article(5))
        fast.stock_stats.dollar_volume = 5e7
        model.confidence = 6
        await one("unsure", article(6))                                 # rated, but not sure enough of the direction
        model.confidence, api.ref = 7, None
        await one("no_reference", article(7))                           # no price from before the news
        api.ref = 99.0
        sess.ph = "regular"
        await one("regular_hours", article(8))                          # the regular session keeps its old rule
        sess.ph = "post"
        fast.stock_stats.size = "small"
        await one("enters", article(9))
        await one("same_again", article(9))
        await one("same_headline", {**article(10), "headline": "Acme raises outlook 9"})
        await one("second_position", article(11, sym="BETA"))           # one position at a time
        off = make(model, cfg={**FAST_ON, "fast": {**FAST_ON["fast"], "enabled": False}})
        before = model.day_calls
        await off.on_article(article(12, sym="GAMMA"))
        out["switched_off"] = (model.day_calls - before, 0)
        return out
    out = asyncio.run(go())
    assert out == {"stale": (0, 0), "fund": (0, 0), "noise": (0, 0), "two_stocks": (0, 0), "too_small": (0, 0),
                   "unsure": (1, 0), "no_reference": (1, 0), "regular_hours": (0, 0), "enters": (1, 1),
                   "same_again": (0, 0), "same_headline": (0, 0), "second_position": (1, 0), "switched_off": (0, 0)}
    assert [e["reason"] for e in events("fast_skipped")] == ["stale", "fund", "noise", "too_small"]
    assert [e["reason"] for e in events("fast_rejected")] == ["no_reference", "max_open_strategy"]
    t = next(iter(ex.trades.values()))
    assert api.orders[t.order_id]["payload"]["qty"] == "49" and t.max_hold_min == 10      # small company: 5,000 $; evening: 10 minutes
    assert len(events("fast_rated")) == 4 and not events("error")


def test_a_falling_stock_is_sold_short_and_a_rating_that_arrives_too_late_is_not_traded(fast_rig, monkeypatch):
    api, sess, ex, tmp, clock, events, make, article, fast_mod = fast_rig
    monkeypatch.setattr(fast_mod, "now", lambda: clock["t"])

    class Slow(FakeModel):
        async def rate(self, *a, **kw):
            clock["t"] += timedelta(seconds=self.takes)
            return await super().rate(*a, **kw)
    model = Slow("claude-opus-5-5", "down", impact=3)
    model.takes = 100
    fast = make(model)

    async def go():
        await fast.on_article(article(1, age_s=250))           # 250 s old on arrival, 350 s when the rating is in
        assert not ex.trades
        model.takes = 2
        api.move(98.0)
        await fast.on_article(article(2, age_s=60))
        t = next(iter(ex.trades.values()))
        await ex.manage()
        return t
    t = asyncio.run(go())
    assert [e["reason"] for e in events("fast_rejected")] == ["too_old"]
    order = api.orders[t.order_id]["payload"]
    # very large news: 10 % expected, the price may be 5 % below the 99.00 from before the news
    assert (order["side"], order["limit_price"]) == ("sell", "94.05") and t.side == "short"
    # stop 2 % x 1.5 = 3 % above the fill, target 2 % x 3 = 6 % below it
    assert (t.entry_price, t.stop_price, t.target_price) == (97.95, 100.89, 92.07)


def test_two_announcements_in_the_same_second_open_only_one_position(fast_rig, monkeypatch):
    api, sess, ex, tmp, clock, events, make, article, fast_mod = fast_rig
    monkeypatch.setattr(fast_mod, "now", lambda: clock["t"])

    async def slow_submit(p, _submit=api.submit):
        for _ in range(5):          # the broker takes a moment to answer: time for the other entry to overlap
            await asyncio.get_running_loop().run_in_executor(None, lambda: None)
        return await _submit(p)
    api.submit = slow_submit

    class NoLock:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    def run(lock):
        ex.trades.clear()
        ex.done_today.clear()
        if not lock:
            ex._entering = NoLock()
        fast = make(FakeModel("claude-opus-5-5", "up"))
        n = len(events("rejected"))

        async def go():
            await asyncio.gather(fast.on_article(article(1, sym="ACME")), fast.on_article(article(2, sym="BETA")))
        asyncio.run(go())
        return len(ex.trades), fast.stats["entered"], [e["reason"] for e in events("rejected")[n:]]
    assert run(lock=True) == (1, 1, ["max_open_strategy"])
    assert run(lock=False)[0] == 2          # without deciding one after the other, both would get through


# ── second version of the immediate entry (24.11.2026): a look at the market first ───────────
def test_a_probe_trades_one_share_there_and_back_and_tells_bid_and_offer(ext_rig):
    api, sess, ex, tmp, clock, events = ext_rig

    async def go():
        out = {}
        out["narrow"] = await ex.probe("ACME", "long", 101.47)
        assert [(o["payload"]["side"], o["payload"]["qty"], o["payload"]["limit_price"]) for o in api.orders.values()] == [
            ("buy", "1", "101.47"), ("sell", "1", "99.55")]            # back at most 0.5 % below the price paid
        out["short"] = await ex.probe("ACME", "short", 96.5)
        api.move(100.0, spread=1.0)                                     # bid 99.50, offer 100.50
        n = len(api.orders)
        out["wide"] = await ex.probe("ACME", "long", 101.47)
        assert await api.position("ACME") is None                       # the share is sold again, far through the market:
        assert [o["payload"]["limit_price"] for o in list(api.orders.values())[n:]] == ["101.47", "100.0", "80.4"]
        api.move(100.0, spread=9.0)                                     # bid 95.50, offer 104.50: wherever the bid is
        out["very_wide"] = await ex.probe("ACME", "long", 105.0)
        assert await api.position("ACME") is None
        api.move(102.0)
        out["ran_away"] = await ex.probe("ACME", "long", 101.47)        # the offer is beyond the limit of the planned trade
        assert await api.position("ACME") is None and not await api.open_orders()
        api.move(100.0)
        assert await ex.enter(dict(FAST))                               # the bot now holds the stock ...
        n = len(api.orders)
        out["busy"] = await ex.probe("ACME", "long", 101.47)            # ... so one more share could not be told apart
        assert len(api.orders) == n
        return out
    out = asyncio.run(go())
    assert (out["narrow"]["ok"], out["narrow"]["bid"], out["narrow"]["ask"], out["narrow"]["spread_pct"]) == (True, 99.95, 100.05, 0.1)
    assert (out["short"]["ok"], out["short"]["bid"], out["short"]["ask"]) == (True, 99.95, 100.05)
    # too wide for an entry, and the probe still tells how wide: the share came back at the bid
    assert (out["wide"]["ok"], out["wide"]["reason"], out["wide"]["bid"], out["wide"]["ask"], out["wide"]["spread_pct"]) == (
        False, "spread", 99.5, 100.5, 1.0)
    assert (out["very_wide"]["reason"], out["very_wide"]["bid"], out["very_wide"]["ask"]) == ("spread", 95.5, 104.5)
    assert (out["ran_away"]["ok"], out["ran_away"]["reason"]) == (False, "probe_no_fill")
    assert (out["busy"]["ok"], out["busy"]["reason"]) == (False, "probe_busy")
    assert [e.get("reason") for e in events("fast_probe")] == [None, None, "spread", "spread", "probe_no_fill", "probe_busy"]
    assert api.market_closes == 0 and not events("error") and not ex.stray


def test_a_share_a_probe_could_not_hand_back_closes_the_stock_to_entries_until_it_is_gone(ext_rig):
    api, sess, ex, tmp, clock, events = ext_rig

    async def go():
        api.move(100.0, spread=1.0)
        submit = api.submit

        async def nobody_buys(p):                    # after the first fill the market is gone: no sell is filled
            api.auto = p["side"] == "buy"
            return await submit(p)
        api.submit = nobody_buys
        res = await ex.probe("ACME", "long", 101.47)
        assert (res["ok"], res["reason"]) == (False, "probe_error") and api.pos["ACME"] == 1
        assert ex.stray == {"ACME": None} and [e["where"] for e in events("error")] == ["probe_leftover"]
        api.submit, api.auto = submit, True
        api.move(100.0)
        assert await ex.can_enter("fast", "ACME", extended=True) == (False, "stray_share")
        assert (await ex.probe("ACME", "long", 101.47))["reason"] == "probe_busy" and not await ex.enter(dict(FAST))
        assert await ex.can_enter("fast", "BETA", extended=True) == (True, "")         # other stocks are not affected
        await ex.close_orphans()                     # what the end of the session does with anything left over
        assert await api.position("ACME") is None
        assert await ex.can_enter("fast", "ACME", extended=True) == (True, "") and not ex.stray
        ex._probing.add("ACME")                      # while one share is on its way, nobody else enters the stock
        assert await ex.can_enter("news", "ACME", extended=True) == (False, "probing")
    asyncio.run(go())
    assert api.market_closes == 0


def test_an_entry_needs_a_narrow_market_that_has_not_run_away(fast_rig, monkeypatch):
    api, sess, ex, tmp, clock, events, make, article, fast_mod = fast_rig
    monkeypatch.setattr(fast_mod, "now", lambda: clock["t"])
    fast = make(FakeModel("claude-opus-5-5", "up"), cfg=FAST_V2)       # the price before the news was 99.00

    async def one(n, price, spread=0.10, quote_age_s=30):
        api.move(price, spread)
        api.quote_age_s = quote_age_s
        before = len(api.orders)
        await fast.on_article(article(n, sym=f"S{n}"))
        return len(api.orders) - before, len(ex.trades)

    async def go():
        out = {}
        out["wide"] = await one(1, 100.0, spread=0.8)                   # 0.8 % between bid and offer
        out["against"] = await one(2, 98.2)                             # good news, but the offer is 0.76 % below the old price
        out["ran_away"] = await one(3, 102.0)                           # beyond half of the expected 5 %
        out["feed_too_far"] = await one(4, 102.0, quote_age_s=2)        # the feed shows it: no probe needed
        out["feed_wide"] = await one(5, 100.0, spread=0.8, quote_age_s=2)   # one exchange's wide quote says nothing: probe
        out["enters"] = await one(6, 100.0)
        await ex.manage()
        return out
    out = asyncio.run(go())
    # orders sent: a probe that comes back is two, one that does not sells the share again (three), an entry is one more
    assert out == {"wide": (3, 0), "against": (2, 0), "ran_away": (1, 0), "feed_too_far": (0, 0), "feed_wide": (3, 0),
                   "enters": (3, 1)}
    assert [e["reason"] for e in events("fast_rejected")] == ["spread", "moved_against", "probe_no_fill", "too_far", "spread"]
    assert [e.get("how") for e in events("fast_rejected")] == ["probe", "probe", "probe", "feed", "probe"]
    t = next(iter(ex.trades.values()))
    order = api.orders[t.order_id]["payload"]
    # the limit sits 0.2 % above the offer seen (100.05), not at the 101.47 the price before the news would allow
    assert (order["limit_price"], order["qty"], order["extended_hours"]) == ("100.25", "99", True)
    assert (t.entry_price, t.stop_price, t.target_price) == (100.05, 98.55, 103.05)
    assert t.meta["market"] == {"how": "probe", "bid": 99.95, "ask": 100.05, "spread_pct": 0.1, "moved_pct": 1.061,
                                "seconds": t.meta["market"]["seconds"]}
    assert await_none(api.position("S1")) is None and await_none(api.position("S5")) is None and not events("error")


def test_a_feed_quote_that_is_fresh_and_narrow_saves_the_probe(fast_rig, monkeypatch):
    api, sess, ex, tmp, clock, events, make, article, fast_mod = fast_rig
    monkeypatch.setattr(fast_mod, "now", lambda: clock["t"])
    api.quote_age_s = 3
    fast = make(FakeModel("claude-opus-5-5", "down"), cfg=FAST_V2)
    api.move(98.5)                                                      # bad news, the stock is 0.5 % below the old 99.00

    async def go():
        await fast.on_article(article())
        await ex.manage()
    asyncio.run(go())
    t = next(iter(ex.trades.values()))
    assert len(api.orders) == 2 and not events("fast_probe")           # the entry and the take-profit, nothing else
    order = api.orders[t.order_id]["payload"]
    assert (order["side"], order["limit_price"]) == ("sell", "98.25")  # 0.2 % below the bid of 98.45
    assert t.meta["market"]["how"] == "feed" and t.entry_price == 98.45


class RegBroker(FakeApi):
    """A broker during the regular session: fills what crosses its bid and offer, keeps brackets."""
    bid, ask, feed = 99.95, 100.05, "iex"

    async def submit(self, p):
        o = await super().submit(p)
        limit = float(p["limit_price"])
        if limit >= self.ask if p["side"] == "buy" else limit <= self.bid:
            self.fill(o["id"], self.ask if p["side"] == "buy" else self.bid, int(p["qty"]))
        return o

    async def last_bar(self, sym, start, end, feed="sip"):
        t = datetime.now(timezone.utc)
        # the whole market's price is 20 minutes old; the free feed's own last minute before the news is newer
        return {"t": iso(t - timedelta(minutes=20)), "c": 99.0} if feed == "sip" else {"t": iso(t - timedelta(minutes=2)), "c": 99.6}


def test_during_the_session_the_immediate_entry_looks_first_and_leaves_stop_and_target_at_the_broker(tmp_path, monkeypatch):
    from bot.fast import FastStrategy

    async def no_sleep(_): return None
    monkeypatch.setattr(ex_mod.asyncio, "sleep", no_sleep)
    api, sess, j = RegBroker(), FakeSession(), Journal(tmp_path)

    class Uni(FakeUniverse):
        async def asset(self, s): return {"symbol": s}
        async def name(self, s): return "Acme Corp"
        async def is_fund(self, s): return False

    class Stats:
        async def get(self, syms):
            return {s: {"price": 100.0, "dollar_volume": 5e7, "daily_range_pct": 2.5, "size": "mid-sized"} for s in syms}

    ex = Executor(api, FAST_V2, j, sess, Uni(), root=tmp_path)
    fast = FastStrategy(api, FAST_V2, j, sess, Uni(), ex, FakeModel("claude-opus-5-5", "up"), stats=Stats())

    def art(n, sym):
        return {"id": f"businesswire:{n}", "headline": f"{sym} to be acquired {n}", "summary": "", "source": "businesswire",
                "symbols": [sym], "url": "", "created_at": iso(datetime.now(timezone.utc) - timedelta(seconds=45))}

    async def go():
        sess.since = 3
        await fast.on_article(art(1, "EARLY"))                          # the first five minutes are left alone
        sess.since, sess.mins = 300, 25
        await fast.on_article(art(2, "LATE"))                           # and the last half hour
        sess.since, sess.mins = 60, 200
        await fast.on_article(art(3, "ACME"))
        await ex.manage()
    asyncio.run(go())
    rows = list(j.recent_events(1))
    assert [r["reason"] for r in rows if r["kind"] == "fast_rejected"] == ["session_window", "session_window"]
    probe = [o["payload"] for o in api.orders.values() if o["payload"]["client_order_id"].startswith("probe-")]
    assert [(p["side"], p["qty"]) for p in probe] == [("buy", "1"), ("sell", "1")] and not any("extended_hours" in p for p in probe)
    t = next(iter(ex.trades.values()))
    order = api.orders[t.order_id]["payload"]
    # the reference is the feed's own last minute before the news (99.60), newer than the whole market's free price
    assert t.meta["ref_price"] == 99.6 and t.meta["market"]["how"] == "probe"
    # one bracket order: limit 0.2 % above the offer; stop 1.5 % and target 3 % from the offer seen (100.05),
    # which is where the order is filled, both resting at the broker
    assert (order["order_class"], order["limit_price"], order["qty"]) == ("bracket", "100.25", "99")
    assert (order["stop_loss"]["stop_price"], order["take_profit"]["limit_price"]) == ("98.55", "103.05")
    assert (t.style, t.phase, t.max_hold_min, t.status, t.entry_price) == ("bracket", "regular", 15, "open", 100.05)
    assert not [r for r in rows if r["kind"] == "error"]


def test_the_model_is_told_what_was_seen_of_the_company_before_and_a_headline_can_be_rated_alone(fast_rig, monkeypatch):
    api, sess, ex, tmp, clock, events, make, article, fast_mod = fast_rig
    monkeypatch.setattr(fast_mod, "now", lambda: clock["t"])
    model = FakeModel("claude-opus-5-5", "none")
    fast = make(model, cfg=FAST_V2)
    fetched = []

    async def no_text():
        fetched.append(1)
        return ""

    async def go():
        await fast.on_article({**article(1, head="Acme to be acquired by Beta for $42 a share"), "url": "",
                               "source": "wa-globenewswire", "fetch": no_text, "text_optional": True})
        clock["t"] += timedelta(minutes=3)
        await fast.on_article({**article(2, age_s=50, head="ACME CORP files 8-K with the SEC: Other Events"), "source": "sec",
                               "note": "This is a filing.", "fetch": lambda: await_text("Item 8.01 Acme agreed to be acquired.")})
        clock["t"] += timedelta(hours=80)                      # days later the old announcements are forgotten
        fast.reset_day()
        await fast.on_article(article(3, head="Acme names new finance chief"))
    asyncio.run(go())
    first, filing, later = model.asked
    assert fetched == [1] and "text" not in first["article"] or first["article"]["text"] == ""      # rated by its headline
    assert first["context"] is None and later["context"] is None
    assert filing["context"].startswith("This is a filing.\nAnnouncements of this company that the system has already seen")
    assert "(wa-globenewswire): Acme to be acquired by Beta for $42 a share" in filing["context"]
    rated = events("fast_rated")
    assert [r.get("seen_before") for r in rated] == [None, True, None] and [r["with_text"] for r in rated] == [False, True, False]
    assert not events("fast_skipped")


def test_a_rating_that_comes_back_after_the_open_obeys_the_rules_of_the_new_session(fast_rig, monkeypatch):
    api, sess, ex, tmp, clock, events, make, article, fast_mod = fast_rig
    monkeypatch.setattr(fast_mod, "now", lambda: clock["t"])

    class AcrossTheOpen(FakeModel):
        async def rate(self, *a, **kw):
            sess.ph, sess.since = self.then        # the bell rings while the model reads
            return await super().rate(*a, **kw)
    model = AcrossTheOpen("claude-opus-5-5", "up")
    fast = make(model, cfg=FAST_V2)

    async def go():
        model.then = ("regular", 0.03)
        await fast.on_article(article(1))           # arrived at 9:29:57, rated at 9:30:02: the first minutes are left alone
        sess.ph = "post"
        model.then = (None, 0)
        await fast.on_article(article(2))           # rated after the evening session ended
    asyncio.run(go())
    assert [e["reason"] for e in events("fast_rejected")] == ["session_window", "session"] and not api.orders


def test_a_source_can_be_rated_without_being_traded_and_other_models_rate_along(fast_rig, monkeypatch):
    from bot import common as common_mod
    api, sess, ex, tmp, clock, events, make, article, fast_mod = fast_rig
    monkeypatch.setattr(fast_mod, "now", lambda: clock["t"])
    main, haiku = FakeModel("claude-opus-5-5", "up"), FakeModel("claude-haiku-5-5", "down")
    fast = make(main, cfg=FAST_V2, shadows=[haiku, FakeModel("claude-opus-5-5", "down")])
    assert [c.model for c in fast.shadows] == ["claude-haiku-5-5"]     # the deciding model does not rate twice
    assert make(main, shadows=[haiku]).shadows == []                    # and without the switch nobody rates along

    async def fetch(): return "Item 1.01 Entry into a Material Definitive Agreement. Acme agreed to be acquired."
    filing = {"id": "0001234567-26-000123", "source": "sec", "symbols": ["ACME"], "created_at": iso(clock["t"] - timedelta(seconds=50)),
              "headline": "ACME CORP files 8-K with the SEC: Entry into a Material Definitive Agreement", "summary": "",
              "url": "", "dedupe_key": "0001234567-26-000123", "fetch": fetch, "rate_only": True,
              "note": "This is a filing with the SEC."}

    async def go():
        await fast.on_article(filing)
        await fast.on_article({**filing, "id": "0001234567-26-000124", "dedupe_key": "0001234567-26-000124",
                               "fetch": lambda: await_text("")})          # a filing whose text cannot be fetched
        await asyncio.gather(*list(common_mod._tasks))
    asyncio.run(go())
    rated = events("fast_rated")
    assert len(rated) == 1 and (rated[0]["article"], rated[0]["source"], rated[0]["with_text"]) == ("0001234567-26-000123", "sec", True)
    assert main.asked[0]["article"]["text"].startswith("Item 1.01") and "fetch" not in main.asked[0]["article"]
    assert main.asked[0]["context"] == "This is a filing with the SEC." and rated[0]["with_expectations"] is False
    assert [e["reason"] for e in events("fast_rejected")] == ["source_off"] and not api.orders and not ex.trades
    assert [e["reason"] for e in events("fast_skipped")] == ["no_text"]
    shadow = events("fast_shadow")
    assert [(e["model"], e["verdict"]["direction"], e["article"]) for e in shadow] == [("claude-haiku-5-5", "down", "0001234567-26-000123")]
    assert fast.stats == {"received": 2, "rated": 1, "candidates": 1, "entered": 0}


async def await_text(text):
    return text


# ── filings with the SEC as a source ─────────────────────────────────────────
SUBMISSION = """<SEC-DOCUMENT>0001234567-26-000123.txt : 20261124
<DOCUMENT>
<TYPE>8-K
<SEQUENCE>1
<FILENAME>acme-8k.htm
<TEXT>
<html><body><ix:header><ix:hidden>0001234567 false</ix:hidden></ix:header>
<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION Washington, D.C. FORM 8-K ACME CORP</p>
<table><tr><td>Common Stock</td><td>ACME</td></tr></table>
<table><tr><td><b>Item&#160;1.01</b></td><td>Entry into a Material Definitive Agreement.</td></tr></table>
<p>On November 24, 2026, Acme Corp entered into an agreement to be acquired by Beta Inc. for $42.00 per share in cash.</p>
<p>Item 9.01 Financial Statements and Exhibits.</p>
</body></html>
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-99.1
<SEQUENCE>2
<FILENAME>ex991.htm
<TEXT>
<html><body><p>Beta to Acquire Acme for $2.1 Billion</p><script>var x = 1;</script>
<p style="display:none">hidden text</p><p>The price is a premium of 35 % to Acme&#8217;s closing price.</p></body></html>
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>GRAPHIC
<SEQUENCE>3
<FILENAME>logo.jpg
<TEXT>
begin 644 logo.jpg
M_]C_X  02D9)1@ ! 0$ 8 !@  #_VP!#
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-99.2
<SEQUENCE>4
<TEXT>
<html><body><p>Investor presentation: the deal in numbers</p><p>cut off he"""


def test_what_a_filing_says_is_read_from_its_report_and_its_press_release():
    from bot.edgar import filing_text, submission_url
    text = filing_text(SUBMISSION)
    assert text.startswith("Item 1.01 Entry into a Material Definitive Agreement. On November 24, 2026, Acme Corp entered")
    assert "Exhibit 99: Beta to Acquire Acme for $2.1 Billion The price is a premium of 35 % to Acme’s closing price." in text
    assert "Exhibit 99: Investor presentation: the deal in numbers" in text      # the end of the file was not read: still used
    for junk in ("UNITED STATES", "Common Stock", "var x", "hidden text", "logo.jpg", "0001234567 false"):
        assert junk not in text
    from bot.edgar import decode
    assert decode("Acme’s née".encode("utf-8")) == "Acme’s née" and decode("Acme née".encode("latin-1")) == "Acme née"
    assert decode("Acme’s né".encode("utf-8")[:-1]) == "Acme’s n"               # the reading stopped inside a character
    assert len(filing_text(SUBMISSION, main_chars=40, exhibit_chars=30)) <= 70 and filing_text("") == ""
    e = {"acc": "0001234567-26-000123", "cik": 1234567, "link": ""}
    assert submission_url(e) == "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000123/0001234567-26-000123.txt"
    listed = "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000123/0001234567-26-000123-index.htm"
    assert submission_url({**e, "link": listed}) == listed[: -len("-index.htm")] + ".txt"


def test_a_fresh_filing_is_seen_by_the_quick_look_and_handed_on_to_be_read(tmp_path, monkeypatch):
    from bot import edgar as edgar_mod
    from bot.edgar import Edgar

    async def no_sleep(_): return None
    monkeypatch.setattr(edgar_mod.asyncio, "sleep", no_sleep)
    clock = [datetime(2026, 11, 24, 15, 30, tzinfo=timezone.utc)]       # Tuesday, 10:30 in New York
    monkeypatch.setattr(edgar_mod, "now", lambda: clock[0])
    stamp = lambda age_s: (clock[0] - timedelta(seconds=age_s)).astimezone(ET).isoformat(timespec="seconds")
    wrap = lambda *entries: ('<?xml version="1.0" encoding="ISO-8859-1" ?><feed xmlns="http://www.w3.org/2005/Atom">'
                             + "".join(entries) + "</feed>").encode("latin-1")
    old = _sec_entry("C1", "8-K - ACME CORP (0000000011) (Filer)", stamp(4000), ["8.01"])
    new = _sec_entry("C2", "8-K - GAMMA &amp; CO (0000000015) (Filer)", stamp(40), ["1.01", "9.01"])

    class Resp:
        def __init__(self, content=b"", data=None): self.content, self.data = content, data
        def raise_for_status(self): pass
        def json(self): return self.data

    class Stream:
        def __init__(self, status, body): self.status_code, self.body = status, body
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def aiter_bytes(self):
            for i in range(0, len(self.body), 500):
                yield self.body[i:i + 500]

    class Http:
        def __init__(self): self.feed, self.asked, self.streams, self.fetched = wrap(old), [], [], []
        async def get(self, url, params=None, timeout=None):
            if url.endswith("company_tickers.json"):
                return Resp(data={"0": {"cik_str": 11, "ticker": "ACME"}, "1": {"cik_str": 15, "ticker": "GAMMA"}})
            self.asked.append((params["start"], params["count"], timeout))
            return Resp(content=self.feed if params["start"] == 0 else wrap())
        def stream(self, method, url, timeout=None):
            self.fetched.append(url)
            return self.streams.pop(0)

    class Uni:
        cache = {"ACME": {}, "GAMMA": {}}

    handed = []
    async def on_filing(entry, sym): handed.append((entry["acc"], sym))

    e = Edgar(CFG["edgar"], "someone@example.org", Uni(), Journal(tmp_path), on_filing=on_filing)
    e.http = Http()

    async def go():
        assert await e.latest() == 1                                    # the first look of a run goes all the way back
        e.http.feed, e.http.asked = wrap(new, old), []
        assert await e.latest() == 1 and await e.latest() == 0          # the quick look: one small request each
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
        entry = edgar_mod.parse_feed(e.http.feed)[0]
        art = e.article(entry, "GAMMA")
        e.http.streams = [Stream(404, b""), Stream(200, SUBMISSION.encode("latin-1"))]
        text = await art["fetch"]()                                     # listed a moment before the file is there
        e.http.streams = [Stream(503, b"")]
        return art, text, await e.text(entry), entry
    art, text, failed, entry = asyncio.run(go())
    assert e.http.asked == [(0, 40, 6), (0, 40, 6)] and e.pace()["looks"] == 2
    assert handed == [("C2", "GAMMA")] and e.handed == 1                # the hour-old filing is logged, not handed on
    assert (art["id"], art["source"], art["symbols"], art["dedupe_key"]) == ("C2", "sec", ["GAMMA"], "C2")
    assert art["headline"] == "GAMMA & CO files 8-K with the SEC: Entry into a Material Definitive Agreement"
    assert art["created_at"] == iso(clock[0] - timedelta(seconds=40)) and "accepted at 2026-11-24T15:29:20Z" in art["note"]
    # "Unregistered Sales of Equity Securities" is not a report of results: it must not count as a copy of one
    from bot.first import FirstReport
    f = FirstReport(Journal(tmp_path))
    sales = e.article({**entry, "acc": "C3", "items": ["3.02"]}, "GAMMA")
    assert "Sales" in sales["headline"] and sales["results"] is False and e.article({**entry, "items": ["2.02"]}, "GAMMA")["results"]
    assert f.claim(sales) and f.claim({"id": "bw:1", "source": "businesswire", "symbols": ["GAMMA"],
                                      "headline": "Gamma Reports Third Quarter Results", "created_at": art["created_at"]})
    assert text.startswith("Item 1.01 Entry into") and failed == "" and len(e.http.fetched) == 3
    assert e.http.fetched[0].endswith("/15/C2/C2.txt")
    # when the quick look is worth it: on weekdays while the SEC publishes 8-Ks
    hours = {}
    for name, t in (("morning", (24, 11, 30)), ("before_six", (24, 10, 30)), ("evening", (24, 22, 45)), ("saturday", (28, 15, 0))):
        clock[0] = datetime(2026, 11, t[0], t[1], t[2], tzinfo=timezone.utc)
        hours[name] = e.fast_hours()
    assert hours == {"morning": True, "before_six": False, "evening": False, "saturday": False}
    assert not Edgar({**CFG["edgar"], "fast_poll_s": 0}, "x@example.org", Uni(), Journal(tmp_path)).fast_hours()


def test_between_two_complete_looks_the_sec_list_is_asked_every_few_seconds(tmp_path, monkeypatch):
    from bot import edgar as edgar_mod
    from bot.edgar import Edgar

    class Uni:
        cache = {"ACME": {}}

    e = Edgar(CFG["edgar"], "someone@example.org", Uni(), Journal(tmp_path))
    e.in_hours = e.fast_hours = lambda: True
    looks, waits = [], []

    async def collect(): looks.append("complete")

    import httpx

    class Refused:
        status_code = 429

    async def latest():
        looks.append("quick")
        if len(looks) in (3, 4, 5):
            raise RuntimeError("the list did not answer")
        if len(looks) == 7:
            raise httpx.HTTPStatusError("too many requests", request=None, response=Refused())

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 8:
            raise asyncio.CancelledError

    e.collect, e.latest = collect, latest
    monkeypatch.setattr(edgar_mod.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(e.run())
    quick, full = CFG["edgar"]["fast_poll_s"], CFG["edgar"]["poll_s"]
    assert looks == ["complete"] + ["quick"] * 7
    # one or two looks without an answer cost no waiting (one in twenty is slow, the next one is not); only when it
    # keeps failing does the bot ask more rarely, and after a refusal it is silent for a whole interval
    assert waits == [quick, quick, quick, quick, quick * 5, quick, full, quick]
    assert e.stats["errors"] == 4


def test_a_filing_goes_to_the_immediate_entry_and_one_switch_decides_whether_it_is_traded(tmp_path):
    from bot.edgar import Edgar
    from bot.first import FirstReport
    from bot.main import Bot
    got = []

    class Strat:
        async def on_article(self, art): got.append((art["id"], art.get("rate_only", False)))

    class Uni:
        cache = {"ACME": {}}

    j = Journal(tmp_path)
    b = Bot.__new__(Bot)
    b.first, b.fast, b.journal, b.errors = FirstReport(j), Strat(), j, 0
    b.edgar = Edgar(CFG["edgar"], "", Uni(), j)
    b.cfg = {"edgar": {**CFG["edgar"], "trade": False}}                 # the switch off: rated to measure, not traded
    t = datetime.now(timezone.utc)
    entry = lambda acc, items: {"acc": acc, "form": "8-K", "company": "ACME CORP", "cik": 11, "accepted": t, "items": items, "link": ""}

    async def run():
        await b.on_filing(entry("F1", ["1.01", "9.01"]), "ACME")
        await b.on_filing(entry("F2", ["2.02", "9.01"]), "ACME")       # quarterly results: the press release was earlier
        await b.on_filing(entry("F3", ["5.07"]), "ACME")               # result of a vote: a formality
        await b.on_filing(entry("F4", ["9.01"]), "ACME")
        b.cfg["edgar"]["trade"] = True                                 # the switch on (as since 24.11.): traded
        await b.on_filing(entry("F5", ["8.01"]), "ACME")
        b.cfg["edgar"]["rate"] = False
        await b.on_filing(entry("F6", ["8.01"]), "ACME")
    asyncio.run(run())
    assert got == [("F1", True), ("F5", False)] and b.errors == 0


# ── WireAlpha: one more source, measured only ───────────────────────────────
def test_wirealpha_releases_are_listed_with_their_wire_and_the_key_never_reaches_the_log(tmp_path, monkeypatch):
    from bot import wirealpha as wa_mod
    from bot.wirealpha import WireAlpha, to_article as wa_article
    from bot.wires import is_alert
    clock = [datetime(2026, 11, 24, 15, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr(wa_mod, "now", lambda: clock[0])
    universe, sources = {"ACME": {}, "BETA": {}}, {"globenewswire", "businesswire"}
    data = lambda i, tickers, src="globenewswire", title="Acme to Acquire Beta Rival", at="2026-11-24T15:29:58Z": {
        "id": i, "title": title, "source": src, "tickers": tickers, "published_at": at, "latency_ms": 420}

    a = wa_article(data("u1", ["ACME", "ZZZZ"]), universe, sources, 420)
    assert (a["id"], a["source"], a["symbols"], a["created_at"]) == ("wa-globenewswire:u1", "wa-globenewswire", ["ACME"], "2026-11-24T15:29:58Z")
    assert wa_article(data("u2", [{"ticker": "beta", "exchange": "NYSE"}]), universe, sources, 420)["symbols"] == ["BETA"]
    assert wa_article(data("u3", ["ACME", "BETA"]), universe, sources, 420) is None          # unclear which stock is meant
    assert wa_article(data("u4", ["ZZZZ"]), universe, sources, 420) is None                  # not tradable
    assert wa_article(data("u5", ["ACME"], src="reddit"), universe, sources, 420) is None    # not a wire
    assert wa_article(data("u6", ["ACME"], at="soon"), universe, sources, 420) is None

    class Uni:
        cache = universe

    j = Journal(tmp_path)
    w = WireAlpha({"enabled": True, "sources": sorted(sources)}, "wa_live_SECRETKEY", Uni(), None, j, is_alert=is_alert)
    assert w.active and not WireAlpha({"enabled": True, "sources": []}, "", Uni(), None, j).active
    msg = lambda d: {"type": "article", "data": d, "timestamp": "2026-11-24T15:29:58.420Z"}
    got = [w.take(msg(data("u1", ["ACME"]))),
           w.take(msg(data("u1", ["ACME"]))),                                                 # the same again
           w.take(msg(data("u7", ["BETA"], title="INVESTOR ALERT: Some Firm LLP Reminds Beta Investors of Deadline"))),
           w.take(msg(data("u8", ["BETA"], at="2026-11-24T14:00:00Z"))),                      # replayed after connecting: old
           w.take({"type": "insight", "data": {}}), w.take("pong")]
    assert [g["id"] if g else None for g in got] == ["wa-globenewswire:u1", None, None, None, None, None]
    # the stream carries the headline only: whoever rates the release asks for its text, and does without if need be

    class Resp:
        def __init__(self, status, data): self.status_code, self.data = status, data
        def json(self): return self.data

    class Http:
        answers, asked = [Resp(200, {"data": {"body": "<p>Acme will pay $42 a share.</p><script>x()</script>"}}),
                          Resp(404, {}), Resp(200, ["unexpected"])], []
        async def get(self, url, headers=None, params=None):
            self.asked.append((url, headers, params))
            return self.answers.pop(0)
    w.http = Http()
    assert got[0]["text_optional"] is True and "text" not in got[0]
    assert [await_none(got[0]["fetch"]()) for _ in range(3)] == ["Acme will pay $42 a share.", "", ""]
    assert w.http.asked[0] == ("https://api.wirealpha.com/v1/articles/u1", {"Authorization": "Bearer wa_live_SECRETKEY"}, None)
    # the addresses and the way the key is sent can be set: the service's documentation is not reliable
    from bot.wirealpha import article_address, auth, stream_address
    other = {"article": "/api/v1/articles/{id}", "auth": "header:X-API-Key", "stream": "/api/v1/ws", "stream_auth": "bearer"}
    assert article_address(other, "K", "u1") == ("https://api.wirealpha.com/api/v1/articles/u1", {"X-API-Key": "K"}, {})
    assert stream_address(other, "K") == ("wss://api.wirealpha.com/api/v1/ws", {"Authorization": "Bearer K"})
    assert stream_address({}, "K") == ("wss://api.wirealpha.com/v1/ws?apiKey=K", {})
    assert auth("query:apiKey", "K") == ({}, {"apiKey": "K"}) and auth("none", "K") == ({}, {}) and auth(None, "K") == ({}, {})
    with_text = w.take(msg({**data("u9", ["ACME"]), "body": "The text came with the message."}))
    assert with_text["text"] == "The text came with the message." and "fetch" not in with_text
    w._error(RuntimeError("handshake failed for wss://api.wirealpha.com/v1/ws?apiKey=wa_live_SECRETKEY"))
    long_key = WireAlpha({"enabled": True, "sources": []}, "wa_live_" + "K" * 180, Uni(), None, j)
    long_key._error(ValueError("x" * 150 + " wss://api.wirealpha.com/v1/ws?apiKey=wa_live_" + "K" * 180))   # cut only after masking
    rows = list(j.recent_events(1))
    items = [r for r in rows if r["kind"] == "wire_item"]
    assert [(r["source"], r["symbol"], r.get("alert"), r["latency_ms"]) for r in items] == [
        ("wa-globenewswire", "ACME", None, 420), ("wa-globenewswire", "BETA", True, 420), ("wa-globenewswire", "ACME", None, 420)]
    assert len([r for r in rows if r["kind"] == "wirealpha_sample"]) == 5                     # as received, for a check of the format
    assert "SECRETKEY" not in json.dumps(rows) and "***" in next(r for r in rows if r["kind"] == "error")["error"]
    assert "KKKK" not in json.dumps(rows) and len([r for r in rows if r["kind"] == "error"]) == 2
    assert w.stats == {"messages": 0, "items": 5, "matched": 3, "errors": 1}


def test_a_wirealpha_connection_that_fails_at_once_is_tried_ever_more_rarely(tmp_path, monkeypatch):
    from bot import wirealpha as wa_mod
    from bot.wirealpha import WireAlpha

    class Uni:
        cache = {"ACME": {}}

    got, sent, waits, clock = [], [], [], [0.0]
    monkeypatch.setattr(wa_mod.time, "monotonic", lambda: clock[0])

    class Conn:
        """One connection: hands out its messages, then the server closes it."""
        def __init__(self, messages, lasts_s): self.messages, self.lasts_s = messages, lasts_s
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def send(self, text): sent.append(text)
        def __aiter__(self): return self
        async def __anext__(self):
            if self.messages:
                return self.messages.pop(0)
            clock[0] += self.lasts_s
            raise StopAsyncIteration

    release = json.dumps({"type": "article", "timestamp": "x", "data": {
        "id": "u1", "title": "Acme to Acquire Beta Rival", "source": "globenewswire", "tickers": ["ACME"],
        "published_at": wa_mod.iso(wa_mod.now())}})
    plan = [lambda: (_ for _ in ()).throw(OSError("no route")), lambda: Conn([], 1), lambda: Conn([], 1),
            lambda: Conn(["ping", '{"type": "ping"}', "not json", release], 600), lambda: Conn([], 1)]
    addresses = []

    def connect(url, **kw):
        addresses.append(url)
        return plan.pop(0)()

    async def sleep(seconds):
        waits.append(seconds)
        if not plan:
            raise asyncio.CancelledError

    async def on_article(art): got.append(art["id"])

    monkeypatch.setattr(wa_mod.websockets, "connect", connect)
    monkeypatch.setattr(wa_mod.asyncio, "sleep", sleep)
    j = Journal(tmp_path)
    w = WireAlpha({"enabled": True, "sources": ["globenewswire"]}, "wa_live_SECRETKEY", Uni(), on_article, j)

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await w.run()
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
    asyncio.run(go())
    # failing or closed at once: 2, 4, 8 seconds; after ten minutes of working connection quickly again, then 4
    assert waits == [2, 4, 8, 2, 4] and got == ["wa-globenewswire:u1"]
    assert sent.count('{"action": "subscribe", "tickers": ["*"]}') == 4 and "pong" in sent and '{"type": "pong"}' in sent
    assert all(a.endswith("apiKey=wa_live_SECRETKEY") for a in addresses) and "SECRETKEY" not in json.dumps(list(j.recent_events(1)))
    assert w.stats["errors"] == 5 and w.stats["messages"] == 4 and not w.connected


def test_a_copy_from_a_measured_source_and_a_law_firms_call_are_not_measured_twice(tmp_path):
    from bot.speed import Speed
    j = Journal(tmp_path)
    t0 = datetime.now(timezone.utc).replace(second=5, microsecond=0) - timedelta(hours=3)
    item = lambda i, src, sym, sec=0, **kw: j.event("wire_item", id=i, source=src, symbol=sym, headline="x",
                                                    published=iso(t0 + timedelta(seconds=sec)), **kw)
    item("prnewswire:1", "prnewswire", "ACME")
    item("wa-prnewswire:9", "wa-prnewswire", "ACME", sec=20)            # the same release over the measured source
    item("wa-globenewswire:3", "wa-globenewswire", "BETA")              # nobody else has it: measured
    item("prnewswire:2", "prnewswire", "GAMMA", alert=True)             # a law firm's call
    j.event("sec_item", acc="S1", symbol="DELTA", accepted=iso(t0), items=["8.01"])
    item("wa-sec_edgar:4", "wa-sec_edgar", "DELTA", sec=30)             # the same filing as the measured source reports it

    class Uni:
        cache = {"ACME": {}, "BETA": {}, "GAMMA": {}, "DELTA": {}}

    todo = Speed(None, CFG["speed"], j, Uni(), None).todo()
    assert sorted((e["src"], e["symbol"]) for e in todo) == [("prnewswire", "ACME"), ("sec", "DELTA"), ("wa-globenewswire", "BETA")]


# ── what analysts expect of the coming quarters and years (Alpha Vantage) ───────────────────
AV_ROWS = [   # as the service answered for MSFT on 25.11.2026 (numbers come as text), plus quarters long past
    {"date": "2028-06-30", "horizon": "fiscal year", "eps_estimate_average": "24.2008", "eps_estimate_analyst_count": "33.0000",
     "revenue_estimate_average": "477761576700.00", "revenue_estimate_analyst_count": "54.00"},
    {"date": "2027-06-30", "horizon": "fiscal year", "eps_estimate_average": "20.0139", "eps_estimate_analyst_count": "32.0000",
     "revenue_estimate_average": "395966031800.00", "revenue_estimate_analyst_count": "47.00"},
    {"date": "2027-03-31", "horizon": "fiscal quarter", "eps_estimate_average": "4.9372", "eps_estimate_analyst_count": "24.0000",
     "revenue_estimate_average": "99261016600.00", "revenue_estimate_analyst_count": "36.00"},
    {"date": "2026-12-31", "horizon": "fiscal quarter", "eps_estimate_average": "4.8558", "eps_estimate_analyst_count": "24.0000",
     "revenue_estimate_average": "96702371680.00", "revenue_estimate_analyst_count": "37.00"},
    {"date": "2026-09-30", "horizon": "fiscal quarter", "eps_estimate_average": "4.1", "revenue_estimate_average": "90000000000"},
    {"date": "2026-06-30", "horizon": "fiscal quarter", "eps_estimate_average": "3.9", "revenue_estimate_average": "88000000000"},
    {"date": "2026-06-30", "horizon": "fiscal year", "eps_estimate_average": "15.5", "revenue_estimate_average": "330000000000"},
    {"date": "not a date", "horizon": "fiscal quarter", "eps_estimate_average": "1"},
    {"date": "2027-06-30", "horizon": "fiscal quarter", "eps_estimate_average": None, "revenue_estimate_average": "None"},
]
TOO_FAST = {"Information": "Thank you for using Alpha Vantage! Please consider spreading out your free API requests more "
                           "sparingly (1 request per second). You may subscribe to any of the premium plans to lift the "
                           "free key rate limit (25 requests per day)."}
DAY_LIMIT = {"Information": "We have detected your API key and our standard API rate limit is 25 requests per day. "
                            "Please subscribe to any of the premium plans to instantly remove all daily rate limits."}


def test_the_analysts_numbers_for_the_coming_periods_are_put_into_words():
    from bot.alphavantage import OUTLOOK, Refused, estimates_text, forward_rows, read_answer
    today = datetime(2026, 11, 25).date()
    rows = forward_rows(read_answer({"symbol": "MSFT", "estimates": AV_ROWS}), today)
    # the quarter that ended eight weeks ago may be the one about to be reported; what ended in June is history
    assert [(r["horizon"], r["date"]) for r in rows] == [
        ("fiscal quarter", "2026-09-30"), ("fiscal quarter", "2026-12-31"), ("fiscal quarter", "2027-03-31"),
        ("fiscal year", "2027-06-30"), ("fiscal year", "2028-06-30")]
    text = estimates_text("MSFT", rows, "2026-11-25T12:00:00Z")
    assert text.startswith("What analysts expect of MSFT on average, by period (as of 2026-11-25)")
    assert "- quarter ending 2026-12-31: earnings per share 4.86 (24 analysts), revenue 96.70 billion (37 analysts)" in text
    assert "- fiscal year ending 2027-06-30: earnings per share 20.01 (32 analysts), revenue 395.97 billion (47 analysts)" in text
    assert "- quarter ending 2026-09-30: earnings per share 4.10, revenue 90.00 billion" in text and "2026-06-30" not in text
    assert estimates_text("MSFT", []) == "" and forward_rows(None, today) == [] and read_answer(["x"]) == []
    for notice, today_gone in ((TOO_FAST, False), (DAY_LIMIT, True)):
        with pytest.raises(Refused) as e:
            read_answer(notice)
        assert e.value.today is today_gone
    assert OUTLOOK.search("Acme Raises Full-Year Guidance") and OUTLOOK.search("Acme announces preliminary unaudited results")
    assert not OUTLOOK.search("Acme to Acquire Beta; expects the deal to close in March")


def test_the_days_requests_go_first_to_who_reports_next_and_the_key_stays_out_of_the_log(tmp_path, monkeypatch):
    from bot import alphavantage as av_mod
    from bot.alphavantage import Estimates
    clock = {"t": datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc), "mono": 1000.0}
    monkeypatch.setattr(av_mod, "now", lambda: clock["t"])
    monkeypatch.setattr(av_mod.time, "monotonic", lambda: clock["mono"])
    waits = []

    async def sleep(seconds):
        waits.append(round(seconds, 1))
        clock["mono"] += seconds
    monkeypatch.setattr(av_mod.asyncio, "sleep", sleep)

    class Resp:
        def __init__(self, data, status=200): self.data, self.status_code = data, status
        def json(self): return self.data

    class Http:
        def __init__(self): self.asked, self.answers = [], {}
        async def get(self, url, params=None):
            self.asked.append(params["symbol"])
            assert params["function"] == "EARNINGS_ESTIMATES" and params["apikey"] == "AVSECRETKEY"
            a = self.answers.get(params["symbol"], {"estimates": AV_ROWS})
            if isinstance(a, list):
                a = a.pop(0)
            if isinstance(a, Exception):
                raise a
            return Resp(a)

    cfg = {"enabled": True, "per_day": 6, "reserve_per_day": 2, "ttl_days": 7}
    j = Journal(tmp_path)
    path = tmp_path / "state" / "estimates.json"

    def make():
        e = Estimates("AVSECRETKEY", cfg, j, path)
        e.http = Http()
        return e
    e = make()
    assert e.available and not Estimates("", cfg, j, path).available and not Estimates("k", {"enabled": False}, j, path).available
    e.http.answers = {"SMALL": {"symbol": "SMALL", "estimates": []},                      # a company nobody covers
                      "BUSY": [TOO_FAST, {"estimates": AV_ROWS}],                          # "not so fast": once more
                      "DOWN": RuntimeError("GET https://www.alphavantage.co/query?apikey=AVSECRETKEY failed")}

    async def go():
        out = {}
        out["ahead"] = await e.prefetch(["AAA", "SMALL", "BUSY", "CCC", "DDD"])           # 6 a day, 2 held back: 4 requests
        out["asked_ahead"], out["left"] = list(e.http.asked), (e.left(), e.left(e.reserve))
        out["kept"] = await e.context("AAA")
        out["nobody"] = await e.context("SMALL")
        out["not_asked"] = await e.context("CCC")                                         # no announcement asked for it
        out["on_the_spot"] = await e.context("CCC", fetch=True)                           # an announcement about its outlook
        out["down"] = await e.context("DOWN", fetch=True)
        out["none_left"] = (e.left(), await e.context("DDD", fetch=True))
        again = make()                                                                    # after a restart: kept, and counted
        out["after_restart"] = (bool(await again.context("AAA")), again.left(), list(again.http.asked))
        clock["t"] += timedelta(days=1)
        out["next_day"] = again.left()
        again.http.answers = {"EEE": DAY_LIMIT}
        out["limit"] = (await again.context("EEE", fetch=True), again.left())
        clock["t"] += timedelta(days=8)
        out["stale"] = (again.fresh("AAA"), await again.context("AAA"))                   # a week old: not used any more
        return out
    out = asyncio.run(go())
    assert out["ahead"] == 3 and out["asked_ahead"] == ["AAA", "SMALL", "BUSY", "BUSY"] and out["left"] == (2, 0)
    assert out["kept"].startswith("What analysts expect of AAA") and out["nobody"] is None and out["not_asked"] is None
    assert out["on_the_spot"].startswith("What analysts expect of CCC") and out["down"] is None
    assert out["none_left"] == (0, None) and out["after_restart"] == (True, 0, [])
    assert out["next_day"] == 6 and out["limit"] == (None, 0) and out["stale"] == (False, None)
    assert all(w in (1.6, 2.0) for w in waits) and waits.count(2.0) == 1                  # a request a second, no faster
    rows = list(j.recent_events(1))
    assert [(r["kind"], r.get("where"), r.get("error")) for r in rows] == [("error", "estimates", "RuntimeError"),
                                                                           ("estimates_limit", None, None)]
    assert "AVSECRETKEY" not in json.dumps(rows) and "AVSECRETKEY" not in path.read_text()


def test_an_announcement_about_the_outlook_is_rated_with_the_analysts_numbers(fast_rig, monkeypatch):
    api, sess, ex, tmp, clock, events, make, article, fast_mod = fast_rig
    monkeypatch.setattr(fast_mod, "now", lambda: clock["t"])
    model = FakeModel("claude-opus-5-5", "none")

    class Est:
        asked = []
        async def context(self, sym, fetch=False):
            self.asked.append((sym, fetch))
            return "What analysts expect of ACME on average, by period: ..." if fetch else None

    class Exp:
        async def context(self, sym): return "BETA reports quarterly results today." if sym == "BETA" else None
    fast = make(model, cfg=FAST_V2, estimates=Est(), expectations=Exp())

    async def go():
        await fast.on_article(article(1, head="Acme raises full-year guidance"))
        await fast.on_article(article(2, head="Acme names new finance chief", sym="GAMMA"))
        await fast.on_article(article(3, head="Beta reports third quarter results", sym="BETA"))
    asyncio.run(go())
    assert Est.asked == [("ACME", True), ("GAMMA", False), ("BETA", True)]        # asked on the spot only where it matters
    assert "What analysts expect of ACME" in model.asked[0]["context"] and model.asked[1]["context"] is None
    assert model.asked[2]["context"].startswith("BETA reports quarterly results today.\nWhat analysts expect")
    assert [r.get("with_estimates") for r in events("fast_rated")] == [True, None, True]


def test_the_companies_that_report_next_are_asked_for_ahead_the_most_traded_first(tmp_path):
    from bot.main import Bot
    b = Bot.__new__(Bot)
    today = datetime.now(timezone.utc).astimezone(ET).date()
    day = lambda n: (today + timedelta(days=n)).isoformat()

    class Exp:
        rows = {"OLD": {"symbol": "OLD", "date": day(-1)}, "BIG": {"symbol": "BIG", "date": day(0)},
                "MID": {"symbol": "MID", "date": day(0)}, "TINY": {"symbol": "TINY", "date": day(0)},
                "NEXT": {"symbol": "NEXT", "date": day(1)}, "FOREIGN": {"symbol": "FOREIGN", "date": day(0)},
                "PENNY": {"symbol": "PENNY", "date": day(0)}}

    class Uni:
        cache = {s: {} for s in ("OLD", "BIG", "MID", "TINY", "NEXT", "PENNY")}

    class Stats:
        async def get(self, syms):
            volume = {"BIG": 9e9, "MID": 5e7, "TINY": 1e6, "NEXT": 9e10, "PENNY": 5e7}
            return {s: {"price": 2.0 if s == "PENNY" else 50.0, "dollar_volume": volume[s]} for s in syms if s in volume}

    class News:
        stock_stats = Stats()
    b.expectations, b.universe, b.news, b.cfg = Exp(), Uni(), News(), CFG
    # yesterday's report is over; too small, too cheap or not tradable is left out; today before tomorrow
    assert asyncio.run(b.reporting_next()) == ["BIG", "MID", "NEXT"]
