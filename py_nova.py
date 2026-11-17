#!/usr/bin/env python3
"""
py_nova.py - Run Claude Code with NOVA personality and timeout

Usage:
    python py_nova.py "your question here"
    python py_nova.py "your question here" --timeout 15
"""

import subprocess
import signal
import sys
import argparse
import os
import re
import shlex
import shutil
import logging
import logging.handlers
import threading
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent
LOCK_FILE = SCRIPT_DIR / "personality" / ".nova.lock"
LOG_FILE = SCRIPT_DIR / "personality" / "logs" / "nova.log"

logger = logging.getLogger("nova")


def setup_logging(verbose: bool = False, silent: bool = False):
    """
    Konfiguriert Logging mit Console- und rotierendem Datei-Handler.
    - Console: INFO (oder DEBUG bei --verbose), kein Timestamp
    - Datei: DEBUG immer, mit Timestamp und Level
    - silent=True: kein Console-Handler (nur Datei), für Subprocess-Nutzung
    """
    logger.setLevel(logging.DEBUG)

    if not silent:
        # Console-Handler – minimales Format für lesbare Terminal-Ausgabe
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console)

    # Datei-Handler – rotierend, max 1 MB, 3 Backups
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(file_handler)


def acquire_lock() -> bool:
    """Versucht Lock zu setzen. Gibt True zurück wenn erfolgreich."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)  # Prüft ob PID noch lebt
            return False  # Prozess läuft noch
        except (ValueError, ProcessLookupError, PermissionError):
            logger.warning("Veraltetes Lock gefunden (PID nicht mehr aktiv), wird entfernt.")
            LOCK_FILE.unlink()
    LOCK_FILE.write_text(str(os.getpid()))
    logger.debug("Lock gesetzt (PID=%s)", os.getpid())
    return True


def release_lock():
    """Entfernt das Lockfile."""
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def load_soul():
    """
    Lädt die NOVA Personality aus soul.md
    """
    script_dir = Path(__file__).parent
    soul_path = script_dir / "personality" / "soul.md"

    if not soul_path.exists():
        logger.warning("⚠️  soul.md nicht gefunden unter %s", soul_path)
        return None

    try:
        with open(soul_path, 'r', encoding='utf-8') as f:
            soul_content = f.read()
        logger.info("✅ NOVA Personality geladen aus %s", soul_path)
        return soul_content
    except Exception as e:
        logger.error("⚠️  Fehler beim Laden von soul.md: %s", e)
        return None


LESSONS_MAX_CHARS = 8000


def load_recent_memory():
    """
    Lädt personality/lessons.md (Novas destillierte Erfahrungen) für die
    Kontext-Injektion. Früher wurden hier memory-*.md-Dateien gesucht, die nie
    geschrieben wurden – der Mechanismus war wirkungslos.
    Gibt den Inhalt (gekürzt auf LESSONS_MAX_CHARS) oder None zurück.
    """
    lessons_path = Path(__file__).parent / "personality" / "lessons.md"
    try:
        text = lessons_path.read_text(encoding='utf-8').strip()
    except Exception:
        return None
    if not text:
        return None
    if len(text) > LESSONS_MAX_CHARS:
        logger.warning("lessons.md ist %d Zeichen lang – gekürzt auf %d", len(text), LESSONS_MAX_CHARS)
        text = text[:LESSONS_MAX_CHARS] + "\n[… gekürzt – lessons.md bitte verdichten]"
    return text


def build_prompt(prompt):
    """
    Ergänzt den Prompt um Batch-Kontext und aktuelles Gedächtnis.
    NOVA soll wissen: keine Rückfragen, handle autonom.
    """
    today = datetime.now().strftime('%Y-%m-%d %H:%M')
    header = f"[BATCH-MODUS | {today} | Nicht-interaktiv – stelle keine Rückfragen, handle vollständig autonom]\n\n"

    memory = load_recent_memory()
    if memory:
        header += f"## Mein Gedächtnis (personality/lessons.md):\n{memory}\n\n## Aufgabe:\n"
        logger.info("🧠 Gedächtnis-Kontext geladen (%d Zeilen)", memory.count('\n') + 1)

    return header + prompt


def run_claude_batch(prompt, timeout_minutes=10, working_dir="."):
    """
    Führt Claude Code mit NOVA Personality und voller Permission aus.
    
    Args:
        prompt: Die Frage/Aufgabe für Claude
        timeout_minutes: Wie lange Claude laufen soll (default: 15 Minuten)
        working_dir: Arbeitsverzeichnis für Claude
    """
    
    # Lade NOVA Personality
    soul = load_soul()

    # Prompt mit Batch-Kontext und Gedächtnis anreichern
    enriched_prompt = build_prompt(prompt)

    # Baue den Claude Command zusammen
    script_dir = Path(__file__).parent
    soul_path = script_dir / "personality" / "soul.md"

    prompt_arg = shlex.quote(enriched_prompt)

    # Vollständiger Pfad zu claude – nötig wenn aus Cron aufgerufen (eingeschränktes PATH)
    claude_bin = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")

    if soul and soul_path.exists():
        complete_command = (
            f"{claude_bin} -p --permission-mode bypassPermissions --dangerously-skip-permissions "
            f"--append-system-prompt-file '{soul_path}' {prompt_arg}"
        )
    else:
        complete_command = (
            f"{claude_bin} -p --permission-mode bypassPermissions --dangerously-skip-permissions "
            f"{prompt_arg}"
        )
    
    logger.info("=" * 60)
    logger.info("🌟 NOVA Batch-Prozess gestartet")
    logger.info("📝 Prompt: %s", prompt)
    logger.info("⏱️  Timeout: %d Minuten", timeout_minutes)
    logger.info("📂 Working Dir: %s", working_dir)
    if soul:
        logger.info("🧠 Personality: NOVA (soul.md geladen)")
    logger.info("🕐 Start: %s", datetime.now().strftime('%H:%M:%S'))
    logger.info("=" * 60)
    
    try:
        # Starte Claude Code als Subprocess
        # stdout wird über einen Reader-Thread erfasst (tee: Terminal + Buffer)
        # --dangerously-skip-permissions erlaubt: lesen, schreiben, löschen, umbenennen
        # CLAUDECODE aus der Umgebung entfernen – Claude verweigert sonst
        # den Start wenn es nested in einer anderen Claude-Session aufgerufen wird
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        process = subprocess.Popen(
            complete_command,
            shell=True,
            cwd=working_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )

        logger.info("✅ NOVA gestartet (PID=%d)", process.pid)
        logger.debug("Kommando: %s", complete_command)

        # Reader-Thread: leitet stdout gleichzeitig ans Terminal und in den Buffer
        output_lines: list[str] = []

        def _reader():
            for line in process.stdout:
                print(line, end="", flush=True)
                output_lines.append(line)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        # Warte bis Claude fertig ist ODER Timeout erreicht ist
        logger.info("⏳ Läuft (max. %d Minuten)...", timeout_minutes)

        try:
            # Warte auf Prozess-Ende mit Timeout
            process.wait(timeout=timeout_minutes * 60)

            # Claude ist von selbst fertig geworden
            logger.info("=" * 60)
            if process.returncode == 0:
                logger.info("✅ NOVA erfolgreich beendet (vor Timeout, rc=0)")
            else:
                logger.error("❌ NOVA mit Fehler beendet (vor Timeout, rc=%s)", process.returncode)

        except subprocess.TimeoutExpired:
            # Timeout erreicht - Claude läuft noch
            logger.warning("=" * 60)
            logger.warning("⏰ Timeout von %d Minuten erreicht", timeout_minutes)
            logger.info("🛑 Beende NOVA gracefully...")

            # Sende SIGINT (Ctrl+C)
            process.send_signal(signal.SIGINT)

            # Warte bis zu 10 Sekunden auf sauberes Beenden
            try:
                process.wait(timeout=10)
                logger.info("✅ NOVA sauber beendet")
            except subprocess.TimeoutExpired:
                # Immer noch nicht beendet - force kill
                logger.warning("⚠️  Reagiert nicht - force kill...")
                process.kill()
                process.wait()
                logger.error("❌ NOVA force terminated")

        reader_thread.join(timeout=15)
        output = "".join(output_lines)

        if process.returncode != 0:
            try:
                from claude_auth import notify_if_auth_error
                notify_if_auth_error(output)
            except Exception as _auth_err:
                logger.warning("Auth-Check fehlgeschlagen: %s", _auth_err)

        logger.info("🕐 Ende: %s", datetime.now().strftime('%H:%M:%S'))
        logger.info("=" * 60)

        return process.returncode, output

    except KeyboardInterrupt:
        logger.warning("=" * 60)
        logger.warning("⚠️  Abbruch durch Benutzer (Ctrl+C)")
        logger.info("🛑 Beende NOVA...")
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            process.kill()
        logger.info("✅ Beendet")
        logger.info("=" * 60)
        return 1, ""

    except Exception as e:
        logger.exception("❌ Fehler beim Ausführen: %s", e)
        return 1, ""


def load_heartbeat_tasks():
    """
    Liest offene Aufgaben aus personality/heartbeat.md.
    Aufgaben sind Zeilen im Format: - [ ] Aufgabentext
    Gibt eine Liste von (zeilennummer, aufgabentext) Tupeln zurück.
    """
    script_dir = Path(__file__).parent
    heartbeat_path = script_dir / "personality" / "heartbeat.md"

    if not heartbeat_path.exists():
        logger.warning("⚠️  heartbeat.md nicht gefunden unter %s", heartbeat_path)
        return []

    with open(heartbeat_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    today = datetime.now().strftime('%Y-%m-%d')
    tasks = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('- [ ]'):
            task_text = stripped[5:].strip()
            if not task_text:
                continue
            # Datums-Sperre: "(ab 2026-11-05) …" oder "Am 2026-11-05 …" erst ab diesem Tag
            due = _DUE_RE.search(task_text)
            if due and due.group(1) > today:
                logger.info("⏳ Noch nicht fällig (%s): %s", due.group(1), task_text[:60])
                continue
            tasks.append((i, task_text))

    return tasks


_DUE_RE = re.compile(r'^\(?\s*(?:ab|am)\s+(\d{4}-\d{2}-\d{2})', re.IGNORECASE)


def mark_task_done(line_index):
    """
    Markiert eine Aufgabe in heartbeat.md als erledigt (- [ ] -> - [x]).
    """
    script_dir = Path(__file__).parent
    heartbeat_path = script_dir / "personality" / "heartbeat.md"

    with open(heartbeat_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    lines[line_index] = lines[line_index].replace('- [ ]', '- [x]', 1)

    with open(heartbeat_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)


def _is_shell_command(task_text: str) -> bool:
    """Gibt True zurück wenn task_text ein direkter Shell-Befehl ist (kein LLM-Prompt)."""
    prefixes = ("python ", "python3 ", "bash ", "sh ", "node ", "./")
    return any(task_text.startswith(p) for p in prefixes)


def _run_shell_task(task_text: str) -> tuple[int, str]:
    """
    Führt einen Shell-Befehl direkt aus (nicht via Claude).
    Gibt (returncode, output) zurück – rc=1 bleibt rc=1 (ZEITSPERRE etc.).
    """
    try:
        result = subprocess.run(
            task_text,
            shell=True,
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            text=True,
            timeout=600,
        )
        output = (result.stdout + result.stderr).strip()
        if output:
            print(output, flush=True)
        logger.info("Shell-Task beendet (rc=%d): %s", result.returncode, task_text[:60])
        return result.returncode, output
    except subprocess.TimeoutExpired:
        logger.warning("⏰ Shell-Task Timeout: %s", task_text[:60])
        return 1, "Timeout"
    except Exception as e:
        logger.error("❌ Shell-Task Fehler: %s", e)
        return 1, str(e)


def run_heartbeat(timeout_minutes=10, working_dir="."):
    """
    Liest offene Aufgaben aus heartbeat.md und führt sie nacheinander aus.
    Shell-Commands (python ..., bash ...) werden direkt ausgeführt – kein Claude-Overhead,
    korrekter Exit-Code (wichtig für ZEITSPERRE-Scripts: rc=1 = noch nicht bereit).
    """
    script_dir = Path(__file__).parent
    heartbeat_path = script_dir / "personality" / "heartbeat.md"

    logger.info("=" * 60)
    logger.info("💓 NOVA Heartbeat-Modus")
    logger.info("📄 Lese Aufgaben aus %s", heartbeat_path)
    logger.info("=" * 60)

    tasks = load_heartbeat_tasks()

    if not tasks:
        logger.info("✅ Keine offenen Aufgaben in heartbeat.md gefunden.")
        return 0

    logger.info("📋 %d offene Aufgabe(n) gefunden:", len(tasks))
    for i, (_, task_text) in enumerate(tasks, 1):
        logger.info("  %d. %s", i, task_text)

    exit_code = 0
    for task_num, (line_index, task_text) in enumerate(tasks, 1):
        logger.info("=" * 60)
        logger.info("🔄 Aufgabe %d/%d: %s", task_num, len(tasks), task_text)
        logger.info("=" * 60)

        if _is_shell_command(task_text):
            rc, output = _run_shell_task(task_text)
        else:
            rc, output = run_claude_batch(
                prompt=task_text,
                timeout_minutes=timeout_minutes,
                working_dir=working_dir
            )

        if rc == 0:
            mark_task_done(line_index)
            logger.info("✅ Aufgabe als erledigt markiert.")
            # Ergebnis an Admin senden (privater Chat, nicht öffentlicher Kanal)
            try:
                from telegram_utils import send_to_admin
                send_to_admin(f"✅ *{task_text[:60]}*\n\n{output[-3000:]}")
            except Exception as _tg_err:
                logger.warning("Telegram-Send fehlgeschlagen: %s", _tg_err)
        else:
            logger.warning("⚠️  Aufgabe mit Fehlercode %d beendet – bleibt offen.", rc)
            exit_code = rc

    return exit_code


TIMEOUT_MINUTES = 15


def main():
    parser = argparse.ArgumentParser(
        description='Führe Claude Code mit NOVA Personality aus',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  python py_nova.py Analysiere alle Python-Dateien und erstelle einen Report
  python py_nova.py Hallo Nova, wie geht es dir?
  python py_nova.py --dir ./src Refactore den Code und optimiere Performance
  python py_nova.py                    # Heartbeat-Modus: Aufgaben aus heartbeat.md
  python py_nova.py --verbose          # Mit Debug-Ausgaben auf der Konsole

NOVA lädt automatisch die Personality aus personality/soul.md
Ohne Prompt werden offene Aufgaben aus personality/heartbeat.md ausgeführt.
Aufgaben-Format in heartbeat.md:
  - [ ] Offene Aufgabe
  - [x] Bereits erledigte Aufgabe

Logs werden geschrieben nach: personality/nova.log (rotierend, max 1 MB)
        """
    )

    parser.add_argument(
        'prompt',
        nargs='*',
        help='Die Frage/Aufgabe für Claude Code (optional; ohne Prompt: Heartbeat-Modus)'
    )

    parser.add_argument(
        '--dir',
        default='.',
        help='Arbeitsverzeichnis für Claude (default: aktuelles Verzeichnis)'
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Debug-Ausgaben auf der Konsole anzeigen'
    )

    parser.add_argument(
        '--silent', '-s',
        action='store_true',
        help='Kein Console-Logging (nur Datei) – für Subprocess-Nutzung z.B. durch Telegram-Bot'
    )

    args = parser.parse_args()
    setup_logging(verbose=args.verbose, silent=args.silent)
    logger.debug("Log-Datei: %s", LOG_FILE)

    prompt = ' '.join(args.prompt) if args.prompt else None

    # Explizit übergebener, aber leerer Prompt (z.B. "" vom Telegram-Bot) ist ein
    # Fehler – nicht in den Heartbeat-Modus fallen und Claude nicht ohne Aufgabe starten
    if prompt is not None and not prompt.strip():
        logger.error("Leerer Prompt übergeben. Abbruch.")
        return 2

    # Heartbeat-Modus wenn kein Prompt angegeben
    if prompt is None:
        if not acquire_lock():
            try:
                pid = LOCK_FILE.read_text().strip()
            except Exception:
                pid = "?"
            logger.info("Heartbeat übersprungen – Nova läuft bereits (PID %s).", pid)
            return 0  # kein Fehler für Cron
        try:
            return run_heartbeat(
                timeout_minutes=TIMEOUT_MINUTES,
                working_dir=args.dir
            )
        finally:
            release_lock()

    # Normaler Modus mit explizitem Prompt
    if not acquire_lock():
        try:
            pid = LOCK_FILE.read_text().strip()
        except Exception:
            pid = "?"
        logger.error("Bereits aktiv (PID %s). Abbruch.", pid)
        return 1
    try:
        rc, _ = run_claude_batch(
            prompt=prompt,
            timeout_minutes=TIMEOUT_MINUTES,
            working_dir=args.dir
        )
        return rc
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())