# Vorschlag: Ziel-Setup für den Nachrichtenhandel

Stand: 24.11.2026. Antwort auf Joshs Frage, wie wir Meldungen so früh wie möglich bekommen, so gut wie
möglich auswerten und daraus einen Trade machen, und ob dafür andere Quellen, Kurse, ein anderer Broker
oder weitere Daten nötig sind. Umgesetzt ist davon nichts; die Entscheidungen stehen in Abschnitt 7.
Preise und Bedingungen der Anbieter sind am 24.11.2026 auf deren Seiten nachgelesen (Quellen am Ende),
die Messwerte stammen aus den Protokollen des Bots vom 13. bis 24.11. Alle Uhrzeiten in deutscher Zeit.

**Nachtrag vom 24.11.2026, Abend:** Josh hat den Vorschlag freigegeben („Lass uns das so alles umbauen …
Wir nehmen alles, was kostenlos ist und hilft“). Umgesetzt und ab dem Neustart am Abend aktiv sind aus
Abschnitt 5 die Punkte 1 bis 4 und 7: SEC-Liste alle 2 Sekunden mit Text und Bewertung, Probe und Einstieg
nur bei enger Spanne, Sofort-Einstieg auch in der Handelszeit, WireAlpha als Quelle (läuft, sobald
der Schlüssel eingetragen ist), Kanzlei-Filter, Alpacas Strom und die Finnhub-Nachrichten aus, Haiku und
Sonnet bewerten mit. Offen sind die Punkte 5 und 6 (abendliche Prüfung der Ausführungen, Dossier).
Entscheidung 3 aus Abschnitt 7 hat Josh am selben Abend getroffen: „alles direkt handeln, kein voriges
Messen, ist ja Paper-Trading“. SEC-Meldungen und die vier Verteiler über WireAlpha werden deshalb von Anfang
an gehandelt; das Freischalten nach Meldungstyp aus Abschnitt 3 entfällt, die Statistik je Typ bleibt.
Interactive Brokers folgt, wenn Josh dort ein Konto hat. Der Stand steht in `memory/strategy.md`,
„Zweite Fassung im Handel“.

## Kurzfassung

1. **Die ersten Sekunden nach Quartalszahlen sind nicht zu holen, mit keiner Quelle.** Dort ist nach
   10 Sekunden fast die Hälfte der Bewegung gelaufen, und die Spanne zwischen Geld- und Briefkurs liegt
   bei 5 %. Eine Studie mit den 50 liquidesten US-Aktien kommt zum selben Ergebnis.
2. **Es gibt aber eine Klasse von Meldungen, bei der genau dein Ansatz passt:** Meldungen ohne
   Quartalszahlen (Übernahmen, Verträge, Kapitalerhöhungen, Personalien, „Company Update“). Der Kurs
   braucht dort 30 bis 120 Sekunden, die Spanne ist öfter eng, und man muss den Text lesen, um die
   Richtung zu erkennen. Das ist die Stärke einer KI und die Schwäche einfacher Lese-Automaten.
3. **Der größte Hebel kostet nichts:** die Liste der US-Börsenaufsicht SEC alle zwei Sekunden abfragen
   statt alle zehn Minuten. Dann sieht der Bot eine Pflichtmitteilung 35 bis 60 Sekunden nach der Annahme
   statt nach gut fünf Minuten. Bei Pflichtmitteilungen ohne Quartalszahlen waren nach 60 Sekunden erst
   4 % der Bewegung gelaufen.
4. **Pressemitteilungen in Echtzeit:** Es gibt bis 200 $ im Monat keinen Weg, der zugleich bestätigt
   schnell und für einen Handels-Bot erlaubt ist. RTPR scheidet aus, seine Bedingungen verbieten
   automatischen Handel. Zwei Kandidaten lassen sich billig messen: WireAlpha (kostenlos) und Benzinga
   über Interactive Brokers (35 $ im Monat, mit Testzugang).
