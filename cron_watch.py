#!/usr/bin/env python3
"""
cron_watch.py – Prüft, ob die NOVA-Cronjobs wirklich gelaufen sind (Ziel Z3).

Für jeden Job wird der letzte planmäßige Termin bestimmt, dessen Kulanzzeit
schon abgelaufen ist. Für diesen Termin muss ein Beleg existieren:
  - eine Marker-Datei (.sent) mit dem passenden Datums-/Wochenschlüssel, oder
  - eine Zeile in personality/logs/scripts.log mit dem Job-Namen im Zeitfenster
    [Termin, Termin + Kulanz].

Jeder verpasste Termin wird genau einmal gemeldet (State in
personality/.cron_watch_state.json). Aufgerufen aus pi_monitoring_check.py
(alle 15 Min.), dadurch geht die Meldung über Gotify inkl. Puffer.

Verwendung:
    python cron_watch.py            # Status aller Jobs ausgeben (ändert keinen State)
    python cron_watch.py --days 14  # Rückblick: alle verpassten Termine der letzten 14 Tage
"""

import argparse
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

SCRIPT_DIR = Path(__file__).parent
PERS = SCRIPT_DIR / "personality"
SENT = PERS / "memory" / "sent"
SCRIPTS_LOG = PERS / "logs" / "scripts.log"
STATE_FILE = PERS / ".cron_watch_state.json"

# Termine vor diesem Datum werden nie gemeldet (Start der Überwachung).
# Steht in personality/.watch_since (ISO-Datum). Fehlt die Datei (Neuinstallation),
# wird sie beim ersten Lauf mit dem heutigen Datum angelegt – sonst meldet eine
# frische Installation alle Termine vor ihrer Existenz als verpasst.
WATCH_SINCE_FILE = PERS / ".watch_since"


def _load_watch_since() -> datetime:
    try:
        return datetime.fromisoformat(WATCH_SINCE_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            WATCH_SINCE_FILE.write_text(today.date().isoformat() + "\n", encoding="utf-8")
        except OSError:
            pass
        return today


WATCH_SINCE = _load_watch_since()

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
WEEKDAYS = (MON, TUE, WED, THU, FRI)
ALL_DAYS = tuple(range(7))


def _d(t: datetime) -> str:
    return t.date().isoformat()


def _w(t: datetime) -> str:          # ki_news / nova_gedanken: 2026-W44
    y, w, _ = t.isocalendar()
    return f"{y}-W{w:02d}"


def _kw(t: datetime) -> str:         # nova_autonom weekly: 2026-KW45
    y, w, _ = t.isocalendar()
    return f"{y}-KW{w:02d}"


@dataclass
class Job:
    name: str
    hour: int
    minute: int
    days: tuple
    grace_h: float
    marker: Optional[Callable[[datetime], Path]] = None   # Beleg per Marker
    log_name: Optional[str] = None                        # Beleg per scripts.log
    start: Optional[datetime] = None                      # neue Jobs: erst ab hier überwachen


IG_AUTO_START = datetime(2026, 11, 19, 12, 0)   # instagram_auto.py eingeführt
IG_WELTLAGE_START = datetime(2026, 11, 21, 12, 0)  # --quelle weltlage (erster Termin Sa 21.11.)
UMWELT_START = datetime(2026, 11, 20, 6, 0)      # umwelt_tipp.py + instagram_auto --quelle umwelt (ab Fr 20.11.)
FLOW_START = datetime(2026, 11, 24, 7, 0)        # flow_paket.py --quelle umwelt (ab Di 24.11.)

JOBS = [
    Job("market_analysis", 8, 0, WEEKDAYS, 3, marker=lambda t: PERS / f".market_analysis_{_d(t)}.sent"),
    Job("midday_analysis", 12, 0, WEEKDAYS, 3, marker=lambda t: PERS / f".midday_analysis_{_d(t)}.sent"),
    Job("depot_b", 17, 45, WEEKDAYS, 2, log_name="depot_b"),
    Job("ki_news --daily", 20, 0, ALL_DAYS, 3, marker=lambda t: SENT / f".ki_news_daily_{_d(t)}.sent"),
    Job("ki_news --weekly", 17, 0, (SUN,), 3, marker=lambda t: SENT / f".ki_news_weekly_{_w(t)}.sent"),
    Job("erklaer_mir_kurz", 21, 0, ALL_DAYS, 2.5, marker=lambda t: SENT / f".erklaer_mir_kurz_{_d(t)}.sent"),
    Job("nova_gedanken", 19, 0, (SUN,), 3, marker=lambda t: SENT / f".nova_gedanken_{_w(t)}.sent"),
    Job("weltlage", 18, 0, (WED, SAT), 3, marker=lambda t: SENT / f".weltlage_{_d(t)}.sent"),
    Job("nova_autonom --daily", 15, 0, ALL_DAYS, 3, marker=lambda t: SENT / f".nova_autonom_daily_{_d(t)}.sent"),
    Job("nova_autonom --weekly", 10, 0, (SAT,), 4, marker=lambda t: SENT / f".nova_autonom_weekly_{_kw(t)}.sent"),
    Job("git_weekly_sync", 23, 0, (SUN,), 1, log_name="git_weekly_sync"),
    # Instagram seit 19.11.: Wochen-Slots ersetzt durch stündliches --naechster (ohne Marker, nicht überwacht)
    Job("instagram_auto --quelle markt", 9, 15, WEEKDAYS, 2,
        marker=lambda t: SENT / f".instagram_auto_markt_{t:%Y-%m-%d}.sent", start=IG_AUTO_START),
    Job("instagram_auto --quelle ki", 20, 30, ALL_DAYS, 2,
        marker=lambda t: SENT / f".instagram_auto_ki_{t:%Y-%m-%d}.sent", start=IG_AUTO_START),
    Job("instagram_auto --quelle emk", 21, 20, ALL_DAYS, 2,
        marker=lambda t: SENT / f".instagram_auto_emk_{t:%Y-%m-%d}.sent", start=IG_AUTO_START),
    Job("umwelt_tipp", 7, 0, ALL_DAYS, 2.5, marker=lambda t: SENT / f".umwelt_tipp_{t:%Y-%m-%d}.sent", start=UMWELT_START),
    Job("instagram_auto --quelle umwelt", 12, 30, ALL_DAYS, 2,
        marker=lambda t: SENT / f".instagram_auto_umwelt_{t:%Y-%m-%d}.sent", start=UMWELT_START),
    Job("flow_paket", 7, 40, ALL_DAYS, 2,
        marker=lambda t: SENT / f".flow_paket_umwelt_{t:%Y-%m-%d}.sent", start=FLOW_START),
    Job("instagram_auto --quelle weltlage", 19, 10, (WED, SAT), 2,
        marker=lambda t: SENT / f".instagram_auto_weltlage_{t:%Y-%m-%d}.sent", start=IG_WELTLAGE_START),
]

# Stündlicher Job: letzter Logeintrag darf höchstens so alt sein.
HOURLY = [("email_checker", 3.0)]

_LOG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\S+)")


