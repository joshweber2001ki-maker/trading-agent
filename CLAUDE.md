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
| `bot/executor.py` | Positionsgröße, Risikogrenzen, Order mit Stop und Ziel, Zeitlimit, Glattstellen. Vor und nach der Börse (`[extended]`): nur Limit-Orders; Stop und Zeitlimit überwacht der Bot selbst, am Kurs, mit dem der Broker die offene Position bewertet |
| `bot/llm.py` | Nachrichtenbewertung über die Anthropic-API. Ein Modell entscheidet (`[llm] model`), weitere bewerten zum Vergleich mit (`shadow`); jedes Modell braucht eine eigene Form der Anfrage |
| `bot/wires.py` | zweite Nachrichtenquelle: Pressemitteilungen von PR Newswire, Firmenname → Börsenkürzel |
| `bot/alpaca.py`, `bot/market.py` | Alpaca-Zugriff, Handelszeiten, handelbare Aktien |
| `bot/fast.py` | Sofort-Einstieg vor und nach der Börse (`[fast]`): Pressemitteilung → voller Text und Erwartungen → KI-Bewertung → Limit ab dem Kurs vor der Meldung → Einstieg |
| `bot/finnhub.py` | Berichtstermine und Analystenerwartungen (Gewinn je Aktie, Umsatz, letzte Quartale) von Finnhub, kostenloses Konto; geht als Vorwissen in die Bewertung des Sofort-Einstiegs |
| `bot/edgar.py` | Liste der Pflichtmitteilungen an die SEC (Formular 8-K) mit Annahmezeit; nur Messung, kein Handel |
| `bot/feedview.py` | Messung alle fünf Minuten: bei wie vielen Aktien die Kursquelle des Bots einen brauchbaren Kurs zeigt (`feed_view`); kein Handel |
| `bot/speed.py` | Tempo-Messung nach Börsenschluss: Kursverlauf im Sekundentakt nach jeder Meldung; kein Handel |
| `tools/check.py` | Verbindungstest |
| `tools/guard.py` | Wächter, läuft jede Minute unabhängig vom Bot: schließt kurz vor Börsenschluss alles, falls der Bot es nicht getan hat; mit `[extended]` auch vor dem Ende der Sitzungen vor und nach der Börse und sobald der Bot dort drei Minuten kein Lebenszeichen gibt |
| `tools/weekly_stats.py` | Wochenstatistik aus den Protokollen; Abschnitt „Sofort-Einstieg“: jede Bewertung gegen den Kursverlauf derselben Meldung (Trefferquote der KI) |
| `tools/backtest_orb.py` | grobe Rückrechnung der Ausbruchs-Strategie (braucht Alpaca-Schlüssel) |
| `tools/replay_news.py` | einen vergangenen Tag durch die News-Strategie spielen (braucht beide Schlüssel) |
| `tools/speed_check.py` | eine einzelne Meldung von Hand nachmessen (braucht Alpaca-Schlüssel) |
| `tools/llm_check.py` | alle eingestellten Modelle drei Beispielmeldungen bewerten lassen: Antwortzeit, Token, Limits (braucht den Anthropic-Schlüssel) |
| `tools/ext_check.py` | zeigt, was der Bot vor und nach der Börse vom Markt sieht: Handelszeiten laut Broker, Alter der Kurse, Spanne (nur lesend, braucht Alpaca-Schlüssel) |
| `tools/fast_check.py` | Trockenlauf des Sofort-Einstiegs an den neuesten echten Pressemitteilungen: Text, Erwartungen, Bewertung, Kurs vor der Meldung, die Order, die er senden WÜRDE. Sendet keine Order (braucht alle Schlüssel, kostet ein paar Cent) |
| `tools/price_check.py` | vergleicht Kursquellen in diesem Moment: Alpacas kostenlose Quelle gegen Finnhub (Abfrage und Strom); nur lesend |
| `tools/price_probe.py` | **handelt**: kauft 1 Aktie im Paper-Konto, beobachtet eine Minute den Kurs der Position laut Broker und verkauft wieder. Zeigt, ob das Paper-Konto vor und nach der Börse den echten Kurs kennt. Nur von Josh von Hand zu starten |
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

