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
- **Über Nacht wird nichts gehalten:** 10 Minuten vor Börsenschluss wird alles glattgestellt.
- Long und Short. Keine Aktien unter 5 $.

## Nachrichtenquellen

1. **Alpacas Nachrichtenstrom.** Liefert seit 2026 nur noch KI-Zusammenfassungen des Anbieters
   fiscal.ai, gebündelt etwa zur Minute 01 und 31 jeder Stunde. Für schnelles Handeln zu spät.
2. **Pressemitteilungen von PR Newswire** (`bot/wires.py`, zehn RSS-Feeds). Eine Meldung steht
   dort nach etwa 2–4 Minuten. Der Feed nennt den Firmennamen, aber kein Börsenkürzel; der Name
   wird deshalb mit den Namen der handelbaren US-Aktien abgeglichen. Es zählen nur exakte
   Treffer, damit nie die falsche Aktie gehandelt wird. Abgedeckt ist nur dieser eine
   Verteiler, also ein Teil aller Unternehmensmeldungen.

Jede Meldung mit höchstens drei Aktien bewertet die KI: Richtung, Überraschung (0–10),
Sicherheit (0–10). Die KI bekommt zum Kürzel den Firmennamen und stuft Meldungen über eine
andere Firma mit zufällig gleichem Kürzel auf 0.

## Strategie 1: Nachrichten (während der Handelszeit)

Idee: nicht die erste Sekunde handeln (die gewinnen Profi-Systeme), sondern die
Fortsetzung in den Minuten danach.

1. Gehandelt wird nur bei Überraschung ≥ 7 und Sicherheit ≥ 6.
2. Dann wartet der Bot bis zu 5 Minuten auf Bestätigung: Kurs mindestens 0,4 % in die
   erwartete Richtung, Minutenvolumen mindestens das Doppelte des Üblichen, enge
   Geld-/Brief-Spanne. Ist der Kurs schon über 3 % gelaufen, wird nicht hinterhergekauft.
3. Stop-Abstand: 0,75 × Kursspanne der letzten 10 Minuten, mindestens 0,4 %, höchstens 1,5 %.
4. Zeitlimit 30 Minuten. Pro Aktie höchstens ein News-Trade am Tag; nach einem Signal
   60 Minuten keine weiteren Signale für dieselbe Aktie.

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
Liste von 20 Aktien, IEX-Daten, 0,02 % Slippage je Seite): 207 Trades, Trefferquote 37 %,
Ergebnis −4,9 %, Profitfaktor 0,92; ohne Slippage −1,5 %. Geprüfte Varianten (nur in
Richtung der Kurslücke, 15-Minuten-Spanne, Ziel 3R, strengeres Volumen, Zeitlimit 30 Minuten)
liegen alle zwischen −12 % und +1 %. Einzige klar positive Variante: Stop auf der anderen
Seite der Spanne, nur in Richtung der Spanne, halten bis Börsenschluss (+9,2 %, Profitfaktor
1,23, 154 Trades). Das widerspricht aber dem Wunsch nach sehr kurzer Haltedauer und ist eine
von 15 Varianten, also kein belastbarer Beleg. **Fazit: Auf der festen Liste zeigt die
kurze Variante keinen Vorteil.** Offen ist, ob die tagesaktuellen Kandidaten (Gewinner,
Verlierer, News) besser laufen; das kann nur der Paper-Test zeigen.

**Nachrichten-Strategie, Nachspiel des 16.11.2026** (`tools/replay_news.py`, nur Alpacas
Strom): 401 Meldungen in der Handelszeit, 232 bewertet, 6 als handelbar eingestuft, bei
keiner bestätigte der Kurs die Richtung, 0 Trades. Grund: die Meldungen kamen zu spät.
Deshalb die zweite Quelle.

## Bekannte Schwächen des Tests

- **Papertrading füllt Orders zu günstig aus.** Echte Kursrutscher bei Nachrichten bildet
  die Simulation nicht ab. Gute Paper-Ergebnisse sind deshalb eine Obergrenze.
- **Kursdaten kommen nur von der Börse IEX** (kostenlose Stufe), ein kleiner Ausschnitt des
  Marktes. Volumen und Geld-/Brief-Kurse sind dadurch ungenau.
- **Die meisten wichtigen Unternehmensmeldungen erscheinen außerhalb der Handelszeit**
  (Quartalszahlen vor Börsenstart oder nach Börsenschluss). Während der Handelszeit gibt es
  deshalb nur wenige News-Signale; die übrigen Meldungen fließen in die Kandidaten für die
  Eröffnung ein.
- Die Startwerte sind begründete Annahmen, nicht an historischen Daten optimiert.

## Änderungsverlauf

- 2026-11-17: Version 2 angelegt (News + Ausbruch), löst ARIA ab. Am selben Tag ergänzt:
  PR Newswire als zweite Quelle, Meldungen seit Börsenschluss als Kandidaten für die
  Eröffnung, Zeitlimit der Ausbruchs-Strategie 30 statt 90 Minuten.
