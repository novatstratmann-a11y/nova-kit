#!/usr/bin/env python3
"""
claude_auth.py – Überwachung des Claude-Logins (OAuth) für NOVA

    check_login_expiry()          → Warnung, wenn der Login bald abläuft (Gotify, mit Cooldown)
    notify_if_auth_error(output)  → Gotify-Alarm, wenn ein claude-Aufruf an der Anmeldung scheiterte

Die Credentials liegen in ~/.claude/.credentials.json (claudeAiOauth.refreshTokenExpiresAt).
Der Access-Token läuft alle paar Stunden ab und wird automatisch erneuert – kritisch
ist nur das Ablaufdatum des Refresh-Tokens. Danach hilft nur ein neuer Login: `claude` → `/login`.

Verwendung als Testskript:
    python claude_auth.py            # Status ausgeben, ggf. warnen
    python claude_auth.py --dry-run  # nur Status
"""

import json
import logging
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
STATE_FILE = SCRIPT_DIR / "personality" / ".claude_auth_state.json"

WARN_DAYS = 7          # ab hier täglich eine Erinnerung
CRIT_DAYS = 2          # ab hier alle 6 h, hohe Priorität
WARN_COOLDOWN_S = 24 * 3600
CRIT_COOLDOWN_S = 6 * 3600
ERROR_COOLDOWN_S = 3 * 3600   # Fehler-Alarm, sonst Spam bei jedem fehlschlagenden Job

AUTH_ERROR_PATTERNS = re.compile(
    r"Failed to authenticate|OAuth session expired|OAuth token has expired|"
    r"could not be refreshed|Invalid API key|Please run /login|authentication_error",
    re.IGNORECASE,
)

HOWTO = "Neu anmelden: auf dem Pi `claude` starten und `/login` ausführen."

logger = logging.getLogger("claude_auth")


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        logger.warning("Auth-State nicht speicherbar: %s", e)


def _notify(key: str, title: str, message: str, priority: int, cooldown: int) -> bool:
    """Sendet per Gotify, höchstens einmal pro Cooldown und Schlüssel."""
    state = _load_state()
    now = time.time()
    if now - state.get(key, 0) < cooldown:
        return False
    try:
        from gotify_utils import send_message
        ok = send_message(message, title=title, priority=priority)
    except Exception as e:
        logger.error("Gotify-Senden fehlgeschlagen: %s", e)
        return False
    if ok:
        state[key] = now
        _save_state(state)
    return ok


def notify_if_auth_error(output: str) -> bool:
    """Alarmiert per Gotify, wenn die claude-Ausgabe auf einen Login-Fehler hindeutet."""
    if not output or not AUTH_ERROR_PATTERNS.search(output):
        return False
    logger.error("Claude-Login ungültig – sende Gotify-Alarm.")
    return _notify(
        "last_error_alert",
        "NOVA – Claude-Login abgelaufen",
        f"Ein claude-Aufruf ist an der Anmeldung gescheitert:\n{output.strip()[-300:]}\n\n{HOWTO}",
        priority=9,
        cooldown=ERROR_COOLDOWN_S,
    )


def login_days_left() -> float | None:
    """Verbleibende Tage bis der Refresh-Token abläuft; None wenn nicht ermittelbar."""
    try:
        oauth = json.loads(CREDENTIALS_FILE.read_text())["claudeAiOauth"]
        exp_ms = oauth.get("refreshTokenExpiresAt")
        return (exp_ms / 1000 - time.time()) / 86400 if exp_ms else None
    except Exception as e:
        logger.warning("Credentials nicht lesbar: %s", e)
        return None


def check_login_expiry(dry_run: bool = False) -> float | None:
    """Warnt per Gotify frühzeitig, bevor der Login abläuft. Gibt die Resttage zurück."""
    days = login_days_left()
    if days is None:
        return None
    logger.info("Claude-Login gültig noch %.1f Tage.", days)
    if days > WARN_DAYS or dry_run:
        return days

    if days <= 0:
        msg, prio, cd = "Der Claude-Login ist abgelaufen – alle NOVA-Jobs schlagen fehl.", 9, CRIT_COOLDOWN_S
    elif days <= CRIT_DAYS:
        msg, prio, cd = f"Der Claude-Login läuft in {days * 24:.0f} Stunden ab!", 8, CRIT_COOLDOWN_S
    else:
        msg, prio, cd = f"Der Claude-Login läuft in {days:.0f} Tagen ab.", 5, WARN_COOLDOWN_S
    _notify("last_expiry_warning", "NOVA – Claude-Login läuft ab", f"{msg}\n{HOWTO}", prio, cd)
    return days


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    d = check_login_expiry(dry_run="--dry-run" in sys.argv)
    print("Resttage:", "unbekannt" if d is None else f"{d:.1f}")
