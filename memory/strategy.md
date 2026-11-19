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

## Bekannte Schwächen des Tests

- **Papertrading füllt Orders zu günstig aus.** Echte Kursrutscher bei Nachrichten bildet
  die Simulation nicht ab. Gute Paper-Ergebnisse sind deshalb eine Obergrenze.
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
