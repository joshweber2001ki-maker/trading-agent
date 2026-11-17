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

Jede Meldung mit höchstens drei Aktien bewertet die KI (Claude Haiku 5.5):

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

## Strategie 2: Ausbruch aus der Eröffnungsspanne

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