5. **Kurse:** Für den Test ist kein Kauf nötig. Die günstigste echte Quelle wäre Interactive Brokers für
   4,50 $ im Monat, die bequemste Alpacas Datenpaket für 99 $.
6. **Broker:** kein Wechsel. Der Engpass liegt nicht bei Alpaca.
7. **Fundamentaldaten:** kostenlos ergänzen, nichts kaufen.
8. **Kosten:** Stufe 0 kostet nichts außer der KI (geschätzt 3 bis 6 $ pro Handelstag statt heute rund
   2,50 $, weil die SEC-Meldungen dazukommen). Stufe 1 kostet rund 40 $ im Monat und lohnt erst, wenn
   Stufe 0 gemessen ist.

Die ehrliche Erwartung: ein bis zwei handelbare Fälle pro Tag mit enger Spanne, nicht viele. Ob die KI die
Richtung oft genug trifft, weiß weiterhin niemand; das Setup ist so gebaut, dass es sich zeigt.

## 1. Warum sich das Ziel verschiebt

Aus den eigenen Messungen, nur Meldungen mit mindestens 1 % Bewegung und 100.000 $ Umsatz in fünf Minuten,
ohne Anlegeraufrufe von Kanzleien. Die Richtung ist nachträglich bekannt, es sind also Obergrenzen.

| Klasse | Fälle | nach 10 s gelaufen | nach 60 s gelaufen | Spanne (Median) | Spanne höchstens 0,5 % |
|---|---|---|---|---|---|
| Quartalszahlen per Pressemitteilung (16. bis 23.11.) | 19 | 43 % | 91 % | 4,9 % | 0 von 19 |
| andere Pressemitteilungen (16. bis 23.11.) | 16 | 18 % | 59 % | 2,2 % | 3 von 16 |
| SEC-Pflichtmitteilungen ohne Quartalszahlen (13. bis 23.11.) | 30 | 0 % | 4 % | 1,2 % | 9 von 30 |

Lesehilfe: Bei den Pressemitteilungen ist die Spanne 5 Sekunden nach der Meldung gemessen, bei den
SEC-Meldungen 60 Sekunden nach der Annahme, weil der Bot sie nicht früher sehen kann. Bei den
SEC-Meldungen waren nach 60 Sekunden noch 2,0 Prozentpunkte zu holen, nach 120 Sekunden 1,3; in 20 von
30 Fällen stand nach 30 Minuten noch mindestens die Hälfte. Unter den 16 anderen Pressemitteilungen
steht eine doppelt (Markel).

Beispiele aus den Protokollen:

- **CrowdStrike kauft Zscaler** (23.11., 11:32 Uhr, Business Wire): nach 60 Sekunden −0,3 %, nach
  2 Minuten −2,6 %, nach 5 Minuten −4,4 %, Spanne 0,37 %. Der Bot bekam die Meldung nach 392 Sekunden
  und hat sie als zu alt übersprungen.
- **OneMain** (17.11., 21:37 Uhr, SEC-Meldung in der Handelszeit): nach 2 Minuten +0,2 %, nach 5 Minuten
  +3,2 %, Spanne 0,13 %.
- **Tronox, „Company Update“** (18.11., 20:24 Uhr): nach 60 Sekunden −0,4 %, nach 2 Minuten +8,4 %.
- **Con Edison, Kapitalerhöhung** (16.11., 22:18 Uhr): nach 60 Sekunden 0,0 %, nach 5 Minuten −1,5 %,
  Spanne 0,3 %.

Was die Forschung dazu sagt:

- Für die 50 S&P-500-Aktien mit dem meisten nachbörslichen Handel (2016 bis 2020) fallen die ersten
  Abschlüsse 15 Millisekunden nach den Zahlen. Wer in Richtung der Überraschung einsteigt, verdient zum
  ersten Handelskurs rund 1,1 %, zum tatsächlichen Geld- und Briefkurs rund 0 % und mit fünf Sekunden
  Verspätung rund −0,3 % (Christensen, Timmermann, Veliyev 2026).
