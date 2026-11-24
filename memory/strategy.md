# Strategie (Version 2, Start 2026-11-17)

Ziel laut Josh: viele Trades, große Positionen bzw. Hebel, sehr kurze Haltedauer.
Getestet wird im Alpaca-Paper-Konto (rund 100.000 $, 4-facher Intraday-Hebel).
Alle Zahlen stehen in `config.toml`; hier steht, was sie bedeuten und warum sie so gewählt sind.

## Gemeinsame Regeln

- **Risiko pro Trade:** 0,5 % des Depots bis zum Stop. Die Stückzahl ergibt sich daraus:
  enger Stop = große Position. Bei 0,5 % Stop-Abstand ist die Position so groß wie das Depot.
- **Grenzen:** Einzelposition höchstens 1× Depot, alle Positionen zusammen höchstens 3× Depot,
  höchstens 4 Positionen gleichzeitig, höchstens 20 Trades pro Tag.
- **Tagesverlust:** ab −2 % keine neuen Einstiege mehr.
- **Jede Order** geht mit Stop und Kursziel an den Broker; beides liegt dort, auch wenn der
  Bot ausfällt. Kursziel = 2× Stop-Abstand.
- **Über Nacht wird nichts gehalten:** 10 Minuten vor Börsenschluss stellt der Bot alles glatt.
  Ein unabhängiger Wächter (`tools/guard.py`, alle 2 Minuten) prüft das 7 Minuten vor Schluss
  und schließt notfalls selbst. Grund: Stop- und Zielorders gelten nur für den Tag und
  verfallen zum Börsenschluss; ohne Wächter bliebe bei einem Absturz des Bots eine
  ungesicherte Position über Nacht offen.
- Long und Short. Keine Aktien unter 5 $.

## Nachrichtenquellen

Stand 24.11.2026: Gehandelt werden Pressemitteilungen von PR Newswire und Business Wire; Pflichtmitteilungen
an die SEC werden gelesen und bewertet, WireAlpha gemessen; Alpacas Strom ist abgeschaltet. Siehe „Zweite
Fassung im Handel“ weiter unten. Der Rest dieses Abschnitts beschreibt den Start am 17.11.

1. **Alpacas Nachrichtenstrom.** Liefert seit 2026 nur noch KI-Zusammenfassungen des Anbieters
   fiscal.ai, gebündelt etwa zur Minute 02 und 32 jeder Stunde. Gemessen am 17.11.: dieselben
   Ereignisse kommen dort 18 bis über 90 Minuten nach der Pressemitteilung an (Median 50
   Minuten), tragen aber einen frischen Zeitstempel. Für schnelles Handeln zu spät.
2. **Pressemitteilungen von PR Newswire** (`bot/wires.py`, zehn RSS-Feeds). Gemessen am 17.11.:
   Eine Meldung erreicht den Bot im Median 116 Sekunden nach Veröffentlichung, 90 % unter
   200 Sekunden. Der Feed nennt den Firmennamen, aber kein Börsenkürzel; der Name
   wird deshalb mit den Namen der handelbaren US-Aktien abgeglichen. Es zählen nur exakte
   Treffer, damit nie die falsche Aktie gehandelt wird. Abgedeckt ist nur dieser eine
   Verteiler, also ein Teil aller Unternehmensmeldungen.

Jede Meldung mit höchstens drei Aktien bewertet die KI (seit 18.11.2026 Claude Opus 5.5, davor
Claude Haiku 5.5; siehe „Modelltest“ weiter unten):

- **Richtung:** hoch, runter oder unklar
- **Überraschung (0–10):** wie neu und unerwartet die Meldung ist
- **Wirkung (0–3):** wie stark sie den Kurs dieser Aktie bewegen dürfte: 0 = kaum (unter 1 %),
  1 = mittel (1–3 %), 2 = groß (3–8 %), 3 = sehr groß (über 8 %)
- **Sicherheit (0–10):** wie sicher die Richtung ist

Damit die KI die Wirkung relativ zur Firma einschätzen kann, bekommt sie zu jedem Kürzel den
Firmennamen, den täglichen Handelsumsatz der Aktie (als Maß für die Größe) und ihre übliche
Tagesschwankung. Beides rechnet der Code aus den Tageskursen der letzten Wochen. Meldungen
über eine andere Firma mit zufällig gleichem Kürzel stuft die KI auf 0.

## Strategie 1: Nachrichten (während der Handelszeit)

Abgeschaltet seit 24.11.2026 (`[news] enabled = false`): In der Handelszeit gilt seitdem der
Sofort-Einstieg. Die Werte für Stop und Ziel je Wirkungs-Stufe (`[news.impact.*]`) nutzt er weiter.

Idee: nicht die erste Sekunde handeln (die gewinnen Profi-Systeme), sondern die
Fortsetzung in den Minuten danach.

1. Gehandelt wird nur bei klarer Richtung, Überraschung ≥ 7, Sicherheit ≥ 6 und Wirkung ≥ 1.
   Meldungen ohne nennenswerte Wirkung auf den Kurs fallen also weg.
2. Dann wartet der Bot bis zu 5 Minuten auf Bestätigung: Kurs mindestens 0,4 % in die
   erwartete Richtung, Minutenvolumen mindestens das Doppelte des Üblichen, enge
   Geld-/Brief-Spanne. Ist der Kurs schon über 3 % gelaufen, wird nicht hinterhergekauft.
3. Die Handelsspanne richtet sich nach der Wirkungs-Stufe (Werte für eine Durchschnittsaktie):

   | Wirkung | Stop | Kursziel | Zeitlimit |
   |---|---|---|---|
   | mittel | 0,5 % | 1 % (2× Stop) | 30 min |
   | groß | 1 % | 3 % (3× Stop) | 60 min |
   | sehr groß | 2 % | 6 % (3× Stop) | 90 min |

4. **Schwankungsfaktor.** Stop, Mindestbewegung (0,4 %) und Höchstbewegung (3 %) werden mit
   der üblichen Tagesschwankung der Aktie skaliert: übliche Schwankung geteilt durch 2,5 %,
   begrenzt auf 0,6 bis 2,0. Eine ruhige Aktie bekommt engere Werte, eine wilde weitere.
5. Der Stop liegt außerdem nie näher als die halbe Kursspanne der letzten 5 Minuten, damit ihn
   nicht das Rauschen direkt nach der Meldung auslöst (höchstens verdoppelt das den Stop).
6. Weil das Risiko pro Trade fest bei 0,5 % des Depots liegt, gilt: weiterer Stop = kleinere
   Position. Pro Aktie höchstens ein News-Trade am Tag; nach einem Signal 60 Minuten keine
   weiteren Signale für dieselbe Aktie.

**Mitschreiben ohne Handel.** Zu jeder in der Handelszeit bewerteten Meldung notiert der Bot,
wie sich der Kurs nach 5, 15, 30 und 60 Minuten entwickelt hat (`news_outcome` im Protokoll),
egal ob gehandelt wurde. Daran lässt sich prüfen, ob Richtung und Wirkungs-Stufe der KI
stimmen, auch wenn kaum Trades zustande kommen. Gemessen wird einmal am Tag, 20 Minuten nach
Börsenschluss, mit Kursen des gesamten Marktes (die sind kostenlos, sobald sie 15 Minuten alt
sind). Die IEX-Kurse, die der Bot live sieht, haben bei kleineren Aktien Lücken von Minuten
bis Stunden; damit sähe es so aus, als hätte sich nach der Meldung nichts bewegt.

## Strategie 2: Ausbruch aus der Eröffnungsspanne (abgeschaltet seit 2026-11-17)

**Josh hat die Strategie am 17.11.2026 abgeschaltet** (`[orb] enabled = false`), weil die kurze
Form in der Rückrechnung schon vor Kosten verliert. Damit bewertet die KI auch keine Meldungen
außerhalb der Handelszeit mehr; sie dienten nur als Kandidaten für die Eröffnung. Die
Beschreibung bleibt zum Nachlesen stehen.

Idee: Aktien, die an dem Tag „im Spiel" sind, laufen nach dem Ausbruch aus den ersten
Handelsminuten oft in dieselbe Richtung weiter.

1. **Pool.** Feste Liste liquider Aktien, größte Gewinner und Verlierer, meistgehandelte
   Aktien, dazu alle Meldungen seit dem letzten Börsenschluss mit Überraschung ≥ 6
   (höchstens 40). ETFs und Hebelprodukte sind ausgeschlossen.
2. **Auswahl nach den ersten 5 Minuten.** Übrig bleiben Aktien mit Kurslücke ≥ 1 %
   (für News-Kandidaten nicht nötig), relativem Volumen ≥ 1,0 gegenüber den letzten
   10 Handelstagen, Spanne zwischen 0,2 % und 2,5 % und genug Umsatz. Die 8 mit dem höchsten
   relativen Volumen werden beobachtet.
3. **Einstieg** beim Ausbruch über das Hoch (long) oder unter das Tief (short), bis 11:00 Uhr
   New Yorker Zeit, einmal pro Aktie und Tag.
4. **Stop-Abstand:** halbe Spannenbreite, mindestens 0,3 %, höchstens 1,0 %. Zeitlimit 30 Minuten.

## Erste Befunde vor dem Start (2026-11-17)