def _read_log_times() -> dict:
    """job_name -> sortierte Liste der Log-Zeitstempel."""
    out: dict = {}
    try:
        with SCRIPTS_LOG.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _LOG_RE.match(line)
                if m:
                    out.setdefault(m.group(2), []).append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    except FileNotFoundError:
        pass
    return out


def due_slots(job: Job, now: datetime, since: datetime) -> list:
    """Alle planmäßigen Termine in [since, now - Kulanz]."""
    slots = []
    if job.start:
        since = max(since, job.start)
    day = since.date()
    while day <= now.date():
        if day.weekday() in job.days:
            t = datetime(day.year, day.month, day.day, job.hour, job.minute)
            if since <= t and t + timedelta(hours=job.grace_h) <= now:
                slots.append(t)
        day += timedelta(days=1)
    return slots


def slot_ok(job: Job, t: datetime, logs: dict) -> bool:
    if job.marker and job.marker(t).exists():
        return True
    if job.log_name:
        end = t + timedelta(hours=job.grace_h)
        return any(t <= x <= end for x in logs.get(job.log_name, []))
    return False


def _installed(name: str) -> bool:
    """Nur Jobs prüfen, deren Skript existiert (Teilinstallationen, z. B. ohne Mail)."""
    return (SCRIPT_DIR / f"{name.split()[0]}.py").exists()


def find_misses(now: Optional[datetime] = None, since: Optional[datetime] = None) -> list:
    """Liste (key, text) aller verpassten Termine im Zeitraum."""
    now = now or datetime.now()
    since = max(since or (now - timedelta(days=2)), WATCH_SINCE)
    logs = _read_log_times()
    misses = []
    for job in (j for j in JOBS if _installed(j.name)):
        for t in due_slots(job, now, since):
            if not slot_ok(job, t, logs):
                misses.append((f"{job.name}@{t:%Y-%m-%d %H:%M}",
                               f"Cronjob {job.name} von {t:%a %d.%m. %H:%M} ohne Beleg "
                               f"(Marker/Log fehlt {job.grace_h:g} h nach Termin)"))
    for name, max_age in HOURLY:
        if not _installed(name):
            continue
        times = logs.get(name, [])
        last = times[-1] if times else None
        if now - WATCH_SINCE > timedelta(hours=max_age) and (last is None or now - last > timedelta(hours=max_age)):
            key = f"{name}@stale@{(last or WATCH_SINCE):%Y-%m-%d %H:%M}"
            misses.append((key, f"Cronjob {name} (stündlich) seit "
                                f"{last:%d.%m. %H:%M} ohne Logeintrag" if last else f"Cronjob {name} ohne Logeintrag"))
    return misses


def check_new_misses(dry_run: bool = False) -> list:
    """Für pi_monitoring_check: nur noch nicht gemeldete Ausfälle, je einmal."""
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    reported = set(state.get("reported", []))
    new = [(k, msg) for k, msg in find_misses() if k not in reported]
    if new and not dry_run:
        # nur Schlüssel der letzten ~14 Tage behalten
        cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        keep = [k for k in reported if re.search(r"@(?:stale@)?(\d{4}-\d{2}-\d{2})", k)
                and re.search(r"(\d{4}-\d{2}-\d{2})", k).group(1) >= cutoff]
        state["reported"] = sorted(set(keep) | {k for k, _ in new})
        STATE_FILE.write_text(json.dumps(state, indent=2))
    return [msg for _, msg in new]


def main():
    ap = argparse.ArgumentParser(description="Prüft, ob die NOVA-Cronjobs gelaufen sind")
    ap.add_argument("--days", type=float, default=2, help="Rückblick in Tagen (Standard 2)")
    ap.add_argument("--since-all", action="store_true", help="WATCH_SINCE ignorieren (historischer Test)")
    args = ap.parse_args()
    global WATCH_SINCE
    if args.since_all:
        WATCH_SINCE = datetime(2000, 1, 1)
    now = datetime.now()
    misses = find_misses(now, now - timedelta(days=args.days))
    if not misses:
        print(f"Alle Cronjobs der letzten {args.days:g} Tage belegt.")
    for _, msg in misses:
        print("FEHLT:", msg)
    return 1 if misses else 0


if __name__ == "__main__":
    raise SystemExit(main())