- Sprachmodelle sagen die sofortige Reaktion auf Nachrichten meist richtig voraus, aber diese Bewegung
  ist nicht handelbar. Die handelbare Fortsetzung danach ist klein (55 bis 58 % Treffer, 0,3 bis 0,5 %
  je Trade vor Kosten) und verschwindet schon bei 0,2 % Kosten je Trade (Lopez-Lira und Tang, Daten 2021
  bis 2024). Das ist die Größenordnung, mit der zu rechnen ist.
- Fehldeutungen von Lese-Automaten wurden in einer älteren Studie (2009 bis 2012) binnen 30 Sekunden bis
  2 Minuten korrigiert; die Studie nennt die Verzerrungen klein. Das ist das Zeitfenster, in dem ein Bot
  mit gründlichem Lesen etwas beitragen kann.
- Zu kleinen und mittleren Firmen vor und nach der Börse gibt es keine veröffentlichte Messung auf
  Sekundenebene. Dort sind die eigenen Zahlen die beste Quelle.

## 2. Antworten auf die Fragen

### Mehr Nachrichtenquellen?

| Quelle | Preis im Monat | Tempo | Stand und Haken |
|---|---|---|---|
| **SEC-Liste, schnell abgefragt** | 0 $ | 35 bis 60 s nach Annahme (heute 326 s) | Offiziell und erlaubt: eine Abfrage alle 2 s bei einem Limit von 10 je Sekunde. Blind von 23:30 bis 12:00 Uhr, weil die SEC spätere Meldungen erst am nächsten Morgen veröffentlicht. |
| **WireAlpha** | 0 $ | „unter 1 s“ laut Anbieter, ungeprüft | Push-Verbindung, vier Verteiler und SEC. Junger Anbieter ohne erkennbare Lizenz der Verteiler; kann jederzeit wegfallen. Du müsstest ein Konto anlegen. Erst nur messen. |
| **Benzinga über Interactive Brokers** | 35 $, laut Preisliste mit Testzugang | Push | 600 bis 900 ausgewählte Firmenmeldungen am Tag. Ob volle Pressemitteilungen dabei sind, steht nirgends. Braucht ein IBKR-Konto. |
| PR Newswire (läuft) | 0 $ | 98 s im Median, schnellste 16 s | Die Bedingungen verbieten nach ihrem Wortlaut automatische Abrufe; alle 10 Sekunden ist viel. Risiko: Sperre wie bei GlobeNewswire. |
| Business Wire (läuft) | 0 $ | 342 s | Zu spät, obwohl nach eigener Angabe die Hälfte der S&P-500-Firmen dort Kunde ist. |
| GlobeNewswire | – | – | Weist den Server ab. Nicht umgehen. |
| RTPR | 139 $ nach 7 Probetagen | „unter 0,5 s“ laut Anbieter | **Scheidet aus:** Die Bedingungen verbieten die Nutzung in „any automated trading application“, ausdrücklich auch zum Testen von Strategien. Geliefert wird außerdem nur ein Hinweis mit Link, ohne Text. Nur mit schriftlicher Erlaubnis des Anbieters. |
| Benzinga über Massive | 99 $ | Abfrage, kein Push | Nicht empfohlen. |
| Finnhub-Nachrichten (läuft, nur gemessen) | 0 $ | 13 bis 14 Minuten | Kann abgeschaltet werden. |
| Alpacas Nachrichtenstrom | 0 $ | Stunden | Aus dem Handel nehmen (Vorschlag vom 21.11.). |

Benzinga direkt, Dow Jones und die Verteiler selbst verkaufen ihre schnellen Feeds an Banken und
Plattformen, ohne öffentlichen Preis. Alle billigen Nachrichten-APIs (Tiingo, FMP, Marketaux, Stock News
API) sind Abfragedienste ohne Angabe zur Verzögerung; EODHD nennt selbst 15 bis 60 Minuten.

### Bessere Möglichkeit für Kurse?

