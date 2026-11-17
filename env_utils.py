#!/usr/bin/env python3
"""
env_utils.py – Minimaler .env-Loader für NOVA (keine externe Abhängigkeit)

Liest KEY=VALUE-Zeilen aus der `.env` im Projekt-Root (einmalig, gecached)
und stellt sie über get_env()/require_env() bereit. `.env` ist in .gitignore
und wird nie committed – siehe .env.example für die erwarteten Keys.

Format:
    KEY=VALUE
    # Kommentar
    KEY="Wert mit Leerzeichen"
    (leere Zeilen werden ignoriert)
"""

import logging
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ENV_FILE = SCRIPT_DIR / ".env"

logger = logging.getLogger("env_utils")

_cache: dict[str, str] | None = None


def _parse_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _load() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    if not ENV_FILE.exists():
        logger.warning(".env nicht gefunden: %s", ENV_FILE)
        _cache = {}
    else:
        _cache = _parse_env(ENV_FILE.read_text(encoding="utf-8"))
    return _cache


def get_env(key: str, default: str | None = None) -> str | None:
    """Liest einen Wert aus .env. Gibt default zurück wenn Key fehlt/leer."""
    value = _load().get(key, "")
    return value if value else default


def require_env(*keys: str) -> dict[str, str] | None:
    """
    Liest mehrere Pflicht-Keys aus .env.
    Gibt None zurück (mit Warnung) wenn einer fehlt/leer ist, sonst ein Dict.
    """
    result = {}
    for key in keys:
        value = get_env(key)
        if not value:
            logger.warning("Pflicht-Variable '%s' fehlt oder ist leer in .env", key)
            return None
        result[key] = value
    return result
