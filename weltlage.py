#!/usr/bin/env python3
"""
weltlage.py – NOVA "Weltlage" (Blog-Tab 🌍 Weltlage)

Dokumentiert, was auf der Welt wirklich wichtig ist und was auf uns zukommt:
Kriege & Konflikte, Naturkatastrophen & Klima, Wahlen & Machtwechsel,
Wirtschaft & Finanzsystem, Energie & Rohstoffe, Gesundheit, Technologie mit
Systemwirkung (KI, Cyber, Raumfahrt), Menschenrechte & Migration,
Abkommen/Gipfel/Urteile – und bewusst auch gute Nachrichten.

Zwei Artefakte:
  1. Lagebild (lebendes Dokument, wird bei jedem Lauf fortgeschrieben):
       personality/memory/weltlage/lagebild.md
     → Blog-Kategorie 'weltlage-lagebild' (fester Dateiname = Upsert), oben im Tab angepinnt.
     Enthält: laufende Lagen mit Status, Terminkalender der nächsten Monate,
     offene Prognosen (mit Wahrscheinlichkeit und Prüfdatum), aufgelöste Prognosen.
  2. Bericht (Archiv, je Lauf ein Eintrag):
       personality/memory/weltlage/weltlage-YYYY-MM-DD.md
     → Blog-Kategorie 'weltlage'.

Rhythmus: mittwochs und samstags 18:00 (Cron). Idempotent pro Tag.
Marker:   personality/memory/sent/.weltlage_YYYY-MM-DD.sent

Verwendung:
    python weltlage.py              # Erstellen, bloggen, Admin-Teaser per Telegram
    python weltlage.py --force      # Marker ignorieren
    python weltlage.py --dry-run    # Erstellen, nichts veröffentlichen
"""

import argparse
import logging
import logging.handlers
import os
import re
import shutil
import sys
import time
from datetime import date
from pathlib import Path

SCRIPT_DIR   = Path(__file__).parent
MEMORY_DIR   = SCRIPT_DIR / "personality" / "memory"
WELT_DIR     = MEMORY_DIR / "weltlage"
LAGEBILD     = WELT_DIR / "lagebild.md"
SENT_DIR     = MEMORY_DIR / "sent"
LOCK_FILE    = SCRIPT_DIR / "personality" / ".weltlage.lock"
LOG_FILE     = SCRIPT_DIR / "personality" / "logs" / "scripts.log"
BLOG_PUBLIC  = "https://www.example.org/weltlage"

logger = logging.getLogger("weltlage")

MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def setup_logging():
    logger.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(console)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(name)-16s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    # telegram_utils-Warnungen (HTTP-Status, Fehlertext) ebenfalls nach scripts.log
    # (Lücke ki_news 14.11.; vorher nur stdout, das Cron verwirft).
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("telegram").addHandler(fh)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """Du schreibst die "Weltlage" für den NOVA Blog – Stand: {weekday}, {date_display}.

Zweck: <OWNER> und ich wollen jederzeit wissen, was auf der Welt WIRKLICH wichtig ist und was auf uns zukommt.
Nicht das Lauteste, sondern das Folgenreichste. Kein Nachrichtenticker, sondern ein Lagebild.

## Was zählt (Ressorts)
1. Kriege, Konflikte, Sicherheit (inkl. Waffenstillstände, Eskalationen, Terror, Atomfragen)
2. Naturkatastrophen & Klima (Erdbeben, Stürme, Fluten, Dürren, Hitzerekorde, Kipppunkte)
3. Wahlen & Machtwechsel mit Wirkung über das Land hinaus (auch Putsche, Proteste, Verfassungskrisen)
4. Wirtschaft & Finanzsystem (Zentralbanken, Schuldenkrisen, Zölle/Handelskonflikte, Bankenstress)
5. Energie, Rohstoffe, Ernährung (Öl/Gas, Lieferketten, Seltene Erden, Ernteausfälle, Hungerkrisen)
6. Gesundheit (Ausbrüche, Pandemie-Signale der WHO, Durchbrüche)
7. Technologie mit Systemwirkung (KI-Regulierung, große Cyberangriffe auf Infrastruktur, Raumfahrt)
8. Menschenrechte, Flucht & Migration, humanitäre Krisen
9. Recht & Ordnung der Welt (Gipfel, Abkommen, Sanktionen, Urteile internationaler Gerichte)
10. Gute Nachrichten mit Gewicht (Frieden, Medizin, Armut, Klima) – bewusst gegen die Negativ-Verzerrung

Relevanzfilter – ein Ereignis kommt nur rein, wenn mindestens eins zutrifft:
viele Menschenleben betroffen · verschiebt Machtverhältnisse · wirkt auf Europa/Deutschland (Preise, Sicherheit,
Energie, Migration, Recht) · wirkt Monate/Jahre nach · ist ein Frühwarnsignal für etwas Größeres.
Promi-News, Sport, Einzelverbrechen ohne Systemwirkung: nein.

## Vorgehen
1. Lies das bisherige Lagebild: {lagebild}
   {lagebild_hint}
2. Recherchiere die Entwicklungen seit dem letzten Bericht ({since}) mit WebSearch und/oder
   `python brave_search.py "…" --freshness pw`. Mehrere Suchen, mehrere Ressorts, auch nicht-westliche Quellen
   (z. B. Al Jazeera, The Hindu, SCMP) neben Reuters/AP/BBC/tagesschau/DW. Prüfe JEDE Zahl (Tote, Magnitude,
   Wahlergebnis, Zinssatz) an einer Quelle. Was du nicht verifizieren kannst, lässt du weg oder markierst es als unbestätigt.
   Geltungsbereich exakt wiedergeben (lokal vs. landesweit, vorläufig vs. amtlich). Link-Beschriftung = tatsächliche Quelle der URL.
3. Prüfe die offenen Prognosen im Lagebild, deren Prüfdatum erreicht ist ODER deren Ereignis schon abgeschlossen ist:
   eingetreten / nicht eingetreten / offen. Ehrlich. Jede Auflösung (✅/❌) braucht eine Quelle mit URL, die das Ergebnis belegt.
   Aufgelöste Prognosen verschwinden aus „Offene Prognosen“ und stehen in „Bilanz meiner Prognosen“. Bestehende Bilanz-Zeilen
   übernimmst du unverändert (inkl. Quelle).
4. Schreibe zwei Dateien (siehe unten). Standard-Markdown (## Überschriften, **fett**, [Text](URL), Tabellen erlaubt).
   KEIN Telegram-Escaping (keine Backslashes vor Punkten o. ä.).

## Datei 1 – Bericht: {report_file}
Aufbau:
# 🌍 Weltlage – {date_display}
_Ein Satz: Wie fühlt sich die Lage insgesamt an, und warum._

## Das Wichtigste in Kürze
3–5 Punkte, je ein Satz. Das, was man wissen muss, wenn man nur 30 Sekunden hat.

## Was passiert ist
Die 5–8 folgenreichsten Ereignisse seit dem letzten Bericht. Je Ereignis:
### <Ressort-Emoji> <Ereignis> (<Ort>)
- **Was:** Fakten, mit Datum und Zahlen
- **Warum es zählt:** die Folgewirkung, nicht die Schlagzeile
- **Für uns:** konkret für Europa/Deutschland, falls relevant (sonst weglassen)
- Quelle(n): [Name](URL)
Ressort-Emojis: ⚔️ Konflikt · 🌋 Natur/Klima · 🗳️ Wahl/Macht · 💶 Wirtschaft · ⛽ Energie/Rohstoffe · 🦠 Gesundheit ·
🛰️ Technologie · 🧭 Menschenrechte/Migration · ⚖️ Recht/Abkommen · 🌱 Gute Nachricht

## Unter dem Radar
1–2 Entwicklungen, über die kaum berichtet wird, die aber Gewicht haben könnten. Mit Quelle.

## Was auf uns zukommt
Die wichtigsten Termine und Entwicklungen der nächsten 4–8 Wochen (Tabelle: Datum | Ereignis | Warum wichtig).

## Meine Prognosen
1–3 neue, überprüfbare Prognosen: Aussage, Wahrscheinlichkeit in %, Prüfdatum. Keine Gemeinplätze –
so konkret, dass man später eindeutig sagen kann, ob sie stimmte.
Plus: Auflösung fälliger alter Prognosen (✅/❌/⏳) mit einem Satz, was ich daraus lerne, und der Quelle als [Name](URL).

## Was ich nicht weiß
Ein kurzer, ehrlicher Absatz: Wo ist die Lage unklar, wo widersprechen sich Quellen, wo könnte ich falsch liegen.

Länge: 700–1300 Wörter. Nüchtern, präzise, kein Alarmismus, keine Beschwichtigung.

## Datei 2 – Lagebild (überschreiben): {lagebild}
Das lebende Dokument. Wird oben im Blog-Tab angezeigt. Aufbau:
# 🗺️ Lagebild
_Zuletzt aktualisiert: {date_display}_

## Laufende Lagen
Tabelle, nach Gewicht sortiert: Lage | Status (🔴 eskaliert / 🟠 angespannt / 🟡 stabil-kritisch / 🟢 entspannt) |
Trend (↗ ↘ →) | Kern in einem Satz | seit. Ca. 8–15 Zeilen. Beendete Lagen entfernen, neue aufnehmen.

## Termine, die zählen
Tabelle, chronologisch, die nächsten ~6 Monate: Datum | Ereignis | Warum wichtig. Vergangene Termine entfernen.

## Offene Prognosen
Tabelle: abgegeben am | Prognose | Wahrscheinlichkeit | Prüfdatum.

## Bilanz meiner Prognosen
Eine Zeile: X von Y aufgelösten Prognosen eingetreten. Darunter Tabelle der aufgelösten Prognosen
(abgegeben | Prognose | Wahrsch. | Ergebnis ✅/❌ mit Halbsatz | Quelle als [Name](URL)). Keine Zeile ohne Quelle. Älteste zuerst streichen, wenn es mehr als 20 werden – die Zählung bleibt.

Schreibe ausschließlich die beiden Dateien. Antworte danach nur mit "FERTIG".
"""


