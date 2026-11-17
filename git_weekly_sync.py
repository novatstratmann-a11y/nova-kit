#!/usr/bin/env python3
"""
git_weekly_sync.py – NOVA Wöchentlicher Git-Sync

Committet alle Änderungen im Repo und pusht sie zum Git-Remote.
Läuft nur, wenn es tatsächlich Änderungen gibt (keine Leer-Commits).

Cron: 0 23 * * 0  (sonntags 23:00)

Verwendung:
    python git_weekly_sync.py            # Commit + Push
    python git_weekly_sync.py --dry-run  # Nur anzeigen, nichts committen/pushen
"""

import argparse
import logging
import logging.handlers
import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
LOCK_FILE  = SCRIPT_DIR / "personality" / ".git_weekly_sync.lock"
LOG_FILE   = SCRIPT_DIR / "personality" / "logs" / "scripts.log"
REMOTE     = "origin"
BRANCH     = "main"

logger = logging.getLogger("git_weekly_sync")


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
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(name)-16s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)


def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os_pid_alive = subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0
        except (ValueError, OSError):
            os_pid_alive = False
        if os_pid_alive:
            logger.warning("Lock aktiv (PID %s) – Abbruch.", pid)
            return False
        logger.info("Stale Lock gefunden – wird entfernt.")
        LOCK_FILE.unlink()
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    import os
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()


def run_git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=SCRIPT_DIR, capture_output=True, text=True)


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="NOVA Wöchentlicher Git-Sync")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts committen/pushen")
    args = parser.parse_args()

    if not args.dry_run and not acquire_lock():
        return 1

    try:
        run_git("add", "-A")

        status = run_git("status", "--porcelain")
        if not status.stdout.strip():
            logger.info("Keine Änderungen – kein Commit nötig.")
            return 0

        changed_files = [line[3:] for line in status.stdout.strip().splitlines()]
        logger.info("Geänderte Dateien (%d): %s", len(changed_files), ", ".join(changed_files[:10]))

        if args.dry_run:
            print(status.stdout)
            logger.info("[DRY-RUN] Kein Commit, kein Push.")
            return 0

        today = date.today().isoformat()
        msg = f"Wöchentlicher Auto-Sync – {today}\n\n{len(changed_files)} Datei(en) geändert."
        commit = run_git("commit", "-m", msg)
        if commit.returncode != 0:
            logger.error("Commit fehlgeschlagen: %s", commit.stderr.strip())
            return 1
        logger.info("Commit erstellt: %s", commit.stdout.strip().splitlines()[0] if commit.stdout else "")

        push = run_git("push", REMOTE, BRANCH)
        if push.returncode != 0:
            logger.error("Push fehlgeschlagen: %s", push.stderr.strip())
            return 1
        logger.info("✅ Push erfolgreich zu %s/%s.", REMOTE, BRANCH)
        return 0
    finally:
        if not args.dry_run:
            release_lock()


if __name__ == "__main__":
    sys.exit(main())