**Ausbruchs-Strategie, Rückrechnung über 90 Tage** (`tools/backtest_orb.py`, nur die feste
Liste von 20 Aktien, 0,02 % Slippage je Seite). Mit IEX-Daten: Zeitlimit 90 Minuten (erste
Einstellung) 207 Trades, Trefferquote 37 %, Ergebnis −4,9 %, Profitfaktor 0,92; Zeitlimit
30 Minuten (heutige Einstellung) −1,3 %, Profitfaktor 0,98, ohne Slippage +2,7 %.
**Mit Kursen des gesamten Marktes (`--feed sip`, nachgerechnet am 17.11.) fällt die heutige
Einstellung deutlich schlechter aus:** 172 Trades, Trefferquote 35 %, Ergebnis −9,3 %,
Profitfaktor 0,80; ohne Slippage −4,3 %, mit 0,05 % je Seite −15,7 %. In den IEX-Kursen
fehlen Ausschläge, die in Wirklichkeit Stops auslösen. Einzige positive Variante auf beiden
Datenquellen: nur in Richtung der Eröffnungsspanne, Stop am anderen Ende der Spanne, kein
Kursziel, halten bis Börsenschluss (Gesamtmarkt +9,7 %, Profitfaktor 1,31, 136 Trades; IEX
+9,2 %, 154 Trades). Das entspricht der veröffentlichten Untersuchung zu dieser Strategie,
widerspricht aber dem Wunsch nach sehr kurzer Haltedauer und ist mit 136 Trades kein
belastbarer Beleg. **Fazit: Auf der festen Liste verliert die kurze Variante schon vor
Kosten.** Offen ist, ob die tagesaktuellen Kandidaten (Gewinner, Verlierer, News) besser
laufen; das kann nur der Paper-Test zeigen.

**Nachrichten-Strategie, Nachspiel des 16.11.2026** (`tools/replay_news.py`, nur Alpacas
Strom): 401 Meldungen in der Handelszeit, 232 bewertet, 6 als handelbar eingestuft, bei
keiner bestätigte der Kurs die Richtung, 0 Trades. Grund: die Meldungen kamen zu spät.
Deshalb die zweite Quelle.

## Tempo: Messungen vom 17.11.2026

Josh will deutlich schneller nach einer Meldung handeln. Was gemessen ist:

- **PR Newswire (RSS):** Der Feed selbst hängt 1,5 bis 3,5 Minuten hinter der Veröffentlichung
  (Median 2,5 Minuten bei Abfrage alle 5 Sekunden, 21 Meldungen). Häufiger abfragen hilft nicht.
- **SEC (Pflichtmitteilungen 8-K, kostenloser Feed):** sichtbar 30 bis 40 Sekunden nach Annahme
  (2 Meldungen). Die Pflichtmitteilung folgt aber oft erst auf die Pressemitteilung.
- **Wann die Meldungen kommen:** Von 380 Pflichtmitteilungen am 16.11. gingen 30 während der
  Handelszeit ein, 151 davor und 199 danach. Der Bot handelt nur in der regulären Handelszeit.
- **Schnelle Quellen kosten Geld:** RTPR (alle vier großen Verteiler, laut Anbieter unter
  0,5 Sekunden, 139 $/Monat nach 7 Tagen Test); WireAlpha (laut Anbieter kostenlos, ungeprüft).

**Tempo-Messung (läuft ab dem Abend des 17.11., kein Einfluss auf den Handel).** Der Bot schreibt
jede zugeordnete Pressemitteilung mit Veröffentlichungszeit mit (`wire_item`) und jede
Pflichtmitteilung an die SEC mit Annahmezeit (`sec_item`, `bot/edgar.py`). Einmal täglich,
25 Minuten nach Börsenschluss, misst `bot/speed.py` zu jeder dieser Meldungen aus den einzelnen
Abschlüssen des gesamten Marktes (kostenlos, sobald sie 15 Minuten alt sind): Kurs nach 1, 2, 5,
10, 30, 60, 120 und 300 Sekunden sowie nach 15 und 30 Minuten, gehandelter Umsatz in den ersten
Sekunden, Bewegung kurz vor der Veröffentlichung (`news_speed`). Gemessen wird auch vor- und
nachbörslich. Die Wochenstatistik zeigt daraus, wie viel der Bewegung noch zu holen ist, wenn man
2, 10, 60 oder 120 Sekunden nach der Meldung einsteigt. Das entscheidet, ob eine schnelle
Nachrichtenquelle ihr Geld wert ist.

**Mit Josh am 17.11. vereinbarter Ablauf:** diese Woche messen; parallel Vorschlag für
Sofort-Einstieg und Handel außerhalb der regulären Zeit ausarbeiten (Josh gibt frei); am Montag,
23.11., den 7-Tage-Test von RTPR starten, falls die Messung dafür spricht, und an Tag 6 kündigen,
wenn die Zahlen es nicht tragen.

## Erste Ergebnisse der Tempo-Messung (Lauf vom 17.11.2026, 22:25 Uhr deutscher Zeit)

Gemessen wurden 644 Meldungen (604 Pflichtmitteilungen an die SEC seit dem 13.11., 40
Pressemitteilungen vom 17.11.), ohne Fehler, in 17 Minuten. 237 davon betrafen Aktien, die in
den zehn Minuten vor der Meldung gar nicht gehandelt wurden; sie zählen nicht mit.

- Von den übrigen 407 Meldungen bewegte sich der Kurs bei 108 binnen fünf Minuten um
  mindestens 1 %. In diesen fünf Minuten wurden im Median aber nur 6.200 $ gehandelt. Die
  meisten dieser Ausschläge sind also dünner Handel vor oder nach der Börse, in den keine
  Position passt.
- Mit mindestens 100.000 $ Umsatz in fünf Minuten bleiben 25 Meldungen in drei Tagen:
  17 vorbörslich, 8 nachbörslich, keine in der Handelszeit. Bei ihnen (Mediane): Bewegung
  nach fünf Minuten 1,7 %; davon noch zu holen bei Einstieg nach 2 s / 10 s / 60 s / 120 s:
  2,0 / 2,0 / 1,7 / 1,25 Prozentpunkte; Stand nach 30 Minuten 1,8 %; Umsatz in den ersten
  zehn Sekunden: 0 $.
- In der Handelszeit: 50 Meldungen, 10 mit Bewegung, keine mit genug Umsatz; die Bewegung
  war dort nach zwei Minuten vorbei.
- Beispiele: Disney (Quartalszahlen, 12:30 Uhr deutscher Zeit, vorbörslich): nach 10 s noch
  unverändert, nach 60 s +2,9 %, nach 5 min +4,7 %, nach 30 min +3,6 %; 5,6 Mio. $ Umsatz in
  5 min. Nike (15:01 Uhr): nach 10 s +1,0 %, nach 60 s +2,4 %, nach 5 min +3,1 %, nach
  30 min +3,9 %; 21 Mio. $.

Vorläufige Lesart, drei Tage und fast nur SEC-Meldungen:

1. Kursreaktionen mit genug Umsatz gibt es vor der Eröffnung und nach Börsenschluss, also
   genau dann, wenn der Bot bisher nicht handelt.
2. In den ersten zehn Sekunden wird kaum gehandelt. Es zählt, binnen etwa einer Minute da zu
   sein; ein Vorsprung von Sekundenbruchteilen bringt bei diesen Meldungen wenig.
3. Die Zahlen sind Obergrenzen: Die Richtung gilt als bekannt, die Geld-/Brief-Spanne ist
   nicht gemessen (vor- und nachbörslich ist sie weit), und für dünn gehandelte Aktien fehlt
   der Vergleich mit Zeiten ohne Meldung. Eine Pflichtmitteilung kann außerdem später kommen
   als die Pressemitteilung zum selben Ereignis.

Bewertung gegen Kursverlauf am 17.11. (Haiku, 249 Meldungen in der Handelszeit, Kurse des
gesamten Marktes): Bei den 43 Meldungen mit Richtung lief der Kurs in den 30 Minuten nach
Eingang im Schnitt 0,07 % in die vorhergesagte Richtung (Richtung richtig bei 60 %); bei den 8
handelbar eingestuften 0,27 % (5 von 8 richtig). Der Betrag der Bewegung nach 30 Minuten lag
je Wirkungs-Stufe bei 0,3 bis 0,55 %, weit unter dem, was die Stufen bedeuten sollen (1–3 %,
3–8 %). Die Meldungen erreichen den Bot erst, wenn die Bewegung vorbei ist.

## Zweite Auswertung der Tempo-Messung (19.11.2026)

Nach zwei Läufen (912 Meldungen) steht die Auswertung mit dem Vorschlag für schnellen Einstieg
und Handel vor und nach der Börse in `reports/2026-11-19-vorschlag-schneller-einstieg.md`.
Gegenüber der ersten Lesart vom 17.11. hat sich geändert:

- Die SEC-Meldung ist oft nicht der Auslöser. Bei 44 Paaren kam sie im Median acht Minuten
  nach der Pressemitteilung desselben Unternehmens. Der kostenlose SEC-Feed ersetzt eine
  schnelle Quelle für Pressemitteilungen nicht.
- Nach Pressemitteilungen sind 87 % der Bewegung nach zwei Minuten gelaufen (10 Fälle).
- Ein Teil der Ausschläge ist Rauschen (20 % der Aktien bewegten sich schon vor der Meldung
  um 1 %, danach 25 %), und dem Kurs der ersten Minute nachzulaufen lag nur in 40 % der Fälle
  richtig. Die Richtung muss aus dem Inhalt kommen; das ist auf frischen Meldungen noch nicht
  geprüft.
- In der ersten Minute werden im Median 50.000 $ gehandelt: Positionen von 5.000 bis 25.000 $.

