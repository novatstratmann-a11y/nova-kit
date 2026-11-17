#!/usr/bin/env python3
"""
telegram_utils.py – Gemeinsame Telegram-Hilfsfunktionen für NOVA

Kein Daemon, nur Funktionen:
    load_config()         → liest Telegram-Config aus .env
    send_message(text)    → POST zur Telegram Bot API

Verwendung als Testskript:
    python telegram_utils.py
"""

import logging
import re

try:
    import requests
except ImportError:
    requests = None  # type: ignore

from env_utils import get_env

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LENGTH = 4096

logger = logging.getLogger("telegram")

# ---------------------------------------------------------------------------
# MarkdownV2-Konvertierung
# ---------------------------------------------------------------------------

# Alle MarkdownV2-Sonderzeichen die escaped werden müssen
_MDV2_ALL = re.compile(r'([\\`\[\]()~>#+\-=|{}.!])')

# Erkenne *bold*- und _italic_-Spans.
# Lookbehind/Lookahead: Marker darf nicht direkt an alphanumerische Zeichen grenzen,
# damit z.B. @dein_kanal nicht als Formatierung erkannt wird.
_FMT_SPAN = re.compile(r'(?<![a-zA-Z0-9])(\*[^*\n]+\*|_[^_\n]+_)(?![a-zA-Z0-9])')


def _esc(text: str) -> str:
    """Escape alle MarkdownV2-Sonderzeichen (außer * und _ – werden separat behandelt)."""
    return _MDV2_ALL.sub(r'\\\1', text)


def to_markdownv2(text: str) -> str:
    """
    Konvertiert einfaches Markdown (*bold*, _italic_) zu gültigem Telegram MarkdownV2.

    Fließtext: alle MarkdownV2-Sonderzeichen werden escaped (inkl. * und _).
    Formatierungs-Spans (*...*  _..._): Inhalt wird escaped, Marker bleiben.
    """
    # Claude escaped slash as \/ – das ist in MarkdownV2 unnötig und wird hier entfernt
    text = text.replace('\\/', '/')
    parts = _FMT_SPAN.split(text)
    result = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            # Formatierungs-Span: Marker erhalten, Inhalt escapen
            marker = part[0]
            inner = _esc(part[1:-1])
            result.append(f"{marker}{inner}{marker}")
        else:
            # Fließtext: * und _ auch escapen
            result.append(re.sub(r'([\\*_`\[\]()~>#+\-=|{}.!])', r'\\\1', part))
    return "".join(result)


def load_config() -> dict | None:
    """
    Liest Telegram-Config aus .env.
    Gibt None zurück wenn TELEGRAM_TOKEN oder TELEGRAM_CHANNEL_ID fehlen.
    """
    token = get_env("TELEGRAM_TOKEN")
    if not token:
        logger.warning("Kein gültiger TELEGRAM_TOKEN in .env")
        return None

    channel_id = get_env("TELEGRAM_CHANNEL_ID")
    if not channel_id:
        logger.warning("Keine TELEGRAM_CHANNEL_ID in .env")
        return None

    return {
        "token": token,
        "channel_id": channel_id,
        "ki_news_channel_id": get_env("TELEGRAM_KI_NEWS_CHANNEL_ID"),
        "admin_id": get_env("TELEGRAM_ADMIN_ID"),
    }


def send_to_admin(text: str, parse_mode: str = "MarkdownV2") -> bool:
    """
    Sendet eine Nachricht direkt an den Admin (private Chat).
    Verwendet TELEGRAM_ADMIN_ID aus .env statt channel_id.
    Gedacht für interne Statusmeldungen, Heartbeat-Ergebnisse etc.
    """
    config = load_config()
    if config is None:
        return False
    admin_id = config.get("admin_id")
    if not admin_id:
        logger.warning("Keine TELEGRAM_ADMIN_ID in .env")
        return False
    return send_message(text, parse_mode=parse_mode, channel_id=str(admin_id))


