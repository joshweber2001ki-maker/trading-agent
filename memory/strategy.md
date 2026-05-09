# Strategy Memory

_This file is maintained by the agent. Updated after each significant learning._

---

## Core Philosophy

- Quality over quantity. 3 great trades beat 10 mediocre ones.
- Trend is your friend. Don't fight the tape.
- Cash is a position. 20% minimum reserve is sacred.
- The journal is the edge. Review it, learn from it.

---

## Active Notes

_Agent will add notes here after each session._

**Initialized:** 2026-04-24
**First live session:** 2026-04-24 (all-SKIP, risk-off macro)

---

## Known Patterns to Watch

- NVDA tends to lead tech moves by 1-2 days
- SPY below 50d SMA = reduce position sizes across board
- Avoid trading 30 min before/after major macro releases (CPI, FOMC)
- Earnings season (1-2 weeks around each quarter end): heightened volatility, tighten stops to -5%

---

## Sectors to Favor in Current Environment

**As of 2026-05-06:** Macro is CONSTRUCTIVE. SPY trading near ATH above $723, well above SMA20. QQQ also above SMA20 in uptrend. WAIT posture lifted. Favored sectors: Technology (NVDA, GOOGL, MSFT), Communication Services (META — already held). Be selective; size conservatively on first new entries.

**Not favored:** Healthcare (UNH under sector pressure), Financials (JPM bearish technicals). TSLA/COST too expensive on fundamentals for new entry.

_Previous note (2026-04-24):_ Macro was risk-off. Energy was the only leading sector. META was flagged as first-entry candidate — entered 2026-05-05 at $604.93.

---

## Data Infrastructure Notes

- **Alpaca IEX feed limitation:** IEX does not carry ETF bars (SPY/QQQ return null). Data also lags ~20 trading days behind real time on the free tier. Use `feed=sip` for ETFs but note SIP requires a paid subscription (returns 403 on free accounts). **Workaround:** use Perplexity for current prices and recent price action; use IEX bars only for longer-horizon SMA context. Flag: SMA values computed from bars will be ~1 month stale.
- **alpaca.py bars fix applied 2026-04-24:** Added `feed=sip` for known ETFs (SPY/QQQ/IWM/DIA) and `feed=iex` for stocks. Also added null guard in bar loop.
- **IEX volume severely understates true ADV (discovered 2026-05-06):** `guardrails.py` volume check uses `get_bars(symbol, 30)` from IEX. IEX captures only ~1-3% of total US equity volume. GOOGL returned ADV 174,716 (true ADV ~25M+) and V returned 58,040 (true ADV ~7M+) — both legitimately liquid stocks that failed the 500k guardrail due to IEX undercount. This blocked two valid BUY orders. **Action needed:** guardrails volume check should use SIP feed or a Perplexity-sourced ADV figure. Until fixed, any large-cap with IEX volume < 500k but Perplexity-confirmed high liquidity should be flagged for manual review — but rule stands: do NOT override guardrails unilaterally.

---

## Recent Lessons Learned

_Agent will append lessons here after weekly reviews._

**2026-05-05:** First overnight hold with a known pre-market macro event (ADP Employment Change, HIGH impact, 2026-05-06). Rule says SELL if overnight risk event present — but position size was only 3% of portfolio, well within limits, and stop was 6.8% away. Applied discretion: hold with flag rather than panic-sell at -0.17%. Key principle: small position size is its own risk management tool. Reassess META at open before any new activity tomorrow.

**2026-05-06:** First active buying session post WAIT posture. NVDA BUY executed (24 shares @ $203.97, order f9431ab9). GOOGL and V blocked by guardrail volume check — IEX feed understates true ADV by ~99%. Guardrails respected; no override taken. MSFT skipped due to wide spread + thin volume. Lesson: guardrails.py volume check needs SIP feed or external ADV source — IEX-only volume is not a valid proxy for stock liquidity.

**2026-05-07:** Pre-stating stop raise triggers with explicit conditions before market open (e.g., "will raise NVDA stop after confirmed break above $211") and executing at exactly that level removes hesitation and prevents scope creep. NVDA hit $213.16 — stop raised immediately from $196.50 to $206.50, locking in $67.68 minimum profit on 24 shares. No second-guessing, no "let me wait for more." This is the discipline that compounds. Operationally: the guardrail ADV threshold may have been adjusted (GOOGL and MSFT cleared today where GOOGL was blocked May 6) — confirm guardrails.py threshold on next session.
