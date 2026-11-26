# Trading-Bot (Alpaca Paper-Trading)

Stand: 2026-11-25. Zweck: kurzfristigen Handel nach Unternehmensmeldungen mit großen
Positionen im **Paper-Konto** testen. Schwerpunkt seit dem 24.11.2026 (Entscheidung Josh): Meldungen
OHNE Quartalszahlen (Übernahmen, Verträge, Kapitalerhöhungen, Personalien), sofort nach der Meldung,
vor, in und nach der Handelszeit. Abgeschaltet, der Code bleibt im Repo: der Ausbruch aus der
Eröffnungsspanne (seit 17.11.2026, `[orb] enabled = false`) und die alte News-Regel mit
Kursbestätigung (seit 24.11.2026, `[news] enabled = false`). Echtgeld ist nicht vorgesehen; `bot/main.py` verweigert den Start,
wenn `APCA_BASE_URL` nicht auf das Paper-Konto zeigt.

## Aufbau

Ein dauerhaft laufendes Python-Programm (`python -m bot.main`) auf Joshs Hostinger-VPS
(`srv1141361`, Dienst `trading-bot`). Kein KI-Agent entscheidet im Handel selbst: die KI
bewertet nur Nachrichten, alles andere sind feste Regeln aus `config.toml`.

