"""News classification with Claude via the Anthropic Messages API."""
import asyncio
import json
import re
import time

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
# How each model is told to answer without thinking first. None: this model always thinks.
# (Sonnet 5.5 and Opus 5.5 reject "disabled" with an HTTP 400.)
NO_THINKING = {"claude-haiku-5-5": "disabled", "claude-sonnet-5-5": "between_tools", "claude-opus-5-5": None}
EFFORTS = ("off", "low", "medium", "high")
RETRY_STATUS = (429, 500, 502, 503, 529)   # rate limit or a busy server: worth one more try

SYSTEM = """You classify stock-market news for an intraday trading system. The system trades the
continuation move in the minutes after a genuinely surprising, company-specific news item.

Judge the article and answer with ONE JSON object and nothing else:
{"symbol": "<the single ticker most affected, from the given list>",
 "direction": "up" | "down" | "none",
 "surprise": <0-10, how unexpected and new this information is for the market>,
 "impact": <0-3, how far this news should move THIS stock today, see below>,
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
- Results without a comparison to expectations or guidance are not a surprise by themselves.

Impact is about size, not novelty: how many percent should this news move the stock?
  0 = hardly at all (under 1 %): routine, or immaterial for a company of this size
  1 = moderate (about 1-3 %)
  2 = large (about 3-8 %)
  3 = very large (more than 8 %)
- Judge it relative to the company. Each ticker comes with how much of the stock is traded per day
  (a proxy for company size) and how far it usually swings in a day. The same contract, approval,
  lawsuit or executive change can be a 3 for a small company and a 0 for a very large one.
- Compare amounts in the article (deal value, revenue, fines) with the size of the company.
- Surprise and impact are independent: a fully unexpected but tiny item is surprise 8, impact 0."""

NOISE = re.compile(
    r"(stocks? (moving|to watch)|top \d+|movers|why (is|are) .* (moving|trading|shares)|"
    r"insider (sold|bought|sale|purchase)|market (wrap|update|clubhouse)|"
    r"what's going on|here's why|price target|mid-?day|pre-?market movers)", re.I
)


def is_noise(headline: str) -> bool:
    return bool(NOISE.search(headline or ""))


def _loose_fields(text: str):
    """Pull the rating fields out of an answer that is not valid JSON (cut off, stray quotes in the reason)."""
    d = {}
    for key in ("symbol", "direction", "category"):
        m = re.search(r'"%s"\s*:\s*"([^"]*)"' % key, text)
        if m:
            d[key] = m.group(1)
    for key in ("surprise", "impact", "confidence"):
        m = re.search(r'"%s"\s*:\s*"?(-?\d+(?:\.\d+)?)' % key, text)
        if m:
            d[key] = m.group(1)
    m = re.search(r'"reason"\s*:\s*"(.*)', text, re.S)
    if m:
        d["reason"] = m.group(1).rstrip().rstrip("}").rstrip().rstrip('"')
    return d


