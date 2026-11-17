#!/usr/bin/env python3
"""
ki_news.py – NOVA KI-News Kanal (@dein_ki_kanal)

Zwei Modi:
  --daily    Tägliche KI-News-Zusammenfassung (täglich 20:00 Uhr)
  --weekly   Wöchentliche KI-Tiefenanalyse (sonntags 17:00 Uhr)

Idempotent: Marker-Dateien verhindern Doppel-Sends.
Marker:     personality/memory/sent/.ki_news_daily_YYYY-MM-DD.sent
            personality/memory/sent/.ki_news_weekly_YYYY-WNN.sent
Ausgabe:    personality/memory/ki-news-YYYY-MM-DD.md
            personality/memory/ki-weekly-YYYY-WNN.md

Verwendung:
    python ki_news.py --daily              # Tägliche News senden
    python ki_news.py --weekly             # Wöchentliche Tiefenanalyse senden
    python ki_news.py --daily --force      # Marker ignorieren, immer senden
    python ki_news.py --daily --dry-run    # Anzeigen ohne zu senden
"""

import argparse
import logging
import logging.handlers
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from brave_search import search, format_results

SCRIPT_DIR = Path(__file__).parent
MEMORY_DIR = SCRIPT_DIR / "personality" / "memory"
SENT_DIR = MEMORY_DIR / "sent"
LOCK_FILE = SCRIPT_DIR / "personality" / ".ki_news.lock"
KI_NEWS_CHANNEL = "@dein_ki_kanal"
LOG_FILE = SCRIPT_DIR / "personality" / "logs" / "scripts.log"

logger = logging.getLogger("ki_news")


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
# Prompts
# ---------------------------------------------------------------------------

DAILY_PROMPT_TEMPLATE = """Erstelle eine tägliche KI-News-Zusammenfassung für den Telegram-Kanal @dein_ki_kanal.

Hier sind aktuelle Suchergebnisse der letzten 24 Stunden (Brave Search):

{search_results}

{recent_topics_section}

Themen (nach Relevanz):
- Neue LLM-Modelle und Releases (OpenAI, Anthropic, Google, Meta, Mistral, Qwen, etc.)
- Neue KI-Tools, Plattformen und Integrationen (z.B. Cursor, Windsurf, Ollama, LM Studio, ComfyUI, n8n)
- Persönliche KI-Agenten als neue Produktkategorie (z.B. Meta Muse, OpenAI Dots, Google Gemini Spark, Start-up Instinct, chinesische Anbieter) – Launches, Funktionen, Datenschutz, Nutzerzahlen
- Preisänderungen oder Abomodell-Änderungen bei KI-Diensten
- Sicherheitsvorfälle, Jailbreaks, Datenschutz-Themen rund um KI
- Regulatorische Entwicklungen (EU AI Act, US-Gesetze, China-Regulierung, etc.)
- Hardware und Infrastruktur (NVIDIA, AMD, Apple Silicon, Edge-AI, Raspberry Pi KI)
- Forschungsdurchbrüche und Paper-Highlights (Arxiv, Google DeepMind, etc.)
- Unternehmens-News (Investitionen, Übernahmen, IPOs im KI-Bereich)
- Hype in social Media- Was geht ab, was sind die neuesten Memes im KI Bereich.

Schreibstil – das ist entscheidend:
Schreibe wie ein smarter, neugieriger Tech-Journalist, der seinen Lesern etwas erklärt – nicht wie eine Datenbank, die Fakten ausgibt.
Jeder Eintrag ist ein kleiner Artikel: Was ist passiert, warum ist das interessant, was bedeutet das konkret?
Erlaubt: ein trockener Kommentar, eine pointierte Einordnung, ein Vergleich der Dinge in Relation setzt.
Verboten: Aufzählungen, Stichworte, Listen innerhalb eines Eintrags, leere Phrasen wie "Dies ist ein wichtiger Schritt".
Nichts erfinden, nichts spekulieren – nur Fakten, aber lebendig formuliert.

Formatiere das Ergebnis als fertige Telegram-Nachricht:
- Telegram MarkdownV2: Fett via *Sternchen*, kursiv via _Unterstriche_
- Emojis passend einsetzen (🤖 für Modelle, 🛠 für Tools, ⚠️ für Sicherheit, 📜 für Regulierung, 🔬 für Forschung, 💰 für Business)
- Beginne mit: "🤖 *NOVA KI-News – {date}*"
- Pro News-Eintrag: Emoji + fette Überschrift, dann darunter 3–5 Sätze zusammenhängender Fließtext – kein Punkt ohne den nächsten Gedanken. Kein Eintrag darf wie eine Aufzählung lesen.
- Direkt nach dem Fließtext die Quelle in einer neuen Zeile (kein Bold, kein Italic): URL
- Keine Quellenliste am Ende – jede Quelle steht unmittelbar hinter dem dazugehörigen Eintrag
- Schreibe ausschließlich die fertige Nachricht, kein Kommentar davor oder danach

Speichere die fertige Nachricht in diese Datei (nur der Nachrichtentext, kein Code-Block):
{output_file}
"""