| Quelle | Preis im Monat | Was sie liefert | Haken |
|---|---|---|---|
| **Probe mit 1 Aktie** (Vorschlag vom 20.11.) | 0 $ | Geld- und Briefkurs im Moment des Einstiegs | Dauer am Konto noch nicht gemessen (geschätzt 1 bis 3 s). |
| **Interactive Brokers**, Netzwerke A, B und C | 4,50 $ | Geld-, Brief- und letzter Kurs aller US-Aktien in Echtzeit | Echtes Konto mit mindestens 500 $ Guthaben, zweite Schnittstelle, IBKR-Programm auf dem Server mit wöchentlicher Anmeldung von Hand. Abdeckung vor und nach der Börse über die Schnittstelle nicht schriftlich bestätigt. |
| Alpaca „Algo Trader Plus“ | 99 $ | dasselbe über die Schnittstelle, die der Bot schon nutzt | Kein Testzeitraum gefunden. Ob es für ein reines Paper-Konto buchbar ist, bestätigt nur der Support. |
| Massive, Databento | je 199 $ | Echtzeit | Kein Vorteil gegenüber den beiden oben. |

Unter 100 $ gibt es sonst nichts, was nachweislich den Kurs des gesamten Marktes vor und nach der Börse
zeigt. Wichtig: Das Paper-Konto führt unabhängig vom Datenpaket zum echten Geld- und Briefkurs aus. Ein
Datenpaket ändert nur, was der Bot vor der Order sieht.

### Anderer Broker?

Nein. Für das Paper-Konto bringt ein Wechsel nichts:

- Interactive Brokers gibt ein Paper-Konto nur zu einem eingezahlten echten Konto und simuliert die
  Ausführung genauso grob wie Alpaca.
- Der eine echte Vorteil dort: Eine Stop-Limit-Order kann auch vor und nach der Börse beim Broker liegen.
  Bei Alpaca muss der Bot den Stop dort selbst überwachen, was er tut.
- Die Schwäche des Paper-Kontos löst kein Broker. Alpaca schreibt selbst, dass die Ordergröße nicht gegen
  die verfügbare Menge geprüft wird. Dagegen hilft nur die abendliche Prüfung jeder Ausführung gegen den
  echten Umsatz, die noch fehlt.
- Falls später doch Echtgeld: Die US-Regel mit 25.000 $ Mindestguthaben für Daytrading gilt seit dem
  4.6.2026 nicht mehr. Ob Alpaca ein echtes Konto für Deutschland eröffnet, bestätigt nur der Support.

### Brauche ich eine News-API, Fundamentaldaten oder Kurse?

Bezahlt im Moment nichts. Kostenlos lässt sich ergänzen:

- **Erwartungen für das nächste Quartal und das Jahr:** Alpha Vantage, kostenloser Schlüssel, 25 Abfragen
  am Tag. Das ist die Lücke von Finnhub. Ob es mit dem kostenlosen Schlüssel für alle Aktien geht, zeigt
  ein Test.
- **Erwartete Kursbewegung aus Optionspreisen:** über Alpacas kostenlose Optionskurse (Näherungswerte,
  für diesen Zweck genau genug). Sie ersetzt die Schätzung der KI, wie stark ein Bericht den Kurs bewegt.
- **Frühere Prognose der Firma:** aus der Anlage der letzten Pflichtmitteilung bei der SEC, von der KI
  am Vorabend herausgezogen. Nur etwa ein Viertel der S&P-500-Firmen gibt überhaupt eine
  Quartalsprognose; „keine“ ist der Normalfall.
- **Übliche Reaktion auf die letzten Berichte:** aus den Kursdaten, die der Bot schon hat.

Nicht kaufen: Finnhub (75 $ für ein Quartal Vorausschau, 200 $ für mehr) und FMP (69 $ für
Quartalsschätzungen). Solange Quartalszahlen wegen der Spanne kaum handelbar sind, lohnt das nicht.

## 3. Das Ziel-Setup

