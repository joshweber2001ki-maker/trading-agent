# Vorschlag: schneller Einstieg und Handel vor und nach der Börse

Stand: 19.11.2026, nach zwei Messläufen. Fünf Punkte am Ende brauchen Joshs Entscheidung.
Umgesetzt ist davon noch nichts, bis auf die Messung selbst.

## Kurzfassung

Die Messung zeigt, wo es nach Unternehmensmeldungen etwas zu holen gäbe: vor Börsenbeginn und
nach Börsenschluss, in den ersten ein bis zwei Minuten nach einer Pressemitteilung, meist bei
Quartalszahlen. Der Bot handelt heute zu keiner dieser Zeiten und bekommt die Meldungen Stunden
zu spät. In zwei Tagen hat er 462 Meldungen bewertet und keinen einzigen Trade gemacht.

Sie zeigt aber auch drei Grenzen. Ob die KI die Richtung einer frischen Meldung trifft, ist
noch nicht gemessen; einfach dem Kurs nachzulaufen funktioniert nicht. In der ersten Minute
werden im Median nur 50.000 $ gehandelt, es passen also Positionen von 5.000 bis 25.000 $ hinein,
keine 100.000 $. Und ein Teil der Ausschläge ist gewöhnliches Zappeln dünn gehandelter Aktien.

Mein Vorschlag ist deshalb eine Reihenfolge: erst eine Woche lang auf frischen Meldungen
nachrechnen, ob die KI richtig liegt (kostet rund 3 $ am Tag), parallel den Handel vor und nach
der Börse bauen und klein in Betrieb nehmen, und den Test der schnellen Quelle RTPR um eine
Woche auf den 30.11. verschieben. Nächste Woche ist Thanksgiving, der 7-Tage-Test hätte nur
dreieinhalb Handelstage.

## Was gemessen wurde

Jeden Abend nach Börsenschluss misst der Bot zu jeder Meldung den Kursverlauf im Sekundentakt,
aus den Handelsdaten des gesamten Marktes. Zwei Läufe (17. und 18.11.) ergeben:

| | Anzahl |
|---|---|
| gemessene Meldungen | 912 |
| davon Pflichtmitteilungen an die US-Börsenaufsicht SEC (13.–18.11.) | 764 |
| davon Pressemitteilungen von PR Newswire (17.–18.11.) | 148 |
| in Aktien, die in den zehn Minuten davor gehandelt wurden | 590 |
| davon mit mindestens 1 % Kursbewegung in fünf Minuten („Ausschlag“) | 145 |
| davon mit mindestens 100.000 $ Umsatz in diesen fünf Minuten („handelbarer Ausschlag“) | 39 |

Das sind rund zehn handelbare Ausschläge pro Handelstag.

## Ergebnisse

**1. Fast alles passiert außerhalb der Handelszeit.** Von den 39 handelbaren Ausschlägen lagen
24 vor Börsenbeginn, 12 nach Börsenschluss und 3 in der Handelszeit. Die Uhrzeiten: 12:00 bis
15:30 Uhr und 22:00 bis 23:15 Uhr deutscher Zeit.

**2. Es sind vor allem Quartalszahlen.** 22 der 39 Fälle. Dort hat die KI ihre bekannte
Schwäche: Sie kennt die Erwartungen der Analysten nicht und kann kaum sagen, ob Zahlen gut
oder schlecht sind.

**3. Nach einer Pressemitteilung ist die Bewegung nach zwei Minuten weitgehend vorbei.**

| Zeit nach der Meldung | Pressemitteilungen (10 Fälle) | SEC-Meldungen (29 Fälle) |
|---|---|---|
| 10 Sekunden | 14 % der Bewegung gelaufen | 3 % |
| 60 Sekunden | 41 % | 16 % |
| 120 Sekunden | 87 % | 33 % |

Beispiele vom 17. und 18.11.: Dolby (Quartalszahlen, 22:15 Uhr) −5,5 % nach zehn Sekunden.
La-Z-Boy (22:15 Uhr) −1,6 % nach zehn Sekunden, −5,8 % nach einer Minute. Lowe's (12:00 Uhr)
+2,3 % nach zehn Sekunden, +4,0 % nach fünf Minuten.