## Umbau seit 19.11.2026: erste Fassung im Handel ab 19.11., 22:07 Uhr deutscher Zeit

Josh will direkt im Paper-Konto handeln statt weiter nur messen: Meldung so schnell und so gründlich
wie möglich bewerten, sofort einsteigen, auch vor und nach der Börse. Die Werte der ersten Fassung hat
er am 19.11.2026 um 15:17 Uhr freigegeben; sie stehen in `config.toml` (`[extended]`, `[fast]`) und in
Worten in `memory/strategy.md`, „Erste Fassung im Handel“. Jede Änderung daran braucht wieder sein Ja.

**Was läuft**
- Vor Börsenbeginn (10:00 bis 15:20 Uhr deutscher Zeit) und nach Börsenschluss (22:00 bis 1:45 Uhr):
  `bot/fast.py`. Quelle ist bisher nur PR Newswire, das anderthalb bis dreieinhalb Minuten zu spät
  kommt. Bewertet wird mit dem Modell aus `[llm]`, mit vollem Text der Meldung und, wenn die Firma
  gerade berichtet, mit den Erwartungen von Finnhub.
- Der Bot hat dort keinen Kurs in Echtzeit (die kostenlose Kursquelle zeigt fast nichts, Alpacas
  Datenpaket für 99 $ im Monat will Josh vorerst nicht). Deshalb: Das Limit geht vom letzten Kurs des
  gesamten Marktes VOR der Meldung aus (kostenlos, sobald 15 Minuten alt) und lässt die halbe
  erwartete Bewegung zu; das Paper-Konto führt zum echten Kurs aus oder gar nicht. Stop und Ziel
  zählen ab dem Ausführungskurs. Die offene Position überwacht der Bot am Kurs, mit dem der Broker
  sie bewertet (am 19.11. mit `tools/price_probe.py` geprüft: er folgt dem echten Markt).
- In der regulären Handelszeit gilt weiter die alte Regel mit Kursbestätigung (`bot/news_strategy.py`).

**Befund vom 20.11. (noch ohne Entscheidung von Josh):** Vor und nach der Börse liegt die Spanne
zwischen Geld- und Briefkurs direkt nach einer Meldung typisch bei 2 bis 4 %, die Bewegung in 5 Minuten
bei 2,2 %. Am gemessenen Kursverlauf durchgespielt verliert die erste Fassung dort im Schnitt auch dann,
wenn die Richtung jedes Mal stimmt (−0,4 bis −1,3 % je Trade, jeder zweite bis dritte endet am Stop).
Vorschlag an Josh: dort nur einsteigen, wenn der Bot vorher Geld- und Briefkurs kennt (Probe über
1 Aktie oder Datenpaket) und die Spanne höchstens 0,5 % beträgt; Sofort-Einstieg auch in der
Handelszeit, wo die Spanne klein ist. Dann muss die KI vor und nach der Börse in etwa zwei von drei
Fällen richtig liegen, in der Handelszeit in jedem zweiten. Zahlen: `memory/strategy.md`, „Dritte Auswertung“.

**Was noch fehlt**
- Schnelle Quelle: RTPR (Pressemitteilungen in unter einer Sekunde, 139 $ im Monat nach sieben
  Probetagen). Josh hat den Start noch nicht entschieden; geplant wird mit dem 30.11. Ohne sie
  entstehen wenige Trades, weil der Kurs bei verspäteten Meldungen meist schon gelaufen ist.
- SEC-Meldungen als zweite Quelle für den Sofort-Einstieg (Text der Anlage holen): nachrangig, denn bei
  Quartalszahlen ist die Pressemitteilung fast immer zuerst da (16 von 17 Fällen, im Median 205 Sekunden).
  Die SEC-Liste bekam am 19.11. ab 6:00 Uhr New Yorker Zeit keine Antwort mehr vom Dienst der SEC (nur
  diese eine Adresse; der Server ist nicht gesperrt); der Bot holt bis zu anderthalb Tage nach.