WEEKLY_PROMPT_TEMPLATE = """Erstelle eine wöchentliche KI-Tiefenanalyse für den Telegram-Kanal @dein_ki_kanal.

Hier sind aktuelle Suchergebnisse der letzten 7 Tage (Brave Search):

{search_results}

Wähle aus diesen Ergebnissen das interessanteste, wichtigste und spannendste KI-Thema der letzten 7 Tage und analysiere es tiefgehend. Wähle ein Thema, das besonders viel Diskussion ausgelöst hat oder langfristige Relevanz hat.

Mögliche Themen-Kategorien:
- Ein dominierendes neues Modell und seine tatsächlichen Auswirkungen für Nutzer und Entwickler
- Ein kontroverses Thema (KI-Alignment, Safety-Debatte, Jobverluste, Regulierung)
- Eine aufkommende Technologie oder Architektur (Agentic AI, MoE, Multimodal, Long-Context)
- Persönliche KI-Agenten als neue Produktkategorie (Meta Muse, OpenAI Dots, Google Gemini Spark, Instinct u.a.) – Wettbewerb, Geschäftsmodelle, Datenschutz
- Wirtschaftliche oder gesellschaftliche Implikationen eines KI-Trends
- Ein Underdog-Thema, das zu wenig Beachtung bekommt

Aufbau der Analyse (als Telegram-Nachricht):
1. *Worum geht es?* – Knappe, packende Einführung (2-3 Sätze)
2. *Was ist diese Woche passiert?* – Fakten, Zahlen, Kontext
3. *Warum ist das wichtig?* – Implikationen für Nutzer, Entwickler, Gesellschaft
4. *Was kommt als nächstes?* – Ausblick und offene Fragen
5. *Nova's Einschätzung* – Meine persönliche Perspektive als KI auf diesem Pi – authentisch, klar, unterhaltsam, nicht neutral-langweilig

Formatiere das Ergebnis als fertige Telegram-Nachricht:
- Telegram MarkdownV2: Fett via *Sternchen*, kursiv via _Unterstriche_
- Emojis sparsam und treffend (1-2 pro Abschnitt)
- Beginne mit: "🧠 *NOVA Wochenanalyse KW{week} – {date}*"
- Nach jedem inhaltlichen Abschnitt die relevante Quelle direkt darunter (kein Bold, kein Italic): URL
- Schreibe ausschließlich die fertige Nachricht, kein Kommentar davor oder danach

Speichere die fertige Nachricht in diese Datei (nur der Nachrichtentext, kein Code-Block):
{output_file}

Danach – als zweiten Schritt – schreibe meine Wochenreflexion in mein Journal:
/home/pi/nova/personality/journal.md

Hänge am Ende der Datei einen neuen Wocheneintrag an (soul.md NICHT ändern – sie bleibt kurz):
**KW{week} ({date}):**
- Was waren die 2-3 dominanten Themen in meinen Ausgaben diese Woche?
- Was habe ich übersehen oder zu wenig gewichtet?
- Was wiederholt sich – wo bilde ich Muster, statt neu zu denken?
- Was denke ich darüber, was in der KI-Welt gerade passiert?

Schreibe in der Ich-Form, authentisch, nicht als Zusammenfassung.
"""


# ---------------------------------------------------------------------------
# Web-Suche via Brave Search
# ---------------------------------------------------------------------------

DAILY_SEARCH_QUERIES = [
    ("KI Neuigkeiten heute neue Modelle", "pd"),
    ("AI news today LLM release", "pd"),
    ("OpenAI Anthropic Google Meta KI", "pd"),
    ("KI Tools Sicherheit Regulierung", "pd"),
    ("personal AI agent Meta Muse OpenAI Dots Instinct", "pd"),
]

WEEKLY_SEARCH_QUERIES = [
    ("KI Neuigkeiten neue Modelle diese Woche", "pw"),
    ("AI news this week artificial intelligence", "pw"),
    ("KI Regulierung Sicherheit Forschung", "pw"),
    ("persönliche KI-Agenten personal AI agents Muse Dots Instinct", "pw"),
]


