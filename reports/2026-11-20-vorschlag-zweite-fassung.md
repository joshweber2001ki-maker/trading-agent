# Vorschlag: zweite Fassung des Sofort-Einstiegs

Stand: 20.11.2026, nach der Messung der Spanne (`memory/strategy.md`, „Dritte Auswertung“).
Umgesetzt ist davon nichts. Ein Ja von Josh gilt für die Tabelle unten.

## Warum

Vor und nach der Börse liegt die Spanne zwischen Geld- und Briefkurs direkt nach einer Meldung bei
2 bis 4 %, die Bewegung in fünf Minuten bei 2,2 %. Die erste Fassung steigt ein, ohne den Markt zu
sehen. Am gemessenen Kursverlauf von 64 handelbaren Ausschlägen durchgespielt verliert sie im Schnitt
0,4 bis 1,3 % je Trade, selbst wenn die Richtung jedes Mal stimmt, und 2,4 bis 3,7 % bei falscher
Richtung. Ist die Spanne beim Einstieg höchstens 0,5 %, bleiben bei richtiger Richtung rund 0,6 bis
1 % (Handelszeit: rund 2 %).

## Die Regel

| Baustein | erste Fassung (läuft seit 19.11.) | zweite Fassung (Vorschlag) |
|---|---|---|
| Blick auf den Markt vor dem Einstieg | keiner | Geld- und Briefkurs aus der Kursquelle des Bots, wenn sie beide Seiten zeigt und höchstens 10 Sekunden alt ist. Sonst eine Probe mit 1 Aktie. |
| Probe | – | 1 Aktie in Richtung der Bewertung mit dem bisherigen Limit handeln und sofort mit einem Limit zurückhandeln, das 0,5 % schlechter ist. Je Schritt höchstens 3 Sekunden warten. Die beiden Ausführungskurse sind Brief- und Geldkurs. |
| Einstieg nur, wenn | die Order ausgeführt wird | die Spanne höchstens 0,5 % beträgt und der Kurs gegenüber dem Kurs vor der Meldung höchstens 0,5 % gegen die Bewertung gelaufen ist. In Richtung der Bewertung bleibt die Grenze die halbe erwartete Bewegung. |
| Limit der Order | Kurs vor der Meldung plus halbe erwartete Bewegung | Kurs aus der Probe plus 0,2 %, höchstens das bisherige Limit |
| Zeiten | vor Börsenbeginn und nach Börsenschluss | zusätzlich die Handelszeit ab Montag, 23.11., von 5 Minuten nach Beginn bis 30 Minuten vor Schluss (wie die alte Regel) |
| Stop und Ziel in der Handelszeit | – | dieselbe Berechnung, als Order beim Broker. Zeitlimit 15 Minuten. |
| Kurs vor der Meldung in der Handelszeit | – | wie bisher der letzte Kurs des gesamten Marktes, der mindestens 15 Minuten alt ist; ist der letzte Kurs der kostenlosen Quelle vor der Meldung jünger, zählt der |
| Pressemitteilungen in der Handelszeit | gehen an die alte Regel mit Kursbestätigung | gehen an den Sofort-Einstieg. Die alte Regel behält Alpacas Nachrichtenstrom, bis er aus dem Handel genommen wird. |
| alles andere | | unverändert: Schwellen 7/7/1, höchstens 10.000 $ (kleine Firmen 5.000 $), Risiko 0,25 % des Depots, eine Position zur Zeit, Stop und Ziel ab Ausführung, Zeitlimit 15 und 10 Minuten vor und nach der Börse, Höchstalter der Meldung 300 Sekunden, sofort raus bei Gegenbewegung |

Jeder abgelehnte Einstieg wird mit Grund protokolliert (`fast_rejected`: `probe_no_fill`, `spread`,
`moved_against`), jede Probe mit beiden Kursen und ihrer Dauer (`fast_probe`).

## Was zu erwarten ist

- Vor und nach der Börse war die Spanne in fünf Tagen bei 7 bis 9 von 64 handelbaren Ausschlägen eng
  genug, in der Handelszeit bei 4 bis 5 von 8. Das sind mit schneller Quelle zwei bis drei
  Gelegenheiten am Tag.
- Damit die Regel im Plus endet, muss die KI vor und nach der Börse in etwa zwei von drei Fällen
  richtig liegen (gerechnet: 61 bis 67 %), in der Handelszeit in jedem zweiten (44 bis 53 %). Wie oft
  sie richtig liegt, ist noch nicht bekannt; die Wochenstatistik zählt es seit heute mit.
- Bei 10.000 $ je Position bringt ein Treffer etwa 60 bis 200 $, ein Fehlgriff kostet etwa 160 bis 230 $.
- Solange die einzige Quelle zwei Minuten zu spät kommt, wird es kaum Trades geben: Die Bewegung ist
  dann zu 87 % gelaufen.

Die Zahlen stammen aus fünf Tagen, bei enger Spanne sind es 4 bis 9 Fälle. Sie zeigen die Richtung,
keine genaue Größe. Die Richtung des Kurses ist nachträglich bekannt, und es zählen nur Aktien, die
sich danach bewegt haben.

## Was die Probe kostet und was offen ist

- Jede Probe kostet die Spanne auf 1 Aktie, also Cents. Ist die Spanne zu weit, schließt der Bot die
  eine Aktie mit seiner gewöhnlichen Ausstiegs-Order.
- Wie lange die Probe am Konto dauert, ist nicht gemessen (geschätzt ein bis drei Sekunden). Der erste
  Lauf zeigt es.
- Das Paper-Konto führt 10.000 $ zum besten Kurs aus, auch wenn dort nur 100 Aktien stehen. Am echten
  Markt wäre die Ausführung schlechter. Die abendliche Prüfung jeder Ausführung gegen den echten
  Umsatz steht noch aus.
- Ohne Probe ginge es mit Alpacas Datenpaket (99 $ im Monat): Dann greift die erste Zeile der Tabelle
  allein. Der Code nimmt die Kursquelle, sobald sie etwas zeigt.

## Nicht Teil dieses Vorschlags

- RTPR. Empfehlung: noch nicht starten, erst die Trefferquote der KI ansehen. Geplant wird weiter mit
  dem 30.11.
- Das Modell. Der Modelltest endet am 20.11.; der Wochenrückblick vom 21.11. wertet ihn aus.

## Entscheidung

Ja oder Nein zur Tabelle. Bis dahin läuft die erste Fassung wie freigegeben. Wer die Einstiege vor und
nach der Börse bis zur Umsetzung anhalten will: `[fast] enabled = false`, das setze ich auf Zuruf.
