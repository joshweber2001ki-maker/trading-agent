# Kritische Bewertung von Strategie und Ablauf (2026-11-17)

Von Josh angefordert nach dem Einbau der Wirkungs-Stufe. Stand: vor der ersten Handelsstunde,
also ohne einen einzigen echten Trade. Alle Zahlen stammen aus eigenen Messungen von heute
(Protokolle des Servers, Rückrechnungen, Datenvergleich für den 16.11.), wo nicht anders vermerkt.
Die Vorschläge am Ende sind **offen**: Nichts davon ist umgesetzt, Josh entscheidet einzeln.

## Kurzfazit

Die Technik läuft, aber für keine der beiden Strategien gibt es einen Beleg, dass sie Geld
verdient. Die Ausbruchs-Strategie verliert in ihrer kurzen Form in der Rückrechnung schon vor
Kosten. Die News-Strategie sieht eine Meldung im Mittel zwei Minuten nach Veröffentlichung;
ob dann noch Bewegung übrig ist, weiß niemand, das misst der Bot ab jetzt mit. Dazu kommen
ein Zielkonflikt (große Positionen und große Kurswirkung gibt es nicht in derselben Aktie),
eine Datenbasis mit Lücken und einige Konstruktionsfehler.

## 1. Kein belegter Vorteil

**Ausbruchs-Strategie, Rückrechnung über 90 Tage, feste Liste von 20 Aktien**
(`tools/backtest_orb.py`, neu mit `--feed sip` für Kurse des gesamten Marktes):

| Variante | Daten | Trades | Treffer | Profitfaktor | Ergebnis |
|---|---|---|---|---|---|
| aktuell (Stop halbe Spanne, Ziel 2R, 30 min), 0,02 % Kosten je Seite | IEX | 207 | 39 % | 0,98 | −1,3 % |
| dieselbe | Gesamtmarkt | 172 | 35 % | 0,80 | −9,3 % |
| dieselbe, ohne Kosten | Gesamtmarkt | 173 | 36 % | 0,90 | −4,3 % |
| dieselbe, 0,05 % Kosten je Seite | Gesamtmarkt | 173 | 34 % | 0,68 | −15,7 % |
| nur in Richtung der Eröffnungsspanne | Gesamtmarkt | 142 | 37 % | 0,90 | −3,8 % |
| Richtung der Spanne, Stop am anderen Ende der Spanne, kein Ziel, halten bis Börsenschluss | Gesamtmarkt | 136 | 47 % | 1,31 | +9,7 % |
| dieselbe | IEX | 154 | 40 % | 1,23 | +9,2 % |

- Die IEX-Rechnung war zu freundlich: In IEX-Kursen fehlen die Ausschläge, die Stops auslösen.
- Die kurze Form verliert schon ohne Kosten. Jedes 0,01 % Kosten je Seite kostet zusätzlich
  rund 2,5 Prozentpunkte in 90 Tagen.
- Positiv ist nur die Form mit langer Haltedauer. Sie entspricht der einzigen veröffentlichten
  Untersuchung (Zarattini, Barbon, Aziz 2024: 5-Minuten-Spanne, die 20 Aktien mit dem höchsten
  relativen Volumen aus dem ganzen Markt, Richtung der ersten Kerze, Ausstieg zum Börsenschluss).
  136 Trades aus einem Zeitraum und 20 Großkonzernen sind aber kein Beweis.

**News-Strategie.** Gemessen heute Vormittag:
- Pressemitteilungen von PR Newswire erreichen den Bot im Median 116 Sekunden nach
  Veröffentlichung (90 % unter 200 Sekunden, 30 Meldungen).
- Alpacas Nachrichtenstrom meldet dieselben Ereignisse 18 bis über 90 Minuten später
  (13 Paare, Median 50 Minuten). Seine Artikel tragen aber einen frischen Zeitstempel und
  gelten dem Bot als neu. Eine „Bestätigung" durch den Kurs ist dann Zufall.
- 267 Meldungen vorbörslich bewertet, 75 % ohne Richtung, 3 nach Bewertung handelbar.
- Nachspiel des 16.11.: 230 bewertet, 3 handelbar, keine bestätigt, 0 Trades.

