# Hinweise für den Wochenrückblick W47

Geschrieben am 20.11.2026. Der Auftrag für den Rückblick stammt vom 19.11. mittags; seitdem hat sich
einiges geändert. Wo dieser Zettel dem Auftrag widerspricht, gilt der Zettel.

1. **Eine zweite Strategie handelt.** Seit dem 19.11., 22:07 Uhr deutscher Zeit läuft der
   Sofort-Einstieg (`bot/fast.py`, Strategie `fast`) vor Börsenbeginn und nach Börsenschluss. Die Werte
   stehen in `config.toml` (`[extended]`, `[fast]`); Josh hat sie am 19.11. freigegeben. Trades davon
   tragen `style: soft` und die Sitzung (`phase`). Die Ereignisse dazu erklärt `CLAUDE.md` unter
   „Protokolle lesen“.
2. **Neuer Abschnitt der Statistik:** „Sofort-Einstieg: Bewertungen gegen den Kursverlauf“. Er zeigt,
   ob die KI die Richtung trifft, auch ohne Trade. Unter 30 Bewertungen mit Richtung ist das nur ein
   Hinweis; die Fallzahl gehört in den Bericht.
3. **Dritte Auswertung** in `memory/strategy.md` (20.11.): Vor und nach der Börse liegt die Spanne
   zwischen Geld- und Briefkurs nach einer Meldung bei 2 bis 4 %. Am gemessenen Kursverlauf
   durchgespielt verliert die erste Fassung dort auch bei richtiger Richtung. Josh hat am 20.11. den
   Vorschlag bekommen, nur noch bei bekannter Spanne bis 0,5 % einzusteigen (Probe mit 1 Aktie) und den
   Sofort-Einstieg ab dem 23.11. auch in der Handelszeit laufen zu lassen. Ob er zugestimmt hat, steht
   in `CLAUDE.md`. Der Rückblick soll diesen Vorschlag an den Daten der Woche prüfen, nicht neu erfinden.
4. **RTPR.** Der Auftrag nennt den 23.11. als Start des Tests. Das gilt nicht mehr: Josh hat nicht
   entschieden, geplant wird mit dem 30.11. Die Empfehlung vom 20.11. lautet, zuerst die Spanne zu
   prüfen und die Trefferquote der KI zu messen. Die Empfehlung des Rückblicks soll einbeziehen, was
   nach der Spanne von einer Bewegung bleibt, nicht nur, wie viel der Bewegung noch zu holen wäre.
5. **SEC-Liste.** Der Auftrag ordnet ihr 40 bis 60 Sekunden zu. Gemessen wurde: Der Bot sieht neue
   Meldungen im Median 305 Sekunden nach der Annahme, und bei Quartalszahlen war die Pressemitteilung
   in 16 von 17 Fällen zuerst da (im Median 205 Sekunden früher).
6. **Lücken in den Daten.** Der Tagesabschluss vom 19.11. zählt Meldungen erst ab dem Neustart um
   21:07 UTC. Am 19.11. bekam die SEC-Liste von etwa 11:00 bis 21:00 UTC keine Antwort (10 Fehler
   `edgar`), und der Nachrichtenstrom brach um 11:43 UTC für 45 Sekunden ab (13 Fehler `news_ws`).
   Die Statistik rechnet, was nach Mitternacht UTC geschieht (Ende der Abendsitzung, nächtliche
   Messung), zum Handelstag davor.
