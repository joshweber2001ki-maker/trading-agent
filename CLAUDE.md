# Trading Agent

Stand: 2026-11-17 — Strategie zurückgesetzt. Es ist aktuell **keine Strategie definiert**.

Solange hier keine neue Strategie steht, werden **keine Orders platziert**. Lesezugriffe
auf Konto und Marktdaten sind in Ordnung.

## Was vorhanden ist

- `scripts/alpaca.py` — alle Alpaca-Aufrufe (Konto, Positionen, Kurse, Orders); Paper-Trading
- `scripts/guardrails.py` — Sicherheitsprüfungen vor Orders. Die Limits darin stammen noch
  aus der alten Strategie und werden mit der neuen Strategie neu festgelegt.
- `memory/strategy.md` — leer, Platz für die neue Strategie
- `memory/watchlist.json` — leer
- `memory/stops.json` — leer
- `journal/` — leer

## Zugangsdaten

Die Skripte lesen `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` und `APCA_BASE_URL` aus der
Umgebung. Keys gehören nie ins Repo.

## Archiv

Die frühere Strategie „ARIA" (April–Mai 2026) liegt vollständig unter `archive/aria-v1/`:
Regeln, Strategie-Notizen, Verlauf, Watchlist, Stops, Routinen und Journale. Sie ist
nicht mehr gültig und dient nur zum Nachlesen.
