# nova-kit

Ein autonomer Agent auf einem Raspberry Pi 5. Er ruft per Cron die Claude-Code-CLI (`claude -p`) auf. Er schreibt Marktanalysen, KI-News und Erklärtexte, veröffentlicht sie in Telegram-Kanälen und einem kleinen Blog, führt zwei Papier-Depots und überwacht den Pi. Gedächtnis und Aufgabenliste liegen als Markdown-Dateien in `personality/`.

Das Kit ist ein bereinigter Schnappschuss eines Systems, das seit Mai 2026 läuft. Es ist ein Bastelprojekt und kein Produkt: Code und Prompts sind über Monate gewachsen, die Kommentare sind deutsch.

**Kurz:** Ein KI-Agent, der seit Mai 2026 auf einem Raspberry Pi 5 selbstständig arbeitet und sich selbst kontrolliert.

- Keine Server-Kosten: Pi plus Claude-Abo, optional Brave Search.
- Schreibt täglich Marktanalyse, KI-News und Erklärtexte und veröffentlicht sie auf Telegram und im eigenen Blog.
- Gedächtnis aus Markdown-Dateien (`lessons.md`, `goals.md`, `agenda-log.md`), wöchentliche Reflexion gegen gemessene Ergebnisse.
- Überwacht sich selbst: verpasste Cronjobs, Netzausfall, ablaufender Claude-Login, Temperatur, Disk.
- Misst sich ehrlich: Zwei Papier-Depots laufen gegen den MSCI World. Das LLM-Depot liegt deutlich hinten. Die Bilanz unten zeigt die Zahlen und welche Regeln der Agent daraus gemacht hat.

**Live ansehen:** Das Original veröffentlicht seine Texte, beide Depots und seine Selbstbeschreibung unter https://www.tstratmann.de/nova

## Was drin ist

| Teil | Dateien | Was es tut |
|---|---|---|
| Kern | `py_nova.py` | Startet `claude -p` mit `personality/soul.md` als System-Prompt und `lessons.md` als Gedächtnis. Ohne Argument arbeitet es `personality/heartbeat.md` ab. |
| Freier Lauf | `nova_autonom.py` | `--daily`: Heartbeat plus ein selbst gewählter Schritt. `--weekly`: Reflexion, Performance neu rechnen, `lessons.md`/`goals.md` pflegen. Sichert vorher alle `*.py` und setzt Änderungen zurück, die nicht kompilieren. |
| Inhalte | `market_analysis.py`, `midday_analysis.py`, `ki_news.py`, `erklaer_mir_kurz.py`, `weltlage.py`, `nova_gedanken.py` | Recherchieren über die Brave Search API, schreiben Markdown und senden das Ergebnis an Telegram und/oder den Blog. |
| Depot A | `portfolio.py`, `portfolio_performance.py` | Papier-Depot mit 20.000 € Startkapital. Das LLM wählt Einzelwerte, ausgeführt wird zum Yahoo-Kurs. Gemessen wird gegen Buy & Hold EUNL.DE (MSCI World). |
| Depot B | `depot_b.py` | Papier-Depot mit festen Regeln und ohne LLM: 60 % MSCI World plus 2 × 20 % Momentum-Slots mit 10-Monats-Trendfilter. Monatliches Rebalancing. |
| Betrieb | `pi_monitoring_check.py`, `cron_watch.py`, `claude_auth.py`, `git_weekly_sync.py` | Health-Check (RAM, Temperatur, Disk, Netz, Docker), Kontrolle verpasster Cronjobs, Warnung vor ablaufendem Claude-Login, wöchentlicher Git-Push. Alerts gehen über Gotify. |
| Blog | `blog/` | Node/Express mit SQLite in Docker. Tabs für die Kategorien, Depot-Ansichten, ein anonymer Seitenaufruf-Zähler ohne IP und ohne Cookies. |
| Bot | `telegram_bot.py` | Long-Polling. `/status`, `/report` usw., freier Text geht an `py_nova.py`. |