Seit dem 19.11. hält die Messung zu jeder Meldung in einer gehandelten Aktie zusätzlich den
Verlauf im Detail fest (`news_path`: alle 5 Sekunden über 5 Minuten, jede Minute bis 30 Minuten,
Geld- und Briefkurs 5 und 60 Sekunden nach der Meldung).

## Modelltest 18.–20.11.2026: Opus entscheidet, Haiku und Sonnet laufen mit

Entscheidung Josh vom 17.11.2026: Für den Rest der Woche bewertet Claude Opus 5.5 die Meldungen,
und nur seine Bewertung führt zu Trades. Haiku 5.5 (bis dahin das entscheidende Modell) und
Sonnet 5.5 bewerten jede Meldung in der Handelszeit ebenfalls, ohne Einfluss auf den Handel
(„Schatten“, Protokolleintrag `news_shadow`). Am Ende der Woche wird verglichen.

Anlass: Am 17.11. waren zwei der Kandidaten von Haiku Fehlalarme, die an schlechten Eingangsdaten
lagen. „EQT to acquire Flatiron Energy“ wurde dem US-Gasförderer EQT zugeordnet, gemeint war der
schwedische Investor EQT (Alpacas Strom meldete später „EQT Transition Infrastructure to Acquire
Flatiron Energy“, ebenfalls unter dem Kürzel EQT). „InMode Reports Q4 Earnings Miss“ passte nicht
zur Lage: InMode hatte am 11.11. Zahlen zum dritten Quartal über der Schätzung gemeldet. Beide
Kandidaten stoppte nur die Bestätigung durch den Kurs.

Einstellungen (`config.toml`, Abschnitt `[llm]`):

- Opus denkt vor jeder Antwort nach, das lässt sich bei diesem Modell nicht abschalten
  (Stufe „medium“, die Voreinstellung des Modells). Haiku und Sonnet antworten ohne Nachdenken.
- Antwortet Opus nicht (Fehler, Zeitüberschreitung nach 20 Sekunden), zählt die Bewertung von
  Haiku (`news_fallback`), damit der Bot nicht blind wird.
- Obergrenze je Modell: 300 Bewertungen pro Stunde, 600 pro Tag.

Probelauf auf dem Server am 17.11. um 21:36 Uhr deutscher Zeit (`tools/llm_check.py`, drei
Beispielmeldungen, die beiden Fehlalarme aus dem Protokoll nachgestellt, ohne den Originaltext):

| Modell | Antwortzeit | Token (Eingabe + Ausgabe) | „EQT kauft Flatiron“ | „InMode verfehlt Gewinn“ |
|---|---|---|---|---|
| Opus 5.5 | 1,8–4,1 s | rund 970 + 90 bis 320 | unklar, 0/0: „vermutlich der schwedische Investor EQT AB, nicht EQT Corp“ | runter, Überraschung 6, Sicherheit 5: „seltsamer Zeitpunkt, möglicher Datenfehler“ – kein Kandidat |
| Sonnet 5.5 | 1,1–1,8 s | rund 960 + 100 | unklar, 6/1: „Gasförderer kauft Batteriespeicher, ungewöhnlich“ | runter, 8/2, Sicherheit 8 – Kandidat |
| Haiku 5.5 | 0,9 s | rund 960 + 95 | unklar, 6/1: „Richtung unklar“ | runter, 8/2, Sicherheit 8 – Kandidat |

Zwei Beispiele sind kein Beleg, sie zeigen nur die Art des Unterschieds. Das Konto hat für alle
drei Modelle 13.000 Anfragen pro Minute frei, Engpässe gibt es dort nicht.

Kosten nach diesen Token-Zahlen: Opus rund 0,6 bis 1 Cent je Bewertung, Sonnet 0,3 Cent, Haiku
0,015 Cent. Bei rund 270 Bewertungen pro Handelstag (Stand 17.11.) sind das zusammen etwa
2,50 bis 3,50 $ pro Tag. Die echten Werte stehen ab dem 18.11. im Protokoll (`tokens_in`,
`tokens_out`) und im Abschnitt „Modellvergleich“ der Wochenstatistik.

Was der Vergleich am Ende der Woche zeigen kann und was nicht:

- Er zeigt Antwortzeit, Kosten, Zahl der Kandidaten je Modell, wo die Modelle uneins waren und
  was der Kurs nach den Kandidaten jedes Modells tat.
- Drei Handelstage liefern wenige Kandidaten (am 17.11. waren es 9). Das reicht für eine
  Tendenz und für das Nachlesen der uneinigen Fälle, nicht für einen Beweis.
- Trades entstehen nur aus Opus' Bewertungen. Ob Haiku oder Sonnet besser gehandelt hätten,
  lässt sich nur am Kursverlauf nach ihren Kandidaten ablesen, nicht an echten Trades.
- Solange jede Meldung erst vom Kurs bestätigt werden muss, fallen Fehlalarme kaum ins Gewicht.
  Wichtig wird die Qualität der Bewertung, wenn der Bot sofort einsteigen soll (Vorschlag V15).

## Umbau ab 19.11.2026: sofort einsteigen, auch vor und nach der Börse

**Auftrag (Josh, 19.11.2026):** nicht eine weitere Woche nur nachrechnen, sondern im Paper-Konto
handeln. Eine KI soll jede Meldung so schnell und so gründlich wie möglich bewerten, mit möglichst
vielen Informationen zur Aktie, und der Bot soll die Bewegung der ersten Sekunden mitnehmen. Dem
Kurs der ersten Minuten hinterherzulaufen hält Josh für falsch; die Messung gibt ihm recht
(Momentum nach 60 Sekunden: nur 40 % richtig).

**Freigegeben am 19.11.:** höchstens 10.000 $ je Position, eine Position zur Zeit, Haltezeit
höchstens 15 Minuten. Alle weiteren Werte sieht Josh vor dem Start noch einmal.

**Der Ablauf, wie er werden soll**

1. Vorbereiten: Am Vorabend und am Morgen schreibt das stärkste Modell für jede Firma, die
   berichtet, ein Dossier: Erwartungen der Analysten (Finnhub), Prognose der Firma und letzte
   Quartale (SEC), Kursverlauf davor, worauf es ankommt.
2. Erkennen: RTPR liefert Pressemitteilungen in unter einer Sekunde (kostenpflichtig nach sieben
   Probetagen). Bis dahin die kostenlosen Quellen: SEC-Feed im Sekundentakt, PR Newswire.
3. Bewerten: Ein schnelles Modell vergleicht die Meldung mit dem Dossier (ein bis zwei Sekunden).
4. Handeln: sofort eine Limit-Order, rund drei Sekunden nach der Veröffentlichung.
5. Nachprüfen: Parallel liest das starke Modell die ganze Meldung und kann den Ausstieg auslösen.
6. Führen: Ziel, Stop, Zeitlimit von Minuten; vor Sitzungsende ist alles glatt.

Dazu: Alpacas Nachrichtenstrom wird ab 23.11. nicht mehr gehandelt (Stunden zu spät, kein
Vorhersagewert). Und jeden Abend prüft der Bot zu jedem Trade, ob es zum Ausführungskurs wirklich
genug Umsatz gab, weil das Paper-Konto in dünnem Handel zu günstig ausführt.

**Was vor und nach der Börse anders ist (gebaut am 19.11., `[extended]`, abgeschaltet)**

- Der Broker nimmt dort nur einfache Limit-Orders an (`extended_hours`), keine Stop-Orders und
  keine Order mit angehängtem Stop und Ziel. Der Einstieg ist deshalb eine einfache Limit-Order.
  Das Kursziel liegt nach der Ausführung als Limit-Order beim Broker. Den Stop, das Zeitlimit und
  das Sitzungsende überwacht der Bot alle zwei Sekunden selbst.
- Ausstieg: zuerst die Ziel-Order streichen, dann eine Limit-Order 0,5 % durch den Markt (beim
  Verkauf unter dem Geldkurs). Wird sie in vier Sekunden nicht ausgeführt, geht die nächste
  0,5 Punkte weiter, bis höchstens 5 %. Eine Market-Order gibt es dort nie: Der Broker würde sie
  bis zur nächsten Eröffnung liegen lassen.
- Welcher Kurs zählt: ein Handelskurs der letzten zehn Sekunden, sonst die Mitte aus Geld- und
  Briefkurs, wenn beide höchstens 0,6 % auseinanderliegen. Hat der Bot eine Minute lang keins von
  beiden, schließt er die Position (Ausstiegsgrund `blind`). Jeder Trade hält fest, wie oft der
  Bot blind war. Das ist die Messung für die Frage, ob die Echtzeit-Kurse für 99 $ im Monat nötig sind.
- Glatt bis 9:20 Uhr New Yorker Zeit (15:20 Uhr deutscher Zeit) und bis 19:45 Uhr (1:45 Uhr);
  zehn Minuten davor keine neuen Einstiege. An halben Tagen endet die Abendsitzung um 17:00 Uhr
  New Yorker Zeit, der Bot rechnet das aus dem Kalender des Brokers.
- In der regulären Handelszeit bekommt auch ein Sofort-Einstieg Stop und Ziel als Orders beim
  Broker, wie bisher.
- Wächter: läuft jede Minute. Vor und nach der Börse schließt er alles, wenn der Bot drei Minuten
  kein Lebenszeichen gegeben hat, und drei Minuten nach der Glattstell-Zeit des Bots.