| Datei | Aufgabe |
|---|---|
| `config.toml` | alle Stellschrauben der Strategien und der Risikogrenzen |
| `bot/fast.py` | Sofort-Einstieg (`[fast]`), die Strategie, die handelt: Meldung → voller Text und Vorwissen → KI-Bewertung → Blick auf Geld- und Briefkurs (`[fast.look]`) → Limit knapp über dem Briefkurs → Einstieg. Vor der Börse, in der Handelszeit und nach der Börse |
| `bot/news_strategy.py` | alte Regel: Meldung → KI-Bewertung → Bestätigung durch Kurs und Volumen → Einstieg (abgeschaltet seit 24.11.2026; ihre Werte für Stop und Ziel je Wirkungs-Stufe nutzt der Sofort-Einstieg weiter) |
| `bot/orb_strategy.py` | Ausbruch aus der Eröffnungsspanne (abgeschaltet seit 17.11.2026) |
| `bot/executor.py` | Positionsgröße, Risikogrenzen, Order mit Stop und Ziel, Zeitlimit, Glattstellen. Vor und nach der Börse (`[extended]`): nur Limit-Orders; Stop und Zeitlimit überwacht der Bot selbst, am Kurs, mit dem der Broker die offene Position bewertet. `probe`: Geld- und Briefkurs feststellen, indem 1 Aktie hin und zurück gehandelt wird |
| `bot/llm.py` | Nachrichtenbewertung über die Anthropic-API. Ein Modell entscheidet (`[llm] model`), weitere bewerten zum Vergleich mit (`shadow`); jedes Modell braucht eine eigene Form der Anfrage |
| `bot/wires.py` | Pressemitteilungen von PR Newswire und Business Wire (seit 23.11.; GlobeNewswire ist eingebaut, weist den Server aber ab: 403), je Verteiler die Zuordnung zum Börsenkürzel; Haupt-Feeds alle 10 s, Themen-Feeds jede Minute (`[wires]`). Anlegeraufrufe von Kanzleien werden erkannt (`is_alert`), mit `alert` protokolliert und weder bewertet noch gemessen |
| `bot/edgar.py` | Pflichtmitteilungen an die SEC (Formular 8-K): die Liste alle 2 Sekunden (`[edgar] fast_poll_s`, werktags 6:00 bis 17:40 Uhr New Yorker Zeit), jede Meldung mit Annahmezeit protokolliert. Frische Meldungen ohne Quartalszahlen gehen mit ihrem Text und dem der Anlage 99 an den Sofort-Einstieg: bewertet (`rate`) und gehandelt (`trade`) |
| `bot/wirealpha.py` | Pressemitteilungen von WireAlpha (kostenlose Push-Verbindung, vom Anbieter „unter einer Sekunde“, ungeprüft), Quelle `wa-<verteiler>`. Die vier Verteiler werden gehandelt (`[wires] trade`), den vollen Text holt der Bot je Meldung nach; die SEC-Meldungen von WireAlpha werden nur gemessen. Läuft nur mit Schlüssel auf dem Server (`[wirealpha]`) |
| `bot/finnhub_news.py` | Nachrichten von Finnhub (kostenloses Konto); abgeschaltet seit 24.11.2026 (kam 13 bis 14 Minuten nach der Veröffentlichung, nie zuerst) |
| `bot/first.py` | dieselbe Nachricht aus mehreren Quellen: nur die erste wird bewertet und gehandelt, jede spätere Kopie protokolliert (`news_dup`, mit Verspätung) |
| `bot/alpaca.py`, `bot/market.py` | Alpaca-Zugriff, Handelszeiten, handelbare Aktien |
| `bot/finnhub.py` | Berichtstermine und Analystenerwartungen (Gewinn je Aktie, Umsatz, letzte Quartale) von Finnhub, kostenloses Konto; geht als Vorwissen in die Bewertung des Sofort-Einstiegs |
| `bot/alphavantage.py` | Erwartungen der Analysten für die kommenden Quartale und das Jahr (Alpha Vantage, kostenloses Konto: 25 Abfragen am Tag, eine je Sekunde). Im Voraus für die Firmen, die heute und morgen berichten (meistgehandelte zuerst), auf der Stelle für Meldungen, die vom Ausblick einer Firma handeln; eine Antwort gilt eine Woche und übersteht Neustarts (`[alphavantage]`). Geht als Vorwissen in die Bewertung |
| `bot/feedview.py` | Messung alle fünf Minuten: bei wie vielen Aktien die Kursquelle des Bots einen brauchbaren Kurs zeigt (`feed_view`); kein Handel |
| `bot/speed.py` | Tempo-Messung nach Börsenschluss: Kursverlauf im Sekundentakt nach jeder Meldung; kein Handel |
| `tools/check.py` | Verbindungstest |
| `tools/guard.py` | Wächter, läuft jede Minute unabhängig vom Bot: schließt kurz vor Börsenschluss alles, falls der Bot es nicht getan hat; mit `[extended]` auch vor dem Ende der Sitzungen vor und nach der Börse und sobald der Bot dort drei Minuten kein Lebenszeichen gibt |
| `tools/weekly_stats.py` | Wochenstatistik aus den Protokollen; Abschnitt „Sofort-Einstieg“: jede Bewertung gegen den Kursverlauf derselben Meldung (Trefferquote der KI), getrennt nach Pressemitteilungen und SEC-Meldungen und nach Art der Meldung, dazu der Blick auf den Markt (Proben, Spanne) und die mitbewertenden Modelle; Abschnitt „Nachrichtenquellen“: Verspätung je Quelle, auch der SEC-Liste und von WireAlpha |
| `tools/backtest_orb.py` | grobe Rückrechnung der Ausbruchs-Strategie (braucht Alpaca-Schlüssel) |
| `tools/replay_news.py` | einen vergangenen Tag durch die News-Strategie spielen (braucht beide Schlüssel) |
| `tools/speed_check.py` | eine einzelne Meldung von Hand nachmessen (braucht Alpaca-Schlüssel) |
| `tools/llm_check.py` | alle eingestellten Modelle drei Beispielmeldungen bewerten lassen: Antwortzeit, Token, Limits (braucht den Anthropic-Schlüssel) |
| `tools/ext_check.py` | zeigt, was der Bot vor und nach der Börse vom Markt sieht: Handelszeiten laut Broker, Alter der Kurse, Spanne (nur lesend, braucht Alpaca-Schlüssel) |
| `tools/fast_check.py` | Trockenlauf des Sofort-Einstiegs an den neuesten echten Pressemitteilungen, mit `sec` an den neuesten SEC-Meldungen: Text, Erwartungen, Bewertung, Kurs vor der Meldung, die Order, die er senden WÜRDE. Sendet keine Order, auch keine Probe (braucht alle Schlüssel, kostet ein paar Cent) |
| `tools/wirealpha_check.py` | zeigt, was der WireAlpha-Schlüssel liefert: neueste Meldungen, dann 90 Sekunden Strom mit Quellen und Verspätung (nur lesend). Antwortet der Dienst an den eingestellten Adressen nicht, sucht es die richtigen (Beschreibung der Schnittstelle des Dienstes, übliche Adressen und Arten, den Schlüssel zu senden) und nennt die Zeilen für `[wirealpha]` |
| `tools/alphavantage_check.py` | zeigt, ob der kostenlose Alpha-Vantage-Schlüssel die Erwartungen für kommende Quartale und das Jahr liefert (2 der 25 Abfragen am Tag; mit einem Kürzel als Argument nur diese Aktie). Geprüft am 25.11.2026: Er liefert sie |
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
  Server unter `/etc/trading-bot/` (eintragen mit `deploy/set-keys.sh`; weitere Schlüssel mit
  `deploy/set-keys.sh finnhub`, `wirealpha` oder `alphavantage`; das startet den Bot nicht neu, bei
  WireAlpha und Alpha Vantage wird ein Neustart angeboten, wenn keine Position offen ist), ebenso Joshs
  Kontaktadresse für Abfragen bei der SEC (`sec.env`, `SEC_CONTACT=...`). Werkzeuge, die
  Schlüssel brauchen, laufen auf dem Server: `bash deploy/run-tool.sh tools/<datei>.py ...`. In der Git-Historie stehen noch alte, öffentliche
  Paper-Keys; sie gelten als kompromittiert. Der WireAlpha-Schlüssel ist Teil der Adresse der
  Verbindung, der von Alpha Vantage Teil jeder Abfrage: nie eine Adresse oder Fehlermeldung mit ihnen protokollieren.
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

