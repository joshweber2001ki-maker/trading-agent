# Trading Agent

Paper-Trading-Setup auf Basis von Claude und Alpaca.

Stand 2026-11-17: Die frühere Strategie „ARIA" wurde zurückgesetzt und liegt unter
`archive/aria-v1/`. Eine neue Strategie ist noch nicht definiert, siehe `CLAUDE.md`.

## Einrichtung

```bash
pip install requests
cp .env.example .env   # eigene Alpaca-Paper-Keys eintragen
```

Die Skripte lesen `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` und `APCA_BASE_URL` aus der
Umgebung.

## Test

```bash
python scripts/alpaca.py account
python scripts/alpaca.py market_status
python scripts/guardrails.py portfolio
```

## Aufbau

```
trading-agent/
├── CLAUDE.md              # Rolle und Regeln des Agenten (aktuell: keine Strategie)
├── memory/
│   ├── strategy.md        # Strategie (leer)
│   ├── watchlist.json     # Handelsuniversum (leer)
│   └── stops.json         # Stop-Level offener Positionen (leer)
├── journal/               # Tagesprotokolle
├── scripts/
│   ├── alpaca.py          # Alpaca-API
│   └── guardrails.py      # Sicherheitsprüfungen vor Orders
├── archive/aria-v1/       # alte Strategie, Routinen und Journale
└── .env.example
```

## Hinweis

Experimentelles Setup für Paper-Trading. Kein Echtgeld.