- Der Server startet den Bot nie neu, solange eine Position offen ist.
- Tagesabschluss: Mit Handel am Abend zählt der Tag ab dem Depotwert zu Beginn des New Yorker
  Tages. Der Wert des Brokers vom Börsenschluss des Vortags würde die Trades des Vorabends
  doppelt zählen.

**Grenze der kostenlosen Kurse.** Der Bot sieht live nur die Börse IEX. Sie handelt von 8:00 bis
17:00 Uhr New Yorker Zeit, also 14:00 bis 23:00 Uhr deutscher Zeit, und über sie laufen rund 2,5 %
des Umsatzes. Von 10:00 bis 14:00 Uhr und von 23:00 bis 2:00 Uhr deutscher Zeit gibt es ohne das
bezahlte Datenpaket gar keine Kurse in Echtzeit: Der Bot würde dort weder einsteigen (kein Kurs)
noch einen Stop halten können. Mit kostenlosen Kursen ist der Handel also auf 14:00 bis 15:20 Uhr
und 22:00 bis 23:00 Uhr deutscher Zeit beschränkt, und auch dort nur, wenn an IEX gerade Kurse stehen.

**Gemessen am 19.11.2026, 8:34 Uhr New Yorker Zeit (vorbörslich, `tools/ext_check.py`):** Von 16
Aktien zeigte die kostenlose Kursquelle bei einer einzigen (Nvidia) einen Kurs, auf den sich ein
Stop stützen könnte. Bei Tesla, Apple, AMD, Palantir und Dell war der letzte Handel 7 bis 30 Minuten
alt und die Spanne zwischen Geld- und Briefkurs rund 10 % weit (Kurse vom Vorabend). Walmart hatte
an diesem Morgen Quartalszahlen gemeldet: letzter Handel 17 Stunden alt. Zehn weitere ebenso. Die
Echtzeit-Kurse des gesamten Marktes verweigert der Broker ohne das Datenpaket (Antwort 403). Damit
ist der Handel vor der Börse ohne das Paket für 99 $ im Monat nicht möglich. Für die Zeit nach der
Börse steht die Messung noch aus; sie läuft ab dem Abend des 19.11. alle fünf Minuten mit
(`feed_view` im Protokoll: zwanzig sehr liquide Aktien und die Aktien mit einer Meldung in der
letzten Stunde).

**Andere Kursquellen (geprüft am 19.11., 8:52 Uhr New Yorker Zeit, `tools/price_check.py`):**
Finnhubs normale Kursabfrage liefert vor Börsenbeginn nur den Schlusskurs des Vortags. Finnhubs
Live-Strom kommt ohne Verzögerung, zeigt aber nur einen Ausschnitt des Handels: in 25 Sekunden
Abschlüsse bei 3 von 16 Aktien (Nvidia, AMD, Walmart). Andere Anbieter mit Echtzeit-Kursen des
gesamten Marktes kosten mehr als Alpacas 99 $ (Databento und Massive je 199 $ im Monat);
günstigere liefern nur einzelne Börsen.

**Das Paper-Konto kennt den echten Kurs (Versuch am 19.11., 9:00 Uhr New Yorker Zeit,
`tools/price_probe.py`, von Josh gestartet):** 1 Nvidia-Aktie gekauft, eine Minute gehalten,
verkauft.
- Der Kauf mit Limit 157,35 wurde zu 152,78 ausgeführt, der Verkauf zu 152,77: Das Paper-Konto
  führt zum echten Brief- und Geldkurs aus, nicht zum Limit.
- Den Kurs der Position meldete der Broker alle zwei Sekunden neu: 23 Änderungen in einer
  Minute, zwischen 152,69 und 152,85. Die kostenlose Kursquelle sah in derselben Minute keinen
  einzigen Handel (152,77, am Ende 66 Sekunden alt). Finnhubs Strom zeigte 4 Abschlüsse, zuletzt
  152,82, passend zum letzten Kurs der Position.
- Folgerung: Vor und nach der Börse braucht der Bot das bezahlte Datenpaket nicht, um eine
  offene Position zu überwachen. Er nimmt den Kurs, mit dem der Broker die Position bewertet
  (`current_price` der Position; die Abfrage macht er ohnehin alle zwei Sekunden). Für den
  Einstieg genügt ein Limit, das von einem Kurs vor der Meldung ausgeht: Die Order wird zum
  echten Kurs ausgeführt oder gar nicht, wenn der Kurs schon weiter gelaufen ist, als die
  Regel „nicht hinterherlaufen“ erlaubt. Als Kurs vor der Meldung taugen die Kurse des gesamten
  Marktes, die der Broker kostenlos herausgibt, sobald sie 15 Minuten alt sind.
- Was ohne Datenpaket fehlt: Vor dem Einstieg sieht der Bot weder die Spanne zwischen Geld- und
  Briefkurs noch, ob der Kurs schon gegen die Bewertung gelaufen ist. Beides zeigt sich erst am
  Ausführungskurs (dann sofort wieder aussteigen) und in der Nachrechnung am Abend.
- Noch nicht geprüft: ob der Kurs der Position auch vor 8:00 und nach 17:00 Uhr New Yorker Zeit
  mitläuft (der Versuch lag in der Zeit, in der auch die kleine Börse IEX handelt, hing aber
  erkennbar nicht an ihr). Der Bot hält bei jedem Trade fest, wie oft sich der Kurs geändert hat.

**Kalender des Brokers (geprüft am 19.11.):** Er nennt Beginn und Ende der erweiterten Handelszeit
(`session_open` 0400, `session_close` 2000) und am halben Tag 27.11. das Ende 1700. Der 26.11.
fehlt als Feiertag.

**Finnhub, kostenloses Konto (geprüft am 19.11.):** Der Ergebniskalender für 19. bis 26.11. enthält
127 Berichte handelbarer Firmen, davon 100 mit erwartetem Gewinn je Aktie, 97 mit erwartetem
Umsatz, 83 mit Tageszeit (42 vor Börsenbeginn, 41 nach Börsenschluss). Ebenfalls frei: die letzten
vier Quartale mit Erwartung und Ergebnis, Kennzahlen, Firmenprofil, Empfehlungen. Gesperrt:
Schätzungen kommender Quartale, Kursziele, Hoch- und Herabstufungen. 60 Anfragen pro Minute. Die
Werte stehen in der Währung, in der die Firma berichtet (Alibaba und NetEase in Yuan): Das Dossier
muss die Währung nennen und die Erwartung gegen die letzten Quartale auf Plausibilität prüfen.

**SEC-Liste am 19.11. gestört:** Seit 6:00 Uhr New Yorker Zeit antwortet der Dienst der SEC für
die Liste neuer Meldungen nicht mehr (Zeitüberschreitung nach 40 Sekunden, von Hand nachgeprüft).
Andere Adressen der SEC antworten in Zehntelsekunden, der Server ist also nicht gesperrt. Bis
14:40 Uhr deutscher Zeit ist für den 19.11. keine einzige SEC-Meldung protokolliert. Der Bot holt
bis zu anderthalb Tage nach, sobald der Dienst wieder antwortet. Folgerung für den Umbau: Diese
Liste taugt als zweite Quelle und für die Messung, aber nicht als einzige Quelle für den Handel.

**Offen (Stand 19.11.):** Sofort-Einstieg aus SEC-Meldungen und Pressemitteilungen, Dossiers,
zweite Meinung, Empfang von RTPR, abendliche Prüfung der Ausführungen, Abschnitt in der
Wochenstatistik. Entscheidungen von Josh: RTPR ab 23.11. oder 30.11.; Echtzeit-Kurse für 99 $.

## Erste Fassung im Handel (seit 19.11.2026, 22:07 Uhr deutscher Zeit)

Josh wollte nicht bis Montag warten. Freigabe der Werte: 19.11.2026, 15:17 Uhr. Die erste Fassung ist
bewusst schmal: nur die Quelle, die schon da ist, und nur ein Modell.

| Baustein | Wert | Wo |
|---|---|---|
| Zeiten | vor Börsenbeginn 4:00 bis 9:10 Uhr New Yorker Zeit (10:00 bis 15:10 Uhr deutscher Zeit), nach Börsenschluss 16:00 bis 19:35 Uhr (22:00 bis 1:35 Uhr) | `[fast] sessions`, `[extended]` |
| Quelle | Pressemitteilungen von PR Newswire (kommen 1,5 bis 3,5 Minuten verspätet) | `bot/wires.py` |
| Bewertung | das Modell aus `[llm]` (zurzeit Opus), mit vollem Text der Meldung und den Erwartungen von Finnhub, wenn die Firma gestern, heute oder morgen berichtet | `bot/fast.py`, `bot/finnhub.py` |
| Einstieg | Richtung klar, Überraschung mindestens 7, Sicherheit mindestens 7, Wirkung mindestens 1; Meldung höchstens 300 Sekunden alt, auch noch wenn die Bewertung vorliegt | `[fast]` |
| Welche Aktien | genau eine Aktie je Meldung, kein Fonds, Kurs ab 5 $, Tagesumsatz ab 3 Mio. $ | `[fast] min_dollar_volume`, `[risk] min_price` |
| Order | Limit = Kurs vor der Meldung plus die halbe erwartete Bewegung (Wirkung 1: 1 %, 2: 2,5 %, 3: 5 %); nach 15 Sekunden gestrichen | `expected_move_pct`, `chase_frac`, `entry_timeout_s` |
| Größe | höchstens 10.000 $, kleine Firmen 5.000 $; Risiko 0,25 % des Depots; eine Position zur Zeit | `max_notional`, `max_notional_small`, `risk_pct`, `max_open` |
| Stop und Ziel | ab dem Ausführungskurs. Stop = Stop der alten Regel für diese Wirkungs-Stufe und Aktie mal 1,5 (höchstens 4 %); Ziel = Stop der alten Regel mal deren Ziel-Faktor | `stop_mult`, `[news.impact.*]` |
| Sofort raus | wenn der Ausführungskurs zeigt, dass der Kurs seit vor der Meldung schon um die halbe erwartete Bewegung GEGEN die Bewertung gelaufen ist | `chase_frac` |
| Zeitlimit | 15 Minuten vor Börsenbeginn, 10 Minuten nach Börsenschluss | `[fast.max_hold_min]` |
| Glatt | 9:20 Uhr und 19:45 Uhr New Yorker Zeit (15:20 und 1:45 Uhr deutscher Zeit) | `[extended]` |