## 2. Zielkonflikt: große Positionen und große Kurswirkung

Starke Kursreaktionen gibt es bei kleinen Firmen. Dort lässt sich eine Position von 25.000 bis
100.000 $ nicht in Sekunden zum angezeigten Kurs handeln. Der Bot kennt bisher keine Grenze
nach Handelsumsatz der Aktie. Das Paper-Konto verdeckt das Problem: Alpaca füllt jede
Ordergröße zum besten Kurs, ohne Markttiefe und ohne Rückwirkung der eigenen Order.

## 3. Datenbasis (kostenloser IEX-Feed)

Vergleich für den 16.11., 26 Aktien:
- IEX-Anteil am Umsatz: 3,3 % in den ersten fünf Minuten, 4,5 % über den Tag (Median).
- Eröffnungsspanne: Bei 19 von 25 Aktien liegt mindestens eine Kante weiter vom echten Wert
  entfernt als der Ausbruchs-Puffer von 0,05 %. Bei mittelgroßen Aktien (DG, GLW, IP) sind es
  0,3 bis 0,6 % des Kurses, also so viel wie der ganze Stop-Abstand.
- Kleine Aktie (TCX): 4 Handelsminuten auf IEX am ganzen Tag gegenüber 59 im Gesamtmarkt.
- Der Bot prüft nicht, wie alt „letzter Kurs" und „letzte Minute" sind. Fehlt ein
  Geld-/Brief-Kurs, entfällt die Prüfung der Spanne stillschweigend.

## 4. Konstruktionsfehler

1. **Kandidaten der Ausbruchs-Strategie.** Die Listen „größte Gewinner/Verlierer" und
   „meistgehandelt" werden 4 Minuten vor der Eröffnung abgefragt. Alpaca setzt sie erst zur
   Eröffnung zurück, bis dahin stehen dort die Aktien von gestern (geprüft: Stand 00:59 UTC).
2. **Bezugskurs der News-Strategie** ist der Kurs beim Eintreffen der Meldung, nicht bei
   Veröffentlichung. Die Regel „nicht hinterherlaufen" sieht die Bewegung der ersten Minuten nicht.
3. **Stop und Ziel** werden vom Signalkurs gerechnet, der Einstieg darf bis 0,15 % schlechter
   sein. Bei 0,5 % Stop kann aus 2:1 im ungünstigen Fall 1,3:1 werden.
4. **Einstieg der Ausbruchs-Strategie:** Abfrage alle 5 Sekunden und Kauf zum dann aktuellen
   Kurs; die Rückrechnung nimmt den Einstieg genau am Ausbruch an.
5. **Tagesverlust-Grenze** sperrt nur neue Einstiege. Mit vier offenen Positionen sind an einem
   Tag rund −4 % möglich, bei Kurssprüngen mehr.
6. **Klumpen:** Die feste Liste ist überwiegend Technologie. Vier Positionen in dieselbe
   Richtung sind eine einzige Wette mit bis zu dreifachem Hebel.
8. **Von Hand geschlossene Positionen** bemerkte der Bot nicht (aufgefallen am 17.11. bei PEP).
   **Behoben am 17.11.:** Er erkennt sie und verbucht den echten Schlusskurs (`external`).
7. **Folgekurse** (`news_outcome`) wurden mit IEX-Kursen gemessen. **Behoben am 17.11.:**
   Messung jetzt einmal nach Börsenschluss mit Kursen des Gesamtmarkts.
9. **Geschätzter Ausstieg als Ergebnis verbucht:** Bei PEP buchte der Bot am 17.11. einen
   Verlust von 507,65 $, den es nie gab (die Position war schon von Hand geschlossen, echt
   −35,75 $). **Behoben am 17.11.:** siehe Punkt 8; der Eintrag ist in
   `reports/trade-corrections.jsonl` richtiggestellt, und die Wochenstatistik listet jeden
   Trade mit geschätztem Ausstiegskurs zum Nachprüfen auf.
10. **Tagesabschluss nach einem Neustart:** Das Tagesergebnis zählte ab dem letzten Start des
   Bots. Nach dem Neustart am Nachmittag des 17.11. steht dort fast null statt rund −400 $.
   **Behoben am 17.11.:** Maßstab ist jetzt der Depotwert zum Börsenschluss des Vortags.

