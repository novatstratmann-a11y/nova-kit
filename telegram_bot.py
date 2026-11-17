#!/usr/bin/env python3
"""
telegram_bot.py – NOVA Telegram-Bot (Long-Polling Daemon)

Empfängt Kommandos und Freitextnachrichten vom Admin.

Commands:
    /status   – Prüft ob monitor.py läuft + letzte Logzeile
    /signals  – Heutigen Signale aus signals-YYYY-MM-DD.md
    /report   – Fügt Report-Task in heartbeat.md ein
    /help     – Kommando-Übersicht

Freitext:
    Jede Nachricht ohne / wird direkt an Nova (py_nova.py) weitergeleitet.
    Nova antwortet asynchron in Telegram.

Start:
    python telegram_bot.py

Sicherheit: Nur Nachrichten von admin_id (aus .env) werden
verarbeitet. Alle anderen Absender werden ignoriert.
"""

import logging
import logging.handlers
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

try:
    import requests
except ImportError:
    print("Fehler: 'requests' nicht installiert.")
    print("Lösung: pip install requests --break-system-packages")
    sys.exit(1)

from env_utils import get_env

# ---------------------------------------------------------------------------
# Pfade
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
OFFSET_FILE = SCRIPT_DIR / "personality" / ".telegram_offset"
LOG_FILE = SCRIPT_DIR / "personality" / "logs" / "nova.log"
HEARTBEAT_FILE = SCRIPT_DIR / "personality" / "heartbeat.md"
SIGNALS_DIR = SCRIPT_DIR / "personality" / "memory"
MONITOR_LOG = SCRIPT_DIR / "personality" / "logs" / "monitor.log"

TELEGRAM_API = "https://api.telegram.org/bot{token}"

# Wartezeit nach fehlgeschlagenem getUpdates: verdoppelt sich bis zum Maximum,
# damit ein längerer Netzwerkausfall nicht alle 5 s eine Log-Zeile erzeugt
POLL_RETRY_MIN = 5
POLL_RETRY_MAX = 300

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("telegram_bot")


def setup_logging():
    logger.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(console)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)