## Stand des Handels: zweite Fassung des Sofort-Einstiegs (Freigabe Josh 24.11.2026)

Josh will direkt im Paper-Konto handeln: Meldung so früh und so gründlich wie möglich bewerten und sofort
einsteigen. Die erste Fassung (Freigabe 19.11.2026, 15:17 Uhr) lief vom 19. bis 24.11. vor und nach der
Börse. Am 24.11.2026 hat Josh den Vorschlag `reports/2026-11-24-vorschlag-zielsetup.md` freigegeben
(„alles, was kostenlos ist und hilft“) und damit die zweite Fassung aus
`reports/2026-11-20-vorschlag-zweite-fassung.md`. Die Werte stehen in `config.toml` (`[fast]`,
`[fast.look]`, `[extended]`, `[edgar]`) und in Worten in `memory/strategy.md`, „Zweite Fassung im Handel“.
Jede Änderung daran braucht wieder sein Ja.

**Warum der Schwerpunkt verlagert ist.** Bei Quartalszahlen ist nach 10 Sekunden fast die Hälfte der
Bewegung gelaufen und die Spanne zwischen Geld- und Briefkurs liegt bei 5 %; das ist mit keiner Quelle zu
holen. Bei Meldungen ohne Quartalszahlen braucht der Kurs 30 bis 120 Sekunden, die Spanne ist öfter eng,
und man muss den Text lesen, um die Richtung zu erkennen (Zahlen: `memory/strategy.md` und der Vorschlag).

**Was läuft**
- Der Sofort-Einstieg `bot/fast.py` in allen drei Sitzungen: vor Börsenbeginn (10:00 bis 15:20 Uhr
  deutscher Zeit), in der Handelszeit (ab 5 Minuten nach Beginn bis 30 Minuten vor Schluss) und nach
  Börsenschluss (22:00 bis 1:45 Uhr). Bewertet wird mit dem Modell aus `[llm]`, mit vollem Text und, wenn
  die Firma gerade berichtet, mit den Erwartungen von Finnhub.
- **Blick auf den Markt vor jedem Einstieg** (`[fast.look]`): Geld- und Briefkurs kommen aus der
  Kursquelle, wenn sie beide zeigt, höchstens 10 Sekunden alt und höchstens 0,5 % auseinander. Sonst die
  Probe: 1 Aktie in Richtung der Bewertung kaufen (oder leer verkaufen) und sofort zurückhandeln; das
  Paper-Konto führt zum echten Brief- und Geldkurs aus. Kommt der Rückweg mit 0,5 % Abstand nicht zustande,
  geht die Aktie mit einem Limit weit durch den Markt zurück (kein Einstieg, aber die Spanne ist gemessen). Einstieg nur bei einer Spanne bis 0,5 %, wenn der
  Kurs höchstens 0,5 % gegen die Bewertung gelaufen ist und nicht weiter als die halbe erwartete Bewegung.
  Limit 0,2 % über dem gesehenen Briefkurs, höchstens das Limit der ersten Fassung (Kurs vor der Meldung
  plus halbe erwartete Bewegung).
