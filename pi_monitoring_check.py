#!/usr/bin/env python3
"""
pi_monitoring_check.py – System-Health-Check für den NOVA-Raspberry-Pi

Prüft:
    - Systemlogs (journalctl -p err) seit dem letzten Lauf
    - Docker-Container (blog etc.): gestoppte Container, Error/Critical-Logs
    - Ressourcen: RAM, CPU-Last, Temperatur, Disk-Nutzung ("/")
    - Netzwerk: Default-Gateway per Ping und DNS-Auflösung; meldet Ausfälle
      und nach der Wiederherstellung deren Dauer
    - Unerwartete Neustarts (Boot-Zeit hat sich seit dem letzten Lauf geändert)
    - Unterspannung/Throttling (vcgencmd get_throttled)

Gibt NUR bei Auffälligkeiten etwas auf stdout aus. Bei Warnungen/kritischen
Befunden geht zusätzlich eine Gotify-Notification raus. Ist Gotify nicht
erreichbar (z.B. Netzausfall), wird der Alert gepuffert und nachgeliefert.
Jeder Lauf wird in personality/logs/pi_monitoring.log protokolliert.

Exit-Codes: 0 = ok, 1 = Warnung, 2 = kritisch.

Läuft als normaler User (pi ist in den Gruppen adm/docker, braucht
also kein root für journalctl/docker) – analog zu den anderen NOVA-Cronjobs:

    */15 * * * * cd /home/pi/nova && python pi_monitoring_check.py

Verwendung:
    python pi_monitoring_check.py              # Normaler Check
    python pi_monitoring_check.py --dry-run    # Nur Ausgabe, kein Telegram/State
    python pi_monitoring_check.py --force      # Lock ignorieren
"""

import argparse
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
LOG_FILE = SCRIPT_DIR / "personality" / "logs" / "pi_monitoring.log"
LOCK_FILE = SCRIPT_DIR / "personality" / ".pi_monitoring.lock"
STATE_FILE = SCRIPT_DIR / "personality" / ".pi_monitoring_state.json"

# --- Schwellwerte ---
TEMP_WARN_C = 70.0
TEMP_CRIT_C = 80.0
DISK_WARN_PCT = 80
DISK_CRIT_PCT = 90
MEM_WARN_PCT = 85

# Host, dessen DNS-Auflösung als Netzwerk-Test dient (ohne DNS läuft kein LLM-Aufruf).
DNS_TEST_HOST = "api.anthropic.com"

# Netzwerk-Watchdog: Eskalation nach N aufeinanderfolgenden Fehl-Checks (à 15 Min).
# Braucht /etc/sudoers.d/nova-watchdog (siehe deploy/sudoers-nova-watchdog).
WIFI_RESTART_AFTER = 2        # ab ~30 Min Ausfall: WLAN aus/an
REBOOT_AFTER = 5              # ab ~75 Min und Gateway weiterhin weg: Reboot
MAX_REBOOTS_PER_DAY = 3       # verhindert Reboot-Schleife bei dauerhaft totem Router
MIN_UPTIME_FOR_REBOOT = 1800  # s – direkt nach einem Boot nicht gleich wieder rebooten

# Alerts, die (z.B. bei Netzausfall) nicht zugestellt werden konnten, werden im
# State gepuffert und beim nächsten Lauf nachgeschickt.
MAX_PENDING_ALERTS = 20

# Bits von "vcgencmd get_throttled" (Raspberry Pi Firmware)
THROTTLE_BITS = {
    0x1: "Unterspannung aktuell",
    0x2: "Takt gedrosselt (ARM freq capped) aktuell",
    0x4: "Throttling aktuell",
    0x8: "Soft-Temperaturlimit aktuell",
    0x10000: "Unterspannung seit Boot aufgetreten",
    0x20000: "Takt-Drosselung seit Boot aufgetreten",
    0x40000: "Throttling seit Boot aufgetreten",
    0x80000: "Soft-Temperaturlimit seit Boot erreicht",
}

OK, WARN, CRIT = 0, 1, 2
_LABEL = {OK: "INFO", WARN: "WARNUNG", CRIT: "KRITISCH"}

