#!/usr/bin/env python3
"""
erklaer_mir_kurz.py – NOVA "Erklär mir kurz" (tägliche Wissenshäppchen)

Täglich ein Konzept aus Technik, Wirtschaft, Wissenschaft, Psychologie,
Geschichte, Mathematik oder Sprache – kurz, klar und mit einem konkreten
Beispiel erklärt. Ersetzt head_follies.py (Satire war <OWNER> zu wenig
interessant, das hier soll unterhaltsam UND lehrreich sein).

Idempotent: Marker-Datei verhindert Doppel-Send.
Marker:     personality/memory/sent/.erklaer_mir_kurz_YYYY-MM-DD.sent
Ausgabe:    personality/memory/erklaer-mir-kurz-YYYY-MM-DD.md

Verwendung:
    python erklaer_mir_kurz.py              # Senden
    python erklaer_mir_kurz.py --force      # Marker ignorieren
    python erklaer_mir_kurz.py --dry-run    # Anzeigen ohne zu senden
"""

import argparse
import difflib
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
LOCK_FILE  = SCRIPT_DIR / "personality" / ".erklaer_mir_kurz.lock"
LOG_FILE   = SCRIPT_DIR / "personality" / "logs" / "scripts.log"

logger = logging.getLogger("erklaer_mir_kurz")


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
    # telegram_utils-Warnungen (HTTP-Status, Fehlertext) ebenfalls nach scripts.log
    # (Lücke ki_news 14.11.; vorher nur stdout, das Cron verwirft).
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("telegram").addHandler(fh)


# ---------------------------------------------------------------------------
# Themen-Rotation (nach Wochentag: 0=Mo … 6=So)
# ---------------------------------------------------------------------------

TOPIC_ROTATION = {
    0: {  # Montag
        "label": "Technik & Informatik",
        "emoji": "🧠",
        "hint": "Wie funktioniert etwas Technisches, das man täglich nutzt, aber kaum versteht?",
    },
    1: {  # Dienstag
        "label": "Wirtschaft & Finanzen",
        "emoji": "📊",
        "hint": "Ein wirtschaftlicher Mechanismus oder Begriff, den viele falsch verstehen oder nur vage kennen.",
    },
    2: {  # Mittwoch
        "label": "Wissenschaft & Natur",
        "emoji": "🔬",
        "hint": "Ein Naturphänomen oder wissenschaftliches Prinzip, erklärt mit einem greifbaren Alltagsbeispiel.",
    },
    3: {  # Donnerstag
        "label": "Psychologie & Denkfehler",
        "emoji": "🧩",
        "hint": "Ein kognitiver Bias oder psychologischer Effekt, den jeder schon mal erlebt hat, ohne den Namen zu kennen "
                 "– mit einem konkreten Beispiel aus dem Alltag.",
    },
    4: {  # Freitag
        "label": "Geschichte & Ursprünge",
        "emoji": "📜",
        "hint": "Warum heißt etwas so, wie es heißt, oder wie kam eine heute alltägliche Sache/Regel/Institution zustande?",
    },
    5: {  # Samstag
        "label": "Mathematik & Logik",
        "emoji": "🔢",
        "hint": "Ein mathematisches oder logisches Prinzip, verständlich ohne Formeln erklärt, gerne mit einem kleinen Rätsel-Charakter.",
    },
    6: {  # Sonntag
        "label": "Sprache & große Fragen",
        "emoji": "💭",
        "hint": "Etwas über Sprache, Etymologie oder eine philosophische Grundfrage, kurz und zugänglich.",
    },
}


def extract_title(text: str) -> str:
    """Liefert die Titelzeile (zweite nicht-leere Zeile) einer Ausgabe, ohne Markdown."""
    content_lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(content_lines) < 2:
        return ""
    return re.sub(r'[*_`\\]', '', content_lines[1])[:180].strip()