Die SEC-Meldungen wirken langsamer, sind aber oft gar nicht der Auslöser: Bei 44 Paaren aus
Pressemitteilung und SEC-Meldung desselben Unternehmens kam die SEC-Meldung im Median acht
Minuten später (Target 17 Minuten, Lowe's 165 Minuten). Nur bei 4 von 44 lag sie innerhalb
einer Minute. Der kostenlose SEC-Feed ersetzt eine schnelle Quelle für Pressemitteilungen
also nicht. Meine erste Einschätzung vom 17.11. war hier zu günstig.

**4. Es wird wenig gehandelt.** Umsatz in der ersten Minute nach der Meldung, bei den 39
handelbaren Fällen: Median 50.000 $, ein Viertel der Fälle unter 8.000 $, ein Viertel über
290.000 $. Wer höchstens ein Zehntel des Umsatzes ausmachen will, kommt auf Positionen von
5.000 bis 25.000 $. Die heutige Positionsgröße von rund 100.000 $ passt hier nicht.

**5. Ein Teil der Ausschläge ist Rauschen.** In gleich langen Zeitfenstern vor der Meldung
hatten sich 20 % der Aktien ebenfalls um mindestens 1 % bewegt, danach 25 % (92 Fälle mit
vergleichbarem Fenster). Dünn gehandelte Aktien zappeln vor und nach der Börse auch ohne
Nachricht.

**6. Dem Kurs nachlaufen funktioniert nicht.** Probe ohne KI: nach 60 Sekunden in die
Richtung einsteigen, in die der Kurs bis dahin gelaufen ist. Fünf Minuten nach der Meldung
lag man damit nur in 40 % der Fälle vorn (35 Fälle; vorbörslich 57 %, nachbörslich 15 %, dort
dreht der Kurs meist wieder). Die Richtung muss also aus dem Inhalt der Meldung kommen.

**7. Der heutige Ablauf bringt nichts.** 462 bewertete Meldungen in zwei Tagen, 11 Kandidaten,
kein Trade. Nach Meldungen mit Richtung lief der Kurs in 30 Minuten im Schnitt 0,05 % gegen
die Vorhersage. Die Meldungen aus Alpacas Nachrichtenstrom kommen Stunden zu spät: Disney
8,5 Stunden nach der SEC-Meldung, ZIM 4 Stunden nach der Pressemitteilung.

**8. Die Antwortzeit des Modells spielt keine Rolle.** Opus braucht 1,7 Sekunden, Sonnet 1,3,
Haiku 1,0. Bei einer Reaktion, die sich über ein bis zwei Minuten aufbaut, sind 0,7 Sekunden
Unterschied nebensächlich. Es zählt, welches Modell besser urteilt.

## Was die Zahlen nicht hergeben

- **Ob die KI die Richtung trifft.** Alle Zahlen oben unterstellen, dass man die Richtung
  kennt. Auf frischen Meldungen ist die KI noch nie geprüft worden.
- **Die Spanne zwischen Geld- und Briefkurs.** Vor und nach der Börse ist sie weit und kann
  den Ertrag auffressen. Sie wird ab heute Abend mitgemessen.
- **Belastbare Mengen.** Vier Handelstage, Pressemitteilungen erst seit anderthalb Tagen,
  zehn handelbare Fälle bei den Pressemitteilungen. Das sind Hinweise, keine Beweise.
- **Genaue Zeiten bei Pressemitteilungen.** PR Newswire nennt nur die Minute. Die Reaktion
  ist deshalb eher schneller, als die Tabelle zeigt.

## Vorschlag in fünf Punkten

### P1 – Eine Woche nachrechnen, ob die KI auf frischen Meldungen richtig liegt

- **Was:** Die KI bewertet ab sofort jede Pressemitteilung und jede SEC-Meldung beim
  Eintreffen, an Handelstagen von 10:00 bis 2:00 Uhr deutscher Zeit, ohne zu handeln. Nach
  Börsenschluss rechnet der Bot aus den echten Kursen aus, was ein Einstieg 5, 45 und 120
  Sekunden nach der Veröffentlichung gebracht hätte, mit Stop, Ziel, Zeitlimit, Umsatzgrenze
  und gemessener Spanne. 5 Sekunden entspricht einer schnellen Quelle wie RTPR, 45 Sekunden
  dem SEC-Feed, 120 Sekunden dem heutigen Stand.
- **Warum:** Das beantwortet die offene Kernfrage, bevor Geld für eine Quelle oder Arbeit in
  den Handel fließt. Die Nachrechnung aus echten Kursen ist außerdem ehrlicher als das
  Paper-Konto, das Orders zu günstig ausführt.
- **Kosten:** etwa 330 Bewertungen pro Handelstag, mit allen drei Modellen rund 3 $ am Tag.
- **Dauer:** bis Freitag, 27.11. Dann liegen gut fünf Handelstage vor.

### P2 – Analystenerwartungen für Quartalszahlen besorgen

- **Was:** Der Bot holt morgens die erwarteten Gewinne und Umsätze der Firmen, die an dem Tag
  berichten, und gibt sie der KI mit. Mögliche Quelle: der Ergebniskalender von Finnhub.
- **Warum:** 22 der 39 handelbaren Fälle waren Quartalszahlen. Ohne Erwartungswerte ist die
  Richtung dort Raterei, mit jedem Modell.
- **Dein Teil:** ein Finnhub-Konto anlegen und den Schlüssel auf dem Server eintragen. Ob das
  kostenlose Konto die Schätzwerte liefert, prüfe ich vorher.

### P3 – Handel vor und nach der Börse bauen und klein starten

- **Was:** Der Bot handelt auch von 10:00 bis 15:30 Uhr und von 22:00 bis 2:00 Uhr deutscher
  Zeit, mit sofortigem Einstieg nach der Bewertung (Regel unten). Start mit den kostenlosen
  Quellen, höchstens 10.000 $ je Position und einer Position zur Zeit. Den SEC-Feed fragt der
  Bot dafür alle paar Sekunden ab statt alle zehn Minuten.
- **Warum:** Dort liegen 36 der 39 handelbaren Fälle. Mit den kostenlosen Quellen kommt der
  Bot meist zu spät: PR Newswire erreicht die Altersgrenze der Regel fast nie, die Einstiege
  kämen also aus SEC-Meldungen. Der Probebetrieb soll die Technik prüfen, bevor die schnelle
  Quelle dazukommt.
- **Wann:** Bau von Freitag bis Montag, Start am Dienstag, 24.11. Die genauen Werte lege ich
  dir vorher noch einmal vor.
- **Wichtig:** Ergebnisse aus diesem Probebetrieb sind kein Beleg, weil das Paper-Konto in
  dünnem Handel zu günstig ausführt. Der Beleg kommt aus P1.

### P4 – RTPR-Test auf den 30.11. verschieben und an P1 knüpfen

- **Was:** Den 7-Tage-Test nicht am 23.11. starten, sondern frühestens am 30.11., und nur,
  wenn P1 zeigt, dass ein Einstieg nach 5 Sekunden mit der Richtung der KI Geld verdient hätte.
- **Warum:** Nächste Woche ist die Börse am Donnerstag geschlossen und schließt am Freitag
  um 19:00 Uhr deutscher Zeit. Außerdem könnte der Bot am 23.11. noch gar nicht vor und nach
  der Börse handeln, der Test würde verpuffen.
- **Einordnung:** RTPR bleibt die richtige Quelle, falls die KI richtig liegt. Nach zwei
  Minuten ist die Bewegung zu 87 % gelaufen, und PR Newswire kommt beim Bot erst nach
  anderthalb bis dreieinhalb Minuten an.

### P5 – Alpacas Nachrichtenstrom ab Montag nicht mehr handeln

- **Was:** Meldungen aus Alpacas Strom werden ab 23.11. nicht mehr bewertet und gehandelt.
  Bis Freitag bleibt alles, wie es ist, damit der Modelltest vergleichbar bleibt.
- **Warum:** Zwei Tage, 462 Bewertungen, kein Trade, kein Vorhersagewert, Verspätung von
  Stunden. Mehr als vier von fünf Bewertungen entfallen auf diesen Strom; das Geld ist in P1
  besser angelegt.

## Die Regel für den sofortigen Einstieg (Entwurf zu P3)

Heute wartet der Bot nach der Bewertung bis zu fünf Minuten, ob Kurs und Volumen die Meldung
bestätigen. Bei frischen Meldungen ist die Bewegung dann vorbei. Der Entwurf, mit Startwerten,
die P1 noch schärfen soll:

| Baustein | Vorschlag |
|---|---|
| Auslöser | Pressemitteilung oder SEC-Meldung; Richtung klar, Überraschung mindestens 7, Sicherheit mindestens 7, Wirkung mindestens „mittel“ |
| Höchstalter | Bewertung liegt binnen 20 Sekunden nach Veröffentlichung vor; bei SEC-Meldungen 90 Sekunden, weil der Feed sie erst nach 30 bis 40 Sekunden zeigt. Sonst kein Sofort-Einstieg |
| Firmenzuordnung | nur wenn die Meldung eindeutig zur Aktie gehört; am 17.11. wurde eine Meldung des schwedischen Investors EQT dem US-Gasförderer EQT zugeordnet |
| Nicht hinterherlaufen | kein Einstieg, wenn der Kurs seit der Meldung schon mehr als die Hälfte der erwarteten Bewegung gelaufen ist |
| Spanne | kein Einstieg, wenn Geld- und Briefkurs mehr als 0,5 % auseinanderliegen oder kein Kurs vorliegt |
| Positionsgröße | höchstens 10.000 $ (kleine Firmen 5.000 $), Risiko je Trade 0,25 % des Depots |
| Order | Limit-Order höchstens 0,3 % über dem letzten Kurs (bei Verkauf darunter), nach 15 Sekunden ohne Ausführung gestrichen |
| Ausstieg | Ziel und Stop je Wirkungs-Stufe wie heute, der Stop anderthalbmal so weit; Zeitlimit 15 Minuten vorbörslich, 10 Minuten nachbörslich |
| Tagesende | alles glatt bis 15:20 Uhr und bis 1:45 Uhr deutscher Zeit |

Meldungen aus langsamen Quellen laufen weiter nach der heutigen Regel mit Kursbestätigung.

## Was vor und nach der Börse technisch anders ist

- **Nur Limit-Orders.** Der Broker nimmt dort keine Stop-Orders und keine Orders mit
  angehängtem Stop und Ziel an. Den Stop muss der Bot selbst überwachen und mit einer
  Limit-Order schließen. Fällt der Bot aus, ist die Position ungeschützt; deshalb kleine
  Positionen, kurze Haltezeit und ein Wächter, der auch vor 1:45 Uhr glattstellt.
- **Kurse in Echtzeit nur von einer kleinen Börse.** Das kostenlose Datenpaket zeigt live nur
  die Börse IEX, über die drei bis fünf Prozent des Handels laufen. Vor und nach der Börse
  kann das für einen Stop zu wenig sein. Wenn der Probebetrieb das bestätigt, braucht es
  Alpacas Echtzeitdaten für 99 $ im Monat.
- **Weite Spannen, wenig Umsatz.** Deshalb die Grenzen für Spanne und Positionsgröße.

## Was im Paper-Konto zu günstig aussieht

Das Paper-Konto führt eine Limit-Order aus, sobald der Marktkurs sie erreicht, ohne zu prüfen,
ob zu diesem Kurs genug Stücke gehandelt wurden. In dünnem Handel bekommt man so Ausführungen,
die es echt nicht gäbe. Der Bot soll deshalb zu jedem Trade vor und nach der Börse am Abend
nachsehen, ob zum Limit tatsächlich genug gehandelt wurde, und Trades ohne diese Deckung
getrennt ausweisen.

## Zeitplan

| Wann | Was |
|---|---|
| Do 19.11. | P1 startet nach deiner Freigabe am selben Abend; die Messung hält ab heute auch Spanne und Kursverlauf im Detail fest |
| Fr 20.11. | P2: Finnhub prüfen, du trägst den Schlüssel ein |
| Sa 21.11. | Wochenrückblick mit der Antwort, ob sich Opus gelohnt hat |
| Fr 20. bis Mo 23.11. | P3 bauen und testen, Werte zur Freigabe vorlegen |
| Di 24.11. | P3 startet klein; P5 gilt seit Montag |
| Fr 27.11. | Auswertung von P1 nach gut fünf Handelstagen, Entscheidung über RTPR |
| Mo 30.11. | Start des RTPR-Tests, falls P1 dafür spricht |

## Deine Entscheidungen

1. **P1:** Nachrechnung auf frischen Meldungen starten, rund 3 $ pro Handelstag? Mit allen
   drei Modellen, oder nur mit Opus und Haiku für rund 2 $?
2. **P2:** Finnhub-Konto anlegen, sobald ich bestätigt habe, dass das kostenlose Konto reicht?
3. **P3:** Handel vor und nach der Börse bauen, Start am 24.11. mit höchstens 10.000 $ je
   Position?
4. **P4:** RTPR-Test auf frühestens 30.11. verschieben und an das Ergebnis von P1 knüpfen?
5. **P5:** Alpacas Nachrichtenstrom ab 23.11. nicht mehr handeln?