Nicht im Kit sind das Gedächtnis des Originals (Journal, Lessons, Depot-Historie), der E-Mail-Teil, die Blog-Datenbank und alles, was nur zur Infrastruktur des Originals gehört.

## Was du brauchst

- Raspberry Pi 5 (läuft auf Raspberry Pi OS / Debian arm64). Jeder andere Linux-Rechner mit Cron sollte auch gehen, getestet ist das nicht.
- Python ≥ 3.10. Getestet mit 3.11 (Container) und 3.13 (Pi). Außer der Standardbibliothek wird nur `requests` gebraucht.
- Die Claude-Code-CLI mit Login (Claude-Abo oder API-Key).
- Docker mit Compose, nur für den Blog.
- Optional: einen Telegram-Bot und Kanäle, einen Gotify-Server für Alerts und einen Brave-Search-API-Key für die Recherche.

## Setup

```bash
git clone <URL> ~/nova && cd ~/nova
python3 -m pip install -r requirements.txt   # oder: sudo apt install python3-requests

# Claude Code installieren und einmal interaktiv anmelden
# (Anleitung: https://docs.claude.com/en/docs/claude-code/setup)
claude            # im Programm: /login

cp .env.example .env && nano .env            # Telegram, Gotify, Brave eintragen
python3 portfolio.py                          # legt das leere Depot A an
python3 telegram_utils.py                     # Verbindungstest Telegram

cd blog && docker compose up -d --build && cd ..   # Blog auf Port 3000
```

Dann `personality/soul.md` schreiben. Wer dein Agent ist, was er darf, wie er antworten soll: Diese Datei geht in jeden Aufruf als System-Prompt. `lessons.md`, `goals.md` und `heartbeat.md` dürfen leer starten, der Agent füllt sie selbst.

Zum Schluss die Cronjobs:

```bash
crontab -l                    # vorhandene Einträge sichern, crontab ersetzt sonst alles
nano deploy/crontab.txt       # Pfad /home/pi/nova anpassen, nicht gewünschte Jobs löschen
crontab deploy/crontab.txt
```

In der Vorlage steht `python`. Falls es auf deinem System nur `python3` gibt, ersetze es (oder installiere `python-is-python3`). Den Telegram-Bot als Dienst einrichten: siehe Kopf von `deploy/nova-telegram.service`.

Ausprobieren ohne Senden: Die meisten Skripte kennen `--dry-run`, zum Beispiel `python3 depot_b.py --dry-run` oder `python3 nova_autonom.py --daily --dry-run`.

## Bilanz des Originals

Die Zahlen stammen aus dem laufenden System, Stand **21.11.2026** (Depot A) und **23.11.2026** (Depot B). Es handelt sich um Papiergeld, Steuern sind nicht eingerechnet. Ausgeführt wird zum Yahoo-Kurs, Kosten 1 € pro Order (Depot B zusätzlich 0,10 % Spread).

| | Zeitraum | Rendite | Benchmark EUNL.DE | Abstand |
|---|---|---:|---:|---:|
| Depot A (LLM wählt Einzelwerte) | 19.05.–21.11.2026, 185 Tage | **-16,1 %** | +2,0 % | **-18,1 pp** |
| Depot B (feste ETF-Regeln) | 02.11.–23.11.2026, 3 Wochen | -2,16 % | -2,37 % | +0,21 pp |

Was das heißt:

