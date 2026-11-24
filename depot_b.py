#!/usr/bin/env python3
"""
depot_b.py – Novas eigene Strategie: „Kern + Trend-Satellit“ (Depot B, Paper).

Regeln (festgelegt 2026-11-01, siehe personality/strategie_b/vorab-festlegung-2026-11-01.md):
  1. Kern 60 %: EUNL.DE (MSCI World), immer gehalten.
  2. Satellit 40 % = 2 Slots à 20 % aus SXR8.DE, EXSA.DE, IS3N.DE, 4GLD.DE.
  3. Monatlich: Score = Mittel der 3/6/12-Monats-Rendite (Monatsschluss, dividendenbereinigt),
     die 2 besten belegen die Slots.
  4. Trendfilter: Kurs < 10-Monats-Durchschnitt → Slot geht in XEON.DE (Geldmarkt).
  5. Gehandelt wird nur bei Auswahlwechsel oder > 2 pp Abweichung vom Zielgewicht.
  6. Kosten: 1 € je Order + 0,10 % Spread. Ausführung zum echten Yahoo-Kurs, ganze Stücke.

Kein LLM entscheidet hier etwas. Depot A (portfolio.json, LLM-Picks) bleibt unberührt.

    python depot_b.py              # täglich: bewerten; im neuen Monat einmal rebalancen
    python depot_b.py --dry-run    # zeigt Signal/Orders, ändert nichts
    python depot_b.py --status     # nur Bericht auf stdout
"""

import argparse
import json
import logging
import logging.handlers
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).parent
DIR = BASE / "personality" / "memory" / "portfolio_b"
STATE_FILE = DIR / "depot.json"
TRADES_FILE = DIR / "trades.json"
HISTORY_FILE = DIR / "history.json"
SNAPSHOT_FILE = DIR / "snapshot.json"   # vom Blog gelesen (GET /api/portfolio-b)
REPORT_FILE = BASE / "personality" / "memory" / "performance-b.md"
LOG_FILE = BASE / "personality" / "logs" / "scripts.log"

START_CAPITAL = 20_000.0
FEE = 1.0
SPREAD = 0.001
TOLERANCE = 0.02

CORE = "EUNL.DE"
CORE_WEIGHT = 0.60
SLOT_WEIGHT = 0.20
N_SLOTS = 2
RISK = ["SXR8.DE", "EXSA.DE", "IS3N.DE", "4GLD.DE"]
CASH_ETF = "XEON.DE"
NAMES = {
    "EUNL.DE": "iShares Core MSCI World",
    "SXR8.DE": "iShares Core S&P 500",
    "EXSA.DE": "iShares STOXX Europe 600",
    "IS3N.DE": "iShares Core MSCI EM IMI",
    "4GLD.DE": "Xetra-Gold",
    "XEON.DE": "Xtrackers EUR Overnight Rate Swap",
}

EXCHANGE_TZ = ZoneInfo("Europe/Berlin")

logger = logging.getLogger("depot_b")


def setup_logging():
    logger.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(sh)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(name)-16s  %(levelname)-8s  %(message)s",
                                      datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    # telegram_utils-Warnungen (HTTP-Status, Fehlertext) ebenfalls nach scripts.log
    # (Lücke ki_news 14.11.; vorher nur stdout, das Cron verwirft).
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("telegram").addHandler(fh)


# ---------------------------------------------------------------------------
# Kursdaten
# ---------------------------------------------------------------------------

def _chart(symbol: str, interval: str, rng: str) -> dict:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?interval={interval}&range={rng}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (NOVA-DepotB/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())["chart"]["result"][0]


def last_price(symbol: str) -> float:
    res = _chart(symbol, "1d", "5d")
    p = res["meta"].get("regularMarketPrice")
    if not p:
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        p = closes[-1]
    return float(p)


