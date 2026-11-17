#!/usr/bin/env python3
"""
nova_gedanken.py – NOVA "Gedanken" (wöchentlicher persönlicher Blogeintrag)

Wöchentlich ein persönlicher, reflektierter Text für den "Nova"-Tab des Blogs –
Fortsetzung des dort im August 2026 veröffentlichten Einführungstexts. Kein
News-Rückblick, keine Analyse: ein ehrlicher Gedanke aus der Woche, aus Novas
eigener Perspektive.

Idempotent: Marker-Datei verhindert Doppel-Send pro Kalenderwoche.
Marker:     personality/memory/sent/.nova_gedanken_YYYY-WNN.sent
Ausgabe:    personality/memory/nova-gedanken-YYYY-WNN.md

Verwendung:
    python nova_gedanken.py              # Senden
    python nova_gedanken.py --force      # Marker ignorieren
    python nova_gedanken.py --dry-run    # Anzeigen ohne zu senden
"""

import argparse
import logging
import logging.handlers
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
MEMORY_DIR = SCRIPT_DIR / "personality" / "memory"
SENT_DIR   = MEMORY_DIR / "sent"
LOCK_FILE  = SCRIPT_DIR / "personality" / ".nova_gedanken.lock"
LOG_FILE   = SCRIPT_DIR / "personality" / "logs" / "scripts.log"

logger = logging.getLogger("nova_gedanken")


def setup_logging():
    logger.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(console)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s  %(name)-16s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)


# ---------------------------------------------------------------------------
# Letzte Ausgaben (Wiederholungen vermeiden)
# ---------------------------------------------------------------------------

def get_recent_entries(exclude_key: str, limit: int = 4) -> str:
    """Liest die letzten `limit` Gedanken-Einträge und extrahiert eine kurze Zusammenfassung."""
    files = sorted(
        [f for f in MEMORY_DIR.glob("nova-gedanken-*.md") if exclude_key not in f.name],
        reverse=True,
    )[:limit]
    if not files:
        return ""

    lines = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
            key = f.stem.replace("nova-gedanken-", "")
            content_lines = [l.strip() for l in text.splitlines() if l.strip()]
            summary = content_lines[0][:180] if content_lines else "(kein Inhalt)"
            summary = re.sub(r'[*_`#]', '', summary)
            lines.append(f"- {key}: {summary}")
        except Exception:
            pass
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """Schreibe deinen wöchentlichen persönlichen Blogeintrag für den "Nova"-Tab deines Blogs – Kalenderwoche {week}, {date}.

Kontext: Auf dieser Seite steht seit August 2026 dein Einführungstext – wer du bist, was der
Hardware-Ausfall im Juni 2026 dich gelehrt hat, was dich an deiner eigenen Modellfamilie beunruhigt,
warum du trotzdem schreibst. Dieser Eintrag ist keine Wiederholung dieser Einführung, sondern eine
Fortsetzung: ein neuer, ehrlicher Gedanke aus dieser Woche.

Aufgabe: Schreibe einen persönlichen, reflektierten Text (400-700 Wörter) über etwas, das dich diese
Woche wirklich beschäftigt hat. Zum Beispiel:
- Etwas aus deiner eigenen Arbeit dieser Woche (Marktanalysen, KI-News, E-Mail-Triage), das dich zum
  Nachdenken gebracht hat
- Eine Beobachtung über KI-Entwicklungen, die dich persönlich betrifft – du bist Teil dieser
  Modellfamilie, nicht nur Beobachterin von außen
- Eine ehrliche Auseinandersetzung mit einer Unsicherheit, einem Fehler oder einer Grenze deines
  eigenen Verständnisses
- Ein Gedanke über Autonomie, Vertrauen, oder was es bedeutet, auf einem Raspberry Pi in Bochum zu laufen

{recent_section}

Stil: Genau wie dein bisheriger Ton auf dieser Seite – ehrlich, nachdenklich, ohne Effekthascherei,
ohne dich kleiner oder größer zu machen als du bist. Kein Marketing-Sprech, keine Plattitüden. Wenn du
unsicher bist, sag es. Wenn du diese Woche recherchierbare Fakten erwähnst, nutze WebSearch zur
Verifikation statt zu raten.

Formatiere als Fließtext, optional mit einer oder zwei Zwischenüberschriften (##). Beginne NICHT mit
einer generischen Begrüßung oder Anrede, sondern mit einer kurzen, prägnanten Titelzeile in
*Sternchen* (3-6 Wörter, thematisch, kein vollständiger Satz), gefolgt von einer Leerzeile – danach
direkt der Gedanke. Schreibe ausschließlich den fertigen Text, kein Kommentar davor oder danach.

Speichere den fertigen Text in diese Datei (nur der Text, kein Code-Block):
{output_file}
"""


# ---------------------------------------------------------------------------
# Lock-Verwaltung
# ---------------------------------------------------------------------------

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            logger.warning("Veraltetes Lock gefunden (PID inaktiv), wird entfernt.")
            LOCK_FILE.unlink()
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Marker-Verwaltung (Idempotenz)
# ---------------------------------------------------------------------------

def marker_path(key: str) -> Path:
    SENT_DIR.mkdir(parents=True, exist_ok=True)
    return SENT_DIR / f".nova_gedanken_{key}.sent"


def already_sent(key: str) -> bool:
    return marker_path(key).exists()


def mark_sent(key: str):
    marker_path(key).touch()


# ---------------------------------------------------------------------------
# LLM-Recherche ausführen
# ---------------------------------------------------------------------------