- Dossiers über die Erwartungen hinaus (Prognose der Firma, Kursverlauf davor), zweite Meinung eines
  zweiten Modells, abendliche Prüfung jeder Ausführung gegen den echten Umsatz, Alpacas
  Nachrichtenstrom aus dem Handel nehmen (ab 23.11.).
- Noch nie am echten Konto gelaufen sind: Ziel-Order und ihr Streichen vor dem Ausstieg, Ausstieg bei
  Stop und Zeitlimit, Leerverkauf vor und nach der Börse. Bei den ersten Trades die Protokolle prüfen.

**Geprüft am 19.11.** (Einzelheiten in `memory/strategy.md`): Kalender des Brokers nennt die erweiterte
Handelszeit, auch am halben Tag 27.11.; Finnhub (kostenloses Konto) liefert die Erwartungen für rund
vier von fünf Berichten handelbarer Firmen; der Trockenlauf `tools/fast_check.py` lief an echten
Meldungen durch (Text, Kurs vor der Meldung, Bewertung in zwei bis drei Sekunden).

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
  Sitzungsende (`session_end`); alle fünf Minuten, wie viel die Kursquelle zeigt (`feed_view`:
  je Gruppe `n` Aktien, davon `seeing` mit brauchbarem Kurs); vom Sofort-Einstieg jede Bewertung
  (`fast_rated`, auch ohne Trade; mit `news_speed` derselben Meldung zeigt sich, ob die Richtung
  stimmte), nicht bewertete Meldungen mit Grund (`fast_skipped`), Kandidaten ohne Einstieg mit Grund
  (`fast_rejected`: `too_old`, `no_reference`, `max_open_strategy`, `entry_refused` …) und einmal je
  Laden die Zahl der Firmen mit Erwartungen (`expectations`)
- `logs/daily/JJJJ-MM-TT.json` – Tagesabschluss. Das Ergebnis misst gegen den Depotwert zum
  Börsenschluss des Vortags (`basis: last_close`), ein Neustart am Tag ändert daran nichts. Der
  Abschluss vom 17.11.2026 stammt noch von der alten Version und zählt erst ab dem Neustart um
  14:45 UTC; `tools/weekly_stats.py` nimmt für solche Tage den ersten Stand des Tages.
- Trades vor und nach der Börse tragen in `trades.jsonl` `style: soft` und die Sitzung (`phase`:
  `pre` oder `post`), dazu `ticks`, `price_changes` und `blind_ticks`: wie oft der Bot den Kurs der
  Position geprüft hat, wie oft er sich geändert hatte und wie oft der Bot gar nichts sah.
  `signal_price` ist dort der Kurs vor der Meldung, `slippage_pct` also, wie weit der Kurs bis zur
  Ausführung schon gelaufen war. Ausstiegsgründe: `target`, `stop`, `time`, `session_end`,
  `moved_against` (schon der Ausführungskurs zeigte, dass der Markt gegen die Bewertung gelaufen
  war), `blind` (eine Minute ohne Kursänderung und ohne Kurs aus der Kursquelle), `recheck`, `external`.
  Mit `[extended]` zählt der Tagesabschluss ab dem Depotwert zu Beginn des New Yorker Tages
  (`basis: day_start`) und wird erst nach der Abendsitzung geschrieben.
- Ein Ausstiegsgrund, der auf `_unconfirmed` endet, heißt: Der Bot konnte den Ausstiegskurs nicht
  beim Broker bestätigen und hat ihn geschätzt. Solche Trades am Konto prüfen. Falsch verbuchte
  Trades werden nicht in `logs/` geändert, sondern in `reports/trade-corrections.jsonl`
  richtiggestellt (eine Zeile je Trade: `id`, `exit_price`, wahlweise `exit_time` und
  `exit_reason`, dazu `note` mit dem Beleg); `tools/weekly_stats.py` rechnet mit dem
  richtiggestellten Ergebnis und nennt beide Werte.
