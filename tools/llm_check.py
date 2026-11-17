#!/usr/bin/env python3
"""Ask every configured model (the deciding one and those in the shadow) to rate a few sample articles.

Shows whether each model accepts the request the bot sends, how long it takes, how many tokens
it uses and which rate limits the account has for it. Needs the Anthropic key, so it runs on the server:

  bash deploy/run-tool.sh tools/llm_check.py          # one line per model and sample
  bash deploy/run-tool.sh tools/llm_check.py --json   # the full answers

Costs a few cents. Places no orders and does not touch the running bot.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.common import env, load_config  # noqa: E402
from bot.llm import Classifier  # noqa: E402

# The second and third sample are two false alarms of 2026-11-17, rebuilt from the bot's log (the
# summaries were not logged, so they are short reconstructions). Sizes and prices are approximate.
SAMPLES = [
    ("klar positiv",
     {"headline": "Acme Corp raises full-year guidance well above analyst expectations", "source": "test",
      "summary": "Acme now expects revenue growth of 18 to 20 percent, up from 10 to 12 percent.", "created_at": ""},
     {"ACME": "Acme Corp"},
     {"ACME": {"size": "mid-sized", "dollar_volume": 8e7, "daily_range_pct": 2.8, "price": 40.0}}),
    ("andere Firma, gleiches Kürzel",
     {"headline": "EQT to acquire Flatiron Energy, a leading developer and operator of battery energy storage in the Eastern U.S.",
      "source": "prnewswire", "summary": "", "created_at": "2026-11-17T15:41:00Z"},
     {"EQT": "EQT Corporation"},
     {"EQT": {"size": "large", "dollar_volume": 4.5e8, "daily_range_pct": 3.39, "price": 51.33}}),
    ("Meldung passt nicht zur Lage",
     {"headline": "InMode Reports Q4 Earnings Miss", "source": "fiscal.ai", "created_at": "2026-11-17T15:31:36Z",
      "summary": "InMode reported fourth-quarter earnings per share that missed the consensus estimate by $0.49."},
     {"INMD": "InMode Ltd. Ordinary Shares"},
     {"INMD": {"size": "small", "dollar_volume": 2e7, "daily_range_pct": 3.25, "price": 13.02}}),
]


async def main():
    cfg = load_config()["llm"]
    key = env("ANTHROPIC_API_KEY", required=True)
    models = [cfg["model"], *[m for m in cfg.get("shadow", []) if m != cfg["model"]]]
    full = []
    for m in models:
        clf = Classifier(key, cfg, model=m)
        role = "entscheidet" if m == cfg["model"] else "Schatten"
        results = await asyncio.gather(*(clf.rate(art, list(names), names, stats) for _, art, names, stats in SAMPLES))
        limits = next((r["limits"] for r in results if r.get("limits")), None) or {}
        print(f"{m} ({role}, Nachdenken: {clf.effort}) - Limits je Minute: Anfragen {limits.get('requests')}, "
              f"Eingabe-Token {limits.get('input-tokens')}, Ausgabe-Token {limits.get('output-tokens')}")
        for (label, *_), r in zip(SAMPLES, results):
            v = r["verdict"]
            if v:
                what = (f'{v["direction"]}, Überraschung {v["surprise"]}, Wirkung {v["impact"]}, Sicherheit {v["confidence"]}'
                        f' - {v["reason"][:110]}')
            else:
                what = "FEHLER " + str(r["error"])[:160]
            print(f'  {label}: {r["seconds"]} s, {r["tokens_in"]}+{r["tokens_out"]} Token: {what}')
            full.append({"model": m, "sample": label, **{k: r[k] for k in ("seconds", "tokens_in", "tokens_out", "verdict", "error")}})
    if "--json" in sys.argv:
        print(json.dumps(full, ensure_ascii=False, indent=1))
    sys.exit(0 if all(r["verdict"] for r in full) else 1)


asyncio.run(main())
