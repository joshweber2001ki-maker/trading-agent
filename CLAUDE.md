# Trading-Bot (Alpaca Paper-Trading)

Stand: 2026-11-19. Zweck: kurzfristigen Handel nach Unternehmensmeldungen mit großen
Positionen im **Paper-Konto** testen. Die zweite Strategie (Ausbruch aus der Eröffnungsspanne)
ist seit dem 17.11.2026 auf Joshs Entscheidung abgeschaltet (`[orb] enabled = false`); der
Code bleibt im Repo. Echtgeld ist nicht vorgesehen; `bot/main.py` verweigert den Start,
wenn `APCA_BASE_URL` nicht auf das Paper-Konto zeigt.

## Aufbau

Ein dauerhaft laufendes Python-Programm (`python -m bot.main`) auf Joshs Hostinger-VPS
(`srv1141361`, Dienst `trading-bot`). Kein KI-Agent entscheidet im Handel selbst: die KI
bewertet nur Nachrichten, alles andere sind feste Regeln aus `config.toml`.

| Datei | Aufgabe |
|---|---|
| `config.toml` | alle Stellschrauben beider Strategien und der Risikogrenzen |
| `bot/news_strategy.py` | Meldung → KI-Bewertung → Bestätigung durch Kurs und Volumen → Einstieg |
| `bot/orb_strategy.py` | Ausbruch aus der Eröffnungsspanne (abgeschaltet seit 17.11.2026) |
| `bot/executor.py` | Positionsgröße, Risikogrenzen, Order mit Stop und Ziel, Zeitlimit, Glattstellen. Vor und nach der Börse (`[extended]`, abgeschaltet): nur Limit-Orders, Stop und Zeitlimit überwacht der Bot selbst |
| `bot/llm.py` | Nachrichtenbewertung über die Anthropic-API. Ein Modell entscheidet (`[llm] model`), weitere bewerten zum Vergleich mit (`shadow`); jedes Modell braucht eine eigene Form der Anfrage |
| `bot/wires.py` | zweite Nachrichtenquelle: Pressemitteilungen von PR Newswire, Firmenname → Börsenkürzel |
| `bot/alpaca.py`, `bot/market.py` | Alpaca-Zugriff, Handelszeiten, handelbare Aktien |
| `bot/finnhub.py` | Berichtstermine und Analystenerwartungen (Gewinn je Aktie, Umsatz) von Finnhub, kostenloses Konto; noch nicht im Handel verwendet |
| `bot/edgar.py` | Liste der Pflichtmitteilungen an die SEC (Formular 8-K) mit Annahmezeit; nur Messung, kein Handel |
| `bot/speed.py` | Tempo-Messung nach Börsenschluss: Kursverlauf im Sekundentakt nach jeder Meldung; kein Handel |
| `tools/check.py` | Verbindungstest |
| `tools/guard.py` | Wächter, läuft jede Minute unabhängig vom Bot: schließt kurz vor Börsenschluss alles, falls der Bot es nicht getan hat; mit `[extended]` auch vor dem Ende der Sitzungen vor und nach der Börse und sobald der Bot dort drei Minuten kein Lebenszeichen gibt |
| `tools/weekly_stats.py` | Wochenstatistik aus den Protokollen |
| `tools/backtest_orb.py` | grobe Rückrechnung der Ausbruchs-Strategie (braucht Alpaca-Schlüssel) |
| `tools/replay_news.py` | einen vergangenen Tag durch die News-Strategie spielen (braucht beide Schlüssel) |
| `tools/speed_check.py` | eine einzelne Meldung von Hand nachmessen (braucht Alpaca-Schlüssel) |
| `tools/llm_check.py` | alle eingestellten Modelle drei Beispielmeldungen bewerten lassen: Antwortzeit, Token, Limits (braucht den Anthropic-Schlüssel) |
| `tools/finnhub_check.py` | zeigt, was der Finnhub-Schlüssel liefert: Erwartungen im Ergebniskalender, gesperrte Daten (braucht den Finnhub-Schlüssel) |
| `deploy/` | Installation, Dienste, stündlicher Abgleich mit GitHub |
| `memory/strategy.md` | die Strategie in Worten, mit Begründung der Startwerte |
| `logs/` | Protokolle des Servers (werden stündlich vom Server hochgeladen) |
| `reports/` | Wochenrückblicke und Bewertungen (offene Vorschläge stehen dort); `trade-corrections.jsonl`: richtiggestellte Trades |
| `archive/aria-v1/` | altes Setup „ARIA" (April–Mai 2026), nur zum Nachlesen |

