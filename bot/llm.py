"""News classification with Claude via the Anthropic Messages API."""
import json
import re
import time

import httpx

SYSTEM = """You classify stock-market news for an intraday trading system. The system trades the
continuation move in the minutes after a genuinely surprising, company-specific news item.

Judge the article and answer with ONE JSON object and nothing else:
{"symbol": "<the single ticker most affected, from the given list>",
 "direction": "up" | "down" | "none",
 "surprise": <0-10, how unexpected and new this information is for the market>,
 "confidence": <0-10, how sure you are about the direction>,
 "category": "<earnings|guidance|m&a|fda|contract|legal|analyst|management|macro|product|other>",
 "reason": "<max 20 words>"}

Scoring guidance:
- surprise 8-10: hard, new, material facts (earnings far off expectations, guidance change, takeover
  bid, FDA decision, large contract, fraud allegation, sudden CEO exit).
- surprise 4-7: relevant but smaller or partly expected (analyst rating change, product launch).
- surprise 0-3: recaps, opinion, lists, price-movement commentary ("why is X moving"), scheduled
  events without results, insider transactions, anything already known.
- An article that only explains a move that has already happened is direction "none".
- If the direction is genuinely unclear, use "none".
- Each ticker is listed with the name of the US-listed company behind it. If the article is about a
  different company (for example a foreign company whose home-market ticker happens to be the same
  letters), answer direction "none" with surprise 0.
- Results without a comparison to expectations or guidance are not a surprise by themselves."""

NOISE = re.compile(
    r"(stocks? (moving|to watch)|top \d+|movers|why (is|are) .* (moving|trading|shares)|"
    r"insider (sold|bought|sale|purchase)|market (wrap|update|clubhouse)|"
    r"what's going on|here's why|price target|mid-?day|pre-?market movers)", re.I
)


def is_noise(headline: str) -> bool:
    return bool(NOISE.search(headline or ""))


def parse_verdict(text: str, symbols):
    """Extract and validate the JSON verdict. Returns None if unusable."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    try:
        v = {
            "symbol": str(d.get("symbol", "")).upper().strip(),
            "direction": str(d.get("direction", "none")).lower(),
            "surprise": max(0, min(10, int(round(float(d.get("surprise", 0)))))),
            "confidence": max(0, min(10, int(round(float(d.get("confidence", 0)))))),
            "category": str(d.get("category", "other"))[:20],
            "reason": str(d.get("reason", ""))[:200],
        }
    except (TypeError, ValueError):
        return None
    if v["direction"] not in ("up", "down", "none"):
        v["direction"] = "none"
    if v["symbol"] not in symbols:
        v["symbol"] = symbols[0] if symbols else ""
    return v


class Classifier:
    def __init__(self, api_key: str, cfg: dict):
        self.key = api_key
        self.model = cfg["model"]
        self.thinking = cfg.get("thinking", False)
        self.max_hour = cfg["max_calls_per_hour"]
        self.max_day = cfg["max_calls_per_day"]
        self.http = httpx.AsyncClient(timeout=cfg["timeout_s"])
        self.calls = []       # monotonic timestamps of the last hour
        self.day_calls = 0

    @property
    def available(self):
        return bool(self.key)

    def reset_day(self):
        self.day_calls = 0

    def budget_ok(self):
        t = time.monotonic()
        self.calls = [c for c in self.calls if t - c < 3600]
        return len(self.calls) < self.max_hour and self.day_calls < self.max_day

    async def classify(self, article: dict, symbols, names=None):
        """Returns (verdict | None, error | None)."""
        if not self.available:
            return None, "no_api_key"
        if not self.budget_ok():
            return None, "llm_budget"
        self.calls.append(time.monotonic())
        self.day_calls += 1
        names = names or {}
        tickers = ", ".join("%s (%s)" % (s, names.get(s) or "?") for s in symbols)
        user = (
            f"Tickers: {tickers}\n"
            f"Source: {article.get('source', '')}\n"
            f"Published: {article.get('created_at', '')}\nHeadline: {article.get('headline', '')}\n"
            f"Summary: {(article.get('summary') or '')[:1200]}"
        )
        try:
            r = await self.http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    # Without this the model reasons first, which costs seconds and output tokens.
                    "max_tokens": 2000 if self.thinking else 300,
                    **({} if self.thinking else {"thinking": {"type": "disabled"}}),
                    "system": SYSTEM,
                    "messages": [{"role": "user", "content": user}],
                },
            )
            if r.status_code != 200:
                return None, f"llm_http_{r.status_code}: {r.text[:150]}"
            text = "".join(b.get("text", "") for b in r.json().get("content", []))
        except httpx.HTTPError as e:
            return None, f"llm_network: {e!r}"[:200]
        v = parse_verdict(text, symbols)
        return (v, None) if v else (None, f"llm_unparseable: {text[:200]!r}")