## 5. KI

- Sie liest Überschrift und Kurztext, nicht die ganze Meldung, und kennt keine
  Analystenerwartungen. Bei Quartalszahlen ist sie deshalb fast blind.
- Die Wirkungs-Stufe ist eine ungeprüfte Schätzung. Antworten sind nicht reproduzierbar,
  ein Modell-Update kann sie verschieben.
- Zuordnung Firmenname → Kürzel: 96 von 514 Pressemitteilungen zugeordnet (Stand 14:06 UTC);
  der Rest sind nicht börsennotierte Firmen oder Namen, die nicht exakt passen.
- **Budget knapp:** In der Stunde vor der Eröffnung liefen 345 Bewertungen, erlaubt sind 400
  pro Stunde und 2.000 pro Tag. An Tagen mit vielen Quartalszahlen greift die Grenze; dann
  fehlen Kandidaten, sichtbar nur im Protokoll (`news_skipped`, Grund `llm_budget`).

## 6. Betrieb und Sicherheit

1. **Alpaca-Schlüssel stehen öffentlich in der Git-Historie.** Jeder kann damit im Paper-Konto
   handeln oder die eine erlaubte Verbindung zum Nachrichtenstrom belegen.
2. **Auslieferung:** Der stündliche Abgleich läuft als root und führt Skripte aus dem Repo aus.
   Wer auf `main` pushen kann, hat binnen einer Stunde root auf dem Server.
3. **Keine Alarmierung.** Steht der Bot oder der Server, merkt es niemand vor dem Wochenrückblick.
4. **Wächter auf demselben Server.** Fällt der Server aus, bleiben Positionen über Nacht
   ungesichert (Stop- und Zielorders gelten nur für den Tag).
5. Stündliche Protokoll-Commits im öffentlichen Repo (rund 8.700 pro Jahr).
6. Ist das KI-Guthaben leer, ruht die News-Strategie ohne Meldung.

## 7. Auswertung

Wenige Trades pro Woche, rund 60 Stellschrauben. Belastbare Aussagen brauchen Monate.
Paper-Ergebnisse sind eine Obergrenze.

## Stand der Vorschläge (17.11.2026, 15:40 Uhr)

- **V6 entschieden:** Josh schaltet die Ausbruchs-Strategie ab (aktiv ab 18.11.). Damit
  entfallen V2 und V11 (außerhalb der Handelszeit wird nichts mehr bewertet).
- **V1 zugesagt:** Josh erzeugt die Alpaca-Schlüssel neu und trägt sie selbst ein.
- **Neu, Wunsch von Josh:** deutlich schneller nach einer Meldung handeln, ohne Kosten
  (Testabos sind in Ordnung). Messungen dazu stehen in `memory/strategy.md` unter „Tempo".
  Daraus die Vorschläge V12 bis V15 unten.