class TokenRedactFilter(logging.Filter):
    """Ersetzt den Bot-Token in Log-Meldungen (z.B. URLs in requests-Exceptions)."""

    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if self.token in msg:
            record.msg = msg.replace(self.token, "<TOKEN>")
            record.args = None
        return True


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Lädt Telegram-Config aus .env. Bricht ab wenn Token fehlt."""
    token = get_env("TELEGRAM_TOKEN")
    if not token:
        logger.error("Kein gültiger TELEGRAM_TOKEN in .env")
        sys.exit(1)

    return {
        "token": token,
        "channel_id": get_env("TELEGRAM_CHANNEL_ID"),
        "ki_news_channel_id": get_env("TELEGRAM_KI_NEWS_CHANNEL_ID"),
        "admin_id": get_env("TELEGRAM_ADMIN_ID"),
    }


# ---------------------------------------------------------------------------
# Telegram API Helpers
# ---------------------------------------------------------------------------

class TokenInvalid(Exception):
    """Telegram meldet 401 – Token gesperrt/ersetzt."""


# Bei 401 so lange warten, dann Prozess beenden: systemd (Restart=always)
# startet neu und liest dabei .env frisch ein -> ein neuer Token wird ohne
# manuellen Neustart übernommen.
TOKEN_INVALID_EXIT_DELAY = 60


def api_get(token: str, method: str, params: dict = None) -> dict | None:
    url = f"{TELEGRAM_API.format(token=token)}/{method}"
    # Long-Poll timeout (Telegram-seitig) + 15s Puffer für Netzwerk-Overhead
    lp_timeout = (params or {}).get("timeout", 0)
    req_timeout = max(30, lp_timeout + 15)
    try:
        resp = requests.get(url, params=params or {}, timeout=req_timeout)
        data = resp.json()
        if not data.get("ok"):
            logger.warning("API-Fehler bei %s: %s", method, data.get("description"))
            if data.get("error_code") == 401:
                raise TokenInvalid(data.get("description"))
            return None
        return data
    except requests.RequestException as e:
        logger.warning("Netzwerkfehler bei %s: %s", method, e)
        return None


def send_reply(token: str, chat_id: int, text: str, parse_mode: str = "Markdown"):
    url = f"{TELEGRAM_API.format(token=token)}/sendMessage"
    if len(text) > 4096:
        text = text[:4093] + "..."
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.warning("send_reply HTTP %d: %s", resp.status_code, resp.text[:200])
        else:
            logger.info("send_reply OK → chat_id=%d (%d Zeichen)", chat_id, len(text))
    except requests.RequestException as e:
        logger.warning("send_reply Fehler: %s", e)


# ---------------------------------------------------------------------------
# Offset-Persistenz
# ---------------------------------------------------------------------------

def load_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text().strip())
        except ValueError:
            pass
    return 0


def save_offset(offset: int):
    OFFSET_FILE.write_text(str(offset))


# ---------------------------------------------------------------------------
# Command-Handler
# ---------------------------------------------------------------------------

def cmd_status() -> str:
    """Prüft ob monitor.py läuft und gibt letzte Logzeile zurück."""
    # PID-Check über pgrep
    import subprocess
    result = subprocess.run(
        ["pgrep", "-f", "monitor.py"],
        capture_output=True, text=True
    )
    pids = result.stdout.strip()

    if pids:
        status = f"✅ *monitor.py läuft* (PID: {pids.replace(chr(10), ', ')})"
    else:
        status = "🔴 *monitor.py läuft NICHT*"

    # Letzte 20 Logzeilen aus monitor.log
    last_lines = ""
    if MONITOR_LOG.exists():
        try:
            lines = MONITOR_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
            last_lines = "\n".join(lines[-20:]) if lines else "(Log leer)"
        except Exception:
            last_lines = "(Lesefehler)"

    return f"{status}\n\n📋 *Letzte 20 Log-Zeilen:*\n`{last_lines}`"


def cmd_signals() -> str:
    """Gibt den Inhalt der heutigen Signaldatei zurück."""
    today = date.today().isoformat()
    signals_file = SIGNALS_DIR / f"signals-{today}.md"

    if not signals_file.exists():
        return f"📭 Noch keine Signale heute ({today})"

    try:
        content = signals_file.read_text(encoding="utf-8").strip()
    except Exception as e:
        return f"❌ Lesefehler: {e}"

    if not content:
        return f"📭 Signaldatei ist leer ({today})"

    # Zu lang → kürzen
    if len(content) > 3800:
        content = content[-3800:]
        content = "...(gekürzt)\n" + content

    return f"📊 *Signale {today}*\n\n```\n{content}\n```"


def cmd_report() -> str:
    """Fügt einen Report-Task in heartbeat.md ein."""
    today = date.today().isoformat()
    task_line = f"- [ ] Erstelle Telegram-Report für heutige Signale ({today})\n"

    if not HEARTBEAT_FILE.exists():
        return "❌ heartbeat.md nicht gefunden"

    try:
        with open(HEARTBEAT_FILE, "a", encoding="utf-8") as f:
            f.write(task_line)
        return "✅ Report-Task in heartbeat.md eingetragen.\nNova führt ihn beim nächsten Heartbeat aus."
    except Exception as e:
        return f"❌ Schreibfehler: {e}"


def cmd_help() -> str:
    return (
        "🤖 *NOVA Bot – Kommandos*\n\n"
        "/status – Monitor-Status + letzte Logzeile\n"
        "/signals – Heutige RSI-Signale\n"
        "/report – Report-Task in heartbeat.md eintragen\n"
        "/help – Diese Übersicht\n\n"
        "💬 *Freitext:* Schreib einfach eine Nachricht – Nova antwortet direkt."
    )


COMMANDS = {
    "/status": cmd_status,
    "/signals": cmd_signals,
    "/report": cmd_report,
    "/help": cmd_help,
}


# ---------------------------------------------------------------------------
# Nova-Freitext-Handler
# ---------------------------------------------------------------------------

def handle_nova_query(token: str, chat_id: int, text: str):
    """Leitet eine Freitextnachricht an py_nova.py weiter und antwortet in Telegram."""

    MAX_RETRIES = 4
    RETRY_WAIT = 30  # Sekunden zwischen Versuchen

    def _run():
        send_reply(token, chat_id, "🤔 _Denke nach..._")
        nova_script = SCRIPT_DIR / "py_nova.py"
        logger.info("nova_query start → chat_id=%d text=%r", chat_id, text[:60])

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = subprocess.run(
                    [sys.executable, str(nova_script), "--silent", text],
                    capture_output=True,
                    text=True,
                    timeout=660,
                    cwd=str(SCRIPT_DIR),
                )
                output = result.stdout.strip()
                logger.info("nova_query Versuch %d/%d rc=%d stdout=%d Zeichen",
                            attempt, MAX_RETRIES, result.returncode, len(output))

                # Lock-Konflikt: Nova ist gerade beschäftigt → retry
                if result.returncode != 0 and not output:
                    logger.warning("nova_query Versuch %d: Lock aktiv (stderr: %s)",
                                   attempt, result.stderr.strip()[:100])
                    if attempt < MAX_RETRIES:
                        logger.info("nova_query wartet %ds vor Versuch %d...",
                                    RETRY_WAIT, attempt + 1)
                        time.sleep(RETRY_WAIT)
                        continue
                    else:
                        send_reply(token, chat_id,
                                   "⏳ Nova ist länger beschäftigt. Bitte später nochmal versuchen.")
                        return

                if not output:
                    output = "(Keine Antwort)"

                # Telegram-Limit: 4096 Zeichen
                if len(output) > 4000:
                    output = "…(gekürzt)\n" + output[-4000:]

                send_reply(token, chat_id, output, parse_mode="")
                return

            except subprocess.TimeoutExpired:
                logger.error("nova_query Timeout nach 660s (Versuch %d)", attempt)
                send_reply(token, chat_id, "⏰ Timeout – Nova hat zu lange gebraucht.")
                return
            except Exception as e:
                logger.error("handle_nova_query Fehler: %s", e)
                send_reply(token, chat_id, f"❌ Fehler: {e}")
                return

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Long-Polling Loop
# ---------------------------------------------------------------------------

def run_polling(config: dict):
    token = config["token"]
    admin_id = int(config.get("admin_id", 0))
    logger.addFilter(TokenRedactFilter(token))

    if admin_id == 0:
        logger.warning("⚠️  admin_id ist 0 – alle Nutzer können Commands senden!")

    offset = load_offset()
    logger.info("🤖 Bot gestartet | Warte auf Updates (offset=%d)...", offset)

    failures = 0
    while True:
        try:
            data = api_get(token, "getUpdates", {"offset": offset, "timeout": 25})
        except TokenInvalid:
            logger.error("Telegram-Token ungültig (401) – Neustart in %ds, "
                         "um .env neu einzulesen", TOKEN_INVALID_EXIT_DELAY)
            time.sleep(TOKEN_INVALID_EXIT_DELAY)
            sys.exit(3)

        if data is None:
            failures += 1
            delay = min(POLL_RETRY_MIN * 2 ** (failures - 1), POLL_RETRY_MAX)
            logger.info("getUpdates fehlgeschlagen (%d×), nächster Versuch in %ds", failures, delay)
            time.sleep(delay)
            continue

        if failures:
            logger.info("✅ Verbindung zu Telegram wieder da nach %d Fehlversuchen", failures)
            failures = 0

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            save_offset(offset)

            message = update.get("message") or update.get("edited_message")
            if not message:
                continue

            from_id = message.get("from", {}).get("id", 0)
            chat_id = message.get("chat", {}).get("id", 0)
            text = (message.get("text") or "").strip()

            # Sicherheitscheck: nur Admin
            if admin_id != 0 and from_id != admin_id:
                logger.info("Nachricht von unbekanntem Nutzer %d ignoriert", from_id)
                continue

            # Nachrichten ohne Text (Sticker, Foto, Sprachnachricht …) nicht an Nova
            # weiterleiten – ein leerer Prompt lässt Nova sonst frei agieren
            # (z.B. heartbeat.md abarbeiten).
            if not text:
                logger.info("Nachricht ohne Text von %d ignoriert", from_id)
                send_reply(token, chat_id, "ℹ️ Ich verarbeite nur Textnachrichten.", parse_mode="")
                continue

            if not text.startswith("/"):
                # Freitext → direkt an Nova weiterleiten
                logger.info("Freitext-Anfrage von %d: %s", from_id, text[:80])
                handle_nova_query(token, chat_id, text)
                continue

            # Kommando extrahieren (ignoriere @botname-Suffix)
            cmd = text.split()[0].split("@")[0].lower()
            logger.info("Kommando: %s von %d", cmd, from_id)

            handler = COMMANDS.get(cmd)
            if handler:
                try:
                    reply = handler()
                except Exception as e:
                    reply = f"❌ Interner Fehler: {e}"
            else:
                reply = f"Unbekanntes Kommando: `{cmd}`\n/help für Übersicht"

            send_reply(token, chat_id, reply)


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main():
    setup_logging()
    logger.info("=" * 60)
    logger.info("🤖 NOVA Telegram-Bot startet")
    logger.info("=" * 60)

    config = load_config()

    try:
        run_polling(config)
    except KeyboardInterrupt:
        logger.info("🛑 Bot gestoppt (Ctrl+C)")
        sys.exit(0)


if __name__ == "__main__":
    main()
