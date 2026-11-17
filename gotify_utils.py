#!/usr/bin/env python3
"""
gotify_utils.py – Gemeinsame Gotify-Hilfsfunktionen für NOVA

Kein Daemon, nur Funktionen:
    load_config()                  → liest Gotify-Config aus .env
    send_message(title, message)   → POST zur Gotify Message API

Verwendung als Testskript:
    python gotify_utils.py
"""

import logging

try:
    import requests
except ImportError:
    requests = None  # type: ignore

from env_utils import get_env

logger = logging.getLogger("gotify")


def load_config() -> dict | None:
    """
    Liest Gotify-Config aus .env.
    Gibt None zurück wenn GOTIFY_URL oder GOTIFY_TOKEN nicht gesetzt sind.
    """
    url = get_env("GOTIFY_URL")
    if not url:
        logger.warning("Keine GOTIFY_URL in .env")
        return None

    token = get_env("GOTIFY_TOKEN")
    if not token:
        logger.warning("Kein GOTIFY_TOKEN in .env")
        return None

    return {
        "url": url,
        "token": token,
        "app_name": get_env("GOTIFY_APP_NAME", "NOVA"),
    }


def send_message(message: str, title: str = "NOVA", priority: int = 5) -> bool:
    """
    Sendet eine Nachricht an den konfigurierten Gotify-Server.

    Args:
        message:  Nachrichtentext
        title:    Titel der Notification (default "NOVA")
        priority: Gotify-Priorität 0-10 (default 5; 8+ = kritisch/laut)

    Returns:
        True bei Erfolg, False bei Fehler (keine Exception)
    """
    if requests is None:
        logger.error("'requests' nicht installiert. Lösung: pip install requests --break-system-packages")
        return False

    config = load_config()
    if config is None:
        return False

    url = config["url"].rstrip("/") + "/message"
    params = {"token": config["token"]}
    payload = {"title": title, "message": message, "priority": priority}

    try:
        response = requests.post(url, params=params, json=payload, timeout=10)
        if response.status_code == 200:
            logger.debug("Gotify-Nachricht gesendet (%d Zeichen)", len(message))
            return True
        logger.warning("Gotify API Fehler %d: %s", response.status_code, response.text[:200])
        return False
    except requests.RequestException as e:
        logger.warning("Gotify-Verbindungsfehler: %s", e)
        return False


# ---------------------------------------------------------------------------
# Testmodus (direkter Aufruf)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)-8s %(message)s")
    print("Sende Testnachricht an Gotify...")
    ok = send_message("Nova online – Gotify-Integration aktiv.", title="NOVA Test")
    print("✅ Gesendet." if ok else "❌ Fehler beim Senden (siehe Logs).")
