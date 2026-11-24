#!/usr/bin/env python3
"""
midday_analysis.py – NOVA Mittel-/Langfrist-Marktanalyse via Telegram

Führt täglich mittags eine Marktrecherche (LLM + WebSearch) durch mit Fokus
auf mittel- bis langfristige Renditechancen und sendet die formatierte Analyse
an den Telegram-Kanal.

Idempotent: Läuft ein zweites Mal am selben Tag, macht er nichts.
Marker-Datei: personality/.midday_analysis_YYYY-MM-DD.sent
Analyse-Datei: personality/memory/midday-analysis-YYYY-MM-DD.md

Verwendung:
    python midday_analysis.py              # Sendet wenn noch nicht gesendet
    python midday_analysis.py --force      # Ignoriert Marker, immer senden
    python midday_analysis.py --dry-run    # Zeigt Analyse ohne zu senden
"""

import re
import argparse
import logging
import logging.handlers
import os
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
MEMORY_DIR = SCRIPT_DIR / "personality" / "memory"
LOCK_FILE = SCRIPT_DIR / "personality" / ".midday_analysis.lock"
LOG_FILE = SCRIPT_DIR / "personality" / "logs" / "scripts.log"

logger = logging.getLogger("midday_analysis")


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
    fh.setFormatter(logging.Formatter("%(asctime)s  %(name)-16s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    # telegram_utils-Warnungen (HTTP-Status, Fehlertext) ebenfalls nach scripts.log
    # (Lücke ki_news 14.11.; vorher nur stdout, das Cron verwirft).
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("telegram").addHandler(fh)
    # portfolio-Warnungen (Symbol fehlgeschlagen, Kursabfrage-Fehler) nach scripts.log
    # (2026-11-23; INFO bleibt draußen, performance_context fragt ~25 Kurse je Lauf ab).
    logging.getLogger("portfolio").setLevel(logging.WARNING)
    logging.getLogger("portfolio").addHandler(fh)


# ---------------------------------------------------------------------------
# Analyse-Prompt für Claude
# ---------------------------------------------------------------------------

ANALYSIS_PROMPT_TEMPLATE = """Erstelle eine Mittel-/Langfrist-Marktanalyse für den Telegram-Kanal @dein_kanal.

Recherchiere (WebSearch) die relevanten Entwicklungen der letzten Wochen an den Aktien-,
Krypto-, Devisen-, ETF-, Rohstoff- und Derivatemärkten.

Fokus:
- Mittel- bis langfristige Renditechancen (Zeithorizont: 4 Wochen bis 12 Monate)
- Strukturelle Trends, makroökonomische Katalysatoren, sektorale Rotationen
- Fundamentale Bewertungen, Wachstumspotenziale, geopolitische Risiken
- Opportunitäten durch überverkaufte Sektoren, technologische Disruption oder zyklische Wenden

{recent_analyses_section}

Gib am Ende der Analyse 1-2 konkrete Empfehlungen, jeweils mit:
- Typ (Aktie, ETF, Krypto, Anleihe, Rohstoff, ...)
- Name des Instruments
- WKN oder ISIN (wenn verfügbar, sonst weglassen)
- Aktueller Kurs
- Kursziel
- Zeithorizont (z.B. "3-6 Monate", "bis Ende 2026")
- Begründung (2-4 Sätze mit fundamentaler/technischer Substanz)

Das Mindestpotential für eine Empfehlung beträgt 20% im gewählten Zeithorizont.
Falls heute keine geeigneten Kandidaten mit ≥20% Potential vorhanden sind,
schreibe stattdessen: "Keine aktuellen Mittel-/Langfrist-Empfehlungen."

Formatiere das Ergebnis als fertige Telegram-Nachricht:
- Telegram MarkdownV2: Fett via *Sternchen*, kursiv via _Unterstriche_
- Emojis passend einsetzen
- Beginne mit "🔭 *NOVA Mittel-/Langfrist-Analyse – [heutiges Datum]*"
- Disclaimer-Zeile am Ende: _Analyse dient ausschließlich zu Informationszwecken und stellt keine Anlageberatung dar. Alle Investments bergen Verlustrisiken._
- Letzte Zeile (kein Italic, kein Bold): Nova 🤖 | @dein_kanal
- Schreibe ausschließlich die fertige Nachricht, kein Kommentar davor oder danach
- Länge: höchstens 3500 Zeichen (eine Telegram-Nachricht). Lieber drei Punkte mit Einordnung als
  zehn Kursmeldungen. Indexstände nur, wenn sie für deine Aussage nötig sind – die Zahlen stehen unten.

{portfolio_section}

Speichere die fertige Nachricht in diese Datei (kein Code-Block):
{output_file}
Wenn du kaufst, verkaufst oder einen Stop nachziehst, MÜSSEN die PORTFOLIO_ACTION-Marker in dieselbe
Datei, direkt hinter die letzte Zeile der Nachricht. Nur Marker in der Datei werden ausgeführt – ein
Trade, der nur im Text steht, findet nicht statt. Die Marker werden vor dem Telegram-Versand entfernt.
"""


# ---------------------------------------------------------------------------
# Kontext aus vergangenen Analysen
# ---------------------------------------------------------------------------

def _load_recent_analyses(prefix: str, n: int = 5) -> str:
    """
    Lädt die letzten n Analyse-Dateien und gibt einen kompakten Kontext zurück.
    Je Datei: Kernaussage (erster Absatz nach der Überschrift) + Empfehlungs-/Depot-Zeilen.
    Früher: die ersten 400 Zeichen – das waren fast nur Titel und Indexstände, die
    "nicht wiederholen"-Anweisung hatte damit nichts in der Hand (geändert 2026-11-07).
    """
    files = sorted(MEMORY_DIR.glob(f"{prefix}-*.md"))
    recent = files[-n:]
    if not recent:
        return ""

    def _clean(t: str) -> str:
        return re.sub(r'\\(.)', r'\1', t).replace('*', '').strip()

    parts = []
    for f in reversed(recent):
        date_str = f.stem.replace(prefix + "-", "")
        try:
            content = f.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        body = lines[1:]
        idx = next((i for i, l in enumerate(body) if len(l) > 80), 0)
        core = _clean(body[idx])[:250] if body else ""
        picks = [
            _clean(l)[:150] for l in body[idx + 1:]
            if re.search(r'Empfehlung|[Kk]auf|[Vv]erkauf|Stop|Kursziel|Short|Long', l)
            and not l.startswith(('<!--', '_Analyse', 'ACTION', 'RATIONALE'))
        ][:3]
        entry = f"**{date_str}:** {core}"
        if picks:
            entry += "\n  Empfehlungen/Depot: " + " | ".join(picks)
        parts.append(entry)

    if not parts:
        return ""

    joined = "\n".join(parts)
    return f"""## Deine letzten {len(parts)} Analysen (NICHT wiederholen!)

{joined}

**Wichtig:** Empfiehl heute keine Assets, die du in den letzten Tagen bereits wiederholt empfohlen hast – es sei denn, es gibt einen fundamentalen Grund (neues Ereignis, Kurskorrektur, geändertes Makroumfeld). Suche aktiv nach neuen Chancen in anderen Sektoren oder Assetklassen. Wiederhole auch keine Makro-Erklärungen, die oben schon stehen – schreibe nur, was sich seitdem geändert hat.
"""


# ---------------------------------------------------------------------------
# Portfolio-Integration
# ---------------------------------------------------------------------------

def _portfolio_marker_path(today: str) -> Path:
    return SCRIPT_DIR / "personality" / f".portfolio_actions_{today}_midday.done"


def _performance_context() -> str:
    """Benchmark-Vergleich + Trade-Statistik aus performance.md (leer bei Fehler)."""
    try:
        from portfolio_performance import performance_context
        return performance_context()
    except Exception as exc:  # noqa: BLE001
        logger.warning("performance_context nicht verfügbar: %s", exc)
        return ""


def _build_portfolio_prompt(portfolio) -> str:
    val = portfolio.get_value()
    return f"""
## Dein virtuelles Depot (Startkapital: {val['initial_capital']:,.0f} €)

Verfügbares Cash: {val['cash']:,.2f} €
Gesamtwert: {val['total']:,.2f} € ({val['pnl_pct']:+.1f}%)

Aktuelle Positionen mit Live-Kursen (abgerufen von Yahoo Finance):
{portfolio.get_live_holdings_table()}

Letzte Trades:
{portfolio.get_trade_history(3)}
{_performance_context()}

**Pflicht-Check für Bestandspositionen (via WebSearch):**
Bevor du über Halten, Verkauf oder Stop-Loss-Anpassung entscheidest, recherchiere für JEDE offene Position gezielt Neuigkeiten der letzten 7 Tage (Analysten-Up-/Downgrades, Guidance-Änderungen, M&A, regulatorische Ereignisse, Sektor-News, fundamentale Änderungen an der These). Nur mechanisch Kurs gegen Kursziel/Stop-Loss zu vergleichen reicht nicht. Fasse das Ergebnis für jede Position kurz im Depot-Teil der Nachricht zusammen – auch wenn es nichts Neues gibt (z.B. "keine relevanten News").

**Verkaufsregeln – prüfe deine Positionen:**
- Kursziel erreicht (aktueller Kurs ≥ TARGET) → Teilverkauf: SELL für 60% der Bestandsmenge (Gewinn sichern), für die restlichen 40% im selben Zug per UPDATE_STOP einen engen Stop-Loss nachziehen (5-8% unter aktuellem Kurs oder auf Einstandskurs, je nachdem was höher liegt). So bleibt Restpotenzial nutzbar, ohne den bereits erzielten Gewinn zu riskieren. Setze beide Marker (SELL + UPDATE_STOP) in derselben Antwort.
- Stop-Loss ausgelöst (aktueller Kurs ≤ STOP_LOSS) → SELL, Verlust begrenzen
- These hat sich grundlegend geändert → SELL mit Begründung
- Position hat ≥80% der Distanz zwischen Kaufkurs und Kursziel zurückgelegt → Stop-Loss nachziehen (Trailing-Stop, z.B. auf Einstandskurs oder 5-8% unter aktuellem Kurs) statt bis zum ursprünglichen Stop-Loss zu warten und den ganzen Gewinn zu riskieren. Ein Stop-Loss darf dabei nur angehoben, nie abgesenkt werden → UPDATE_STOP

**Rotation bei fehlendem Cash (nur bei sehr überzeugender neuer Chance):**
Wenn eine neue Chance das 20%-Positions-Limit oder das 40%-Sektor-Limit (siehe Kaufregeln) sprengt und kein/kaum Cash frei ist, darfst du eine bestehende Position verkaufen, um Platz zu schaffen – aber nur wenn ALLE drei Bedingungen erfüllt sind:
1. Die Bestandsposition hat bereits ≥90% der Distanz zum Kursziel zurückgelegt (kaum noch Restpotenzial) ODER die These ist erkennbar geschwächt.
2. Die neue Chance ist KEIN Tagesausschlag von bereits mehreren Prozent (kein Chasing eines Kurssprungs von heute) – Einstieg mit intaktem Setup, nicht mit FOMO.
3. Der technische Pflicht-Check (siehe unten) fällt für die neue Chance mit mindestens 3 von 4 grün aus.
Fehlt eine dieser Bedingungen: HOLD, keine Rotation. Nenne im RATIONALE-Feld explizit "Rotation aus [Position]" mit Begründung, welche der drei Bedingungen erfüllt sind.

**Kaufregeln:**
- Max 20% des Gesamtdepots pro Position
- Max 40% des Gesamtdepots in einem Sektor/Themencluster (inkl. bestehender Positionen im selben Sektor/Thema, z.B. "Halbleiter" oder "Rohstoffe/Bergbau") – verhindert Klumpenrisiko durch mehrere korrelierte Einzelpositionen
- Nur wenn du eine klare mittel-/langfristige Chance siehst (≥20% Potenzial)
- Kein Kauf, wenn der Kurs in den letzten 60 Handelstagen um mehr als 30 % gestiegen ist (Regel R-A1, seit 2026-11-07 in portfolio.py erzwungen – der Kauf würde abgelehnt). Prüfe den 60-Tage-Lauf vorher und nenne ihn im RATIONALE. Steigt ein Wert nur wegen einer Schlagzeile, ist sie meist schon eingepreist.
- Immer mit Kursziel (+20% bis +50%) und Stop-Loss (-12% bis -18%)

**Technischer Pflicht-Check vor jedem Kauf (via WebSearch):**
Recherchiere für jeden Kaufkandidaten diese vier Punkte – und kaufe NUR wenn mindestens 3 von 4 grün sind:
- RSI (14 Tage) < 70 → nicht überkauft ✅ | ≥ 70 → überhitzt, warten ❌
- Kurs > 20-Tage-Durchschnitt → Aufwärtstrend intakt ✅ | darunter → Gegenwind ❌
- Abstand zum 52-Wochen-Hoch ≤ 95% → noch Luft nach oben ✅ | >95% = bereits am ATH ❌
- Volumen der letzten Tage ≥ Durchschnitt → Momentum bestätigt ✅ | fallend → schwaches Signal ❌
Nenne das Ergebnis dieses Checks im RATIONALE-Feld des Markers (z.B. "RSI 58 ✅, über 20MA ✅, 12% unter ATH ✅, Volumen normal ✅").

Wenn du handelst, füge NACH der Telegram-Nachricht (außerhalb des Nachrichtentexts) einen Marker ein.
Für BUY sind TARGET, STOP_LOSS und SECTOR Pflichtfelder:

<!-- PORTFOLIO_ACTION
ACTION: BUY
ASSET: Vollständiger Name
IDENTIFIER: WKN, ISIN oder Ticker
QUANTITY: Anzahl
PRICE_PER_UNIT: Aktueller Kurs in EUR
TOTAL: Gesamtbetrag in EUR
TARGET: Kursziel in EUR
STOP_LOSS: Stop-Loss in EUR
SECTOR: Sektor/Themencluster (z.B. Halbleiter, Rohstoffe/Bergbau, Biotech, Rüstung, E-Commerce, Energie, Schifffahrt, Krypto)
RATIONALE: Kurze Begründung (1-2 Sätze)
-->

Für SELL (bei Teilverkauf wegen erreichtem Kursziel: QUANTITY = 60% der aktuellen Bestandsmenge, danach zusätzlich einen UPDATE_STOP-Marker für die Restmenge):

<!-- PORTFOLIO_ACTION
ACTION: SELL
ASSET: Vollständiger Name
IDENTIFIER: WKN, ISIN oder Ticker
QUANTITY: Anzahl (oder gesamten Bestand)
PRICE_PER_UNIT: Aktueller Kurs in EUR
TOTAL: Gesamterlös in EUR
RATIONALE: Kurze Begründung (Kursziel erreicht – Teilverkauf / Stop-Loss / Thesenänderung)
-->

Für UPDATE_STOP (Stop-Loss nachziehen, wenn eine Position sich dem Kursziel nähert):

<!-- PORTFOLIO_ACTION
ACTION: UPDATE_STOP
ASSET: Vollständiger Name
IDENTIFIER: WKN, ISIN oder Ticker
NEW_STOP_LOSS: Neuer Stop-Loss in EUR (muss über dem bisherigen liegen)
RATIONALE: Kurze Begründung
-->

Kein Marker = HOLD. Handle nur wenn du wirklich überzeugt bist.
"""


def _execute_portfolio_actions(content: str, today: str, dry_run: bool = False):
    marker = _portfolio_marker_path(today)
    if marker.exists() and not dry_run:
        logger.info("Portfolio-Aktionen für heute bereits ausgeführt – übersprungen.")
        return

    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from portfolio import parse_portfolio_actions, Portfolio, get_current_price, find_trade_intents, known_price_symbol
        from telegram_utils import send_message, send_to_admin
        from blog_client import post_to_blog
    except ImportError as exc:
        logger.warning("Portfolio-Module nicht verfügbar: %s", exc)
        return

    actions = parse_portfolio_actions(content)

    if not actions:
        logger.info("Keine Portfolio-Aktionen in dieser Analyse (HOLD).")
        # Konsistenz-Check: Text kündigt Trade an, aber kein (gültiger) Marker → Trade geht verloren
        intents = find_trade_intents(content)
        if intents or "PORTFOLIO_ACTION" in content:
            logger.warning("Text kündigt Trade an, aber kein gültiger PORTFOLIO_ACTION-Marker: %s", intents)
            if not dry_run:
                lines = "\n".join(f"• {h}" for h in intents) or "• (Marker vorhanden, aber unvollständig)"
                send_to_admin(
                    "⚠️ Midday-Analyse {}: Der Text kündigt einen Trade an, aber es wurde kein gültiger "
                    "PORTFOLIO_ACTION-Marker gefunden. Es wurde nichts ausgeführt.\n\n{}".format(today, lines)
                )
        if not dry_run:
            marker.touch()
        return

    # Aktuellen Kurs abrufen und Nova-Schätzung überschreiben (nur BUY/SELL)
    for action in actions:
        if action['action'] not in ('BUY', 'SELL'):
            continue
        result = get_current_price(action['identifier'], action['name'],
                                   known_price_symbol(action['identifier']))
        # Plausibilität (2026-11-14): Die Yahoo-Namenssuche kann ein falsches Symbol
        # treffen (TSMC → 0,96 € statt ~400 €). Weicht der Live-Kurs um mehr als 50 %
        # von Novas Zahl ab, gilt er als Fehltreffer und Novas Zahl bleibt stehen.
        if result is not None and action['price_per_unit'] > 0 and not (
                0.5 <= result['price'] / action['price_per_unit'] <= 1.5):
            logger.warning("Live-Kurs %s unplausibel (%.2f € via %s, Nova %.2f €) – verworfen.",
                           action['identifier'], result['price'], result['symbol'],
                           action['price_per_unit'])
            result = None
        if result is not None:
            live_price = result['price']
            # Beleg in scripts.log (2026-11-23): Der portfolio-Logger schreibt
            # „Aktueller Kurs … (Symbol: …)“ nur nach stdout, Cron verwirft das.
            logger.info("Live-Kurs %s: %.2f € (Symbol: %s)",
                        action['identifier'], live_price, result['symbol'])
            if abs(live_price - action['price_per_unit']) > 0.01:
                logger.info(
                    "Kurskorrektur %s: Nova %.2f € → Live %.2f €",
                    action['identifier'], action['price_per_unit'], live_price,
                )
            action['price_per_unit'] = live_price
            action['total'] = action['quantity'] * live_price
            action['_price_symbol'] = result['symbol']
        else:
            logger.warning(
                "Kein Live-Kurs für %s – verwende Nova-Schätzung %.2f €",
                action['identifier'], action['price_per_unit'],
            )
            action['_price_symbol'] = None

    p = Portfolio()
    for action in actions:
        prefix = "[DRY-RUN] " if dry_run else ""

        if action['action'] == 'UPDATE_STOP':
            logger.info(
                "%sPortfolio-Aktion: UPDATE_STOP %s (%s) → %s €",
                prefix, action['name'], action['identifier'], action['new_stop_loss'],
            )
            if dry_run:
                print(
                    f"\n[DRY-RUN] Würde Stop-Loss nachziehen: {action['name']} "
                    f"({action['identifier']}) → {action['new_stop_loss']:,.2f} €"
                )
                continue

            trade = p.update_stop_loss(
                identifier=action['identifier'],
                new_stop_loss=action['new_stop_loss'],
                source='midday_analysis',
                rationale=action['rationale'],
            )
            if trade is None:
                reason = (
                    f"UPDATE_STOP abgelehnt: {action['name']} – Position unbekannt oder "
                    f"neuer Stop-Loss nicht über dem bisherigen"
                )
                logger.warning(reason)
                try:
                    send_to_admin(f"⚠️ {reason}")
                except Exception:
                    pass
                continue

            try:
                send_message(p.format_stop_update_for_telegram(trade))
            except Exception as exc:
                logger.error("Telegram-Stop-Update-Benachrichtigung fehlgeschlagen: %s", exc)

            try:
                trade_file = MEMORY_DIR / f"portfolio-trade-{trade['timestamp'][:10]}-update-stop-{re.sub(r'[^a-z0-9._-]+', '-', action['identifier'].lower())}.md"
                trade_file.write_text(p.format_stop_update_for_blog(trade), encoding='utf-8')
                post_to_blog(category="portfolio", md_file=trade_file, date=today)
            except Exception as exc:
                logger.warning("Blog-Post für Stop-Update fehlgeschlagen: %s", exc)

            continue

        logger.info(
            "%sPortfolio-Aktion: %s %s (%s) x%s @ %s €",
            prefix, action['action'], action['name'], action['identifier'],
            action['quantity'], action['price_per_unit'],
        )

        if dry_run:
            print(
                f"\n[DRY-RUN] Würde ausführen: {action['action']} {action['name']} "
                f"({action['identifier']}) – {action['quantity']:g} × "
                f"{action['price_per_unit']:,.2f} € = {action['total']:,.2f} €"
            )
            continue

        if action['action'] == 'BUY':
            trade = p.buy(
                name=action['name'],
                identifier=action['identifier'],
                quantity=action['quantity'],
                price_per_unit=action['price_per_unit'],
                source='midday_analysis',
                rationale=action['rationale'],
                target=action.get('target'),
                stop_loss=action.get('stop_loss'),
                price_symbol=action.get('_price_symbol'),
                sector=action.get('sector'),
            )
        else:
            trade = p.sell(
                name=action['name'],
                identifier=action['identifier'],
                quantity=action['quantity'],
                price_per_unit=action['price_per_unit'],
                source='midday_analysis',
                rationale=action['rationale'],
            )

        if trade is None:
            reason = (
                f"Portfolio-Aktion abgelehnt: {action['action']} {action['name']} – "
                f"unzureichend Cash oder 20%-Limit"
            )
            logger.warning(reason)
            try:
                send_to_admin(f"⚠️ {reason}")
            except Exception:
                pass
            continue

        try:
            send_message(p.format_trade_for_telegram(trade))
        except Exception as exc:
            logger.error("Telegram-Trade-Benachrichtigung fehlgeschlagen: %s", exc)

        try:
            trade_file = MEMORY_DIR / f"portfolio-trade-{trade['timestamp'][:10]}-{action['action'].lower()}-{re.sub(r'[^a-z0-9._-]+', '-', action['identifier'].lower())}.md"
            trade_file.write_text(p.format_trade_for_blog(trade), encoding='utf-8')
            post_to_blog(category="portfolio", md_file=trade_file, date=today)
        except Exception as exc:
            logger.warning("Blog-Post für Trade fehlgeschlagen: %s", exc)

    if not dry_run:
        marker.touch()


# ---------------------------------------------------------------------------
# Lock-Verwaltung (eigener Lock, unabhängig von py_nova)
# ---------------------------------------------------------------------------

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)
            return False  # Prozess läuft noch
        except (ValueError, ProcessLookupError, PermissionError):
            logger.warning("Veraltetes Lock gefunden (PID inaktiv), wird entfernt.")
            LOCK_FILE.unlink()
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Marker-Verwaltung (Idempotenz)
# ---------------------------------------------------------------------------

def marker_path(today: str) -> Path:
    return SCRIPT_DIR / "personality" / f".midday_analysis_{today}.sent"


def already_sent(today: str) -> bool:
    return marker_path(today).exists()


def mark_sent(today: str):
    marker_path(today).touch()


# ---------------------------------------------------------------------------
# Analyse ausführen
# ---------------------------------------------------------------------------

def run_analysis(today: str, portfolio_section: str = "", recent_analyses_section: str = "") -> str | None:
    """
    Ruft py_nova run_claude_batch() auf, um die LLM-Marktrecherche durchzuführen.
    Gibt den Inhalt der geschriebenen Analyse-Datei zurück, oder None bei Fehler.
    """
    output_file = MEMORY_DIR / f"midday-analysis-{today}.md"

    # Wenn Datei bereits existiert (z.B. durch --force), einfach lesen
    if output_file.exists() and output_file.stat().st_size > 100:
        logger.info("Analyse-Datei bereits vorhanden: %s", output_file)
        return output_file.read_text(encoding="utf-8").strip()

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from py_nova import run_claude_batch
    except ImportError as e:
        logger.error("Import von py_nova fehlgeschlagen: %s", e)
        return None

    prompt = ANALYSIS_PROMPT_TEMPLATE.format(
        output_file=output_file,
        portfolio_section=portfolio_section,
        recent_analyses_section=recent_analyses_section,
    )

    logger.info("Starte LLM Mittel-/Langfrist-Analyse (Timeout: 20 Minuten)...")
    rc, _ = run_claude_batch(
        prompt=prompt,
        timeout_minutes=20,
        working_dir=str(SCRIPT_DIR),
    )

    if rc != 0:
        logger.error("LLM-Analyse fehlgeschlagen (rc=%d).", rc)
        return None

    if not output_file.exists():
        logger.error("Analyse-Datei wurde nicht erstellt: %s", output_file)
        return None

    content = output_file.read_text(encoding="utf-8").strip()
    if len(content) < 50:
        logger.error("Analyse-Datei zu kurz (%d Zeichen) – vermutlich leer.", len(content))
        return None

    logger.info("Analyse fertig: %d Zeichen.", len(content))
    return content


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    parser = argparse.ArgumentParser(description="NOVA Mittel-/Langfrist-Marktanalyse via Telegram")
    parser.add_argument("--force", action="store_true", help="Marker ignorieren, immer senden")
    parser.add_argument("--dry-run", action="store_true", help="Analyse anzeigen, nicht senden")
    args = parser.parse_args()

    today = date.today().isoformat()

    if not args.force and not args.dry_run and already_sent(today):
        logger.info("Mittel-/Langfrist-Analyse heute bereits gesendet (%s). Übersprungen.", today)
        return 0

    if not acquire_lock():
        logger.warning("Mittel-/Langfrist-Analyse läuft bereits. Übersprungen.")
        return 0

    # Portfolio-Kontext laden
    portfolio_section = ""
    p = None
    try:
        from portfolio import Portfolio
        p = Portfolio()
        portfolio_section = _build_portfolio_prompt(p)
        logger.info("Portfolio-Kontext geladen (Cash: %.2f €)", p.get_value()['cash'])
    except Exception as exc:
        logger.warning("Portfolio-Kontext nicht verfügbar: %s", exc)

    try:
        from market_data import build_market_data_section, build_holdings_section
        portfolio_section += build_market_data_section()
        if p is not None:
            portfolio_section += build_holdings_section(p)
    except Exception as exc:
        logger.warning("Marktdaten nicht verfügbar: %s", exc)

    recent_analyses_section = _load_recent_analyses("midday-analysis", n=5)

    try:
        analysis = run_analysis(today, portfolio_section=portfolio_section,
                                recent_analyses_section=recent_analyses_section)
    except Exception as exc:
        logger.exception("Unerwarteter Fehler in run_analysis: %s", exc)
        analysis = None
    finally:
        release_lock()

    if not analysis:
        return 1

    # Portfolio-Aktionen auswerten
    try:
        _execute_portfolio_actions(analysis, today, dry_run=args.dry_run)
    except Exception as exc:
        logger.warning("Portfolio-Aktionen fehlgeschlagen: %s", exc)

    if args.dry_run:
        print("\n" + "=" * 60)
        print(analysis)
        print("=" * 60)
        print(f"\n[DRY-RUN] {len(analysis)} Zeichen. Nicht gesendet.")
        return 0

    try:
        from telegram_utils import send_message
        from portfolio import strip_portfolio_actions
        ok = send_message(strip_portfolio_actions(analysis))
    except Exception as e:
        logger.error("Telegram-Fehler: %s", e)
        return 1

    if ok:
        mark_sent(today)
        logger.info("✅ Mittel-/Langfrist-Analyse gesendet (%d Zeichen).", len(analysis))
        try:
            from blog_client import post_to_blog
            output_file = MEMORY_DIR / f"midday-analysis-{today}.md"
            post_to_blog(category="midday", md_file=output_file, date=today)
        except Exception as exc:  # Blog ist optional – darf den erfolgreichen Lauf nicht abbrechen
            logger.warning("Blog-Post fehlgeschlagen: %s", exc)
        return 0
    else:
        logger.error("❌ Telegram-Send fehlgeschlagen.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