- Vor und nach der Börse zählen Stop und Ziel ab dem Ausführungskurs und der Bot überwacht sie am Kurs,
  mit dem der Broker die Position bewertet. In der Handelszeit liegen Stop und Ziel als Orders beim Broker,
  gerechnet ab dem gesehenen Brief- oder Geldkurs.
- **Alles handelt sofort, nichts wird erst gemessen** (Entscheidung Josh 24.11.2026, „ist ja Paper-Trading“).
- **Pressemitteilungen:** PR Newswire (im Median 98 Sekunden zu spät) und Business Wire (rund 340 Sekunden)
  über ihre Feeds; GlobeNewswire weist den Server ab (403), nicht umgehen. Dazu WireAlpha per Push
  (PR Newswire, Business Wire, GlobeNewswire, AccessWire), sobald Josh den Schlüssel eingetragen hat;
  ungeprüft ist, ob es wirklich in unter einer Sekunde liefert und wie gut seine Zuordnung zum Börsenkürzel
  ist. Kommt dieselbe Nachricht mehrfach, zählt die erste.
- **SEC-Pflichtmitteilungen:** Die Liste wird alle 2 Sekunden abgefragt; der Bot sieht eine Meldung damit
  rund 35 bis 60 Sekunden nach der Annahme statt nach fünf Minuten. Frische Meldungen (höchstens 10 Minuten
  alt, ohne Quartalszahlen, keine bloße Formalie) werden gelesen, bewertet und gehandelt (`fast_rated` mit
  Quelle `sec`; `[edgar] trade = false` würde nur bewerten). Die SEC veröffentlicht nach 17:30 Uhr New Yorker
  Zeit Angenommenes erst um 6:00 Uhr am nächsten Morgen.
- **Vorwissen für die KI:** zu jeder Meldung, was der Bot in den letzten drei Tagen schon von derselben Firma
  gesehen hat (eine Pflichtmitteilung wiederholt oft eine Pressemitteilung), bei Pflichtmitteilungen der
  Hinweis darauf, bei Berichten die Erwartungen der Analysten für das berichtete Quartal (Finnhub) und, seit
  25.11., für die kommenden Quartale und das Jahr (Alpha Vantage; auch bei Meldungen über den Ausblick).
- **Nur zum Vergleich:** Haiku und Sonnet bewerten jede Meldung des Sofort-Einstiegs mit (`fast_shadow`),
  ohne Einfluss auf den Handel. Die SEC-Meldungen, die WireAlpha liefert, werden nur gemessen.
- **Abgeschaltet:** die alte Regel mit Kursbestätigung, Alpacas Nachrichtenstrom (`[news] stream = false`;
  Stunden verspätet, jede siebte Meldung zur falschen Firma) und die Finnhub-Nachrichten.

**Was noch fehlt**
- Die Schlüssel von Alpha Vantage (25.11.) und WireAlpha (26.11.) liegen auf dem Server. WireAlpha läuft
  trotzdem noch nicht: Die Adressen aus seiner Dokumentation kennt sein Server nicht (Abfrage 404, Strom
  403), und die dort genannten Programmbibliotheken gibt es nicht. Die Adressen sind deshalb in `[wirealpha]`
  einstellbar; `bash deploy/run-tool.sh tools/wirealpha_check.py` sucht die richtigen. Bis sie eingetragen
  sind, versucht der Bot alle fünf Minuten vergeblich zu verbinden. WireAlpha ist am echten Dienst ungeprüft: nach dem ersten Tag `wirealpha_sample`, `wire_item` mit `wa-…`
  und die Zuordnung zum Börsenkürzel ansehen.
