# Strategie (Version 2, Start 2026-11-17)

Ziel laut Josh: viele Trades, große Positionen bzw. Hebel, sehr kurze Haltedauer.
Getestet wird im Alpaca-Paper-Konto (100.000 $ Startgröße, 4-facher Intraday-Hebel).
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

## Strategie 1: Nachrichten

Idee: Nicht die erste Sekunde handeln (die gewinnen Profi-Systeme), sondern die
Fortsetzung in den Minuten danach.

1. Jede Meldung aus Alpacas Nachrichtenstrom mit höchstens drei Aktien wird von der KI
   bewertet: Richtung, Überraschung (0–10), Sicherheit (0–10).
2. Gehandelt wird nur bei Überraschung ≥ 7 und Sicherheit ≥ 6.
3. Dann wartet der Bot bis zu 5 Minuten auf Bestätigung: Kurs mindestens 0,4 % in die
   erwartete Richtung, Minutenvolumen mindestens das Doppelte des Üblichen, enge
   Geld-/Brief-Spanne. Ist der Kurs schon über 3 % gelaufen, wird nicht hinterhergekauft.
4. Stop-Abstand: 0,75 × Kursspanne der letzten 10 Minuten, mindestens 0,4 %, höchstens 1,5 %.
5. Zeitlimit 30 Minuten. Danach 60 Minuten Pause für dieselbe Aktie.

## Strategie 2: Ausbruch aus der Eröffnungsspanne

Idee: Aktien, die an dem Tag „im Spiel" sind, laufen nach dem Ausbruch aus den ersten
Handelsminuten oft in dieselbe Richtung weiter.

1. Vor der Eröffnung entsteht ein Pool: feste Liste liquider Aktien, größte Gewinner und
   Verlierer, meistgehandelte Aktien und vorbörsliche Meldungen mit Überraschung ≥ 6.
2. Nach den ersten 5 Minuten wird jede Aktie vermessen. Übrig bleiben die mit Kurslücke
   ≥ 1 %, relativem Volumen ≥ 1,0 (gegenüber den letzten 10 Tagen), Spanne zwischen 0,2 %
   und 2,5 % und genug Umsatz. Die 8 mit dem höchsten relativen Volumen werden beobachtet.
3. Einstieg beim Ausbruch über das Hoch (long) oder unter das Tief (short), bis 11:00 Uhr
   New Yorker Zeit, einmal pro Aktie und Tag.
4. Stop-Abstand: halbe Spannenbreite, mindestens 0,3 %, höchstens 1,0 %. Zeitlimit 90 Minuten.

## Bekannte Schwächen des Tests

- **Papertrading füllt Orders zu günstig aus.** Echte Kursrutscher bei Nachrichten bildet
  die Simulation nicht ab. Gute Paper-Ergebnisse sind deshalb eine Obergrenze.
- **Kursdaten kommen nur von der Börse IEX** (kostenlose Stufe), ein kleiner Ausschnitt des
  Marktes. Volumen und Geld-/Brief-Kurse sind dadurch ungenau.
- Die Startwerte sind begründete Annahmen, nicht an historischen Daten optimiert. Der
  Wochenrückblick soll zeigen, welche davon tragen.

## Änderungsverlauf

- 2026-11-17: Version 2 angelegt (News + Ausbruch), löst ARIA ab.
