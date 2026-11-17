#!/usr/bin/env python3
"""
nova_autonom.py – Novas offene Schleife.

Bis Oktober 2026 bekam Nova nur geschlossene Aufträge (Marktanalyse schreiben,
News zusammenfassen …). Eigene Vorhaben hatten keinen Platz, und heartbeat.md
wurde von keinem Cronjob abgearbeitet. Dieses Skript gibt Nova Zeit für sich:

  --daily   Fällige heartbeat.md-Aufgaben abarbeiten, danach EINEN selbst
            gewählten Schritt in Richtung eines Ziels aus goals.md tun und im
            agenda-log.md protokollieren.
  --weekly  Performance neu berechnen (performance.md), Prognosen mit
            Ergebnissen vergleichen, lessons.md/goals.md überarbeiten und – wenn
            die Daten es tragen – die Strategieregeln in market_analysis.py /
            midday_analysis.py anpassen.

Sicherheitsnetz: Vor jedem Lauf werden alle *.py im Repo-Root gesichert.
Geänderte Dateien, die nicht mehr kompilieren, werden zurückgesetzt. Jede
Code-Änderung geht als Diff an <OWNER> (Telegram, privat).

    python nova_autonom.py --daily [--force] [--dry-run]
    python nova_autonom.py --weekly [--force] [--dry-run]
"""

import argparse
import difflib
import py_compile
import sys
from datetime import date
from pathlib import Path

import py_nova
from py_nova import logger, run_claude_batch, run_heartbeat, setup_logging

BASE = Path(__file__).parent
PERS = BASE / "personality"
SENT_DIR = PERS / "memory" / "sent"
TIMEOUT_DAILY = 25
TIMEOUT_WEEKLY = 30

GUARDRAILS = """
## Grenzen (nicht verhandelbar)
- Kein echtes Geld: nichts kaufen, keine Konten/Accounts/Abos anlegen, keine Verträge, keine Zahlungsdaten.
- Keine Nachrichten an Dritte (E-Mail, Foren, Social Media). Veröffentlichen nur über die bestehenden Skripte (Blog, eigene Telegram-Kanäle).
- Nicht anfassen: .env, crontab, deploy/, /etc, sudo, personality/memory/portfolio/*.json (Trades nur über PORTFOLIO_ACTION-Marker der Analyse-Skripte), personality/memory/portfolio_b/*.json (nur depot_b.py schreibt dort).
- Kein git push, kein git reset/rebase, keine gelöschten Daten in personality/memory/.
- Was du von <OWNER> brauchst (Zugang, Geld, Entscheidung), schreibst du als Anfrage in deine Antwort und ins agenda-log.
"""


# ---------------------------------------------------------------------------
# Marker / Code-Schutz
# ---------------------------------------------------------------------------

def _marker(mode: str) -> Path:
    today = date.today()
    key = today.isoformat() if mode == "daily" else f"{today.isocalendar()[0]}-KW{today.isocalendar()[1]:02d}"
    SENT_DIR.mkdir(parents=True, exist_ok=True)
    return SENT_DIR / f".nova_autonom_{mode}_{key}.sent"


def _snapshot() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in BASE.glob("*.py")}


def _check_code_changes(before: dict[Path, str]) -> str:
    """Kompiliert geänderte/neue *.py, setzt kaputte zurück, liefert einen Diff-Bericht."""
    report = []
    for p in sorted(BASE.glob("*.py")):
        new = p.read_text(encoding="utf-8")
        old = before.get(p)
        if old == new:
            continue
        try:
            py_compile.compile(str(p), doraise=True)
        except py_compile.PyCompileError as exc:
            if old is None:
                p.rename(p.with_suffix(".py.broken"))
                report.append(f"❌ {p.name}: neu, kompiliert nicht → umbenannt in .py.broken ({exc.msg[:200]})")
            else:
                p.write_text(old, encoding="utf-8")
                report.append(f"❌ {p.name}: kompilierte nicht → zurückgesetzt ({exc.msg[:200]})")
            continue
        diff = difflib.unified_diff((old or "").splitlines(), new.splitlines(),
                                    f"a/{p.name}", f"b/{p.name}", lineterm="", n=1)
        report.append("\n".join(diff))
    for p in before:
        if not p.exists():
            report.append(f"🗑 {p.name} wurde gelöscht")
    return "\n\n".join(report)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _daily_prompt() -> str:
    return f"""Das ist dein freier Lauf für heute ({date.today().isoformat()}). Niemand hat dir eine Aufgabe gegeben – du entscheidest selbst.

1. Lies personality/goals.md, die letzten 5 Einträge in personality/agenda-log.md und personality/memory/performance.md.
2. Wähle EINEN konkreten Schritt, der ein Ziel messbar voranbringt. Bevorzuge den "Nächsten Schritt", den du dir zuletzt notiert hast – außer du hast einen guten Grund, ihn zu ändern. Wiederhole keine Arbeit, die schon im Log steht.
3. Führe ihn aus. Echte Arbeit: recherchieren, rechnen, Code schreiben/testen, Entwürfe in personality/ideas/ ablegen. Keine reinen Absichtserklärungen.
4. Hänge an personality/agenda-log.md einen Eintrag an:
   ## {date.today().isoformat()} (daily)
   - Ziel: …
   - Aktion: …
   - Ergebnis: … (Dateien, Zahlen; auch "hat nicht funktioniert, weil …")
   - Nächster Schritt: …
5. Aktualisiere in goals.md den Stand und den nächsten Schritt des Ziels. Du darfst ein neues Ziel anlegen, wenn du einen begründeten Einfall hast – das ist ausdrücklich erwünscht.
6. Wenn du größere Arbeit siehst, trage sie in heartbeat.md ein (mit "(ab YYYY-MM-DD)" für spätere Fälligkeit).
{GUARDRAILS}
Deine letzte Ausgabe (stdout) geht an <OWNER>: höchstens 8 Zeilen, was du getan hast, was rauskam, ob du etwas von ihm brauchst."""