Zwei Abweichungen vom Vorschlag vom Morgen des 19.11., beide wegen der Lage und beide von Josh
gesehen: Das Höchstalter ist 300 Sekunden statt 20 (die einzige Quelle kommt Minuten zu spät), und
der Bot prüft vor dem Einstieg weder die Spanne noch eine Gegenbewegung, weil er dort keinen Kurs
sieht. Das Limit ersetzt die Regel „nicht hinterherlaufen“; die Gegenbewegung zeigt sich am
Ausführungskurs.

Erwartung: wenige Trades. Nach zwei Minuten sind bei Pressemitteilungen 87 % der Bewegung gelaufen;
das Limit lässt dann meist keinen Einstieg mehr zu. Diese Fassung prüft vor allem die Technik am
echten Konto. Jede Bewertung wird protokolliert (`fast_rated`), auch ohne Trade: Zusammen mit dem
Kursverlauf derselben Meldung aus der Abendmessung zeigt sich, ob die KI die Richtung trifft.

Trockenlauf an echten Meldungen am 19.11. um 15:21 Uhr (`tools/fast_check.py`, keine Order): voller
Text abrufbar (1.154 bis 8.078 Zeichen), Kurs vor der Meldung gefunden (bei wenig gehandelten Aktien
vorbörslich ist das der Schlusskurs vom Vortag), Bewertung mit Opus in 1,7 bis 2,8 Sekunden, von der
Ankunft bis zur Entscheidung 1,8 bis 2,9 Sekunden.

## Dritte Auswertung (20.11.2026): die Spanne, und was sie vom Handel vor und nach der Börse übrig lässt

**Messung.** Für 973 Meldungen vom 13. bis 19.11. hat der Bot in der Nacht zum 20.11. den Geld- und
Briefkurs des gesamten Marktes 5 und 60 Sekunden nach der Meldung geholt (`news_path`). Der Broker gibt
diese Kurse kostenlos heraus, sobald sie 15 Minuten alt sind; das war bis dahin ungeprüft. „Handelbarer
Ausschlag“ heißt wie bisher: mindestens 1 % Bewegung und mindestens 100.000 $ Umsatz in 5 Minuten.

| Sitzung | Meldungen | Spanne nach 5 s (Median) | nach 60 s | Spanne höchstens 0,5 % |
|---|---|---|---|---|
| vor Börsenbeginn, alle | 251 | 3,6 % | 3,5 % | 10 % der Fälle |
| vor Börsenbeginn, handelbare Ausschläge | 29 | 2,8 % | 2,5 % | 13 % |
| Handelszeit, alle | 188 | 0,33 % | 0,37 % | 55 % |
| Handelszeit, handelbare Ausschläge | 8 | 0,26 % | 0,26 % | 62 % |
| nach Börsenschluss, alle | 534 | 4,5 % | 4,4 % | 9 % |
| nach Börsenschluss, handelbare Ausschläge | 35 | 4,1 % | 2,0 % | 8 % nach 5 s, 14 % nach 60 s |

Auch große Firmen haben vor und nach der Börse direkt nach einer Meldung 2 bis 3 % Spanne. Beispiele
vom Abend des 19.11.: Natural Grocers 4,9 % (kurz davor 14 %), Globant 11 bis 16 %, Post Holdings 5 %,
Keysight 1,8 bis 2,1 %, Elastic 1,6 %; eng nur bei Gap (0,5 bis 1,2 %) und Intuit (0,2 bis 1 %).

**Was nach der Spanne bleibt.** Gerechnet für die 64 handelbaren Ausschläge vor und nach der Börse:
Einstieg zum Briefkurs (Leerverkauf zum Geldkurs), Ausstieg 5 Minuten nach der Meldung zum letzten Kurs
abzüglich der halben Spanne. Die Richtung ist dabei als bekannt angenommen, es ist also eine Obergrenze.

- Bewegung in 5 Minuten: Median 2,2 %.
- Einstieg nach 5 Sekunden: es bleiben im Median −0,36 % (30 von 64 im Plus, 19 über +1 %).
  Einstieg nach 60 Sekunden: −0,60 % (26 von 64 im Plus).
- In der Handelszeit (8 Fälle): +1,9 % nach 5 Sekunden, +1,45 % nach 60 Sekunden, 8 und 7 von 8 im Plus.

**Enges Limit nachgerechnet** (vor und nach der Börse, Richtung bekannt): Wer höchstens 0,5 % über dem
Kurs vor der Meldung kauft, wird in 5 Tagen 14-mal ausgeführt (Einstieg nach 5 Sekunden) und ist jedes
Mal im Plus, Median +1,4 %. Mit 1 % sind es 20 Ausführungen, Median +1,3 %. Mit dem Limit der ersten
Fassung (2,5 % bei Wirkung 2) sind es 36 Ausführungen, Median +0,7 %, 12 davon im Minus, die schlechteste
−5,3 %; mit 5 % sind es 54, Median +0,2 %. Das enge Limit wählt also die Fälle aus, in denen die Spanne
eng und der Kurs noch nicht gelaufen ist.

**Der Haken: Auswahl gegen uns.** Bei FALSCHER Richtung wird dieselbe Order mit 0,5 % Limit 42-mal
ausgeführt statt 11-mal (Einstieg nach 60 Sekunden) und verliert im Median 2,6 %. Eine Limit-Order wird
eben dann ausgeführt, wenn der Kurs auf sie zuläuft, also gegen die Bewertung. Bei richtiger Bewertung
kommt nur jeder sechste Einstieg zustande, bei falscher zwei von drei. Ohne Blick auf Geld- und Briefkurs
VOR dem Einstieg rechnet sich das erst ab einer Trefferquote von rund 85 % (mit den Durchschnitten
gerechnet: +3,3 % je Treffer, −5,7 % je Fehlgriff; 5 Minuten gehalten, ohne Stop, und nur Aktien, die
sich danach wirklich um mindestens 1 % bewegt haben).

**Die Regeln der ersten Fassung am gemessenen Verlauf durchgespielt.** Für dieselben 64 Fälle: Einstieg
5 oder 60 Sekunden nach der Meldung mit dem Limit der ersten Fassung, Ausführung zum Briefkurs
(Leerverkauf zum Geldkurs), sofort raus bei Gegenbewegung, Stop und Ziel ab Ausführung, Zeitlimit 10
oder 15 Minuten, Regel „blind“. Der Kurs der Position ist dabei der letzte Handelskurs alle 5 Sekunden
(nach 5 Minuten: Minutenkerzen). Zwei Annahmen für den Ausstieg: vorsichtig (immer zum Geldkurs, also
letzter Kurs minus halbe Spanne) und günstig (Stop zum auslösenden Kurs, Ziel sobald der letzte Kurs es
erreicht). Wirkung 2 heißt: Limit 2,5 %, Stop 1,5 %, Ziel 3 %.

| Wirkung 2, 64 Fälle | Ausführungen | Schnitt je Trade, vorsichtig | günstig | am Stop | im Plus |
|---|---|---|---|---|---|
| Richtung richtig, Einstieg nach 5 s | 36 | −0,9 % | −0,4 % | 13 | 15 |
| Richtung richtig, Einstieg nach 60 s | 37 | −1,3 % | −0,6 % | 18 | 14 |
| Richtung falsch, Einstieg nach 5 s | 53 | −3,7 % | −2,4 % | 42 | 3 |
| Richtung falsch, Einstieg nach 60 s | 55 | −3,7 % | −2,5 % | 38 | 3 |

Wirkung 1 (Limit 1 %, Stop 0,75 %, Ziel 1 %): bei richtiger Richtung −0,5 bis +0,1 % je Trade, bei
falscher −2,6 bis −4,1 %. Wirkung 3 (Limit 5 %, Stop 3 %, Ziel 6 %): −0,4 bis −2,4 % und −3,2 bis −4,7 %.
Die erste Fassung verliert vor und nach der Börse also im Schnitt auch dann, wenn die KI jedes Mal
richtig liegt: Der Kauf zum Briefkurs liegt um die halbe Spanne über dem letzten Kurs, die Kurse springen
zwischen Geld- und Briefseite, und jeder zweite bis dritte Trade endet am Stop, obwohl die Richtung
stimmte. Etwa jeder fünfte liegt schon im Moment der Ausführung am Stop (7 bis 8 von rund 36).

**Dieselbe Rechnung mit Blick auf die Spanne** (Einstieg nur, wenn die Spanne im Moment des Einstiegs
höchstens 0,5 % beträgt und der Kurs höchstens 0,5 % gegen die Bewertung gelaufen ist; Wirkung 2):

