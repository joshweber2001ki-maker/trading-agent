"""Shared helpers: config, time, journal."""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
# Logs and state live outside the code checkout on the server (BOT_DATA_DIR), inside it locally.
DATA = Path(os.environ.get("BOT_DATA_DIR") or ROOT)
ET = ZoneInfo("America/New_York")
log = logging.getLogger("bot")


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str) -> datetime:
    """Parse an Alpaca RFC3339 timestamp (nanoseconds, trailing Z)."""
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, tz = rest[:-6], rest[-6:]
        s = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(path=None) -> dict:
    with open(path or ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def env(name: str, default=None, required=False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        raise SystemExit(f"Umgebungsvariable {name} fehlt")
    return v


class Journal:
    """Append-only JSONL logs: one events file per day, one trades file overall."""

    def __init__(self, root: Path = None):
        self.dir = (root or DATA) / "logs"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "daily").mkdir(exist_ok=True)

    def _append(self, path: Path, row: dict):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def event(self, kind: str, **data):
        t = now()
        day = t.astimezone(ET).strftime("%Y-%m-%d")
        self._append(self.dir / f"events-{day}.jsonl", {"t": iso(t), "kind": kind, **data})
        if kind in ("error", "trade_open", "trade_closed", "halt", "orphan"):
            log.info("%s %s", kind, json.dumps(data, default=str)[:300])

    def trade(self, row: dict):
        self._append(self.dir / "trades.jsonl", row)

    def status(self, row: dict):
        tmp = self.dir / "status.json.tmp"
        tmp.write_text(json.dumps({"t": iso(now()), **row}, indent=1, default=str))
        tmp.replace(self.dir / "status.json")

    def daily(self, day: str, row: dict):
        (self.dir / "daily" / f"{day}.json").write_text(json.dumps(row, indent=2, default=str))