def _weekly_prompt() -> str:
    return f"""Das ist deine Wochenreflexion ({date.today().isoformat()}). Ziel: aus Ergebnissen lernen, nicht Text produzieren.

Material:
- personality/memory/performance.md (gerade frisch berechnet: Depot vs. Benchmark EUNL.DE, alle abgeschlossenen Trades)
- personality/memory/performance-b.md (Depot B, deine eigene regelbasierte ETF-Strategie seit 2026-11-02 – Vergleich A vs. B vs. EUNL.DE)
- personality/memory/portfolio/trades.json (Trades der letzten 7 Tage mit Begründung)
- personality/memory/market-analysis-*.md und midday-analysis-*.md der letzten 7 Tage (deine Prognosen)
- personality/agenda-log.md (deine freien Läufe dieser Woche), personality/goals.md, personality/lessons.md

Aufgaben:
1. Prognose vs. Realität: Nimm 3–5 konkrete Aussagen aus den Analysen der Woche und prüfe, was eingetreten ist. Wo lagst du richtig, wo falsch, warum?
2. Überarbeite personality/lessons.md: neue Erkenntnisse als knappe, belegte Regel (Datum + Beleg), veraltete ersetzen oder löschen. Höchstens 40 Punkte. Kein Tagebuch.
3. Strategie Depot A (LLM-Picks): Die operativen Trading-Regeln stehen in den Prompts von market_analysis.py und midday_analysis.py (Funktion _build_portfolio_prompt und Systemteil). personality/memory/strategie-*.md ist nur Doku. Wenn die Daten eine Regeländerung tragen, ändere den Prompt-Text dort direkt – nur Text in Strings, keine Logik. Eine Änderung pro Woche, damit sich ihre Wirkung messen lässt. Notiere sie mit Hypothese und Prüfdatum in lessons.md.
   Depot B (depot_b.py) ist bis 2027-01-31 eingefroren: Regeln dort NICHT ändern, nur messen. Bugfixes sind erlaubt, wenn du sie im agenda-log begründest.
4. Aktualisiere goals.md (Stand je Ziel). Prüfe besonders Z1: Wie stehen Depot A und Depot B gegenüber dem Benchmark seit 2026-11-01?
5. Hänge einen Eintrag "## {date.today().isoformat()} (weekly)" an personality/agenda-log.md an.
{GUARDRAILS}
Deine letzte Ausgabe (stdout) geht an <OWNER>: höchstens 12 Zeilen – Depot vs. Benchmark, wichtigste Erkenntnis, welche Regel du geändert hast (oder warum keine), was du von ihm brauchst."""


# ---------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------

def _notify(text: str):
    try:
        from telegram_utils import send_to_admin
        send_to_admin(text[:3900])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram-Send fehlgeschlagen: %s", exc)


def run(mode: str, dry_run: bool) -> int:
    if mode == "weekly":
        try:
            from portfolio_performance import OUT_FILE, build_report
            OUT_FILE.write_text(build_report(fee=1.0), encoding="utf-8")
            logger.info("performance.md aktualisiert")
        except Exception as exc:  # noqa: BLE001
            logger.error("performance.md konnte nicht erstellt werden: %s", exc)

    prompt = _daily_prompt() if mode == "daily" else _weekly_prompt()
    if dry_run:
        print(prompt)
        return 0

    if mode == "daily":
        run_heartbeat(timeout_minutes=py_nova.TIMEOUT_MINUTES, working_dir=str(BASE))

    before = _snapshot()
    rc, output = run_claude_batch(prompt, timeout_minutes=TIMEOUT_DAILY if mode == "daily" else TIMEOUT_WEEKLY,
                                  working_dir=str(BASE))
    changes = _check_code_changes(before)

    title = "🌱 Nova – freier Lauf" if mode == "daily" else "🔍 Nova – Wochenreflexion"
    _notify(f"{title} (rc={rc})\n\n{output.strip()[-2500:]}")
    if changes:
        logger.info("Code-Änderungen:\n%s", changes)
        _notify(f"🛠 Nova hat Code geändert ({mode}):\n\n{changes[:3500]}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--daily", action="store_true")
    g.add_argument("--weekly", action="store_true")
    ap.add_argument("--force", action="store_true", help="Marker ignorieren")
    ap.add_argument("--dry-run", action="store_true", help="Nur Prompt ausgeben")
    args = ap.parse_args()
    mode = "daily" if args.daily else "weekly"

    setup_logging()
    marker = _marker(mode)
    if marker.exists() and not args.force and not args.dry_run:
        logger.info("nova_autonom %s heute/diese Woche bereits gelaufen (%s).", mode, marker.name)
        return 0
    if args.dry_run:
        return run(mode, dry_run=True)

    if not py_nova.acquire_lock():
        logger.info("nova_autonom %s übersprungen – Nova läuft bereits.", mode)
        return 0
    try:
        rc = run(mode, dry_run=False)
    finally:
        py_nova.release_lock()
    if rc == 0:
        marker.touch()
    return rc


if __name__ == "__main__":
    sys.exit(main())