def parse_verdict(text: str, symbols):
    """Extract and validate the verdict. Returns None if unusable."""
    text = text or ""
    start = text.find("{")
    if start < 0:
        return None
    try:
        d, _ = json.JSONDecoder().raw_decode(text[start:])   # first complete object; trailing text is ignored
        if not isinstance(d, dict):
            return None
    except json.JSONDecodeError:
        d = _loose_fields(text[start:])
        # The rating itself must be complete; only the free-text reason may be damaged.
        if not all(k in d for k in ("direction", "surprise", "confidence")):
            return None
    try:
        v = {
            "symbol": str(d.get("symbol", "")).upper().strip(),
            "direction": str(d.get("direction", "none")).lower(),
            "surprise": max(0, min(10, int(round(float(d.get("surprise", 0)))))),
            # an answer without the field counts as "moderate", so that it neither blocks nor inflates
            "impact": max(0, min(3, int(round(float(d.get("impact", 1)))))),
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
    """One model that rates articles. The bot runs one that decides and may run more for comparison."""

    def __init__(self, api_key: str, cfg: dict, model: str = None):
        self.key = api_key
        self.model = model or cfg["model"]
        # How thoroughly the model thinks before it answers: "off" is the fastest.
        self.effort = (cfg.get("effort") or {}).get(self.model, "off")
        if self.effort not in EFFORTS:
            raise ValueError(f"[llm.effort] {self.model}: {self.effort!r} is not one of {EFFORTS}")
        if self.effort == "off" and NO_THINKING.get(self.model, "disabled") is None:
            raise ValueError(f"{self.model} cannot answer without thinking: set [llm.effort] to low, medium or high")
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

    @staticmethod
    def describe(symbol, name, stats):
        if not stats:
            return "%s (%s; size and usual swing unknown)" % (symbol, name or "?")
        dv = stats["dollar_volume"]
        traded = "$%.1fbn" % (dv / 1e9) if dv >= 1e9 else "$%.0fm" % (dv / 1e6)
        return "%s (%s; %s company, about %s traded per day, usual daily swing %.1f %%, price $%.2f)" % (
            symbol, name or "?", stats["size"], traded, stats["daily_range_pct"], stats["price"])

    def request(self, user: str) -> dict:
        """The request body. The models differ in how thinking is switched off, and one cannot switch it off."""
        body = {"model": self.model, "system": SYSTEM, "messages": [{"role": "user", "content": user}]}
        if self.effort == "off":
            # Without this the model reasons first, which costs seconds and output tokens.
            body.update(max_tokens=300, thinking={"type": NO_THINKING.get(self.model, "disabled")})
        else:
            # The thinking counts against max_tokens: leave room for it before the short answer.
            body.update(max_tokens=3000, thinking={"type": "adaptive"}, output_config={"effort": self.effort})
        return body

    async def rate(self, article: dict, symbols, names=None, stats=None, context=None, text_chars=1200) -> dict:
        """Rate one article. Always returns a dict: verdict (None if it failed), error (None if it worked),
        model, seconds for the call, tokens_in, tokens_out and the account's rate limits for this model.

        context: what is known beforehand (for example what analysts expect of today's results); it is
        put in front of the article. An article may carry its full "text"; text_chars limits how much
        of it (or of the summary) the model gets.
        """
        out = {"verdict": None, "error": None, "model": self.model, "seconds": None,
               "tokens_in": None, "tokens_out": None, "limits": None}
        if not self.available:
            return {**out, "error": "no_api_key"}
        if not self.budget_ok():
            return {**out, "error": "llm_budget"}
        self.calls.append(time.monotonic())
        self.day_calls += 1
        names, stats = names or {}, stats or {}
        tickers = "\n".join("- " + self.describe(s, names.get(s), stats.get(s)) for s in symbols)
        body = article.get("text") or ""
        user = (
            f"Tickers:\n{tickers}\n"
            + (f"Known beforehand:\n{context}\n" if context else "")
            + f"Source: {article.get('source', '')}\n"
            f"Published: {article.get('created_at', '')}\nHeadline: {article.get('headline', '')}\n"
            + (f"Text: {body[:text_chars]}" if body else f"Summary: {(article.get('summary') or '')[:text_chars]}")
        )
        t0 = time.monotonic()
        try:
            for attempt in (1, 2):
                r = await self.http.post(
                    API_URL,
                    headers={"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json=self.request(user),
                )
                if r.status_code not in RETRY_STATUS or attempt == 2:
                    break
                try:
                    wait = float(r.headers.get("retry-after") or 1.5)
                except ValueError:
                    wait = 1.5
                await asyncio.sleep(min(wait, 5.0))
            out["seconds"] = round(time.monotonic() - t0, 2)
            if r.status_code != 200:
                return {**out, "error": f"llm_http_{r.status_code}: {r.text[:150]}"}
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return {**out, "seconds": round(time.monotonic() - t0, 2), "error": f"llm_network: {e!r}"[:200]}
        usage = data.get("usage") or {}
        out.update(tokens_in=usage.get("input_tokens"), tokens_out=usage.get("output_tokens"),
                   limits={k: r.headers.get(f"anthropic-ratelimit-{k}-limit") for k in ("requests", "input-tokens", "output-tokens")})
        # A model that thinks first sends its thinking as separate blocks ahead of the answer.
        text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
        v = parse_verdict(text, symbols)
        if v:
            return {**out, "verdict": v}
        why = "llm_cut_off" if data.get("stop_reason") == "max_tokens" else "llm_unparseable"
        return {**out, "error": f"{why}: {text[:500]!r}"}

    async def classify(self, article: dict, symbols, names=None, stats=None):
        """Returns (verdict | None, error | None)."""
        res = await self.rate(article, symbols, names, stats)
        return res["verdict"], res["error"]