- Abendliche Prüfung jeder Ausführung gegen den echten Umsatz (das Paper-Konto prüft die Ordergröße nicht
  gegen die verfügbare Menge; die Probe mit 1 Aktie zeigt den Kurs, nicht die Tiefe).
- Rest des Dossiers: erwartete Bewegung aus Optionspreisen, frühere Prognose der Firma, und der
  Zahlenvergleich im Code statt durch die KI.
- Zweiter Weg zu den SEC-Meldungen (die Liste gab am 19.11. einen Tag lang keine Antwort).
- Zwei offene Punkte für Josh aus der Prüfung des Umbaus (`memory/strategy.md`, „Offener Punkt für Josh“):
  Der engste Stop (0,45 %) liegt innerhalb der erlaubten Spanne (0,5 %); Business Wire kommt meist später
  an als das Höchstalter von 300 Sekunden.
- Interactive Brokers als Datenquelle (Kurse 4,50 $, Benzinga-Meldungen 35 $ im Monat): erst, wenn Josh
  dort ein Konto mit mindestens 500 $ hat; vorher lässt sich nichts bauen oder prüfen. RTPR scheidet aus:
  Seine Bedingungen verbieten die Nutzung in einem Handels-Bot.
- Noch nie am echten Konto gelaufen sind: die Probe (Dauer geschätzt 1 bis 3 Sekunden), der Einstieg in
  der Handelszeit über den Sofort-Einstieg, Leerverkauf vor und nach der Börse, Ziel-Order und ihr Streichen
  vor dem Ausstieg. Bei den ersten Trades die Protokolle prüfen (`fast_probe`, `trade_open`, `exit_order`).

**Geprüft am 19.11.** (Einzelheiten in `memory/strategy.md`): Kalender des Brokers nennt die erweiterte
Handelszeit, auch am halben Tag 27.11.; Finnhub (kostenloses Konto) liefert die Erwartungen für rund
vier von fünf Berichten handelbarer Firmen; der Trockenlauf `tools/fast_check.py` lief an echten
Meldungen durch (Text, Kurs vor der Meldung, Bewertung in zwei bis drei Sekunden); der Broker bewertet
eine offene Position vor und nach der Börse am echten Markt (`tools/price_probe.py`).

## Modelle

Seit dem 18.11.2026 bewertet Opus 5.5 und entscheidet (Wunsch von Josh). Haiku 5.5 und Sonnet 5.5
bewerten zum Vergleich mit: bis 24.11. bei der alten Regel in der Handelszeit (`news_shadow`), seitdem
bei jeder Meldung des Sofort-Einstiegs (`fast_shadow`, `[fast] shadows`). Der Wochenrückblick wertet das
aus (`tools/weekly_stats.py`, „mitbewertet von …“). Welches Modell entscheidet, legt Josh fest; bis dahin
`[llm]` in `config.toml` nicht ändern. Einzelheiten: `memory/strategy.md`, „Modelltest“.

## Protokolle lesen

- `logs/status.json` – Lebenszeichen des Bots (jede Minute neu, stündlich hochgeladen); darin unter
  `edgar` auch, wie lange die Abfragen der SEC-Liste dauern (`median_s`, `p90_s`), unter `estimates`, wie
  viele Abfragen bei Alpha Vantage heute verbraucht sind (`asked_today`) und für wie viele Firmen Zahlen
  vorliegen (`kept`)