## Regeln für die Arbeit an diesem Repo

- **Keine Zugangsdaten und keine persönlichen Daten ins Repo.** Schlüssel liegen nur auf dem
  Server unter `/etc/trading-bot/` (eintragen mit `deploy/set-keys.sh`; der Finnhub-Schlüssel mit
  `deploy/set-keys.sh finnhub`, das startet den Bot nicht neu), ebenso Joshs
  Kontaktadresse für Abfragen bei der SEC (`sec.env`, `SEC_CONTACT=...`). Werkzeuge, die
  Schlüssel brauchen, laufen auf dem Server: `bash deploy/run-tool.sh tools/<datei>.py ...`. In der Git-Historie stehen noch alte, öffentliche
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
  (`deploy/sync.sh`) und startet den Bot nur außerhalb der US-Handelszeit neu, und nie, solange
  eine Position offen ist. Alles, was gepusht wird, läuft also spätestens am Abend auf dem Server:
  Neues am Handel bleibt in `config.toml` abgeschaltet, bis Josh es freigegeben hat.
- **Nicht von Hand im Alpaca-Dashboard handeln, solange der Bot läuft.** Der Bot erkennt eine
  von Hand geschlossene Position und verbucht sie mit dem Ausstiegsgrund `external`; fremde
  Positionen schließt er beim nächsten Start oder vor Börsenschluss. Zum Anhalten die Notbremse nutzen.
- **Notbremse:** Datei `/var/lib/trading-bot/HALT` auf dem Server anlegen = keine neuen
  Einstiege. `systemctl stop trading-bot` beendet den Bot; offene Positionen sind über Stop-
  und Zielorders beim Broker abgesichert. Positionen vor und nach der Börse haben keinen Stop
  beim Broker; sie schließt dann der Wächter binnen weniger Minuten.

## Umbau seit 19.11.2026 (Auftrag von Josh, noch nichts davon im Handel)

Josh will direkt im Paper-Konto handeln statt weiter nur messen: Meldung so schnell und so gründlich
wie möglich bewerten, sofort einsteigen, auch vor und nach der Börse. Freigegeben hat er am 19.11.:
höchstens 10.000 $ je Position, eine Position zur Zeit, Haltezeit höchstens 15 Minuten. Die übrigen
Werte bekommt er vor dem Start noch einmal vorgelegt; bis dahin bleiben `[extended]` und alles
Weitere abgeschaltet. Plan und Stand: `memory/strategy.md`, „Umbau ab 19.11.2026“. Vorschlag mit den
Messwerten dahinter: `reports/2026-11-19-vorschlag-schneller-einstieg.md`.

- Fertig, abgeschaltet: Handel vor und nach der Börse in `bot/executor.py` (`[extended]`), Wächter
  dafür, Ablage und Prüfung des Finnhub-Schlüssels.
- Offen: Sofort-Einstieg aus frischen Meldungen (SEC im Sekundentakt, später RTPR), Dossiers mit den
  Erwartungen vor Quartalszahlen, zweite Meinung des starken Modells, abendliche Prüfung jeder
  Ausführung gegen den echten Umsatz, Alpacas Nachrichtenstrom aus dem Handel nehmen (ab 23.11.).