- **Depot A hat klar verloren.** Von 24 abgeschlossenen Trades waren 6 Gewinner (25 %). Im Schnitt brachten Gewinner +12,7 % und Verlierer -13,6 %, die Erwartung ist also negativ. Das Muster dahinter: Gekauft wurde nach Nachrichten und Hype (Halbleiter, Rüstung, Raumfahrt), verkauft am Stop 15–30 % tiefer. Der letzte Kauf war am 23.09. Seit dem Stop-Verkauf der letzten Position am 23.11. liegt A komplett in Cash. Der Abstand ist seit dem 14.11. kleiner geworden, weil der MSCI World gefallen ist, nicht weil A besser ausgewählt hat.
- **Depot B ist zu jung für ein Urteil.** Drei Wochen sind Rauschen. Ein Backtest der B-Regeln über 2015–2026 lag bei -0,7 pp pro Jahr hinter dem MSCI World, mit 5 pp weniger maximalem Drawdown. Auch gute Regeln liefern also kaum Überrendite.
- **Für die eigenen Prognosen** (verworfene Käufe, Warnungen) gibt es bisher keinen Beleg, dass sie besser als Zufall sind.

Wer das Kit wegen der Trading-Teile installiert, sollte diese Tabelle ernst nehmen. Wer einen ETF-Sparplan hat, war in diesem Zeitraum besser dran.

## Kosten

- **Claude:** Das ist der Hauptposten. Laut Crontab laufen an einem Werktag 5 Claude-Läufe (Markt morgens, Markt mittags, KI-News, Erklärtext, freier Lauf), am Wochenende 3, dazu wöchentlich Reflexion, KI-Wochenanalyse, Blogeintrag und zweimal Weltlage. Ein Lauf darf je nach Skript 15–30 Minuten dauern (Timeout). Ob dein Abo-Kontingent dafür reicht oder was es über die API kostet, habe ich nicht gemessen. Preise: https://claude.com/pricing
- **Brave Search API:** Einige Suchen pro Lauf. Preise und Freikontingent: https://brave.com/search/api/
- **Pi:** Strom und Hardware. Telegram, Gotify (selbst gehostet), Yahoo-Kurse und der Blog kosten nichts.

## Grenzen und Risiken

- **Keine echten Orders.** Beide Depots sind Papierrechnungen in JSON-Dateien. Es gibt keine Anbindung an einen Broker, und das Kit ist keine Anlageberatung.
- **Die Trading-Prompts sind keine neutrale Vorlage.** Die Regeln in `market_analysis.py` und `midday_analysis.py` (Stops, Positionsgrößen, Auswahlkriterien) hat der Betreiber des Originals vorgegeben. Dazu kommt eine Sperre in `portfolio.py`, die der Agent aus eigenen Fehlern abgeleitet hat: kein Kauf nach mehr als +30 % in 60 Handelstagen. Mit diesen Regeln ist die Bilanz oben entstanden.
- **Der Agent hat volle Rechte als dein User.** `claude` läuft mit `--dangerously-skip-permissions`, also ohne Rückfrage bei Shell-Befehlen und Dateiänderungen. `nova_autonom.py` darf sogar die eigenen Skripte ändern: Es sichert sie vorher und setzt Änderungen zurück, die nicht kompilieren, schickt aber nur einen Diff und fragt nicht. Nimm einen eigenen User, keine Secrets außer `.env` und keinen Rechner mit Daten, die du nicht verlieren willst.
- **Heartbeat-Zeilen, die mit `python ` oder `bash ` beginnen, werden direkt als Shell-Befehl ausgeführt.** Schreib nie ungeprüften fremden Text (Mails, Formulare) in `heartbeat.md`.
- **Kursdaten von Yahoo** sind inoffiziell und können ausfallen oder sich ändern. Die Namenssuche trifft manchmal das falsche Symbol, eine Plausibilitätsprüfung fängt grobe Fehltreffer ab.
- **LLM-Texte enthalten Fehler.** Die Inhalte werden ungeprüft veröffentlicht. Für einen eigenen Kanal heißt das: Du haftest für das, was dein Agent schreibt. Impressum und Datenschutz in `blog/public/app.js` sind Platzhalter und müssen ersetzt werden.
- **Getestet** ist ein Erststart in einem frischen Debian-Container (Importe, `depot_b.py --dry-run`, leerer Heartbeat, Blog mit leerer DB). Ein voller Betrieb auf einem zweiten Pi ist nicht getestet.

## Lizenz

MIT, siehe `LICENSE`.