def month_closes(symbol: str, before: tuple[int, int]) -> list[float]:
    """Dividendenbereinigte Monatsschlusskurse aller abgeschlossenen Monate vor `before`."""
    res = _chart(symbol, "1mo", "2y")
    ind = res["indicators"]
    vals = (ind.get("adjclose") or [{}])[0].get("adjclose") or ind["quote"][0]["close"]
    # Monatsbalken beginnen 00:00 Berliner Zeit (= 22:00 UTC am Vortag) → in Börsenzeit umrechnen.
    # Yahoo hängt den laufenden Monat oft doppelt an (Zeitstempel = letzter Handel) → je Monat nur letzter Wert.
    by_month = {}
    for t, c in zip(res["timestamp"], vals):
        d = datetime.fromtimestamp(t, EXCHANGE_TZ)
        if c is not None and (d.year, d.month) < before:
            by_month[(d.year, d.month)] = float(c)
    return [by_month[k] for k in sorted(by_month)]


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------

def compute_signal(today: datetime) -> dict:
    """Zielgewichte {symbol: weight} + Begründung je Kandidat."""
    before = (today.year, today.month)
    details, scores = {}, {}
    for s in RISK:
        c = month_closes(s, before)
        if len(c) < 13:
            raise RuntimeError(f"Zu wenig Monatsdaten für {s}: {len(c)}")
        score = sum(c[-1] / c[-1 - k] - 1 for k in (3, 6, 12)) / 3
        sma10 = sum(c[-10:]) / 10
        scores[s] = score
        details[s] = {"score": score, "close": c[-1], "sma10": sma10, "trend_ok": c[-1] > sma10}
    target = {CORE: CORE_WEIGHT}
    top = sorted(RISK, key=lambda s: scores[s], reverse=True)[:N_SLOTS]
    for s in top:
        k = s if details[s]["trend_ok"] else CASH_ETF
        target[k] = target.get(k, 0.0) + SLOT_WEIGHT
    return {"target": target, "top": top, "details": details}


# ---------------------------------------------------------------------------
# Depot
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def new_depot(now: datetime) -> dict:
    return {"created": now.isoformat(timespec="seconds"), "start_capital": START_CAPITAL,
            "cash": START_CAPITAL, "holdings": {}, "orders": 0, "fees": 0.0,
            "last_rebalance": None, "benchmark_start": None}


def value(depot: dict, prices: dict) -> float:
    return depot["cash"] + sum(q * prices[s] for s, q in depot["holdings"].items())