def _split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """
    Teilt einen langen Text an Absatz-/Zeilengrenzen in Teile ≤ limit.
    Verhindert abgeschnittene Markdown-Entities.
    """
    if len(text) <= limit:
        return [text]

    parts = []
    # Zuerst an Doppel-Zeilenumbrüchen (Absätze) trennen
    paragraphs = text.split("\n\n")
    current = ""
    for para in paragraphs:
        candidate = (current + "\n\n" + para).lstrip("\n") if current else para
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                parts.append(current)
            # Absatz selbst zu lang? An Einzel-Zeilenumbrüchen trennen
            if len(para) > limit:
                lines = para.split("\n")
                current = ""
                for line in lines:
                    candidate = (current + "\n" + line).lstrip("\n") if current else line
                    if len(candidate) <= limit:
                        current = candidate
                    else:
                        if current:
                            parts.append(current)
                        # Einzelne Zeile zu lang: hart abschneiden (Notfall)
                        current = line[:limit]
            else:
                current = para
    if current:
        parts.append(current)
    return parts


def send_message(text: str, parse_mode: str = "MarkdownV2", channel_id: str | None = None) -> bool:
    """
    Sendet eine Nachricht an den konfigurierten Telegram-Kanal.
    Nachrichten über 4096 Zeichen werden in mehrere Teile aufgeteilt.

    Args:
        text:       Nachrichtentext (für MarkdownV2 wird to_markdownv2() automatisch angewandt)
        parse_mode: 'MarkdownV2' (default), 'Markdown' oder 'HTML'
        channel_id: Optionaler Kanal-Override (z.B. '@dein_ki_kanal')
                    Wenn None, wird channel_id aus der Config verwendet.

    Returns:
        True bei Erfolg aller Teile, False bei Fehler (keine Exception)
    """
    if parse_mode == "MarkdownV2":
        text = to_markdownv2(text)
    if requests is None:
        logger.error("'requests' nicht installiert. Lösung: pip install requests --break-system-packages")
        return False

    config = load_config()
    if config is None:
        return False

    parts = _split_message(text)
    if len(parts) > 1:
        logger.info("Nachricht zu lang (%d Zeichen), wird in %d Teile aufgeteilt.", len(text), len(parts))

    url = TELEGRAM_API.format(token=config["token"])
    target = channel_id if channel_id else config["channel_id"]

    for i, part in enumerate(parts, 1):
        payload = {
            "chat_id": target,
            "text": part,
            "parse_mode": parse_mode,
        }
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                logger.debug("Telegram-Teil %d/%d gesendet (%d Zeichen)", i, len(parts), len(part))
            elif response.status_code == 400 and parse_mode:
                # Markdown-Parse-Fehler: nochmal ohne Formatierung senden
                logger.warning(
                    "Telegram Markdown-Fehler (Teil %d/%d), Fallback auf Plaintext: %s",
                    i, len(parts), response.text[:200],
                )
                plain_payload = {k: v for k, v in payload.items() if k != "parse_mode"}
                plain_response = requests.post(url, json=plain_payload, timeout=10)
                if plain_response.status_code == 200:
                    logger.info("Telegram-Teil %d/%d als Plaintext gesendet.", i, len(parts))
                else:
                    logger.warning(
                        "Telegram Plaintext-Fallback fehlgeschlagen (Teil %d/%d): %s",
                        i, len(parts), plain_response.text[:200],
                    )
                    return False
            else:
                logger.warning(
                    "Telegram API Fehler %d (Teil %d/%d): %s",
                    response.status_code, i, len(parts),
                    response.text[:200],
                )
                return False
        except requests.RequestException as e:
            logger.warning("Telegram-Verbindungsfehler (Teil %d/%d): %s", i, len(parts), e)
            return False

    return True


# ---------------------------------------------------------------------------
# Testmodus (direkter Aufruf)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)-8s %(message)s")
    print("Sende Testnachricht an Telegram...")
    ok = send_message("🤖 *Nova online* – Telegram-Integration aktiv.")
    print("✅ Gesendet." if ok else "❌ Fehler beim Senden (siehe Logs).")
