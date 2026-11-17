#!/usr/bin/env python3
"""
portfolio_performance.py – Ehrliche Erfolgsmessung des Paper-Depots.

Vergleicht Novas Depot (zu aktuellen Kursen) mit einem Buy-and-Hold-Benchmark
(Standard: iShares Core MSCI World, EUNL.DE) über denselben Zeitraum und wertet
die Trades aus: Trefferquote, Ø Gewinn/Verlust, Haltedauer, Gebühren-Effekt und
"Was wäre, wenn nicht verkauft" für geschlossene Positionen.

Nur lesend – ändert weder portfolio.json noch trades.json.

    python portfolio_performance.py                 # Bericht auf stdout
    python portfolio_performance.py --save          # zusätzlich nach personality/memory/performance.md
    python portfolio_performance.py --fee 1.0       # Gebühr pro Order in EUR (Default 1,00)
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from portfolio import get_current_price

BASE = Path(__file__).parent
PORTFOLIO_FILE = BASE / "personality" / "memory" / "portfolio" / "portfolio.json"
TRADES_FILE = BASE / "personality" / "memory" / "portfolio" / "trades.json"
OUT_FILE = BASE / "personality" / "memory" / "performance.md"
# Kompakte Kennzahlen für die öffentliche Nova-Seite im Blog (Verzeichnis ist im Container gemountet)
SUMMARY_FILE = BASE / "personality" / "memory" / "portfolio" / "performance.json"
BENCHMARK = "EUNL.DE"  # iShares Core MSCI World UCITS ETF (Xetra, EUR)
# Ziel Z1 (personality/goals.md): Wertungszeitraum mit eingefrorenem Startwert.
# Die Baseline wird beim ersten Lauf ab Z1_START einmalig geschrieben und danach nie überschrieben.
Z1_START = "2026-11-01"
Z1_END = "2027-01-31"
Z1_BASELINE_FILE = BASE / "personality" / "memory" / "z1_baseline.json"


def _benchmark_return(symbol: str, start: datetime) -> tuple[float, float, float] | None:
    """(Startkurs, aktueller Kurs, Rendite) des Benchmarks seit `start`."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?interval=1d&period1={int(start.timestamp())}&period2={int(datetime.now().timestamp())}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (NOVA-Perf/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        res = data["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        last = res["meta"].get("regularMarketPrice") or closes[-1]
        return closes[0], last, last / closes[0] - 1
    except Exception as exc:  # noqa: BLE001
        print(f"Benchmark-Abfrage fehlgeschlagen: {exc}", file=sys.stderr)
        return None


def _z1_period(total: float, orders: int, bm_last: float | None, fee: float) -> dict | None:
    """Rendite Depot vs. Benchmark seit der Z1-Baseline (legt sie beim ersten Aufruf an)."""
    today = datetime.now().date().isoformat()
    if today < Z1_START or bm_last is None:
        return None
    if Z1_BASELINE_FILE.exists():
        base = json.loads(Z1_BASELINE_FILE.read_text(encoding="utf-8"))
    else:
        base = {"created": datetime.now().isoformat(timespec="minutes"), "start": Z1_START, "end": Z1_END,
                "depot_value": round(total, 2), "orders": orders,
                "benchmark": BENCHMARK, "benchmark_price": round(bm_last, 4),
                "note": "Depot zu Live-Kursen (Wochenende = letzter Schlusskurs), brutto. Nicht überschreiben."}
        Z1_BASELINE_FILE.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
    new_orders = orders - base["orders"]
    depot_ret = (total - new_orders * fee) / base["depot_value"] - 1
    bm_ret = bm_last / base["benchmark_price"] - 1
    return {"start": base["start"], "end": base["end"], "base_value": base["depot_value"],
            "new_orders": new_orders, "depot_ret": depot_ret, "bm_ret": bm_ret,
            "gap_pp": (depot_ret - bm_ret) * 100, "final": today > base["end"]}