- Offene Entscheidungen bei Josh: Start von RTPR am 23.11. oder 30.11. (geplant wird mit dem 30.11.),
  Echtzeit-Kurse des gesamten Marktes für 99 $ im Monat (geplant wird vorerst ohne).

## Modelltest bis 20.11.2026

Vom 18. bis 20.11.2026 bewertet Opus 5.5 und entscheidet (Wunsch von Josh), Haiku 5.5 und Sonnet 5.5
bewerten jede Meldung in der Handelszeit zum Vergleich mit. Der Wochenrückblick wertet das aus
(`tools/weekly_stats.py`, Abschnitt „Modellvergleich“). Welches Modell danach entscheidet, legt Josh
fest; bis dahin `[llm]` in `config.toml` nicht ändern. Einzelheiten: `memory/strategy.md`, „Modelltest“.

## Protokolle lesen

- `logs/status.json` – Lebenszeichen des Bots (jede Minute neu, stündlich hochgeladen)
- `logs/trades.jsonl` – ein Eintrag je abgeschlossenem Trade
- `logs/events-JJJJ-MM-TT.jsonl` – alle Entscheidungen: jede zugeordnete Pressemitteilung mit
  Veröffentlichungszeit (`wire_item`), jede SEC-Meldung mit Annahmezeit (`sec_item`), der
  Kursverlauf im Sekundentakt danach (`news_speed`) und im Detail für die Nachrechnung von
  Einstiegen (`news_path`), bewertete Meldungen (`news_classified`, mit
  `model` = das Modell, dessen Bewertung zählt), die Bewertungen der mitlaufenden Modelle
  (`news_shadow`), Einspringen des Ersatzmodells (`news_fallback`),
  Kursverlauf in Minuten danach (`news_outcome`), abgelehnte Signale mit Grund, Fehler,
  Eingriffe des Wächters (`guard`); bei Trades vor und nach der Börse zusätzlich der Entschluss
  zum Ausstieg (`trade_closing`), jede Ausstiegsorder (`exit_order`) und das Glattstellen zum
  Sitzungsende (`session_end`)
- `logs/daily/JJJJ-MM-TT.json` – Tagesabschluss. Das Ergebnis misst gegen den Depotwert zum
  Börsenschluss des Vortags (`basis: last_close`), ein Neustart am Tag ändert daran nichts. Der
  Abschluss vom 17.11.2026 stammt noch von der alten Version und zählt erst ab dem Neustart um
  14:45 UTC; `tools/weekly_stats.py` nimmt für solche Tage den ersten Stand des Tages.
- Trades vor und nach der Börse tragen in `trades.jsonl` `style: soft` und die Sitzung (`phase`:
  `pre` oder `post`), dazu `ticks` und `blind_ticks`: wie oft der Bot den Kurs geprüft hat und wie
  oft er dabei keinen verlässlichen Kurs sah. Ausstiegsgründe dort: `target`, `stop`, `time`,
  `session_end`, `blind` (eine Minute ohne verlässlichen Kurs), `recheck` (zweite Meinung), `external`.
  Mit `[extended]` zählt der Tagesabschluss ab dem Depotwert zu Beginn des New Yorker Tages
  (`basis: day_start`) und wird erst nach der Abendsitzung geschrieben.
- Ein Ausstiegsgrund, der auf `_unconfirmed` endet, heißt: Der Bot konnte den Ausstiegskurs nicht
  beim Broker bestätigen und hat ihn geschätzt. Solche Trades am Konto prüfen. Falsch verbuchte
  Trades werden nicht in `logs/` geändert, sondern in `reports/trade-corrections.jsonl`
  richtiggestellt (eine Zeile je Trade: `id`, `exit_price`, wahlweise `exit_time` und
  `exit_reason`, dazu `note` mit dem Beleg); `tools/weekly_stats.py` rechnet mit dem
  richtiggestellten Ergebnis und nennt beide Werte.