# Bekannte, unkritische journalctl-Fehler, die nicht gemeldet werden sollen.
# systemd-networkd-wait-online läuft parallel zu NetworkManager auf diesem Pi
# und timet dadurch wiederholt aus, obwohl das Netzwerk (via NetworkManager)
# tatsächlich funktioniert – daher hier bewusst ignoriert statt Alert-Spam.
# Der brcmfmac-WLAN-Treiber des Pi unterstützt kein Signal-Monitoring für
# wpa_supplicants bgscan; die Meldung kommt nach jedem (Re-)Connect und ist
# rein kosmetisch – bgscan fällt dann einfach auf periodisches Scannen zurück.
JOURNAL_IGNORE_PATTERNS = [
    re.compile(r"systemd-networkd-wait-online.*Timeout occurred while waiting for network connectivity"),
    re.compile(r"wpa_supplicant.*bgscan simple: Failed to enable signal strength monitoring"),
]

logger = logging.getLogger("pi_monitoring")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False):
    logger.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            logger.warning("Veraltetes Lock (PID inaktiv), wird entfernt.")
            LOCK_FILE.unlink()
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def run(cmd):
    """Führt einen Befehl aus, gibt stdout zurück oder None bei Fehler."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def run_rc(cmd, timeout: int = 15):
    """Führt einen Befehl aus, gibt den Returncode zurück oder None bei Fehler/Timeout."""
    try:
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout, check=False
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def restart_wifi() -> bool:
    if run_rc(["sudo", "-n", "/usr/bin/nmcli", "radio", "wifi", "off"]) != 0:
        return False
    time.sleep(5)
    return run_rc(["sudo", "-n", "/usr/bin/nmcli", "radio", "wifi", "on"]) == 0


def network_watchdog(state: dict, now: datetime, gateway_down: bool, dry_run: bool):
    """Eskaliert bei anhaltendem Ausfall: erst WLAN-Neustart, dann Reboot.
    Gibt eine Beschreibung der ausgeführten Aktion zurück (oder None)."""
    fails = state.get("net_fail_count", 0)

    # Reboot nur, wenn schon das Gateway weg ist – fehlt nur DNS/Internet,
    # liegt das Problem beim Router/Provider und ein Reboot hilft nicht.
    if fails >= REBOOT_AFTER and gateway_down:
        day_ago = (now - timedelta(days=1)).isoformat()
        reboots = [t for t in state.get("watchdog_reboots", []) if t > day_ago]
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        if len(reboots) >= MAX_REBOOTS_PER_DAY:
            return f"Reboot übersprungen – bereits {len(reboots)} Watchdog-Reboots in 24 h"
        if uptime < MIN_UPTIME_FOR_REBOOT:
            return f"Reboot übersprungen – Uptime erst {uptime / 60:.0f} Min."
        if dry_run:
            return "Reboot (dry-run, nicht ausgeführt)"
        # State vor dem Reboot sichern, damit check_reboot() danach die Ursache kennt
        state["watchdog_reboots"] = reboots + [now.isoformat()]
        state["watchdog_reboot_pending"] = True
        state["last_run"] = now.isoformat()
        save_state(state)
        logger.critical("Netzwerk-Watchdog: Reboot nach %d Fehl-Checks", fails)
        if run_rc(["sudo", "-n", "/usr/sbin/reboot"]) != 0:
            state.pop("watchdog_reboot_pending", None)
            return "Reboot fehlgeschlagen (sudoers-Regel nova-watchdog installiert?)"
        return "Reboot ausgelöst"

    if fails >= WIFI_RESTART_AFTER:
        if dry_run:
            return "WLAN-Neustart (dry-run, nicht ausgeführt)"
        if restart_wifi():
            return "WLAN neu gestartet (nmcli radio wifi off/on)"
        return "WLAN-Neustart fehlgeschlagen (sudoers-Regel nova-watchdog installiert?)"

    return None


def check_network(state: dict, now: datetime, dry_run: bool = False):
    """Prüft Gateway-Erreichbarkeit und DNS. Merkt sich den Ausfallbeginn im State,
    damit nach der Wiederherstellung die Ausfalldauer gemeldet werden kann, und
    stößt bei anhaltendem Ausfall den Watchdog an."""
    findings = []
    problems = []
    gateway_down = False

    route = run(["ip", "route", "show", "default"]) or ""
    m = re.search(r"default via (\S+)", route)
    if not m:
        problems.append("keine Default-Route")
        gateway_down = True
    elif run_rc(["ping", "-c", "2", "-W", "3", m.group(1)]) != 0:
        problems.append(f"Gateway {m.group(1)} nicht erreichbar")
        gateway_down = True

    if run_rc(["getent", "hosts", DNS_TEST_HOST]) != 0:
        problems.append(f"DNS-Auflösung von {DNS_TEST_HOST} fehlgeschlagen")

    down_since = state.get("net_down_since")
    if problems:
        if not down_since:
            down_since = now.isoformat()
            state["net_down_since"] = down_since
        state["net_fail_count"] = state.get("net_fail_count", 0) + 1
        msg = f"Netzwerk gestört seit {down_since}: {'; '.join(problems)}"
        action = network_watchdog(state, now, gateway_down, dry_run)
        if action:
            msg += f" – Watchdog: {action}"
            state.setdefault("net_actions", []).append(f"{now.strftime('%H:%M')} UTC {action}")
        findings.append((CRIT, msg))
        return findings, False

    if down_since:
        minutes = (now - datetime.fromisoformat(down_since)).total_seconds() / 60
        msg = f"Netzwerk wieder da – war ca. {minutes:.0f} Min. gestört (seit {down_since})"
        if state.get("net_actions"):
            msg += f". Watchdog-Aktionen: {'; '.join(state['net_actions'])}"
        findings.append((WARN, msg))
    for key in ("net_down_since", "net_fail_count", "net_actions"):
        state.pop(key, None)
    return findings, True


def check_reboot(state: dict):
    """Meldet, wenn der Pi seit dem letzten Lauf neu gestartet wurde."""
    findings = []
    try:
        btime = int(re.search(r"^btime (\d+)", Path("/proc/stat").read_text(), re.M).group(1))
    except (OSError, AttributeError, ValueError):
        return findings
    # btime verschiebt sich bei NTP-Zeitkorrekturen, daher großzügige Toleranz
    prev = state.get("boot_time")
    if prev is not None and abs(btime - prev) > 120:
        boot = datetime.fromtimestamp(btime, timezone.utc).isoformat(timespec="seconds")
        cause = " durch Netzwerk-Watchdog" if state.get("watchdog_reboot_pending") else ""
        findings.append((WARN, f"Pi wurde{cause} neu gestartet (Boot: {boot}, letzter Check davor: {state.get('last_run')})"))
    state.pop("watchdog_reboot_pending", None)
    state["boot_time"] = btime
    return findings


def check_power(state: dict):
    """Meldet Unterspannung/Throttling, sobald sich der Status ändert (die
    'seit Boot'-Bits bleiben bis zum Reboot gesetzt – sonst Alert-Spam)."""
    findings = []
    out = run(["vcgencmd", "get_throttled"])
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", out or "")
    if not m:
        return findings
    value = int(m.group(1), 16)
    if value and value != state.get("throttled"):
        flags = ", ".join(label for bit, label in THROTTLE_BITS.items() if value & bit)
        sev = CRIT if value & 0x10001 else WARN
        findings.append((sev, f"Stromversorgung/Throttling: {m.group(1)} ({flags}) – Netzteil prüfen"))
    state["throttled"] = value
    return findings


def check_journal_errors(since_iso: str):
    findings = []
    out = run(["journalctl", "-p", "err", "--since", since_iso, "--no-pager", "-q"])
    if out is None:
        findings.append((WARN, "journalctl konnte nicht gelesen werden"))
        return findings
    lines = [l for l in out.splitlines() if l.strip()]
    lines = [l for l in lines if not any(p.search(l) for p in JOURNAL_IGNORE_PATTERNS)]
    if lines:
        preview = "\n    ".join(lines[-10:])
        findings.append((WARN, f"{len(lines)} Fehler-Einträge in journalctl seit {since_iso}:\n    {preview}"))
    return findings


def check_containers():
    findings = []
    out = run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}"])
    if out is None:
        findings.append((WARN, "docker ps fehlgeschlagen (Docker nicht erreichbar?)"))
        return findings

    running = []
    for line in out.splitlines():
        if not line.strip():
            continue
        name, state = line.split("\t", 1)
        if state.strip() != "running":
            findings.append((CRIT, f"Container '{name}' läuft nicht (Status: {state.strip()})"))
        else:
            running.append(name)

    for name in running:
        logs = run(["docker", "logs", "--since", "15m", name])
        if not logs:
            continue
        hits = [l for l in logs.splitlines() if re.search(r"\b(error|critical|fatal)\b", l, re.IGNORECASE)]
        if hits:
            preview = "\n    ".join(hits[-5:])
            findings.append((WARN, f"Container '{name}': {len(hits)} Error/Critical-Log-Zeilen (letzte 15 Min):\n    {preview}"))

    return findings


def check_temperature():
    findings = []
    temp_c = None
    thermal_zone = Path("/sys/class/thermal/thermal_zone0/temp")
    if thermal_zone.exists():
        try:
            temp_c = int(thermal_zone.read_text().strip()) / 1000.0
        except (ValueError, OSError):
            temp_c = None
    if temp_c is None:
        out = run(["vcgencmd", "measure_temp"])
        if out:
            m = re.search(r"temp=([\d.]+)", out)
            if m:
                temp_c = float(m.group(1))

    if temp_c is None:
        findings.append((WARN, "Temperatur konnte nicht ermittelt werden"))
        return findings, None

    if temp_c >= TEMP_CRIT_C:
        findings.append((CRIT, f"CPU-Temperatur kritisch hoch: {temp_c:.1f} C"))
    elif temp_c >= TEMP_WARN_C:
        findings.append((WARN, f"CPU-Temperatur erhöht: {temp_c:.1f} C"))

    return findings, temp_c


def check_memory():
    findings = []
    try:
        meminfo = Path("/proc/meminfo").read_text()
        total = int(re.search(r"MemTotal:\s*(\d+)", meminfo).group(1))
        available = int(re.search(r"MemAvailable:\s*(\d+)", meminfo).group(1))
        used_pct = 100 * (total - available) / total
    except (OSError, AttributeError, ZeroDivisionError):
        findings.append((WARN, "Speichernutzung konnte nicht ermittelt werden"))
        return findings, None

    if used_pct >= MEM_WARN_PCT:
        findings.append((WARN, f"Arbeitsspeicher zu {used_pct:.0f}% belegt"))

    return findings, used_pct


def check_cpu_load():
    findings = []
    try:
        load1, load5, load15 = os.getloadavg()
        cpu_count = os.cpu_count() or 1
    except OSError:
        findings.append((WARN, "CPU-Load konnte nicht ermittelt werden"))
        return findings, None

    if load1 > cpu_count:
        findings.append((WARN, f"CPU-Load hoch: {load1:.2f} (1 Min, {cpu_count} Kerne)"))

    return findings, (load1, load5, load15)


def check_disk():
    findings = []
    usage = shutil.disk_usage("/")
    used_pct = 100 * usage.used / usage.total
    if used_pct >= DISK_CRIT_PCT:
        findings.append((CRIT, f"Disk '/' zu {used_pct:.0f}% voll ({usage.free / 1e9:.1f} GB frei)"))
    elif used_pct >= DISK_WARN_PCT:
        findings.append((WARN, f"Disk '/' zu {used_pct:.0f}% voll ({usage.free / 1e9:.1f} GB frei)"))
    return findings, used_pct


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Alert-Zustellung
# ---------------------------------------------------------------------------

def deliver_pending_alerts(state: dict, now: datetime):
    """Sendet gepufferte Alerts per Gotify. Bei einem Netzausfall ist Gotify selbst
    nicht erreichbar – dann bleiben die Alerts im State und gehen beim nächsten
    Lauf mit Netz raus (älteste zuerst, max. MAX_PENDING_ALERTS)."""
    pending = state.get("pending_alerts", [])[-MAX_PENDING_ALERTS:]
    try:
        from gotify_utils import send_message
    except Exception as e:
        logger.error("Gotify-Import fehlgeschlagen: %s", e)
        state["pending_alerts"] = pending
        return

    while pending:
        ts, severity, report = pending[0]
        title = f"NOVA Pi Monitoring – {_LABEL[severity]}"
        if ts != now.isoformat():
            title += " (verspätet zugestellt)"
        try:
            ok = send_message(report, title=title, priority=8 if severity == CRIT else 5)
        except Exception as e:
            logger.error("Gotify-Alert fehlgeschlagen: %s", e)
            ok = False
        if not ok:
            logger.warning("Alert nicht zustellbar, %d im Puffer für den nächsten Lauf.", len(pending))
            break
        pending.pop(0)

    if pending:
        state["pending_alerts"] = pending
    else:
        state.pop("pending_alerts", None)


def main():
    parser = argparse.ArgumentParser(description="NOVA Pi System-Health-Check")
    parser.add_argument("--dry-run", action="store_true", help="Nur Ausgabe, kein Telegram/State-Update")
    parser.add_argument("--force", action="store_true", help="Lock ignorieren")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    if not args.force and not acquire_lock():
        logger.info("Bereits ein Lauf aktiv, überspringe.")
        return 0

    try:
        now = datetime.now(timezone.utc)
        state = load_state()
        since_iso = state.get("last_run") or (now - timedelta(minutes=15)).isoformat()

        all_findings = []
        all_findings += check_reboot(state)
        all_findings += check_power(state)
        n_findings, net_ok = check_network(state, now, dry_run=args.dry_run)
        all_findings += n_findings
        all_findings += check_journal_errors(since_iso)
        all_findings += check_containers()

        t_findings, temp_c = check_temperature()
        all_findings += t_findings

        m_findings, mem_pct = check_memory()
        all_findings += m_findings

        c_findings, load = check_cpu_load()
        all_findings += c_findings

        d_findings, disk_pct = check_disk()
        all_findings += d_findings

        # Cronjobs: verpasste Termine (je Termin genau eine Meldung, State in .cron_watch_state.json)
        try:
            from cron_watch import check_new_misses
            all_findings += [(WARN, m) for m in check_new_misses(dry_run=args.dry_run)]
        except Exception as e:
            logger.warning("Cron-Watch fehlgeschlagen: %s", e)

        # Claude-Login-Ablauf: eigene Gotify-Meldung mit Cooldown (kein 15-Min-Spam)
        try:
            from claude_auth import check_login_expiry
            check_login_expiry(dry_run=args.dry_run)
        except Exception as e:
            logger.warning("Claude-Login-Check fehlgeschlagen: %s", e)

        severity = max([OK] + [f[0] for f in all_findings])

        report_lines = [f"=== Pi Monitoring Check: {now.strftime('%Y-%m-%d %H:%M:%S UTC')} ==="]
        if temp_c is not None:
            report_lines.append(f"Temperatur: {temp_c:.1f} C")
        if mem_pct is not None:
            report_lines.append(f"RAM belegt: {mem_pct:.0f}%")
        if load is not None:
            report_lines.append(f"CPU-Load (1/5/15 Min): {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}")
        if disk_pct is not None:
            report_lines.append(f"Disk '/' belegt: {disk_pct:.0f}%")
        report_lines.append(f"Netzwerk: {'OK' if net_ok else 'GESTÖRT'}")

        if all_findings:
            report_lines.append("")
            report_lines.append("--- Auffälligkeiten ---")
            for sev, msg in sorted(all_findings, key=lambda f: -f[0]):
                report_lines.append(f"[{_LABEL[sev]}] {msg}")
        else:
            report_lines.append("Keine Auffälligkeiten.")

        report = "\n".join(report_lines)
        logger.info(report)

        if not args.dry_run:
            state["last_run"] = now.isoformat()
            if severity > OK:
                state.setdefault("pending_alerts", []).append([now.isoformat(), severity, report])
            deliver_pending_alerts(state, now)
            save_state(state)

        if severity > OK:
            print(report)

        return severity
    finally:
        if not args.force:
            release_lock()


if __name__ == "__main__":
    sys.exit(main())
