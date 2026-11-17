# ARIA Strategy History

This file records weekly review summaries to ensure strategic consistency over time.

---

## Week of May 4, 2026 – May 8, 2026

**P&L:** +$212.98 (+0.21%)
**Win Rate:** 1/1 closed trades (100%) — 3 winning / 2 losing open positions as of May 8 close
**Strategy Used:** Conservative-to-moderate bottom-up stock picking within a constructive macro environment. Week began with single META hold (WAIT posture from Apr 24 still active), shifted to risk-on after SPY confirmed uptrend near ATH on May 6. Deployed into 5 large-cap US equities (NVDA, MSFT, GOOGL, AMZN, AAPL) across Technology, Communication Services, and Consumer Discretionary. Sizing conservative (~22% invested, 78% cash at week end). All entries via limit orders. All positions managed with trailing stop-losses.

**What Worked:**
- NVDA pullback entry at $203.68 with pre-committed trailing stops — best trade of the week (+$270.66 unrealized, +5.53%); hit 52-week high $217.80 on May 8
- META stop discipline: progressive stop raises ($562 → $600 → $612) resulted in a profitable exit at ~$609 while stock fell to ~$584 after-hours; stop process preserved ~$28/share of downside
- All-cash discipline on May 5 (waited for macro confirmation) enabled cleaner, higher-quality entries on May 6–8
- Pre-committing stop-raise triggers before market open (e.g., "raise NVDA stop after confirmed $211 break") eliminated hesitation and second-guessing during live sessions
- AAPL entry on a confirmed new 52-week high breakout with 5/5 signals aligned; hit new ATH $294.76 on entry day

**What Didn't Work:**
- MSFT entry timing: entered at $423.57 near prior-month resistance ($424.96 Apr 27 high) without breakout confirmation; immediately pulled back to $414.90 and remained below technical support ($417) for two sessions
- AMZN entry without fresh catalyst: entered near 52-week high ($273.50 vs $278.56 high) on sentiment alone; flat-to-negative two sessions post-entry with no momentum follow-through
- V (Visa) missed twice due to IEX guardrail volume block (ADV 58k and 70k vs true ~7M shares/day); Visa reported strongest revenue growth since 2022 during the week — a missed high-quality trade caused by data infrastructure flaw, not genuine liquidity risk

**Proposed Changes (pending approval):**
- For symbols at prior-month resistance at entry: size at 50% allocation, add remaining 50% only after confirmed close through resistance
- At midday scans: if a position gains >3% on above-average volume in a single session, perform an immediate stop-raise assessment rather than deferring to next pre-market
- Update `scripts/guardrails.py` volume check to cross-reference Perplexity ADV when IEX ADV is below threshold; if Perplexity confirms large-cap liquidity, proceed and log discrepancy

---