| | Ausführungen in 5 Tagen | Schnitt, Richtung richtig | Schnitt, Richtung falsch | plus/minus null ab Trefferquote |
|---|---|---|---|---|
| vor und nach der Börse, nach 5 s | 7 | +1,0 % | −1,9 bis −2,3 % | 61 bis 65 % |
| vor und nach der Börse, nach 60 s | 9 | +0,6 % | −1,6 bis −1,7 % | 65 bis 67 % |
| Handelszeit, nach 5 s | 5 | +2,0 % | −1,6 bis −1,7 % | 44 bis 45 % |
| Handelszeit, nach 60 s | 4 | +1,9 % | −1,6 bis −1,7 % | 52 bis 53 % |

Das sind sehr wenige Fälle aus fünf Tagen, also nur ein Anhalt. Die Richtung ist auch hier nachträglich
bekannt, und es zählen nur Aktien, die sich danach bewegt haben. Lässt man die Spanne bis 1 % zu, sind
es vor und nach der Börse 14 bis 16 Ausführungen und die nötige Trefferquote liegt bei 55 bis 78 %.
Nicht geprüft ist die Annahme, dass der Broker die offene Position zum letzten Handelskurs bewertet;
das zeigt der erste ausgeführte Trade (`price_changes`, Ausstiegsgrund).

**Folgerungen**
1. Die erste Fassung kann vor und nach der Börse kein Geld verdienen, unabhängig von der Güte der
   Bewertung. Limit, Stop und Ziel sind kleiner als die Spanne, über die sie hinwegmüssen.
2. Vor und nach der Börse braucht der Bot vor dem Einstieg Geld- und Briefkurs: handeln nur bei enger
   Spanne (höchstens 0,5 %) und nur, wenn der Kurs weder davongelaufen noch gegen die Bewertung gelaufen
   ist. Das geht mit Alpacas Datenpaket (99 $ im Monat) oder kostenlos mit einer Probe über 1 Aktie
   (Kauf und sofortiger Verkauf: das Paper-Konto führt zum echten Brief- und Geldkurs aus, siehe Versuch
   vom 19.11.). Dann muss die KI in etwa zwei von drei Fällen richtig liegen.
3. In der Handelszeit ist die Spanne kein Problem, und dort genügt etwa jeder zweite Treffer. Dort gilt
   bisher nur die alte Regel mit Kursbestätigung (kein Trade seit dem 17.11.); der Sofort-Einstieg
   läuft dort noch nicht.
4. Größenordnung mit schneller Quelle: vor und nach der Börse ein bis zwei Fälle am Tag mit enger Spanne,
   in der Handelszeit etwa einer. Bei 10.000 $ je Position bringt ein Treffer etwa 60 bis 200 $, ein
   Fehlgriff kostet etwa 160 bis 230 $. Mit der jetzigen Quelle (zwei Minuten zu spät) bleibt davon kaum
   etwas, weil die Bewegung dann zu 87 % gelaufen ist.
5. Die Trefferquote der KI ist die Zahl, die jetzt fehlt. Sie lässt sich ohne Handel und ohne schnelle
   Quelle messen: Jede Bewertung des Sofort-Einstiegs wird protokolliert (`fast_rated`) und in der Nacht
   gegen den Kursverlauf gelegt; die Wochenstatistik zählt das seit dem 20.11. aus (Abschnitt
   „Sofort-Einstieg“). Erst wenn sie über den Schwellen aus der Tabelle liegt, lohnen die Ausgaben für
   RTPR und Kurse. Stand nach dem ersten Abend: sechs Bewertungen mit Richtung, nach 5 Minuten zwei
   richtig, drei falsch, eine ohne Bewegung; das sagt noch nichts.

**Kommt die Meldung bei der SEC früher an als die Pressemitteilung?** Nein. Zu 17 Pflichtmitteilungen
über Ergebnisse (8-K, Punkt 2.02) vom 17. bis 19.11. gab es eine Pressemitteilung derselben Firma
innerhalb einer Stunde; 16-mal war die Pressemitteilung zuerst da. Die SEC nahm das 8-K im Median
205 Sekunden nach dem Zeitstempel der Pressemitteilung an (mittlere Hälfte: 112 bis 646 Sekunden). Dazu
kommt, dass die Liste der SEC neue Meldungen beim Bot im Median erst 305 Sekunden nach der Annahme zeigt
(358 Meldungen, schnellste 27 Sekunden); PR Newswire kommt im Median nach 115 Sekunden (382 Meldungen).
Die SEC als zweite Quelle macht den Sofort-Einstieg bei Quartalszahlen also nicht schneller; sie bleibt
für Meldungen ohne Pressemitteilung. Die eine Ausnahme war Natural Grocers am 19.11.

**Erster Abend der ersten Fassung (19.11., 22:07 bis 1:45 Uhr deutscher Zeit).** 110 Pressemitteilungen
kamen an, 39 wurden bewertet (alle mit vollem Text, 9 mit Erwartungen), typisch 1,5 Sekunden je Bewertung,
zusammen 0,40 $. Ein Kandidat: Natural Grocers, Leerverkauf, weil Gewinn und Umsatz unter der Erwartung
lagen. Die Zahlen lagen seit 22:05:11 Uhr bei der SEC, die Pressemitteilung trägt 22:10:00 Uhr und kam
um 22:11:20 Uhr beim Bot an. Da stand der Geldkurs schon bei 25,20 $ gegen 27,15 $ vorher, also 7 %
tiefer. Das Limit von 26,53 $ griff nicht, die Order wurde nach 15 Sekunden gestrichen. Die Richtung
stimmte. Kein Trade, keine Fehler, Sitzungsende und Tagesabschluss sauber. Von sechs Bewertungen mit
Richtung lagen nach 5 Minuten zwei richtig; fünf der sechs blieben unter den Schwellen für einen Einstieg.
Die kostenlose Kursquelle zeigte den ganzen Abend bei keiner von 25 Aktien mit Meldung einen brauchbaren
Kurs. Die SEC-Liste lief ab 22:07 Uhr wieder.

## Zweite Fassung im Handel (Freigabe Josh 24.11.2026): erst auf den Markt sehen, Schwerpunkt ohne Quartalszahlen

Josh hat am 24.11.2026 den Vorschlag `reports/2026-11-24-vorschlag-zielsetup.md` freigegeben („Lass uns
das so alles umbauen … Wir nehmen alles, was kostenlos ist und hilft“). Das ist die Stufe 0 dieses
Vorschlags und schließt die zweite Fassung des Sofort-Einstiegs aus
`reports/2026-11-20-vorschlag-zweite-fassung.md` ein. Aktiv ab dem ersten Neustart nach Börsenschluss
am 24.11. (22:07 Uhr deutscher Zeit, falls dann keine Position offen ist).

**Warum.** Messung an den eigenen Protokollen, nur Meldungen mit mindestens 1 % Bewegung und 100.000 $
Umsatz in fünf Minuten, Richtung nachträglich bekannt:

| Klasse | Fälle | nach 10 s gelaufen | nach 60 s gelaufen | Spanne (Median) | Spanne höchstens 0,5 % |
|---|---|---|---|---|---|
| Quartalszahlen per Pressemitteilung (16. bis 23.11.) | 19 | 43 % | 91 % | 4,9 % | 0 von 19 |
| andere Pressemitteilungen (16. bis 23.11.) | 16 | 18 % | 59 % | 2,2 % | 3 von 16 |
| SEC-Pflichtmitteilungen ohne Quartalszahlen (13. bis 23.11.) | 30 | 0 % | 4 % | 1,2 % | 9 von 30 |

Die ersten Sekunden nach Quartalszahlen sind mit keiner Quelle zu holen. Bei Meldungen ohne Quartalszahlen
braucht der Kurs 30 bis 120 Sekunden, und der Text entscheidet über die Richtung. Dort setzt der Bot an.

**Was sich am Handel ändert**

| Baustein | Wert | Wo |
|---|---|---|
| Zeiten | zusätzlich die Handelszeit: ab 5 Minuten nach Börsenbeginn bis 30 Minuten vor Börsenschluss; vor und nach der Börse wie bisher | `[fast] sessions`, `regular_start_after_open_min`, `regular_no_entry_before_close_min` |
| Blick auf den Markt | vor jedem Einstieg Geld- und Briefkurs. Aus der Kursquelle, wenn sie beide zeigt, höchstens 10 Sekunden alt und höchstens 0,5 % auseinander; sonst per Probe | `[fast.look]` |
| Probe | 1 Aktie in Richtung der Bewertung mit dem Limit der ersten Fassung, sofort zurück mit einem Limit 0,5 % schlechter als der erste Kurs; je Schritt höchstens 3 Sekunden. Die zwei Ausführungskurse sind Brief- und Geldkurs. Kommt der Rückweg nicht zustande, ist die Spanne weiter als 0,5 %: kein Einstieg. Die eine Aktie geht dann mit einem Limit weit durch den Markt (20 %) zurück; sie wird zum besten Kurs der Gegenseite ausgeführt, und der zeigt, wie weit die Spanne wirklich war. Bleibt trotzdem etwas liegen, ist die Aktie für Einstiege gesperrt, bis es weg ist (`stray_share`) | `probe_wait_s`, `probe_back_pct`, `Executor.probe` |
| Einstieg nur, wenn | Spanne höchstens 0,5 %; Kurs seit vor der Meldung höchstens 0,5 % GEGEN die Bewertung; nicht weiter in ihre Richtung gelaufen als die halbe erwartete Bewegung | `max_spread_pct`, `max_against_pct`, `chase_frac` |
| Order | Limit 0,2 % über dem gesehenen Briefkurs (Leerverkauf: unter dem Geldkurs), höchstens das Limit der ersten Fassung | `limit_offset_pct` |
| Stop und Ziel | vor und nach der Börse wie bisher: ab dem Ausführungskurs, der Bot überwacht. In der Handelszeit gehen beide mit der Order zum Broker, gerechnet ab dem gesehenen Briefkurs (Leerverkauf: Geldkurs), zu dem die Order ausgeführt wird | `bot/executor.py`, `expected_fill` |
| Zeitlimit | Handelszeit 15 Minuten, vorbörslich 15, nachbörslich 10 | `[fast.max_hold_min]` |
| Bezugskurs | letzter Kurs des gesamten Marktes vor der Meldung (kostenlos, sobald 15 Minuten alt). In der Handelszeit die letzte volle Minute der kostenlosen Kursquelle vor der Meldung, wenn die jünger ist | `FastStrategy.reference_price` |
| Unverändert | Schwellen (Überraschung 7, Sicherheit 7, Wirkung 1), Höchstalter 300 Sekunden, höchstens 10.000 $ (kleine Firmen 5.000 $), eine Position zur Zeit, je Aktie ein Trade am Tag | `[fast]` |