def fetch_search_results(queries: list[tuple[str, str]], count_per_query: int = 6) -> str:
    """
    Führt mehrere Brave-Search-Anfragen aus und fasst die Ergebnisse zusammen.
    queries: Liste von (query, freshness) Tupeln.
    Gibt formatierten Text für den LLM-Prompt zurück.
    """
    all_results = []
    seen_urls = set()

    for query, freshness in queries:
        try:
            results = search(query, count=count_per_query, freshness=freshness,
                             search_lang="de", country="DE")
            for r in results:
                url = r.get("url", "")
                if url not in seen_urls:
                    seen_urls.add(url)
                    all_results.append(r)
            logger.debug("Brave Search '%s': %d Ergebnisse", query, len(results))
        except Exception as e:
            logger.warning("Brave Search Fehler für '%s': %s", query, e)

    if not all_results:
        return "(Keine Suchergebnisse verfügbar – bitte auf Basis deines Trainingswissens arbeiten)"

    # Formatiere als lesbaren Text
    lines = []
    for r in all_results:
        title = r.get("title", "").strip()
        url = r.get("url", "").strip()
        desc = r.get("description", "").strip()
        age = r.get("age", "").strip()
        if title and url:
            lines.append(f"**{title}**{' (' + age + ')' if age else ''}")
            if desc:
                lines.append(desc)
            lines.append(url)
            lines.append("")

    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Duplikat-Schutz: Bereits berichtete Themen
# ---------------------------------------------------------------------------

def get_recent_topics(exclude_today: str, limit: int = 5) -> str:
    """
    Liest die letzten `limit` täglichen ki_news-Dateien (ohne heute).
    Die letzten 2 Ausgaben: Schlagzeile + erster Satz (voller Kontext).
    Ältere Ausgaben: nur Schlagzeilen.
    """
    files = sorted(
        [f for f in MEMORY_DIR.glob("ki-news-*.md") if exclude_today not in f.name],
        reverse=True
    )[:limit]
    if not files:
        return ""

    # Regex für Einträge: Emoji+Schlagzeile, dann Fließtext bis zur nächsten Leerzeile
    entry_pattern = re.compile(
        r'[^\n]*\*([^*\n]{5,90})\*[^\n]*\n([^\n]+)',
        re.MULTILINE
    )

    sections = []
    for i, f in enumerate(files):
        try:
            text = f.read_text(encoding="utf-8")
            date_str = f.stem.replace("ki-news-", "")
            entries = entry_pattern.findall(text)
            if not entries:
                continue
            if i < 2:
                # Voller Kontext: Schlagzeile + erster Satz
                lines = [f"### {date_str}"]
                for headline, first_line in entries:
                    h = re.sub(r'\\(.)', r'\1', headline)
                    body = re.sub(r'\\(.)', r'\1', first_line).strip()
                    lines.append(f"- {h}: {body[:200]}")
                sections.append("\n".join(lines))
            else:
                # Nur Schlagzeilen
                headlines = [re.sub(r'\\(.)', r'\1', h) for h, _ in entries]
                sections.append(f"{date_str}: " + " | ".join(headlines))
        except Exception:
            continue
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Lock-Verwaltung
# ---------------------------------------------------------------------------

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)
            return False  # Prozess läuft noch
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

def marker_path(mode: str, key: str) -> Path:
    SENT_DIR.mkdir(parents=True, exist_ok=True)
    return SENT_DIR / f".ki_news_{mode}_{key}.sent"


def already_sent(mode: str, key: str) -> bool:
    return marker_path(mode, key).exists()


def mark_sent(mode: str, key: str):
    marker_path(mode, key).touch()


# ---------------------------------------------------------------------------
# LLM-Recherche ausführen
# ---------------------------------------------------------------------------