- `logs/trades.jsonl` – ein Eintrag je abgeschlossenem Trade
- `logs/events-JJJJ-MM-TT.jsonl` – alle Entscheidungen: jede zugeordnete Pressemitteilung mit
  Veröffentlichungszeit (`wire_item`; `alert: true` = Anlegeraufruf einer Kanzlei, weder bewertet noch
  gemessen; Quelle `wa-…` = über WireAlpha), jede SEC-Meldung mit Annahmezeit (`sec_item`), der
  Kursverlauf im Sekundentakt danach (`news_speed`) und im Detail für die Nachrechnung von
  Einstiegen (`news_path`), bewertete Meldungen (`news_classified`, mit
  `model` = das Modell, dessen Bewertung zählt), die Bewertungen der mitlaufenden Modelle
  (`news_shadow`), Einspringen des Ersatzmodells (`news_fallback`),
  Kursverlauf in Minuten danach (`news_outcome`), abgelehnte Signale mit Grund, Fehler,
  Eingriffe des Wächters (`guard`); bei Trades vor und nach der Börse zusätzlich der Entschluss
  zum Ausstieg (`trade_closing`), jede Ausstiegsorder (`exit_order`) und das Glattstellen zum
  Sitzungsende (`session_end`); alle fünf Minuten, wie viel die Kursquelle zeigt (`feed_view`:
  je Gruppe `n` Aktien, davon `seeing` mit brauchbarem Kurs); vom Sofort-Einstieg jede Bewertung
  (`fast_rated`, auch ohne Trade; `source: sec` = Pflichtmitteilung, `article` ist dann ihre
  Zugangsnummer; `seen_before` = die KI bekam frühere Meldungen derselben Firma als Vorwissen, `with_estimates` = die
  Erwartungen für die kommenden Quartale und das Jahr; mit `news_speed` derselben Meldung zeigt sich, ob die Richtung stimmte), die Bewertung
  derselben Meldung durch die mitlaufenden Modelle (`fast_shadow`), nicht bewertete Meldungen mit Grund
  (`fast_skipped`; `no_text` = Text der Pflichtmitteilung nicht abrufbar), Kandidaten ohne Einstieg mit Grund
  (`fast_rejected`: `too_old`, `no_reference`, `max_open_strategy`, `entry_refused`; `source_off` = Quelle
  wird nur bewertet, nicht gehandelt (seit 24.11. keine mehr); `session_window` = erste 5 oder letzte 30 Minuten der Handelszeit;
  `session` = Sitzung endete während der Bewertung; `stray_share` = von einer Probe ist in dieser Aktie
  etwas liegen geblieben, siehe `error` mit `where: probe_leftover`;
  aus dem Blick auf den Markt, mit `how` = `feed` oder `probe` und den gesehenen Kursen: `spread`,
  `moved_against`, `too_far`, `probe_no_fill` = Kurs schon über dem Limit, `probe_error`, `probe_busy`),
  jede Probe mit 1 Aktie (`fast_probe`: `ok`, `bid`, `ask`, `spread_pct`, `seconds`), die ersten fünf
  Nachrichten von WireAlpha je Tag im Wortlaut (`wirealpha_sample`), einmal je
  Laden die Zahl der Firmen mit Erwartungen (`expectations`) und jede spätere Kopie einer schon
  gemeldeten Nachricht (`news_dup`: Quelle, erste Quelle, `lag_s` = wie viel später; `blocked: false`
  heißt, die erste Quelle wird nur gemessen, die spätere wurde trotzdem gehandelt)
- `logs/daily/JJJJ-MM-TT.json` – Tagesabschluss. Das Ergebnis misst gegen den Depotwert zum
  Börsenschluss des Vortags (`basis: last_close`), ein Neustart am Tag ändert daran nichts. Der
  Abschluss vom 17.11.2026 stammt noch von der alten Version und zählt erst ab dem Neustart um
  14:45 UTC; `tools/weekly_stats.py` nimmt für solche Tage den ersten Stand des Tages.
- Trades vor und nach der Börse tragen in `trades.jsonl` `style: soft` und die Sitzung (`phase`:
  `pre` oder `post`), dazu `ticks`, `price_changes` und `blind_ticks`: wie oft der Bot den Kurs der
  Position geprüft hat, wie oft er sich geändert hatte und wie oft der Bot gar nichts sah.
  `signal_price` ist dort der Kurs vor der Meldung, `slippage_pct` also, wie weit der Kurs bis zur
  Ausführung schon gelaufen war. Trades des Sofort-Einstiegs in der Handelszeit tragen `style: bracket`
  und `phase: regular`. `meta.market` nennt, was der Bot vor dem Einstieg vom Markt sah (`how`, `bid`,
  `ask`, `spread_pct`, `moved_pct` = Lauf seit vor der Meldung in Richtung der Bewertung). Die Proben
  stehen nicht in `trades.jsonl`; sie kosten je die Spanne auf 1 Aktie und stecken im Depotwert. Ausstiegsgründe: `target`, `stop`, `time`, `session_end`,
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
