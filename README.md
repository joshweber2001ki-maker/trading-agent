# Trading-Bot

Testet zwei kurzfristige Strategien im Alpaca-**Paper-Konto**: Nachrichten-Trading mit
KI-Bewertung und Ausbrüche aus der Eröffnungsspanne. Die Strategie steht in
[`memory/strategy.md`](memory/strategy.md), die Stellschrauben in [`config.toml`](config.toml).

## Auf dem Server

```bash
systemctl status trading-bot          # läuft der Bot?
journalctl -u trading-bot -n 50       # letzte Meldungen
bash /opt/trading-agent/deploy/check.sh   # Verbindungstest
bash /opt/trading-agent/deploy/set-keys.sh  # Zugangsdaten eintragen oder ersetzen
touch /var/lib/trading-bot/HALT       # Notbremse: keine neuen Einstiege (Datei löschen = weiter)
```

Neu installieren oder aktualisieren:

```bash
curl -fsSL https://raw.githubusercontent.com/joshweber2001ki-maker/trading-agent/main/deploy/install.sh | bash
```

## Lokal

```bash
pip install -r requirements.txt pytest
python -m pytest tests -q                 # Logik-Tests mit simuliertem Broker
python tools/check.py                     # Verbindungstest (braucht Schlüssel in der Umgebung)
python tools/weekly_stats.py              # Wochenstatistik aus logs/
```

## Auswertung

Der Server lädt seine Protokolle stündlich nach `logs/` hoch. Samstags schreibt eine geplante
Claude-Aufgabe daraus einen Wochenrückblick nach `reports/`: was gut lief, wo es Probleme
gab und welche Änderungen sie vorschlägt. Umgesetzt wird davon erst etwas nach Freigabe.

Experimentelles Setup, kein Echtgeld.
