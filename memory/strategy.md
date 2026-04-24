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

**As of 2026-04-24:** Macro is risk-off. Energy is the only leading sector. Broad equities in downtrend. WAIT posture — no sector is favored for new longs until SPY reclaims 50d SMA.

**Watch list for first entry when macro improves:** META (strongest recovery, highest analyst conviction), AAPL (above SMA20, defensive tech). MSFT showing distribution — lower priority.

---

## Data Infrastructure Notes

- **Alpaca IEX feed limitation:** IEX does not carry ETF bars (SPY/QQQ return null). Data also lags ~20 trading days behind real time on the free tier. Use `feed=sip` for ETFs but note SIP requires a paid subscription (returns 403 on free accounts). **Workaround:** use Perplexity for current prices and recent price action; use IEX bars only for longer-horizon SMA context. Flag: SMA values computed from bars will be ~1 month stale.
- **alpaca.py bars fix applied 2026-04-24:** Added `feed=sip` for known ETFs (SPY/QQQ/IWM/DIA) and `feed=iex` for stocks. Also added null guard in bar loop.

---

## Recent Lessons Learned

_Agent will append lessons here after weekly reviews._