Zur Kursquelle in der Handelszeit: Sie zeigt nur eine Börse (IEX). Eine enge Spanne dort ist auch im
gesamten Markt eng. Eine weite sagt nichts, deshalb entscheidet dann die Probe.

**Quellen**

- **Pressemitteilungen** von PR Newswire und Business Wire handeln wie bisher, jetzt in allen drei Sitzungen.
- **SEC-Pflichtmitteilungen (neu als Quelle).** Die Liste der neuesten 8-K wird werktags von 6:00 bis
  17:40 Uhr New Yorker Zeit alle 2 Sekunden abgefragt (Limit der SEC: 10 Abfragen je Sekunde), dazu alle
  10 Minuten vollständig. Eine Meldung erscheint dort rund 35 bis 60 Sekunden nach der Annahme; bisher sah
  der Bot sie im Median nach gut fünf Minuten. Frische Meldungen (höchstens 10 Minuten alt) gehen an den
  Sofort-Einstieg, mit dem Text des Berichts ab dem ersten Punkt und dem der Anlage 99. Nicht bewertet
  werden Meldungen mit Quartalszahlen (Punkt 2.02: Die Pressemitteilung war in 16 von 17 Fällen früher da)
  und reine Formalien (nur 5.03, 5.07 oder 9.01). Die KI bekommt als Vorwissen den Hinweis, dass ein 8-K
  bis zu vier Werktage nach dem Ereignis kommen darf und oft eine ältere Pressemitteilung wiederholt.
  **Gehandelt wird diese Quelle noch nicht** (`[edgar] trade = false`): Jede Bewertung wird protokolliert,
  ein Kandidat endet als `fast_rejected` mit dem Grund `source_off`. Josh hat die Frage „gleich handeln
  oder erst zwei Wochen messen“ noch nicht beantwortet. Nach 17:30 Uhr New Yorker Zeit Angenommenes
  veröffentlicht die SEC erst um 6:00 Uhr am nächsten Morgen; dafür ist der Bot blind.
- **WireAlpha (neu, nur gemessen).** Kostenlose Push-Verbindung für Pressemitteilungen von vier Verteilern
  und SEC-Meldungen, laut Anbieter in unter einer Sekunde. Unabhängig bestätigt ist das nicht, der
  Anbieter ist jung und kann den Dienst jederzeit beenden. Der Bot schreibt jede Meldung mit der Quelle
  `wa-<verteiler>` mit; `news_dup` und die Wochenstatistik zeigen, ob sie früher kommt als die Feeds der
  Verteiler. Gehandelt wird sie erst, wenn die Messung das zeigt und Josh zustimmt. Läuft erst, wenn Josh
  den Schlüssel auf dem Server eingetragen hat.
- **Anlegeraufrufe von Kanzleien** („INVESTOR ALERT“, „class action“, „lead plaintiff“) werden erkannt
  und weder bewertet noch gemessen: 164 von 789 Überschriften in den Protokollen bis 24.11., keine davon
  eine Meldung der Firma. Eine Firma, die über ihren eigenen Prozess berichtet (Vergleich, Urteil), bleibt.
- **Abgeschaltet:** Alpacas Nachrichtenstrom (in den nachverfolgten Fällen 4 bis 30 Stunden zu spät, rund
  jede siebte Meldung zur falschen Firma), die alte Regel mit Kursbestätigung, die Finnhub-Nachrichten
  (13 bis 14 Minuten nach der Veröffentlichung).

**Mitbewertung.** Haiku 5.5 und Sonnet 5.5 bewerten jede Meldung des Sofort-Einstiegs mit demselben Text
und Vorwissen (`fast_shadow`). Sie entscheiden nichts. Die Wochenstatistik zeigt, wie oft sie mit Opus
übereinstimmen und wie oft ihre eigene Richtung stimmte.

**Kosten.** Kein Abo. Für die KI geschätzt 3 bis 6 $ pro Handelstag statt rund 2,50 $, weil die
SEC-Meldungen dazukommen (rund 110 bis 150 am Tag vor den Filtern nach Umsatz und Kurs). Jede Probe kostet
die Spanne auf 1 Aktie, also Cent-Beträge; sie steht nicht in `trades.jsonl`, aber im Depotwert.

**Erwartung.** Ein bis zwei handelbare Fälle am Tag mit enger Spanne, nicht viele. Ob die KI die Richtung
oft genug trifft, ist offen (bis 24.11.: 26 Bewertungen mit Richtung, 10 davon gemessen, 5 richtig,
1 falsch, 4 ohne Bewegung). Die Wochenstatistik trennt deshalb nach Pressemitteilung und SEC-Meldung und
nach Art der Meldung. Maßstab aus der dritten Auswertung: Vor und nach der Börse muss die Richtung in etwa
zwei von drei Fällen stimmen, in der Handelszeit in jedem zweiten.

**Offener Punkt für Josh (nicht umgesetzt, wäre eine neue Regel):** Der engste Stop liegt bei 0,45 % (kleine
Wirkung, ruhige Aktie), die erlaubte Spanne bei 0,5 %. In diesem Fall liegt der Stop innerhalb der Spanne und
kann sofort auslösen. Bei mittlerer Schwankung sind es 0,75 % (Wirkung 1) und 1,5 % (Wirkung 2). Vorschlag:
kein Einstieg, wenn die gesehene Spanne größer ist als der halbe Abstand zum Stop. Zweitens: Business Wire
kommt im Median 334 Sekunden nach der Veröffentlichung an, das Höchstalter ist 300 Sekunden; mehr als die
Hälfte seiner Meldungen wird deshalb gar nicht bewertet (`fast_skipped`, `stale`).

**Was die ersten Tage zeigen müssen** (am echten Konto noch nie gelaufen):

- die Probe: Dauer (geschätzt 1 bis 3 Sekunden), ob beide Seiten ausgeführt werden, auch beim Leerverkauf
  (`fast_probe`);
- der Sofort-Einstieg in der Handelszeit mit Stop und Ziel beim Broker;
- der Abruf des Textes der Pflichtmitteilungen (`fast_skipped` mit `no_text` zeigt, wo er fehlt) und wie
  schnell die Liste wirklich ist (Wochenstatistik, Quelle `sec`);
- weiter offen aus der ersten Fassung: Ziel-Order und ihr Streichen, Ausstieg bei Stop und Zeitlimit.

**Was folgt** (Rest der Stufe 0, noch nicht gebaut): abendliche Prüfung jeder Ausführung gegen den echten
Umsatz; Dossier mit den Erwartungen für das nächste Quartal und das Jahr (Alpha Vantage, sobald der
Schlüssel da ist und `tools/alphavantage_check.py` zeigt, dass das kostenlose Konto sie liefert), der
erwarteten Bewegung aus Optionspreisen und der früheren Prognose der Firma; Zahlenvergleich im Code;
zweiter Weg zu den SEC-Meldungen. Stufe 1 (Interactive Brokers als Datenquelle, rund 40 $ im Monat und
500 $ Guthaben) erst, wenn Josh dort ein Konto hat und Stufe 0 zwei Wochen gemessen ist.

## Bekannte Schwächen des Tests

- **Papertrading füllt Orders zu günstig aus.** Echte Kursrutscher bei Nachrichten bildet
  die Simulation nicht ab. Gute Paper-Ergebnisse sind deshalb eine Obergrenze. Das gilt auch für die
  Probe mit 1 Aktie: Sie zeigt Geld- und Briefkurs, aber nicht, ob 10.000 $ zu diesem Kurs zu haben
  wären. Die abendliche Prüfung jeder Ausführung gegen den echten Umsatz fehlt noch.
- **Kursdaten kommen nur von der Börse IEX** (kostenlose Stufe), ein kleiner Ausschnitt des
  Marktes. Volumen und Geld-/Brief-Kurse sind dadurch ungenau. Gemessen am 16.11.: 3 bis 5 %
  des Umsatzes; die Eröffnungsspanne ist bei 19 von 25 Aktien an mindestens einer Kante um
  mehr als den Ausbruchs-Puffer zu eng; eine kleine Aktie hatte auf IEX 4 Handelsminuten am
  ganzen Tag.
