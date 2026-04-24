#!/usr/bin/env python3
"""
Perplexity API helper for market research.

Usage:
  python scripts/research.py news AAPL
  python scripts/research.py macro
  python scripts/research.py sentiment AAPL MSFT NVDA
  python scripts/research.py events           # upcoming macro events
"""

import os
import sys
import json
import re
import requests

PERPLEXITY_API_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"

HEADERS = {
    "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
    "Content-Type": "application/json"
}

def _ask(prompt, model="sonar"):
    """Send a question to Perplexity and return the answer text."""
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a concise financial research assistant. "
                    "Always respond in structured JSON format. "
                    "Be factual, cite sources when possible, and flag uncertainty clearly."
                )
            },
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 800,
        "temperature": 0.2,
        "return_citations": True,
    }
    r = requests.post(PERPLEXITY_URL, headers=HEADERS, json=payload)
    r.raise_for_status()
    data = r.json()
    content = data["choices"][0]["message"]["content"]
    citations = data.get("citations", [])
    return content, citations

def _extract_json(content):
    """Robustly extract JSON from a string that may contain markdown fences or prose.

    Strategy:
    1. Strip a wrapping ```json ... ``` or ``` ... ``` fence if present.
    2. Scan for the first { or [ and slice from there to the matching last } or ].
    3. Return the parsed object, or None on failure.
    """
    text = content.strip()

    # Remove markdown code fences (```json ... ``` or ``` ... ```)
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    # Find the outermost JSON object or array
    start = next((i for i, c in enumerate(text) if c in "{["), None)
    if start is None:
        return None

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    end = len(text) - 1
    while end >= start and text[end] != closer:
        end -= 1

    if end < start:
        return None

    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def news_for_symbol(symbol):
    """Get latest news and sentiment for a stock symbol."""
    prompt = f"""
Research {symbol} stock RIGHT NOW. Return JSON with this exact structure:
{{
  "symbol": "{symbol}",
  "sentiment": "bullish|bearish|neutral",
  "sentiment_score": 0.0 to 1.0,
  "key_headlines": ["headline 1", "headline 2", "headline 3"],
  "risk_events": ["any earnings/FDA/macro events in next 14 days"],
  "analyst_view": "one sentence summary of current analyst consensus",
  "red_flags": ["any concerning news items"],
  "trade_signal": "buy|sell|hold|skip",
  "confidence": "high|medium|low"
}}
Return ONLY the JSON, no extra text.
"""
    content, citations = _ask(prompt)
    result = _extract_json(content)
    if result is not None:
        result["citations"] = citations[:3]
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps({"symbol": symbol, "parse_error": True, "raw": content, "citations": citations}))

def macro_overview():
    """Get current macro market overview."""
    prompt = """
Give me a current macro market overview. Return JSON:
{
  "date": "today's date",
  "market_sentiment": "risk-on|risk-off|neutral",
  "spy_trend": "uptrend|downtrend|sideways",
  "vix_level": "low(<15)|normal(15-20)|elevated(20-30)|fear(>30)",
  "key_themes": ["theme 1", "theme 2", "theme 3"],
  "sectors_leading": ["sector1", "sector2"],
  "sectors_lagging": ["sector1", "sector2"],
  "macro_risks": ["risk 1", "risk 2"],
  "overall_bias": "buy_dips|sell_rips|wait|accumulate",
  "one_line_summary": "single sentence market summary"
}
Return ONLY the JSON.
"""
    content, citations = _ask(prompt)
    result = _extract_json(content)
    if result is not None:
        result["citations"] = citations[:3]
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps({"parse_error": True, "raw": content, "citations": citations}))

def sentiment_multi(symbols):
    """Quick sentiment scan for multiple symbols."""
    symbols_str = ", ".join(symbols)
    prompt = f"""
Quick sentiment scan for these stocks: {symbols_str}
Return a JSON array where each element is:
{{
  "symbol": "TICKER",
  "sentiment": "bullish|bearish|neutral",
  "one_line": "key reason in one sentence",
  "signal": "buy|sell|hold|skip"
}}
Return ONLY the JSON array.
"""
    content, citations = _ask(prompt)
    result = _extract_json(content)
    if result is not None:
        # Accept both a bare array and {"results": [...]}
        items = result if isinstance(result, list) else result.get("results", result)
        print(json.dumps({"results": items, "citations": citations[:3]}, indent=2))
    else:
        print(json.dumps({"parse_error": True, "raw": content}))

def upcoming_events():
    """Get upcoming macro events that could move markets."""
    prompt = """
What are the key market-moving events in the next 14 days?
Return JSON:
{
  "events": [
    {
      "date": "YYYY-MM-DD",
      "event": "event name",
      "impact": "high|medium|low",
      "expected": "brief expectation",
      "affected_sectors": ["sector1"]
    }
  ]
}
Return ONLY the JSON.
"""
    content, citations = _ask(prompt)
    result = _extract_json(content)
    if result is not None:
        result["citations"] = citations[:3]
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps({"parse_error": True, "raw": content}))

# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not PERPLEXITY_API_KEY:
        print(json.dumps({"error": "Missing PERPLEXITY_API_KEY env var"}))
        sys.exit(1)

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0].lower()

    try:
        if cmd == "news" and len(args) >= 2:
            news_for_symbol(args[1].upper())
        elif cmd == "macro":
            macro_overview()
        elif cmd == "sentiment" and len(args) >= 2:
            sentiment_multi([s.upper() for s in args[1:]])
        elif cmd == "events":
            upcoming_events()
        else:
            print(__doc__)
            sys.exit(1)
    except requests.HTTPError as e:
        print(json.dumps({"error": str(e), "response": e.response.text}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