| Schritt | Was passiert | Dauer | Stand |
|---|---|---|---|
| 0. Vorbereiten (Vorabend, Morgen) | Für jede Firma mit Berichtstermin ein Dossier: Erwartungen aus zwei Quellen, frühere Prognose, übliche Reaktion, erwartete Bewegung aus Optionen. Stärkstes Modell, kein Zeitdruck. | – | fehlt |
| 1. Erkennen | SEC-Liste alle 2 s, dazu als zweiter Weg die Firmendaten der SEC. Pressemitteilungen per Push, sobald eine Quelle gemessen schnell ist; bis dahin die RSS-Feeds. Dieselbe Nachricht zählt nur einmal. | SEC 35 bis 60 s, Push unter 1 s | SEC alle 10 min; Doppel-Erkennung läuft |
| 2. Aussortieren (ohne KI) | Kanzlei-Aufrufe, Fonds, Aktien unter 5 $ oder mit zu wenig Umsatz, schon gemeldete Nachrichten. | unter 0,1 s | zum Teil vorhanden |
| 3. Text holen | Volle Pressemitteilung oder Pflichtmitteilung mit Anlage. | 0,2 bis 0,5 s | für Pressemitteilungen vorhanden |
| 4. Lesen | Die KI liefert Meldungstyp, Richtung, Stärke, Sicherheit, die Zahlen als Felder und die Antwort auf „ist das neu?“. Je Meldungstyp eigene Prüffragen. Bei Quartalszahlen rechnet der Code den Abstand zur Erwartung, nicht die KI. | Opus rund 3 s, schnelles Modell rund 1 s | Opus mit vollem Text läuft vor und nach der Börse |
| 5. Markt prüfen | Geld- und Briefkurs holen. Einstieg nur bei Spanne bis 0,5 % und wenn der Kurs weder davongelaufen noch gegen die Bewertung gelaufen ist. | Probe 1 bis 3 s, mit Kursquelle unter 0,1 s | Vorschlag vom 20.11., offen |
| 6. Order | Limit knapp über dem Briefkurs, höchstens 10.000 $. In der Handelszeit Stop und Ziel beim Broker, sonst überwacht der Bot. | unter 0,5 s | vorhanden |
| 7. Nachprüfen | Opus liest parallel nach und kann den Ausstieg auslösen, sobald ein schnelleres Modell auslöst. | – | fehlt; erst mit schneller Quelle nötig |
| 8. Abends | Jede Ausführung gegen den echten Umsatz prüfen. Jede Bewertung gegen den Kursverlauf legen, getrennt nach Meldungstyp. | – | Bewertungen ja, Ausführungen nein |

Vom Erkennen bis zur Order sind das 5 bis 8 Sekunden. Bei SEC-Meldungen steht der Bot damit rund eine
Minute nach der Annahme im Markt, bei den Meldungen der Tabelle oben also meist vor der Bewegung.

**Freischalten nach Meldungstyp.** Die Wochenstatistik zählt je Typ, wie oft die Richtung stimmte.
Gehandelt wird ein Typ erst, wenn mindestens 30 Bewertungen vorliegen und die Trefferquote über der
Schwelle liegt, die die Spanne verlangt (vor und nach der Börse etwa zwei von drei, in der Handelszeit
etwa jeder zweite). Im Paper-Konto kann man lockerer anfangen; das entscheidest du.

## 4. Kosten in Stufen

| Stufe | Inhalt | Kosten |
|---|---|---|
| **0** | SEC schnell, Text der Pflichtmitteilungen lesen, Probe mit 1 Aktie, Sofort-Einstieg auch in der Handelszeit, WireAlpha messen, Dossier aus kostenlosen Daten, Kanzlei-Filter, abendliche Prüfung | 0 $ im Monat; KI geschätzt 3 bis 6 $ pro Handelstag (heute rund 2,50 $) |
| **1** | IBKR-Konto als Datenquelle: Benzinga-Meldungen per Push und Echtzeitkurse | rund 40 $ im Monat (35 + 4,50), dazu 500 $ Guthaben, das dir bleibt |
| statt IBKR-Kursen | Alpacas Datenpaket | 99 $ im Monat |