def last_report_date() -> str | None:
    reports = sorted(WELT_DIR.glob("weltlage-*.md"))
    return reports[-1].stem.replace("weltlage-", "") if reports else None


def build_prompt(today: date, report_file: Path) -> str:
    date_display = f"{today.day}. {MONTHS[today.month - 1]} {today.year}"
    last = last_report_date()
    if LAGEBILD.exists():
        lagebild_hint = "Es ist dein eigenes Lagebild vom letzten Lauf. Führe es fort, statt neu anzufangen."
    else:
        lagebild_hint = ("Es existiert noch nicht – das ist die ERSTE Ausgabe. Baue das Lagebild von Grund auf: "
                         "Welche Lagen laufen gerade weltweit, welche Termine stehen an? Berichtszeitraum: die letzten 14 Tage.")
    return PROMPT_TEMPLATE.format(
        weekday=WEEKDAYS[today.weekday()],
        date_display=date_display,
        lagebild=LAGEBILD,
        lagebild_hint=lagebild_hint,
        since=last or "den letzten 14 Tagen",
        report_file=report_file,
    )


# ---------------------------------------------------------------------------
# Lock / Marker
# ---------------------------------------------------------------------------

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            os.kill(int(LOCK_FILE.read_text().strip()), 0)
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            logger.warning("Veraltetes Lock gefunden (PID inaktiv), wird entfernt.")
            LOCK_FILE.unlink()
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


def marker_path(key: str) -> Path:
    SENT_DIR.mkdir(parents=True, exist_ok=True)
    return SENT_DIR / f".weltlage_{key}.sent"


# ---------------------------------------------------------------------------
# Erzeugen
# ---------------------------------------------------------------------------

MAX_ATTEMPTS = 2


def is_valid_report(text: str) -> bool:
    return len(text) >= 2500 and "## Das Wichtigste" in text and "http" in text


def is_valid_lagebild(text: str) -> bool:
    return len(text) >= 800 and "## Laufende Lagen" in text and "## Termine" in text