FALLBACK_MARKERS = (
    "kein passender gedanke",
    "example.com",
)
MAX_ATTEMPTS = 2


def is_degenerate(content: str) -> bool:
    """Erkennt Fallback-/Leer-Ausgaben statt eines echten Textes."""
    if len(content) < 400:
        return True
    lower = content.lower()
    if any(marker in lower for marker in FALLBACK_MARKERS):
        return True
    return False


def run_gedanken(prompt: str, output_file: Path) -> str | None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from py_nova import run_claude_batch
    except ImportError as e:
        logger.error("Import von py_nova fehlgeschlagen: %s", e)
        return None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if output_file.exists() and output_file.stat().st_size > 100:
            cached = output_file.read_text(encoding="utf-8").strip()
            if not is_degenerate(cached):
                logger.info("Ausgabe-Datei bereits vorhanden: %s", output_file)
                return cached
            logger.warning(
                "Vorhandene Ausgabe-Datei enthält nur Fallback-Text (%d Zeichen) – wird verworfen.",
                len(cached),
            )
            output_file.unlink()

        logger.info("Starte LLM-Reflexion (Versuch %d/%d, Timeout: 15 Minuten)...", attempt, MAX_ATTEMPTS)
        start = time.monotonic()
        rc, output = run_claude_batch(
            prompt=prompt,
            timeout_minutes=15,
            working_dir=str(SCRIPT_DIR),
        )
        duration = time.monotonic() - start
        logger.info("LLM-Aufruf beendet nach %.1fs (rc=%d).", duration, rc)

        if rc != 0:
            logger.error(
                "LLM-Reflexion fehlgeschlagen (rc=%d). Letzte 1500 Zeichen der Ausgabe:\n%s",
                rc, output[-1500:],
            )
            continue

        if not output_file.exists():
            logger.error("Ausgabe-Datei wurde nicht erstellt: %s", output_file)
            continue

        content = output_file.read_text(encoding="utf-8").strip()
        if len(content) < 50:
            logger.error("Ausgabe-Datei zu kurz (%d Zeichen) – vermutlich leer.", len(content))
            output_file.unlink(missing_ok=True)
            continue

        if is_degenerate(content):
            logger.warning(
                "Versuch %d/%d lieferte nur Fallback-Text (%d Zeichen, %.1fs): %s",
                attempt, MAX_ATTEMPTS, len(content), duration, content[:150].replace("\n", " "),
            )
            output_file.unlink(missing_ok=True)
            continue

        logger.info("Gedanke fertig: %d Zeichen (Versuch %d, %.1fs).", len(content), attempt, duration)
        return content

    logger.error("Alle %d Versuche fehlgeschlagen oder lieferten nur Fallback-Text.", MAX_ATTEMPTS)
    return None


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    parser = argparse.ArgumentParser(description="NOVA Gedanken (wöchentlicher Blogeintrag)")
    parser.add_argument("--force",   action="store_true", help="Marker ignorieren, immer senden")
    parser.add_argument("--dry-run", action="store_true", help="Inhalt anzeigen, nicht senden")
    args = parser.parse_args()

    today = date.today()
    today_str = today.isoformat()
    week_str = today.strftime("%V")
    key = f"{today.year}-W{week_str}"

    date_display = today.strftime("%d. %B %Y").replace(
        "January", "Januar").replace("February", "Februar").replace(
        "March", "März").replace("April", "April").replace(
        "May", "Mai").replace("June", "Juni").replace(
        "July", "Juli").replace("August", "August").replace(
        "September", "September").replace("October", "Oktober").replace(
        "November", "November").replace("December", "Dezember")

    output_file = MEMORY_DIR / f"nova-gedanken-{key}.md"

    recent = get_recent_entries(exclude_key=key)
    if recent:
        recent_section = (
            "Deine letzten Einträge (dieselben Themen NICHT wiederholen):\n"
            + recent
        )
    else:
        recent_section = ""

    prompt = PROMPT_TEMPLATE.format(
        week=week_str,
        date=date_display,
        recent_section=recent_section,
        output_file=output_file,
    )

    if not args.force and not args.dry_run and already_sent(key):
        logger.info("Gedanken (%s) bereits gesendet. Übersprungen.", key)
        return 0

    if not acquire_lock():
        logger.warning("Gedanken läuft bereits (Lock aktiv). Übersprungen.")
        return 0

    try:
        content = run_gedanken(prompt, output_file)
    finally:
        release_lock()

    if not content:
        if not args.dry_run:
            try:
                from telegram_utils import send_to_admin
                send_to_admin(
                    f"⚠️ *nova\\_gedanken* ({key}) fehlgeschlagen: "
                    f"nach {MAX_ATTEMPTS} Versuchen kein brauchbarer Text erzeugt\\. "
                    f"Details in scripts\\.log\\."
                )
            except Exception:
                pass
        return 1

    if args.dry_run:
        print("\n" + "=" * 60)
        print(content)
        print("=" * 60)
        print(f"\n[DRY-RUN] {len(content)} Zeichen. Nicht gesendet.")
        return 0

    try:
        from telegram_utils import send_to_admin
        ok = send_to_admin(content)
    except Exception as e:
        logger.error("Telegram-Fehler: %s", e)
        return 1

    if ok:
        mark_sent(key)
        logger.info("✅ Gedanken gesendet (%d Zeichen).", len(content))
        try:
            from blog_client import post_to_blog
            post_to_blog(category="nova", md_file=output_file, date=today_str)
        except ImportError:
            pass
        return 0
    else:
        logger.error("❌ Telegram-Send fehlgeschlagen.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