Stufe 1 erst, wenn Stufe 0 zwei Wochen gelaufen ist und zeigt, dass (a) die KI oft genug richtig liegt und
(b) Fälle wegen später Pressemitteilungen entgehen, die WireAlpha nicht liefert.

## 5. Reihenfolge

1. SEC-Liste alle 2 Sekunden, Text und Anlage holen, bewerten. Erst messen oder gleich handeln: deine
   Entscheidung.
2. Probe mit 1 Aktie und Einstieg nur bei enger Spanne; Sofort-Einstieg auch in der Handelszeit.
3. WireAlpha als gemessene Quelle, sobald der Schlüssel da ist. Nach drei Handelstagen zeigt die
   Statistik, ob es wirklich schneller ist als die RSS-Feeds.
4. Kanzlei-Filter, Alpacas Strom raus, Finnhub-Nachrichten aus.
5. Abendliche Prüfung jeder Ausführung gegen den echten Umsatz.
6. Dossier mit Alpha Vantage und Optionen; Zahlenvergleich im Code.
7. Haiku und Sonnet beim Sofort-Einstieg mitbewerten lassen, Statistik je Meldungstyp.

Punkte 1 bis 3 sind der Kern, der Rest folgt.

## 6. Was du tun müsstest

1. Entscheiden (Abschnitt 7).
2. Bei WireAlpha ein Konto anlegen und den Schlüssel auf dem Server eintragen. Ich ergänze dafür
   `deploy/set-keys.sh`.
3. Bei Alpha Vantage einen kostenlosen Schlüssel holen, ebenso eintragen.
4. Später und nur für Stufe 1: ein Konto bei Interactive Brokers (IBKR Pro, irische Gesellschaft), 500 $
   einzahlen, den Status auf „nicht-professionell“ stellen, Benzinga und die drei Netzwerke buchen.
5. Wenn du RTPR doch willst: dort schriftlich fragen, ob automatischer Handel im Paper-Konto erlaubt wird.

## 7. Entscheidungen

1. **Schwerpunkt verlagern?** Meldungen ohne Quartalszahlen werden die Hauptklasse. Quartalszahlen nach
   Börsenschluss handelt der Bot nur noch bei enger Spanne, sonst misst er sie.
2. **Zweite Fassung des Sofort-Einstiegs** (Vorschlag vom 20.11.: Probe, Spanne bis 0,5 %, auch in der
   Handelszeit): Ja oder Nein? Ohne sie ergibt Punkt 1 keinen Sinn.
3. **SEC-Meldungen:** gleich handeln oder erst zwei Wochen nur bewerten und messen?
4. **WireAlpha und Alpha Vantage:** Legst du die Konten an?
5. **Interactive Brokers als Datenquelle:** jetzt, nach der Messung oder gar nicht?

Ein „Stufe 0 ja“ genügt für die Punkte 1, 2 und 4; zu Punkt 3 brauche ich ein Wort.

## 8. Was sich gegenüber dem Wochenrückblick vom 21.11. ändert

- **RTPR:** Der Rückblick empfahl den Test frühestens ab dem 30.11. Das ziehe ich zurück. Die Bedingungen
  schließen einen Handels-Bot aus, und geliefert wird nur ein Hinweis ohne Text.
- **SEC-Liste:** Der Rückblick nannte sie für den Handel nicht ausreichend. Für Quartalszahlen bleibt es
  dabei: Die Pressemitteilung kommt zuerst. Für Meldungen ohne Quartalszahlen ist sie die beste kostenlose
  Quelle. Neu ist die getrennte Auswertung und der Befund, dass 35 bis 60 Sekunden erreichbar sind.
- **Mehrere kostenlose Verteiler (seit 22.11.):** Die Messung zeigt die Grenze. PR Newswire 98 Sekunden,
  Business Wire 342 Sekunden, GlobeNewswire gesperrt. Schneller wird es auf diesem Weg nicht.

## 9. Was unsicher ist