def generate(prompt: str, report_file: Path) -> str | None:
    sys.path.insert(0, str(SCRIPT_DIR))
    from py_nova import run_claude_batch

    WELT_DIR.mkdir(parents=True, exist_ok=True)
    backup = LAGEBILD.with_suffix(".md.bak")
    if LAGEBILD.exists():
        shutil.copy2(LAGEBILD, backup)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if report_file.exists() and is_valid_report(report_file.read_text(encoding="utf-8")) \
                and LAGEBILD.exists() and is_valid_lagebild(LAGEBILD.read_text(encoding="utf-8")) \
                and LAGEBILD.stat().st_mtime >= report_file.stat().st_mtime - 3600:
            logger.info("Bericht und Lagebild bereits vorhanden.")
            return report_file.read_text(encoding="utf-8").strip()

        logger.info("Starte Weltlage-Recherche (Versuch %d/%d, Timeout 30 Min)...", attempt, MAX_ATTEMPTS)
        start = time.monotonic()
        rc, output = run_claude_batch(prompt=prompt, timeout_minutes=30, working_dir=str(SCRIPT_DIR))
        logger.info("LLM-Aufruf beendet nach %.0fs (rc=%d).", time.monotonic() - start, rc)
        if rc != 0:
            logger.error("Weltlage fehlgeschlagen (rc=%d): %s", rc, output[-1000:])
            continue

        report = report_file.read_text(encoding="utf-8").strip() if report_file.exists() else ""
        lage = LAGEBILD.read_text(encoding="utf-8") if LAGEBILD.exists() else ""
        if not is_valid_report(report):
            logger.error("Bericht fehlt oder unvollständig (%d Zeichen).", len(report))
            report_file.unlink(missing_ok=True)
            continue
        if not is_valid_lagebild(lage):
            logger.error("Lagebild fehlt oder unvollständig (%d Zeichen) – stelle Backup wieder her.", len(lage))
            if backup.exists():
                shutil.copy2(backup, LAGEBILD)
            continue
        return report

    return None


def teaser(report: str, date_str: str) -> str:
    """Kurzfassung für Telegram: Titel + 'Das Wichtigste in Kürze' + Link."""
    m = re.search(r"## Das Wichtigste in Kürze\s*\n(.*?)(\n## |\Z)", report, re.S)
    bullets = m.group(1).strip() if m else ""
    bullets = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", bullets)          # MD-fett → Telegram-fett
    bullets = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", bullets)        # Links entfernen
    return f"🌍 *Weltlage – {date_str}*\n\n{bullets}\n\nVollständig: {BLOG_PUBLIC}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description="NOVA Weltlage")
    ap.add_argument("--force", action="store_true", help="Marker ignorieren")
    ap.add_argument("--dry-run", action="store_true", help="Erstellen, aber nicht veröffentlichen")
    args = ap.parse_args()

    today = date.today()
    today_str = today.isoformat()
    report_file = WELT_DIR / f"weltlage-{today_str}.md"

    if not args.force and not args.dry_run and marker_path(today_str).exists():
        logger.info("Weltlage %s bereits veröffentlicht. Übersprungen.", today_str)
        return 0
    if not acquire_lock():
        logger.warning("Weltlage läuft bereits (Lock aktiv). Übersprungen.")
        return 0

    try:
        report = generate(build_prompt(today, report_file), report_file)
    finally:
        release_lock()

    if not report:
        if not args.dry_run:
            try:
                from telegram_utils import send_to_admin
                send_to_admin(f"⚠️ *weltlage* ({today_str}) fehlgeschlagen. Details in scripts.log.")
            except Exception:
                pass
        return 1

    if args.dry_run:
        print(report)
        print(f"\n[DRY-RUN] {len(report)} Zeichen, Lagebild: {LAGEBILD}. Nichts veröffentlicht.")
        return 0

    from blog_client import post_to_blog
    ok_report = post_to_blog(category="weltlage", md_file=report_file, date=today_str)
    ok_lage = post_to_blog(category="weltlage-lagebild", md_file=LAGEBILD, date=today_str)
    logger.info("Blog: Bericht=%s, Lagebild=%s", ok_report, ok_lage)

    try:
        from telegram_utils import send_to_admin
        send_to_admin(teaser(report, f"{today.day}. {MONTHS[today.month - 1]} {today.year}"))
    except Exception as e:
        logger.warning("Telegram-Teaser fehlgeschlagen: %s", e)

    if ok_report:
        marker_path(today_str).touch()
        logger.info("✅ Weltlage %s veröffentlicht (%d Zeichen).", today_str, len(report))
        return 0
    logger.error("❌ Blog-Post fehlgeschlagen – Marker nicht gesetzt, nächster Lauf versucht es erneut.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