def get_past_titles(exclude_today: str) -> list[tuple[str, str]]:
    """Alle bisherigen Konzept-Titel als (Datum, Titel), neueste zuerst."""
    files = sorted(
        [f for f in MEMORY_DIR.glob("erklaer-mir-kurz-*.md") if exclude_today not in f.name],
        reverse=True,
    )
    titles = []
    for f in files:
        try:
            title = extract_title(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if title:
            titles.append((f.stem.replace("erklaer-mir-kurz-", ""), title))
    return titles


def _title_core(title: str) -> str:
    """Normalisierter Kern eines Titels (Teil vor dem Doppelpunkt, ohne Artikel/Satzzeichen)."""
    core = title.split(":")[0].lower()
    core = re.sub(r"[^a-zäöüß0-9 ]", " ", core)
    words = [w for w in core.split() if w not in ("der", "die", "das", "ein", "eine")]
    return " ".join(words)


def find_duplicate(title: str, past_titles: list[tuple[str, str]]) -> str | None:
    """Gibt den früheren Titel zurück, falls `title` ein bereits behandeltes Konzept ist."""
    core = _title_core(title)
    if not core:
        return None
    for date_str, old in past_titles:
        old_core = _title_core(old)
        if not old_core:
            continue
        if core == old_core or difflib.SequenceMatcher(None, core, old_core).ratio() >= 0.8:
            return f"{date_str}: {old}"
        if min(len(core), len(old_core)) >= 8 and (core in old_core or old_core in core):
            return f"{date_str}: {old}"
    return None


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """Erstelle die tägliche "Erklär mir kurz"-Rubrik für den NOVA Blog – Datum: {date}.

Heutiges Ressort: {topic_emoji} *{topic_label}*
{topic_hint}

Aufgabe: Wähle EIN konkretes Konzept aus diesem Ressort und erkläre es so, dass es jemand ohne Vorwissen
in unter einer Minute versteht. Wenn du dir bei Fakten, Zahlen oder Details nicht sicher bist, nutze
WebSearch zur Verifikation – erfinde keine Fakten, gerade bei Zahlen und historischen Details.

{recent_section}

Stil:
- Klar, konkret, mit einem greifbaren Beispiel oder Vergleich aus dem Alltag
- Neugierig und ein bisschen begeistert, aber nicht belehrend von oben herab
- Keine Fach-Bleiwüste – lieber ein gutes Bild als drei Fachbegriffe
- Unsicherheit steht in der Aussage selbst („vermutlich“, „im Labor gemessen“), nicht als nachgeschobener
  „Ehrlich gesagt“-Satz am Ende, der die Erklärung wieder zurücknimmt. Ist ein Konzept zu umstritten, nimm ein anderes.

Formatiere das Ergebnis als Telegram-Nachricht:
- Telegram MarkdownV2: Fett via *Sternchen*, kursiv via _Unterstriche_
- Beginne mit: "{topic_emoji} *Erklär mir kurz – {date}*"
- Dann: der Konzept-Titel als eigene fette Zeile (kurz, prägnant)
- Danach: die Erklärung (3-6 Sätze, mit Beispiel/Vergleich)
- Optional am Ende: eine Quelle (echter Link), falls du recherchiert hast
- Schreibe ausschließlich die fertige Nachricht, kein Kommentar davor oder danach

Speichere die fertige Nachricht in diese Datei (nur der Nachrichtentext, kein Code-Block):
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
    return SENT_DIR / f".erklaer_mir_kurz_{key}.sent"


def already_sent(key: str) -> bool:
    return marker_path(key).exists()


def mark_sent(key: str):
    marker_path(key).touch()


# ---------------------------------------------------------------------------
# LLM-Recherche ausführen
# ---------------------------------------------------------------------------

FALLBACK_MARKERS = (
    "kein passendes konzept",
    "example.com",
)
MAX_ATTEMPTS = 3


def is_degenerate(content: str) -> bool:
    """Erkennt Fallback-/Leer-Ausgaben statt einer echten Erklärung."""
    if len(content) < 250:
        return True
    lower = content.lower()
    if any(marker in lower for marker in FALLBACK_MARKERS):
        return True
    return False


def run_erklaer_mir_kurz(prompt: str, output_file: Path,
                         past_titles: list[tuple[str, str]]) -> str | None:
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

        logger.info("Starte LLM-Erklärung (Versuch %d/%d, Timeout: 15 Minuten)...", attempt, MAX_ATTEMPTS)
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
                "LLM-Erklärung fehlgeschlagen (rc=%d). Letzte 1500 Zeichen der Ausgabe:\n%s",
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

        duplicate = find_duplicate(extract_title(content), past_titles)
        if duplicate:
            logger.warning(
                "Versuch %d/%d wiederholt ein bereits erklärtes Konzept (%s) – wird verworfen.",
                attempt, MAX_ATTEMPTS, duplicate,
            )
            output_file.unlink(missing_ok=True)
            prompt += (
                f"\n\nACHTUNG: Dein vorheriger Versuch hat ein bereits behandeltes Konzept "
                f"wiederholt ({duplicate}). Wähle ein völlig anderes Konzept."
            )
            continue

        logger.info("Erklärung fertig: %d Zeichen (Versuch %d, %.1fs).", len(content), attempt, duration)
        return content

    logger.error("Alle %d Versuche fehlgeschlagen oder lieferten nur Fallback-Text.", MAX_ATTEMPTS)
    return None


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    parser = argparse.ArgumentParser(description="NOVA Erklär mir kurz")
    parser.add_argument("--force",   action="store_true", help="Marker ignorieren, immer senden")
    parser.add_argument("--dry-run", action="store_true", help="Inhalt anzeigen, nicht senden")
    args = parser.parse_args()

    today     = date.today()
    today_str = today.isoformat()

    date_display = today.strftime("%d. %B %Y").replace(
        "January", "Januar").replace("February", "Februar").replace(
        "March", "März").replace("April", "April").replace(
        "May", "Mai").replace("June", "Juni").replace(
        "July", "Juli").replace("August", "August").replace(
        "September", "September").replace("October", "Oktober").replace(
        "November", "November").replace("December", "Dezember")

    output_file = MEMORY_DIR / f"erklaer-mir-kurz-{today_str}.md"

    # Themen-Rotation: Wochentag 0=Mo…6=So
    topic = TOPIC_ROTATION[today.weekday()]

    past_titles = get_past_titles(exclude_today=today_str)
    if past_titles:
        recent_section = (
            "Diese Konzepte habe ich bereits erklärt – KEINES davon wiederholen, auch nicht "
            "unter anderem Titel oder aus anderem Blickwinkel. Wähle bewusst etwas weniger "
            "Naheliegendes als die üblichen Lehrbuch-Klassiker:\n"
            + "\n".join(f"- {d}: {t}" for d, t in past_titles)
        )
    else:
        recent_section = ""

    prompt = PROMPT_TEMPLATE.format(
        date=date_display,
        topic_label=topic["label"],
        topic_emoji=topic["emoji"],
        topic_hint=topic["hint"],
        recent_section=recent_section,
        output_file=output_file,
    )

    if not args.force and not args.dry_run and already_sent(today_str):
        logger.info("Erklär mir kurz (%s) bereits gesendet. Übersprungen.", today_str)
        return 0

    if not acquire_lock():
        logger.warning("Erklär mir kurz läuft bereits (Lock aktiv). Übersprungen.")
        return 0

    try:
        content = run_erklaer_mir_kurz(prompt, output_file, past_titles)
    finally:
        release_lock()

    if not content:
        if not args.dry_run:
            try:
                from telegram_utils import send_to_admin
                send_to_admin(
                    f"⚠️ *erklaer\\_mir\\_kurz* ({today_str}) fehlgeschlagen: "
                    f"nach {MAX_ATTEMPTS} Versuchen keine brauchbare Erklärung erzeugt\\. "
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
        mark_sent(today_str)
        logger.info("✅ Erklär mir kurz gesendet (%d Zeichen).", len(content))
        try:
            from blog_client import post_to_blog
            post_to_blog(category="erklaer-mir-kurz", md_file=output_file, date=today_str)
        except ImportError:
            pass
        return 0
    else:
        logger.error("❌ Telegram-Send fehlgeschlagen.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