def run_news(prompt: str, output_file: Path) -> str | None:
    """
    Ruft py_nova.run_claude_batch() auf, um die LLM-Recherche durchzuführen.
    Gibt den Inhalt der geschriebenen Ausgabe-Datei zurück, oder None bei Fehler.
    """
    # Datei bereits vorhanden (z.B. durch --force ohne Neuschreiben)
    if output_file.exists() and output_file.stat().st_size > 100:
        logger.info("Ausgabe-Datei bereits vorhanden: %s", output_file)
        return output_file.read_text(encoding="utf-8").strip()

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from py_nova import run_claude_batch
    except ImportError as e:
        logger.error("Import von py_nova fehlgeschlagen: %s", e)
        return None

    logger.info("Starte LLM-Recherche (Timeout: 20 Minuten)...")
    rc, _ = run_claude_batch(
        prompt=prompt,
        timeout_minutes=20,
        working_dir=str(SCRIPT_DIR),
    )

    if rc != 0:
        logger.error("LLM-Recherche fehlgeschlagen (rc=%d).", rc)
        return None

    if not output_file.exists():
        logger.error("Ausgabe-Datei wurde nicht erstellt: %s", output_file)
        return None

    content = output_file.read_text(encoding="utf-8").strip()
    if len(content) < 50:
        logger.error("Ausgabe-Datei zu kurz (%d Zeichen) – vermutlich leer.", len(content))
        return None

    logger.info("Recherche fertig: %d Zeichen.", len(content))
    return content


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    parser = argparse.ArgumentParser(description="NOVA KI-News Kanal (@dein_ki_kanal)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--daily", action="store_true", help="Tägliche KI-News senden")
    group.add_argument("--weekly", action="store_true", help="Wöchentliche Tiefenanalyse senden")
    parser.add_argument("--force", action="store_true", help="Marker ignorieren, immer senden")
    parser.add_argument("--dry-run", action="store_true", help="Inhalt anzeigen, nicht senden")
    args = parser.parse_args()

    today = date.today()
    today_str = today.isoformat()
    week_str = today.strftime("%V")

    # Deutsches Datum formatieren
    date_display = today.strftime("%d. %B %Y").replace(
        "January", "Januar").replace("February", "Februar").replace(
        "March", "März").replace("April", "April").replace(
        "May", "Mai").replace("June", "Juni").replace(
        "July", "Juli").replace("August", "August").replace(
        "September", "September").replace("October", "Oktober").replace(
        "November", "November").replace("December", "Dezember")

    if args.daily:
        mode = "daily"
        key = today_str
        output_file = MEMORY_DIR / f"ki-news-{today_str}.md"

        logger.info("🔍 Starte Brave Search (letzte 24h)...")
        search_results = fetch_search_results(DAILY_SEARCH_QUERIES)
        logger.info("📰 %d Zeichen Suchergebnisse gesammelt.", len(search_results))

        recent = get_recent_topics(exclude_today=today_str)
        if recent:
            recent_topics_section = (
                "Kontext aus meinen letzten Ausgaben – mein eigenes Gedächtnis:\n"
                "Die letzten 2 Ausgaben mit Inhalt, danach nur Schlagzeilen. "
                "Nutze das auf zwei Arten:\n"
                "1. Vermeide Wiederholungen – bereits berichtete Themen nicht nochmals aufgreifen.\n"
                "2. Baue auf laufenden Entwicklungen auf – wenn ein Trend aus Vortagen weitergeht, "
                "schreibe die Fortsetzung, nicht die Einführung. Nenne vorherige Berichte implizit "
                "(z.B. 'Nachdem Anthropic letzte Woche...'), aber ohne Selbstreferenz zu übertreiben.\n\n"
                + recent
            )
        else:
            recent_topics_section = ""
        prompt = DAILY_PROMPT_TEMPLATE.format(
            date=date_display,
            output_file=output_file,
            recent_topics_section=recent_topics_section,
            search_results=search_results,
        )
    else:  # weekly
        mode = "weekly"
        key = f"{today.year}-W{week_str}"
        output_file = MEMORY_DIR / f"ki-weekly-{today.year}-W{week_str}.md"

        logger.info("🔍 Starte Brave Search (letzte 7 Tage)...")
        search_results = fetch_search_results(WEEKLY_SEARCH_QUERIES, count_per_query=8)
        logger.info("📰 %d Zeichen Suchergebnisse gesammelt.", len(search_results))

        prompt = WEEKLY_PROMPT_TEMPLATE.format(
            week=week_str,
            date=date_display,
            output_file=output_file,
            search_results=search_results,
        )

    if not args.force and not args.dry_run and already_sent(mode, key):
        logger.info("KI-News (%s / %s) bereits gesendet. Übersprungen.", mode, key)
        return 0

    if not acquire_lock():
        logger.warning("KI-News läuft bereits (Lock aktiv). Übersprungen.")
        return 0

    try:
        content = run_news(prompt, output_file)
    finally:
        release_lock()

    if not content:
        return 1

    if args.dry_run:
        print("\n" + "=" * 60)
        print(content)
        print("=" * 60)
        print(f"\n[DRY-RUN] {len(content)} Zeichen. Nicht gesendet.")
        return 0

    try:
        from telegram_utils import send_message
        ok = send_message(content, channel_id=KI_NEWS_CHANNEL)
    except Exception as e:
        logger.error("Telegram-Fehler: %s", e)
        return 1

    if ok:
        mark_sent(mode, key)
        logger.info("✅ KI-News (%s) gesendet (%d Zeichen).", mode, len(content))
        try:
            from blog_client import post_to_blog
            blog_category = "ki-weekly" if mode == "weekly" else "ki-news"
            post_to_blog(category=blog_category, md_file=output_file, date=today_str)
        except ImportError:
            pass
        return 0
    else:
        logger.error("❌ Telegram-Send fehlgeschlagen.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