- **V12 umgesetzt (17.11.):** Die Tempo-Messung läuft ab dem Abend des 17.11. (`news_speed`,
  Abschnitt „Tempo" der Wochenstatistik). Josh hat den Ablauf freigegeben: diese Woche messen,
  am Montag, 23.11., den RTPR-Test starten (V14), falls die Messung dafür spricht.
- **V16 entschieden (17.11., Wunsch von Josh):** Vom 18. bis 20.11. bewertet Opus 5.5 statt
  Haiku 5.5 und entscheidet; Haiku und Sonnet 5.5 bewerten jede Meldung zum Vergleich mit.
  Auswertung im Wochenrückblick (Abschnitt „Modellvergleich" der Wochenstatistik). Hintergrund
  und Probelauf in `memory/strategy.md` unter „Modelltest".
- Alle übrigen Vorschläge sind offen.

## Vorschläge (Entscheidung Josh)

| Nr. | Was | Warum | Risiko |
|---|---|---|---|
| V1 | Alpaca-Schlüssel neu erzeugen, Repo auf privat | Abschnitt 6.1 | keins; Josh muss die Schlüssel selbst eintragen |
| V2 | Kandidaten der Ausbruchs-Strategie direkt nach der Eröffnung neu abfragen | 4.1 | mehr und kleinere Aktien im Pool |
| V3 | News: Bezugskurs = Kurs vor Veröffentlichung; live nur Pressemitteilungen handeln, Alpacas Strom nur für Kandidaten und Statistik | 1, 4.2 | weniger Signale |
| V4 | Alter von Kurs und Minutenvolumen prüfen; ohne Geld-/Brief-Kurs kein Einstieg | 3 | weniger Signale in kleinen Aktien |
| V5 | Positionsgröße höchstens 1 % des Tagesumsatzes der Aktie; News-Trades erst ab 20 Mio. $ Tagesumsatz | 2 | kleinere Positionen |
| V6 | Ausbruchs-Strategie auf die Form mit Beleg umstellen (Richtung der Spanne, Stop am anderen Ende, halten bis Schluss) oder abschalten | 1 | widerspricht dem Wunsch nach kurzer Haltedauer |
| V7 | Stop und Ziel nach der Ausführung vom echten Einstiegskurs rechnen | 4.3 | mehr Order-Schritte |
| V8 | Abgleich ohne root, Neustart über einen festen Auslöser | 6.2 | einmaliger Umbau am Server |
| V9 | Täglicher Kontrolllauf in der Cloud mit Push-Nachricht bei Stillstand | 6.3 | ein kurzer Lauf pro Tag |
| V11 | KI-Budget auf 800 pro Stunde und 5.000 pro Tag anheben | 5 | Kosten: bei rund 0,0001 $ je Bewertung höchstens etwa 0,50 $ pro Tag |
| V10 | Echtzeitdaten des Gesamtmarkts (Alpaca „Algo Trader Plus", 99 $/Monat) erst buchen, wenn die Folgekurse zeigen, dass nach zwei Minuten noch Bewegung übrig ist | 3 | Kosten |

| V12 | Tempo-Messung: zu jeder Pressemitteilung und Pflichtmitteilung den Kursverlauf im Sekundentakt aus Handelsdaten des gesamten Marktes nachmessen (kostenlos, 15 Minuten verzögert) | zeigt vor jeder Ausgabe, wie viel Bewegung nach 2 s, 10 s, 40 s und 2 min noch übrig ist | keins für den Handel; für den SEC-Feed braucht es eine Kontaktadresse in der Kennung |
| V13 | WireAlpha (laut Anbieter kostenlos und in Echtzeit) anschließen und die Verzögerung gegen die Veröffentlichungszeit messen | einzige kostenlose Quelle, die Echtzeit verspricht | unbekannter Anbieter, Angaben ungeprüft; Josh muss ein Konto anlegen |
| V14 | RTPR sieben Tage testen (danach 139 $/Monat, Zahlungsdaten nötig) | einziger gefundener Anbieter mit Echtzeit-Schnittstelle für Privatkunden und Testzeitraum | Kosten, wenn nicht rechtzeitig gekündigt; erst nach V12 und V15 sinnvoll |
| V16 | Stärkeres Modell für die Bewertung: Opus 5.5 entscheidet, Haiku 5.5 und Sonnet 5.5 laufen zum Vergleich mit | zwei Fehlalarme am 17.11. durch falsche Firmenzuordnung und eine unstimmige Meldung; ohne Kursbestätigung (V15) zählt die Qualität der Bewertung mehr | etwa 3 $ pro Handelstag; Opus braucht 2 bis 4 statt 1 Sekunde je Bewertung |
| V15 | Sofort-Einstieg nach der KI-Bewertung statt bis zu 5 Minuten auf Bestätigung zu warten; Handel auch vor und nach der regulären Handelszeit | ohne das nützt eine schnelle Quelle nichts; über 90 % der Pflichtmitteilungen kommen außerhalb der Handelszeit | Strategieänderung; außerhalb der Handelszeit weite Spannen, keine Stop-Orders beim Broker, Paper-Ergebnisse dort besonders unzuverlässig |

**Abbruchkriterium für die News-Idee** (Vorschlag): Nach vier Wochen oder 30 Meldungen je
Wirkungs-Stufe aus Pressemitteilungen muss die Bewegung in vorhergesagter Richtung nach
30 Minuten im Schnitt über 0,3 % liegen und die Richtung in über 55 % der Fälle stimmen.
Sonst wird die Minuten-Variante beendet.