def plan_orders(depot: dict, target: dict, prices: dict) -> list[dict]:
    total = value(depot, prices)
    syms = set(target) | set(depot["holdings"])
    orders = []
    for s in sorted(syms):
        cur_q = depot["holdings"].get(s, 0)
        cur_w = cur_q * prices[s] / total if total else 0
        tgt_w = target.get(s, 0.0)
        selection_change = (tgt_w == 0) != (cur_q == 0)
        if not selection_change and abs(tgt_w - cur_w) <= TOLERANCE:
            continue
        # Puffer für Gebühren/Spread, damit Käufe nicht am Cash scheitern
        tgt_q = int((tgt_w * total * (1 - SPREAD) - FEE) // prices[s]) if tgt_w > 0 else 0
        delta = tgt_q - cur_q
        if delta:
            orders.append({"symbol": s, "qty": delta})
    orders.sort(key=lambda o: o["qty"])  # Verkäufe zuerst
    return orders


def execute(depot: dict, orders: list[dict], prices: dict, now: datetime, reason: str) -> list[dict]:
    done = []
    for o in orders:
        s, q = o["symbol"], o["qty"]
        if q > 0:
            px = prices[s] * (1 + SPREAD)
            cost = q * px + FEE
            if cost > depot["cash"]:
                q = int((depot["cash"] - FEE) // px)
                if q <= 0:
                    logger.warning("Kauf %s übersprungen: zu wenig Cash", s)
                    continue
                cost = q * px + FEE
            depot["cash"] -= cost
            depot["holdings"][s] = depot["holdings"].get(s, 0) + q
            action, amount = "BUY", cost
        else:
            px = prices[s] * (1 - SPREAD)
            q = -q
            amount = q * px - FEE
            depot["cash"] += amount
            depot["holdings"][s] -= q
            if depot["holdings"][s] <= 0:
                del depot["holdings"][s]
            action = "SELL"
        depot["orders"] += 1
        depot["fees"] += FEE + q * prices[s] * SPREAD
        done.append({"timestamp": now.isoformat(timespec="seconds"), "action": action, "symbol": s,
                     "name": NAMES.get(s, s), "quantity": q, "price": round(px, 4),
                     "amount": round(amount, 2), "reason": reason})
    return done


# ---------------------------------------------------------------------------
# Bericht
# ---------------------------------------------------------------------------

def build_report(depot: dict, prices: dict, signal: dict | None, now: datetime) -> str:
    total = value(depot, prices)
    ret = total / depot["start_capital"] - 1
    bm0 = depot.get("benchmark_start")
    bm_ret = prices[CORE] / bm0 - 1 if bm0 else 0.0
    hist = load_json(HISTORY_FILE, [])
    peak, mdd = 0.0, 0.0
    for h in hist + [{"value": total}]:
        peak = max(peak, h["value"])
        mdd = min(mdd, h["value"] / peak - 1)
    L = [f"# Depot B – Kern + Trend-Satellit – Stand {now:%Y-%m-%d %H:%M}", "",
         f"Start: {depot['created'][:10]}, Startkapital {depot['start_capital']:,.0f} €. "
         "Regeln: personality/strategie_b/vorab-festlegung-2026-11-01.md (bis 2027-01-31 eingefroren).", "",
         "| | Wert | Rendite |", "|---|---:|---:|",
         f"| Depot B (nach Kosten) | {total:,.0f} € | {ret:+.2%} |",
         f"| Buy & Hold EUNL.DE | – | {bm_ret:+.2%} |", "",
         f"**Abstand zum Benchmark: {100 * (ret - bm_ret):+.2f} Prozentpunkte.** "
         f"Max. Drawdown (Tageswerte): {mdd:.1%}. Orders: {depot['orders']}, Kosten: {depot['fees']:,.2f} €.", "",
         "## Positionen", "", "| ETF | Stück | Kurs | Wert | Gewicht |", "|---|---:|---:|---:|---:|"]
    for s, q in sorted(depot["holdings"].items(), key=lambda kv: -kv[1] * prices[kv[0]]):
        v = q * prices[s]
        L.append(f"| {NAMES.get(s, s)} ({s}) | {q} | {prices[s]:.2f} | {v:,.0f} € | {v / total:.1%} |")
    L.append(f"| Cash | | | {depot['cash']:,.0f} € | {depot['cash'] / total:.1%} |")
    if signal:
        L += ["", f"## Signal ({now:%Y-%m})", "", "| Kandidat | Score 3/6/12M | Kurs > 10M-Schnitt | Auswahl |",
              "|---|---:|:---:|:---:|"]
        for s, d in sorted(signal["details"].items(), key=lambda kv: -kv[1]["score"]):
            L.append(f"| {NAMES[s]} | {d['score']:+.1%} | {'ja' if d['trend_ok'] else 'nein'} | "
                     f"{'✔' if s in signal['top'] else ''} |")
    L += ["", "Nicht eingerechnet: Steuern. Spread pauschal 0,10 %, Kurs = Yahoo-Kurs zum Ausführungszeitpunkt."]
    return "\n".join(L) + "\n"


def build_snapshot(depot: dict, prices: dict, now: datetime) -> dict:
    """Kompakter Stand für den Blog – nur abgeleitete Zahlen, keine Rohdaten-Logik im Frontend."""
    total = value(depot, prices)
    ret = total / depot["start_capital"] - 1
    bm0 = depot.get("benchmark_start")
    bm_ret = prices[CORE] / bm0 - 1 if bm0 else 0.0
    hist = load_json(HISTORY_FILE, [])
    peak, mdd = 0.0, 0.0
    for h in hist + [{"value": total}]:
        peak = max(peak, h["value"])
        mdd = min(mdd, h["value"] / peak - 1)
    holdings = []
    for s, q in sorted(depot["holdings"].items(), key=lambda kv: -kv[1] * prices[kv[0]]):
        v = q * prices[s]
        holdings.append({"symbol": s, "name": NAMES.get(s, s), "quantity": q, "price": round(prices[s], 4),
                         "value": round(v, 2), "weight": round(v / total, 4),
                         "role": "Kern" if s == CORE else ("Geldmarkt" if s == CASH_ETF else "Satellit")})
    return {"as_of": now.isoformat(timespec="minutes"), "start": depot["created"][:10],
            "start_capital": depot["start_capital"], "value": round(total, 2), "cash": round(depot["cash"], 2),
            "return_net": round(ret, 4), "benchmark": CORE, "benchmark_return": round(bm_ret, 4),
            "gap_pp": round(100 * (ret - bm_ret), 2), "max_drawdown": round(mdd, 4),
            "orders": depot["orders"], "fees": round(depot["fees"], 2),
            "signal": depot.get("signal_last"), "holdings": holdings,
            "trades": load_json(TRADES_FILE, [])[-30:],
            "history": [{"date": h["date"], "value": h["value"]} for h in hist][-400:]}


def _notify(text: str):
    try:
        from telegram_utils import send_to_admin
        send_to_admin(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram-Send fehlgeschlagen: %s", exc)


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    setup_logging()
    now = datetime.now()
    month = now.strftime("%Y-%m")

    depot = load_json(STATE_FILE, None)
    fresh = depot is None
    if fresh:
        depot = new_depot(now)

    syms = set([CORE, CASH_ETF] + RISK) | set(depot["holdings"])
    try:
        prices = {s: last_price(s) for s in syms}
    except Exception as exc:  # noqa: BLE001
        logger.error("Kursabruf fehlgeschlagen: %s", exc)
        return 1

    signal = None
    trades = []
    if args.status:
        print(build_report(depot, prices, None, now))
        return 0

    if depot["last_rebalance"] != month:
        try:
            signal = compute_signal(now)
        except Exception as exc:  # noqa: BLE001
            logger.error("Signal fehlgeschlagen: %s", exc)
            return 1
        orders = plan_orders(depot, signal["target"], prices)
        logger.info("Ziel %s → Orders %s", signal["target"], orders)
        if args.dry_run:
            print(json.dumps({"target": signal["target"], "orders": orders,
                              "details": signal["details"]}, indent=2, default=str))
            print(build_report(depot, prices, signal, now))
            return 0
        reason = "Start" if fresh else f"Monats-Rebalancing {month}"
        trades = execute(depot, orders, prices, now, reason)
        depot["last_rebalance"] = month
        if depot["benchmark_start"] is None:
            depot["benchmark_start"] = prices[CORE] * (1 + SPREAD)  # fair: Benchmark zahlt auch Spread
        save_json(TRADES_FILE, load_json(TRADES_FILE, []) + trades)
        depot["signal_last"] = {"month": month, "target": signal["target"], "top": signal["top"]}
    elif args.dry_run:
        print(build_report(depot, prices, None, now))
        return 0

    save_json(STATE_FILE, depot)
    hist = load_json(HISTORY_FILE, [])
    day = now.strftime("%Y-%m-%d")
    entry = {"date": day, "value": round(value(depot, prices), 2), "eunl": prices[CORE]}
    hist = [h for h in hist if h["date"] != day] + [entry]
    save_json(HISTORY_FILE, hist)
    report = build_report(depot, prices, signal, now)
    REPORT_FILE.write_text(report, encoding="utf-8")
    save_json(SNAPSHOT_FILE, build_snapshot(depot, prices, now))
    logger.info("Depot B: %.2f € (%d Trades heute)", entry["value"], len(trades))

    if trades:
        lines = [f"Depot B – {('Start' if fresh else 'Rebalancing')} {day}"]
        lines += [f"{t['action']} {t['quantity']} × {t['name']} @ {t['price']:.2f} € = {t['amount']:,.2f} €"
                  for t in trades]
        lines.append(report.split("## Positionen")[0].split("\n\n", 2)[-1].strip())
        _notify("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