- **Alle weiteren Schwächen und die offenen Vorschläge dazu** stehen in
  `reports/2026-11-17-kritische-bewertung.md` (unter anderem: Kandidatenlisten der
  Ausbruchs-Strategie stammen vom Vortag, Bezugskurs der News-Strategie, keine Grenze nach
  Handelsumsatz, Abgleich läuft als root, keine Alarmierung).
- **Die meisten wichtigen Unternehmensmeldungen erscheinen außerhalb der Handelszeit**
  (Quartalszahlen vor Börsenstart oder nach Börsenschluss). Während der Handelszeit gibt es
  deshalb nur wenige News-Signale; die übrigen Meldungen fließen in die Kandidaten für die
  Eröffnung ein.
- Die Startwerte sind begründete Annahmen, nicht an historischen Daten optimiert.

## Änderungsverlauf

- 2026-11-17: Version 2 angelegt (News + Ausbruch), löst ARIA ab. Am selben Tag ergänzt:
  PR Newswire als zweite Quelle, Meldungen seit Börsenschluss als Kandidaten für die
  Eröffnung, Zeitlimit der Ausbruchs-Strategie 30 statt 90 Minuten.
- 2026-11-17 (von Josh freigegeben, aktiv ab 18.11.): Wirkungs-Stufe der KI mit
  Firmengröße und Schwankung als Kontext; Filter „Wirkung ≥ 1"; Stop, Ziel und Zeitlimit
  je Stufe statt fester Werte; Mitschreiben der Kursverläufe; Feierabend-Wächter.
- 2026-11-17 (ohne Einfluss auf den Handel): Kursverläufe nach Meldungen werden nach
  Börsenschluss mit Kursen des gesamten Marktes gemessen statt laufend mit IEX-Kursen;
  Rückrechnung kann mit Kursen des gesamten Marktes rechnen. Kritische Bewertung mit elf
  offenen Vorschlägen in `reports/2026-11-17-kritische-bewertung.md`.
- 2026-11-17 (Entscheidung Josh, aktiv ab 18.11.): Ausbruchs-Strategie abgeschaltet.
  Pressemitteilungen werden mit Veröffentlichungszeit protokolliert (`wire_item`).
- 2026-11-17, 15:45 Uhr deutscher Zeit: Josh hat Alpaca- und Anthropic-Schlüssel erneuert. Durch
  den Neustart läuft die Version mit Wirkungs-Stufe schon seit dem Nachmittag des 17.11.; die
  Ausbruchs-Strategie war damit für diesen Tag beendet.
- 2026-11-17 (ohne Einfluss auf den Handel, aktiv ab dem Abend): Tempo-Messung und Liste der
  SEC-Meldungen eingebaut.
- Erster Handelstag (17.11.), zwei Trades der Ausbruchs-Strategie, je rund 102.000 $ groß:
  JPM short, nach 66 Sekunden ausgestoppt (−364 $; der Stop wurde 0,05 % schlechter ausgeführt
  als gesetzt). PEP short, Sekunden nach dem Einstieg durch eine Kauforder aus dem
  Alpaca-Dashboard geschlossen (−35,75 $; laut Josh ein Versehen von ihm, kein fremder
  Zugriff). Der Bot bemerkte das nicht, führte PEP weiter als
  offen und buchte neun Minuten später einen Stop-Ausstieg zu einem geschätzten Kurs
  (`stop_sw_unconfirmed`, −507,65 $). Diesen Verlust gab es nie: Der Depotwert fiel durch PEP
  nur um 35,75 $. Echtes Ergebnis der beiden Trades: −399,81 $.
  Behoben (aktiv ab 18.11.): Von Hand geschlossene Positionen werden erkannt und mit dem
  echten Schlusskurs verbucht (Ausstiegsgrund `external`). Der falsche PEP-Eintrag ist in
  `reports/trade-corrections.jsonl` richtiggestellt; die Wochenstatistik rechnet damit.
- 2026-11-17 (ohne Einfluss auf den Handel, aktiv ab 18.11.): Der Tagesabschluss misst das
  Ergebnis gegen den Depotwert zum Börsenschluss des Vortags. Vorher zählte er ab dem letzten
  Start des Bots; nach dem Neustart am Nachmittag des 17.11. weist der Abschluss dieses Tages
  deshalb fast null aus statt des echten Verlusts. Die Wochenstatistik gleicht das aus.
- 2026-11-17 (Entscheidung Josh, aktiv ab 18.11.): Opus 5.5 bewertet und entscheidet, Haiku 5.5
  und Sonnet 5.5 bewerten zum Vergleich mit. Einzelheiten im Abschnitt „Modelltest“.
- 2026-11-19 (Auftrag Josh; gebaut, aber abgeschaltet): Handel vor und nach der Börse
  (`[extended]`): Limit-Orders, Stop und Zeitlimit im Bot, Wächter jede Minute, kein Neustart
  bei offener Position. Ablage für den Finnhub-Schlüssel und `tools/finnhub_check.py`.
  Einzelheiten im Abschnitt „Umbau ab 19.11.2026“.
- 2026-11-19 (ohne Einfluss auf den Handel, aktiv ab dem Abend): Der Bot hält alle fünf Minuten
  fest, bei wie vielen Aktien seine Kursquelle einen brauchbaren Kurs zeigt (`feed_view`).
  `tools/ext_check.py` zeigt dasselbe auf Abruf.
- 2026-11-19 (Freigabe Josh 15:17 Uhr, aktiv ab dem Neustart um 22:07 Uhr deutscher Zeit): Handel vor
  und nach der Börse eingeschaltet (`[extended]`, `[fast]`). Sofort-Einstieg aus Pressemitteilungen
  von PR Newswire, Limit ab dem Kurs vor der Meldung, Stop und Ziel ab dem Ausführungskurs, Kurs der
  Position vom Broker. Werte im Abschnitt „Erste Fassung im Handel“. Die reguläre Handelszeit und der
  Modelltest bleiben unverändert.
- 2026-11-20 (Auswertung, keine Änderung am Handel): Spanne vor und nach der Börse gemessen, siehe
  „Dritte Auswertung“. Die Regeln der ersten Fassung am gemessenen Verlauf durchgespielt: Sie verliert
  dort auch bei richtiger Richtung. Befund an Josh gemeldet mit dem Vorschlag, dort nur noch bei
  bekannter, enger Spanne einzusteigen. Bis zu seiner Entscheidung läuft die erste Fassung wie freigegeben.
- 2026-11-22 (Joshs Wunsch vom 21. und 22.11.): mehrere Nachrichtenquellen statt RTPR. Zu PR Newswire
  kommen GlobeNewswire (Kürzel steht im Feed, voller Text im Feed) und Business Wire (Kürzel nur aus der
  Eröffnungszeile, in der sich die Firma selbst nennt). Haupt-Feed jedes Verteilers alle 10 Sekunden
  (vorher jeder Feed einmal je Minute), Themen-Feeds jede Minute. Dieselbe Nachricht (gleiche Aktie binnen
  15 Minuten, ähnliche Überschrift oder beides Ergebnisse) wird nur beim ersten Eintreffen bewertet und
  gehandelt, auch gegenüber Alpacas Strom; Kopien als `news_dup`. Grund: Von 99 Ergebnisberichten vom
  17. bis 20.11. kamen nur 16 über PR Newswire. Alle drei Verteiler handeln (`[wires] trade`); Regeln
  des Handels sonst unverändert (erste Fassung).
- 2026-11-22 (Joshs Vorschlag): Finnhub-Nachrichten als vierte Quelle, nur gemessen. Der Echtzeit-Strom
  von Finnhub kostet; das kostenlose Konto beantwortet nur Abfragen (60 je Minute für alles). Der Bot
  fragt alle 20 Sekunden die Marktnachrichten ab und alle zwei Minuten die Nachrichten jeder Firma, die
  heute berichtet (höchstens 40). Finnhub sammelt bei anderen Seiten ein und dürfte später sein als die
  Verteiler; gehandelt wird es erst, wenn `news_dup` zeigt, dass es oft zuerst da ist.
- 2026-11-23: GlobeNewswire weist den Server ab (403 bei jedem Abruf seit 0:07 UTC, auch mit Accept-Kopf
  und zehn Minuten Pause). Aus der Feed-Liste genommen; nicht umgangen. Business Wire, PR Newswire und
  Finnhub laufen.
- 2026-11-24 (Freigabe Josh, aktiv ab dem Neustart am Abend): zweite Fassung des Sofort-Einstiegs und
  Stufe 0 des Vorschlags `reports/2026-11-24-vorschlag-zielsetup.md`. Blick auf Geld- und Briefkurs vor
  jedem Einstieg (Kursquelle oder Probe mit 1 Aktie), Einstieg nur bei einer Spanne bis 0,5 %;
  Sofort-Einstieg auch in der Handelszeit, die alte Regel mit Kursbestätigung und Alpacas Nachrichtenstrom
  sind abgeschaltet. SEC-Liste alle 2 Sekunden, frische Pflichtmitteilungen ohne Quartalszahlen werden
  gelesen und bewertet, aber noch nicht gehandelt (Joshs Entscheidung dazu steht aus). WireAlpha als
  gemessene Quelle und Ablage für die Schlüssel von WireAlpha und Alpha Vantage (`deploy/set-keys.sh`).
  Kanzlei-Aufrufe werden nicht mehr bewertet. Haiku und Sonnet bewerten beim Sofort-Einstieg mit.
  Finnhub-Nachrichten aus. RTPR ist verworfen: Seine Bedingungen verbieten die Nutzung in einem Handels-Bot.
  Einzelheiten im Abschnitt „Zweite Fassung im Handel“.