def _daily_closes(symbol: str, start: datetime) -> list[tuple[datetime, float]]:
    """Tagesschlusskurse (Zeitstempel, Kurs) seit `start`; leer bei Fehler."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?interval=1d&period1={int(start.timestamp())}&period2={int(datetime.now().timestamp())}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (NOVA-Perf/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read().decode())["chart"]["result"][0]
        return [(datetime.fromtimestamp(ts), c) for ts, c in
                zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c is not None]
    except Exception as exc:  # noqa: BLE001
        print(f"Kursreihe {symbol} fehlgeschlagen: {exc}", file=sys.stderr)
        return []


def _z1_cash_shadow(trades: list[dict], holdings_val: float, bm_last: float | None, fee: float) -> dict | None:
    """Schattenrechnung (Entscheidung 2026-11-07, agenda-log): Was hätte Depot A im Z1-Zeitraum,
    wenn freies Cash statt bar in EUNL.DE gelegen hätte? Gleiche Einzelwert-Trades, Cash-Bewegungen
    werden zum letzten EUNL-Schluss vor dem Trade in EUNL-Anteile umgerechnet (ohne Extra-Gebühren
    für die EUNL-Umschichtung). Nur Messung, ändert nichts am Depot und fließt nicht in die Prompts."""
    if not Z1_BASELINE_FILE.exists() or bm_last is None:
        return None
    base = json.loads(Z1_BASELINE_FILE.read_text(encoding="utf-8"))
    base_ts = datetime.fromisoformat(base["created"])
    flows = [t for t in trades if t["action"] in ("BUY", "SELL") and "cash_after" in t]
    before = [t for t in flows if datetime.fromisoformat(t["timestamp"]) < base_ts]
    after = [t for t in flows if datetime.fromisoformat(t["timestamp"]) >= base_ts]
    if not before:
        return None
    cash_prev = before[-1]["cash_after"]
    closes = _daily_closes(BENCHMARK, base_ts.replace(hour=0, minute=0) - timedelta(days=7)) if after else []
    units = cash_prev / base["benchmark_price"]
    for t in after:
        ts = datetime.fromisoformat(t["timestamp"])
        prior = [c for d, c in closes if d.date() < ts.date()]
        if not prior:
            return None
        units += (t["cash_after"] - cash_prev) / prior[-1]
        cash_prev = t["cash_after"]
    shadow_total = holdings_val + units * bm_last - len(after) * fee
    shadow_ret = shadow_total / base["depot_value"] - 1
    bm_ret = bm_last / base["benchmark_price"] - 1
    return {"shadow_ret": shadow_ret, "gap_pp": (shadow_ret - bm_ret) * 100, "units": units,
            "cash_value": units * bm_last}


def _closed_positions(trades: list[dict]) -> list[dict]:
    """Rekonstruiert abgeschlossene Round-Trips (Durchschnittskosten-Methode)."""
    pos = defaultdict(lambda: {"qty": 0.0, "cost": 0.0, "first_buy": None})
    closed = []
    for t in trades:
        key = t["name"].split(" (")[0].strip()
        if t["action"] == "BUY":
            p = pos[key]
            p["qty"] += t["quantity"]
            p["cost"] += t["total_price"]
            p["first_buy"] = p["first_buy"] or t["timestamp"]
            p["identifier"] = t["identifier"]
        elif t["action"] == "SELL":
            p = pos[key]
            if p["qty"] <= 0:
                continue
            q = float(t["quantity"])
            avg = p["cost"] / p["qty"]
            pnl = (t["price_per_unit"] - avg) * q
            days = (datetime.fromisoformat(t["timestamp"]) - datetime.fromisoformat(p["first_buy"])).days
            closed.append({
                "name": key, "identifier": p.get("identifier", ""), "qty": q,
                "buy": avg, "sell": t["price_per_unit"], "pnl": pnl,
                "pct": t["price_per_unit"] / avg - 1, "days": days,
                "date": t["timestamp"][:10], "reason": t.get("rationale", "")[:80],
            })
            p["qty"] -= q
            p["cost"] -= avg * q
            if p["qty"] <= 1e-9:
                pos[key] = {"qty": 0.0, "cost": 0.0, "first_buy": None}
    return closed


def build_report(fee: float) -> str:
    if not PORTFOLIO_FILE.exists() or not TRADES_FILE.exists():
        return ("# Depot-Performance\n\nNoch kein Depot angelegt "
                f"({PORTFOLIO_FILE.relative_to(BASE)} fehlt). Die erste Marktanalyse legt es an.\n")
    portfolio = json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
    trades = json.loads(TRADES_FILE.read_text(encoding="utf-8"))["trades"]
    start = datetime.fromisoformat(portfolio["created"])
    initial = portfolio["initial_capital"]
    cash = portfolio["cash"]
    orders = sum(1 for t in trades if t["action"] in ("BUY", "SELL"))

    # --- Offene Positionen zu aktuellen Kursen
    rows, holdings_val, missing = [], 0.0, []
    for h in portfolio["holdings"]:
        cur = get_current_price(h["identifier"], h["name"], h.get("price_symbol", ""))
        price = cur["price"] if cur else h["avg_buy_price"]
        if not cur:
            missing.append(h["name"])
        val = price * h["quantity"]
        holdings_val += val
        rows.append(f"| {h['name'][:28]} | {h['quantity']:g} | {h['avg_buy_price']:.2f} | "
                    f"{price:.2f}{'' if cur else ' (Einstand!)'} | {val - h['total_invested']:+.0f} € "
                    f"({price / h['avg_buy_price'] - 1:+.1%}) |")
    total = cash + holdings_val
    ret = total / initial - 1
    total_net = total - orders * fee
    ret_net = total_net / initial - 1

    # --- Benchmark
    bm = _benchmark_return(BENCHMARK, start)

    # --- Abgeschlossene Trades
    closed = _closed_positions(trades)
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] <= 0]
    realized = sum(c["pnl"] for c in closed)

    # --- Was wäre, wenn gehalten?
    hindsight = []
    for c in closed:
        cur = get_current_price(c["identifier"], c["name"])
        # Plausibilität (2026-11-14): Die Namenssuche traf bei TSMC ein falsches
        # Symbol (0,96 € statt ~400 €) und verfälschte „Halten wäre“ um ~2.000 €.
        # Kurse außerhalb Faktor 5 zum Verkaufskurs gelten als Fehltreffer.
        if cur and c.get("sell") and not (0.2 <= cur["price"] / c["sell"] <= 5):
            continue
        if cur:
            hindsight.append((c, cur["price"]))

    z1 = _z1_period(total, orders, bm[1] if bm else None, fee)
    shadow = _z1_cash_shadow(trades, holdings_val, bm[1] if bm else None, fee) if z1 else None

    days = (datetime.now() - start).days
    _write_summary({
        "as_of": datetime.now().isoformat(timespec="minutes"),
        "start": start.date().isoformat(),
        "days": days,
        "initial": round(initial, 2),
        "value_net": round(total_net, 2),
        "return_net": round(ret_net, 4),
        "benchmark": BENCHMARK,
        "benchmark_return": round(bm[2], 4) if bm else None,
        "gap_pp": round((ret_net - bm[2]) * 100, 1) if bm else None,
        "closed_trades": len(closed),
        "winners": len(wins),
        "avg_win": round(sum(c["pct"] for c in wins) / len(wins), 4) if wins else None,
        "avg_loss": round(sum(c["pct"] for c in losses) / len(losses), 4) if losses else None,
        "cash_share": round(cash / total, 3) if total else None,
        "orders": orders,
        "z1": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in z1.items()} if z1 else None,
        "z1_cash_shadow": {k: round(v, 4) for k, v in shadow.items()} if shadow else None,
    })
    L = []
    L.append(f"# Depot-Performance – Stand {datetime.now():%Y-%m-%d %H:%M}\n")
    L.append(f"Zeitraum: {start:%Y-%m-%d} bis heute ({days} Tage), Startkapital {initial:,.0f} €\n")
    L.append("## Ergebnis vs. Benchmark\n")
    L.append("| | Wert | Rendite |")
    L.append("|---|---:|---:|")
    L.append(f"| Nova-Depot (brutto) | {total:,.0f} € | {ret:+.1%} |")
    L.append(f"| Nova-Depot (nach {orders} Orders à {fee:.2f} €) | {total_net:,.0f} € | {ret_net:+.1%} |")
    if bm:
        L.append(f"| Buy & Hold {BENCHMARK} | {initial * (1 + bm[2]):,.0f} € | {bm[2]:+.1%} |")
        L.append(f"\n**Abstand zum Benchmark: {(ret_net - bm[2]) * 100:+.1f} Prozentpunkte.**")
    if z1:
        L.append(f"\n### Z1-Wertungszeitraum {z1['start']} bis {z1['end']}"
                 f"{' (abgeschlossen)' if z1['final'] else ''}\n")
        L.append("| | Rendite seit Start |")
        L.append("|---|---:|")
        L.append(f"| Nova-Depot (Start {z1['base_value']:,.0f} €, {z1['new_orders']} neue Orders) | {z1['depot_ret']:+.2%} |")
        L.append(f"| {BENCHMARK} | {z1['bm_ret']:+.2%} |")
        L.append(f"\n**Z1-Abstand: {z1['gap_pp']:+.2f} Prozentpunkte.** Kriterium: > 0 am {z1['end']}.")
    L.append("\nNicht eingerechnet: Spread, Steuern, Slippage. Ausführungskurse: Yahoo-Kurs zum Zeitpunkt der "
             "Analyse (Kurskorrektur in market/midday_analysis.py), nur ohne Live-Kurs die Zahl aus dem LLM-Text.\n")

    L.append("## Offene Positionen\n")
    L.append("| Position | Stück | Einstand | Aktuell | unreal. G/V |")
    L.append("|---|---:|---:|---:|---:|")
    L.extend(rows)
    L.append(f"\nCash: {cash:,.0f} € ({cash / total:.0%} des Depots)")
    if missing:
        L.append(f"\n⚠️ Kein aktueller Kurs für: {', '.join(missing)} (mit Einstand bewertet)")

    L.append("\n## Abgeschlossene Trades\n")
    if closed:
        avg_w = sum(c["pct"] for c in wins) / len(wins) if wins else 0
        avg_l = sum(c["pct"] for c in losses) / len(losses) if losses else 0
        L.append(f"- Anzahl: {len(closed)}, davon Gewinner {len(wins)} ({len(wins) / len(closed):.0%})")
        L.append(f"- Ø Gewinner {avg_w:+.1%}, Ø Verlierer {avg_l:+.1%}")
        L.append(f"- Realisiert: {realized:+,.0f} €")
        L.append(f"- Ø Haltedauer: {sum(c['days'] for c in closed) / len(closed):.0f} Tage")
        L.append("\n| Datum | Position | Kauf | Verkauf | G/V | Tage | Heute | Halten wäre |")
        L.append("|---|---|---:|---:|---:|---:|---:|---:|")
        hs = {id(c): p for c, p in hindsight}
        for c in closed:
            now = hs.get(id(c))
            now_s = f"{now:.2f}" if now else "–"
            diff = f"{(now - c['sell']) * c['qty']:+.0f} €" if now else "–"
            L.append(f"| {c['date']} | {c['name'][:24]} | {c['buy']:.2f} | {c['sell']:.2f} | "
                     f"{c['pnl']:+.0f} € ({c['pct']:+.0%}) | {c['days']} | {now_s} | {diff} |")
        if hindsight:
            missed = sum((p - c["sell"]) * c["qty"] for c, p in hindsight)
            L.append(f"\nHätte Nova alle verkauften Positionen bis heute gehalten: {missed:+,.0f} € "
                     f"gegenüber den tatsächlichen Verkäufen ({len(hindsight)}/{len(closed)} mit Kurs).")
    if z1 and shadow:
        # Bewusst ohne "- "-Zeilen: Dieser Abschnitt soll nicht in performance_context() (Prompts) landen.
        L.append("\n## Schattenrechnung Cash-Drag (nur Messung)\n")
        L.append("Gleiche Trades, aber freies Cash hätte seit Z1-Start in EUNL.DE gelegen. "
                 "Ändert nichts an Depot A. Entscheidung und Prüfdatum: agenda-log 2026-11-07.\n")
        L.append("| | Z1-Rendite | Abstand zu EUNL |")
        L.append("|---|---:|---:|")
        L.append(f"| Depot A tatsächlich | {z1['depot_ret']:+.2%} | {z1['gap_pp']:+.2f} pp |")
        L.append(f"| Depot A mit Cash in EUNL | {shadow['shadow_ret']:+.2%} | {shadow['gap_pp']:+.2f} pp |")
        eff = z1["gap_pp"] - shadow["gap_pp"]
        L.append(f"\nBeitrag des Bar-Cash bisher: {eff:+.2f} pp ({'geholfen' if eff > 0 else 'gekostet'}). "
                 f"Ohne Cash-Effekt stünde A bei {shadow['gap_pp']:+.2f} pp. Das ist der Abstand, "
                 "den die Einzelwert-Auswahl erklärt.")
    return "\n".join(L) + "\n"


def _write_summary(data: dict) -> None:
    """Schreibt die Kennzahlen als JSON für den Blog (Fehler werden nur gemeldet)."""
    try:
        SUMMARY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"performance.json konnte nicht geschrieben werden: {exc}", file=sys.stderr)


def performance_context() -> str:
    """Kompakter Auszug aus performance.md für die Trading-Prompts (leer, falls nicht vorhanden).

    Enthält Benchmark-Vergleich und Trade-Statistik, damit Nova bei jeder
    Entscheidung ihre eigene Bilanz vor Augen hat.
    """
    try:
        text = OUT_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    head = text.split("## Offene Positionen")[0].strip()
    stats = ""
    if "## Abgeschlossene Trades" in text:
        block = text.split("## Abgeschlossene Trades")[1]
        stats = "\n".join(l for l in block.splitlines() if l.startswith("- "))
    return (
        "\n## Deine bisherige Bilanz (aus personality/memory/performance.md)\n\n"
        f"{head.replace('# Depot-Performance', '### Depot-Performance')}\n\n{stats}\n\n"
        "Bevor du kaufst: Begründe, warum dieser Trade nicht wieder dem Muster deiner "
        "Verlusttrades entspricht (siehe lessons.md). Ein Trade, der nicht besser ist als "
        "der Benchmark zu halten, ist kein guter Trade.\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", action="store_true", help="Bericht nach personality/memory/performance.md schreiben")
    ap.add_argument("--fee", type=float, default=1.0, help="Gebühr pro Order in EUR (Default 1,00)")
    args = ap.parse_args()
    import logging
    logging.basicConfig(level=logging.ERROR)
    report = build_report(args.fee)
    print(report)
    if args.save:
        OUT_FILE.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
