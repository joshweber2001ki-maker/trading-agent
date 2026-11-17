# Trading-Bot (Alpaca Paper-Trading)

Stand: 2026-11-17. Zweck: zwei kurzfristige Strategien mit großen Positionen im
**Paper-Konto** testen. Echtgeld ist nicht vorgesehen; `bot/main.py` verweigert den Start,
wenn `APCA_BASE_URL` nicht auf das Paper-Konto zeigt.

## Aufbau

Ein dauerhaft laufendes Python-Programm (`python -m bot.main`) auf Joshs Hostinger-VPS
(`srv1141361`, Dienst `trading-bot`). Kein KI-Agent entscheidet im Handel selbst: die KI
bewertet nur Nachrichten, alles andere sind feste Regeln aus `config.toml`.

| Datei | Aufgabe |
|---|---|
| `config.toml` | alle Stellschrauben beider Strategien und der Risikogrenzen |
| `bot/news_strategy.py` | Meldung → KI-Bewertung → Bestätigung durch Kurs und Volumen → Einstieg |
| `bot/orb_strategy.py` | Ausbruch aus der Eröffnungsspanne bei Aktien mit hohem relativem Volumen |
| `bot/executor.py` | Positionsgröße, Risikogrenzen, Order mit Stop und Ziel, Zeitlimit, Glattstellen |
| `bot/llm.py` | Nachrichtenbewertung über die Anthropic-API |
| `bot/wires.py` | zweite Nachrichtenquelle: Pressemitteilungen von PR Newswire, Firmenname → Börsenkürzel |
| `bot/alpaca.py`, `bot/market.py` | Alpaca-Zugriff, Handelszeiten, handelbare Aktien |
| `tools/check.py` | Verbindungstest |
| `tools/guard.py` | Feierabend-Wächter: schließt kurz vor Börsenschluss alles, falls der Bot es nicht getan hat |
| `tools/weekly_stats.py` | Wochenstatistik aus den Protokollen |
| `tools/backtest_orb.py` | grobe Rückrechnung der Ausbruchs-Strategie (braucht Alpaca-Schlüssel) |
| `tools/replay_news.py` | einen vergangenen Tag durch die News-Strategie spielen (braucht beide Schlüssel) |
| `deploy/` | Installation, Dienste, stündlicher Abgleich mit GitHub |
| `memory/strategy.md` | die Strategie in Worten, mit Begründung der Startwerte |
| `logs/` | Protokolle des Servers (werden stündlich vom Server hochgeladen) |
| `reports/` | Wochenrückblicke und Bewertungen (offene Vorschläge stehen dort) |
| `archive/aria-v1/` | altes Setup „ARIA" (April–Mai 2026), nur zum Nachlesen |

## Regeln für die Arbeit an diesem Repo

- **Keine Zugangsdaten ins Repo.** Sie liegen nur auf dem Server unter `/etc/trading-bot/`
  (eintragen mit `deploy/set-keys.sh`). In der Git-Historie stehen noch alte, öffentliche
  Paper-Keys; sie gelten als kompromittiert.
- **Strategieänderungen erst vorschlagen, dann umsetzen.** Josh will Änderungen an
  `config.toml` oder an der Handelslogik vorher sehen und freigeben. Fehlerbehebungen ohne
  Einfluss auf die Strategie sind in Ordnung.
- **`logs/` schreibt nur der Server.** Nicht von Hand ändern, sonst gibt es Konflikte.
- **Vor jedem Push:** `python -m pytest tests -q`.
- **Nie eine zweite Instanz des Bots mit denselben Alpaca-Schlüsseln starten**, auch nicht zum
  Testen: Alpaca erlaubt nur eine Verbindung zum Nachrichtenstrom, und eine zweite Instanz
  würde Positionen der ersten als fremd ansehen und schließen.
- **Auslieferung:** Push auf `main` genügt. Der Server holt Änderungen stündlich
  (`deploy/sync.sh`) und startet den Bot nur außerhalb der US-Handelszeit neu.
- **Notbremse:** Datei `/var/lib/trading-bot/HALT` auf dem Server anlegen = keine neuen
  Einstiege. `systemctl stop trading-bot` beendet den Bot; offene Positionen sind über Stop-
  und Zielorders beim Broker abgesichert.

## Protokolle lesen

- `logs/status.json` – Lebenszeichen des Bots (jede Minute neu, stündlich hochgeladen)
- `logs/trades.jsonl` – ein Eintrag je abgeschlossenem Trade
- `logs/events-JJJJ-MM-TT.jsonl` – alle Entscheidungen: bewertete Meldungen (`news_classified`),
  Kursverlauf danach (`news_outcome`), abgelehnte Signale mit Grund, Fehler, Eingriffe des
  Wächters (`guard`)
- `logs/daily/JJJJ-MM-TT.json` – Tagesabschluss