- Die Klasse „ohne Quartalszahlen“ hat 30 und 16 Fälle aus acht Handelstagen, die Fälle mit enger Spanne
  9 und 3. Das zeigt die Richtung, keine genaue Größe.
- Die Richtung ist in allen Zahlen nachträglich bekannt. Ob die KI sie trifft, ist offen: bisher 26
  Bewertungen mit Richtung in dieser Woche, 10 davon mit gemessenem Verlauf (5 richtig, 1 falsch, 4 ohne
  Bewegung).
- Die 35 Sekunden bei der SEC stammen aus einer fremden Messung in einer ruhigen Morgenstunde. Der eigene
  schnellste Wert war 27 Sekunden. Zwischen 22:00 und 23:30 Uhr kann es langsamer sein.
- WireAlpha und Benzinga über IBKR sind inhaltlich ungeprüft. Beide brauchen eine Messung, bevor sie
  handeln dürfen.
- Paper-Ergebnisse bleiben eine Obergrenze.

## Quellen

Nachrichtenquellen: [RTPR Bedingungen](https://rtpr.io/terms) · [RTPR Preise](https://rtpr.io/pricing) ·
[WireAlpha](https://wirealpha.com/) · [WireAlpha Dokumentation](https://wirealpha.com/docs) ·
[IBKR Preisliste Nachrichten](https://investors.interactivebrokers.com/en/pricing/research-news-services.php?menu=B) ·
[IBKR Nachrichten über die Schnittstelle](https://www.interactivebrokers.com/docs/tws-api/doc/news/introduction.md) ·
[Massive, Benzinga](https://massive.com/partners/benzinga) ·
[PR Newswire Nutzungsbedingungen](https://prnewswire.com/terms-of-use/) ·
[Business Wire Nutzungsbedingungen](https://www.businesswire.com/legal/terms-of-use)

SEC: [Accessing EDGAR Data](https://www.sec.gov/os/accessing-edgar-data) ·
[SEC Developer Resources](https://www.sec.gov/developer) ·
[SEC Webmaster FAQ](https://www.sec.gov/about/webmaster-frequently-asked-questions) ·
[Messung der Liste, Edgrapi 26.10.2026](https://edgrapi.com/blog/sec-edgar-rss-feed) ·
[Meldungen nach 17:30 Uhr, EdgarTools](https://www.edgartools.io/edgar-live-feed-after-hours-filings/)

Kurse und Broker: [Alpaca Marktdaten](https://docs.alpaca.markets/us/docs/about-market-data-api.md) ·
[Alpaca Paper Trading](https://docs.alpaca.markets/us/docs/paper-trading) ·
[Alpaca Orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca.md) ·
[IBKR Marktdaten-Preise](https://www.interactivebrokers.com/en/pricing/market-data-pricing.php?menu=A) ·
[IBKR Stop-Orders außerhalb der Handelszeit](https://www.interactivebrokers.com/support/faq/content/did-market-stop-order-u-35803691) ·
[FINRA Regulatory Notice 26-10](https://www.finra.org/sites/default/files/2026-04/Regulatory-Notice-26-10.pdf)

Fundamentaldaten: [Alpha Vantage Dokumentation](https://www.alphavantage.co/documentation/) ·
[FactSet zur Zahl der Firmen mit Prognose](https://insight.factset.com/record-high-number-of-sp-500-companies-issuing-positive-eps-guidance-for-q3) ·
[Business Wire zu seinen Kunden](https://www.businesswire.com/news/home/20261002569693/en/Business-Wire-Celebrates-65-Years-of-Trusted-Press-Release-Distribution)

Forschung: [Christensen, Timmermann, Veliyev 2026](https://arxiv.org/html/2601.08962v1) ·
[Lopez-Lira und Tang](https://arxiv.org/html/2304.07619v6) ·
[von Beschwitz, Keim, Massa 2013](https://conference.nber.org/conf_papers/f69879.pdf)

Eigene Messwerte: `logs/` (Ereignisse `news_speed`, `news_path`, `sec_item`, `wire_item`),
`tools/weekly_stats.py`, `memory/strategy.md`.
