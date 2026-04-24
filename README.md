# ARIA Trading Agent — Setup Guide

Autonomous trading agent built on Claude Code + Alpaca + Perplexity.

---

## Prerequisites

- Claude Code installed and logged in (`claude --version` to verify)
- Claude Code **Pro plan** (required for Cloud Routines)
- Alpaca account (paper trading API keys ready)
- Perplexity Pro account (API key ready)
- Python 3.9+ installed
- `requests` library: `pip install requests`

---

## Step 1 — Install Python Dependencies

```bash
pip install requests
```

---

## Step 2 — Configure API Keys

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `APCA_API_KEY_ID` — your Alpaca **paper** API key
- `APCA_API_SECRET_KEY` — your Alpaca **paper** secret
- `APCA_BASE_URL` — leave as `https://paper-api.alpaca.markets` for now
- `PERPLEXITY_API_KEY` — from perplexity.ai/settings/api

> ⚠️ **Start with paper trading. Do NOT use live keys until you've run at least 2 weeks paper.**

---

## Step 3 — Load Environment Variables

Add to your shell profile (`~/.zshrc` or `~/.bashrc`), or export manually:

```bash
export APCA_API_KEY_ID="your_key"
export APCA_API_SECRET_KEY="your_secret"
export APCA_BASE_URL="https://paper-api.alpaca.markets"
export PERPLEXITY_API_KEY="your_perplexity_key"
```

Or use a tool like `direnv` to auto-load `.env`.

---

## Step 4 — Test the Setup

```bash
# Test Alpaca connection
python scripts/alpaca.py account
python scripts/alpaca.py market_status

# Test Perplexity connection
python scripts/research.py macro

# Test guardrails
python scripts/guardrails.py portfolio
```

All should return clean JSON without errors.

---

## Step 5 — Open in Claude Code

```bash
cd trading-agent
claude
```

Claude Code will automatically read `CLAUDE.md` and understand its role.

---

## Step 6 — Test Manually First

Before enabling routines, run a manual test session in Claude Code:

```
Run the pre-market research routine
```

Watch what it does. Check the journal file it creates. Verify it's making sensible decisions.

Then test a paper trade:

```
Run the market open execution routine
```

---

## Step 7 — Enable Cloud Routines

In Claude Code, run:

```
/routines
```

This will show the 5 configured routines from `.claude/routines.json`. Enable them.

The routines will fire on their cron schedule — no terminal window needed.

---

## Routine Schedule (US Eastern Time)

| Routine | Time | Days |
|---------|------|------|
| 1. Pre-Market Research | 9:25 AM | Mon–Fri |
| 2. Market Open Execution | 10:00 AM | Mon–Fri |
| 3. Midday Scan | 1:00 PM | Mon–Fri |
| 4. End-of-Day Summary | 4:15 PM | Mon–Fri |
| 5. Weekly Review | 5:00 PM | Friday |

---

## File Structure

```
trading-agent/
├── CLAUDE.md              # Agent identity, strategy, hard rules
├── .claude/
│   └── routines.json      # 5 scheduled automations
├── memory/
│   ├── watchlist.json     # Tradeable universe (agent can edit this)
│   └── strategy.md        # Evolving strategy notes (agent updates this)
├── journal/               # Daily logs — YYYY-MM-DD.md
│   └── .gitkeep
├── scripts/
│   ├── alpaca.py          # All Alpaca API calls
│   ├── research.py        # Perplexity market research
│   └── guardrails.py      # Pre-trade safety checks
├── .env.example           # Template for API keys
├── .env                   # Your real keys (NEVER commit)
└── .gitignore
```

---

## Risk Warning

⚠️ **This is an experimental AI trading agent. It can and will lose money.**

- Always start with **paper trading**
- Run for at least 2–4 weeks paper before considering live funds
- Only use money you can afford to lose entirely
- Monitor the journal daily — it's your audit trail
- The guardrails protect you but are not foolproof
- Past paper performance does NOT guarantee live performance

---

## Switching to Live Trading

Only after extensive paper testing:

1. Generate live API keys on Alpaca
2. Update `.env`:
   ```
   APCA_API_KEY_ID=your_live_key
   APCA_API_SECRET_KEY=your_live_secret
   APCA_BASE_URL=https://api.alpaca.markets
   ```
3. Reload env vars
4. Start with a small account (e.g. $1,000–$5,000)
5. Monitor every session for the first week

---

## Monitoring

Check daily:
- `journal/YYYY-MM-DD.md` — what did the agent do and why?
- Alpaca dashboard — do orders match the journal?
- `memory/strategy.md` — is the agent learning?

If something looks wrong, disable routines immediately via `/routines` in Claude Code.
