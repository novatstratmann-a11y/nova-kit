"""Marktdaten-Snapshot (Yahoo Finance) für market_analysis.py und midday_analysis.py.

Liefert Indizes, Rohstoffe, Krypto und Volatilität mit Tagesstand, Wochen-/Monatsänderung,
RSI(14), 20-Tage-Linie, Abstand zum 52-Wochen-Hoch und Volumen – damit das LLM die
technischen Pflicht-Checks nicht per Websuche zusammensuchen muss.
"""
import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; NOVA-Market/1.0)"

SYMBOLS = [
    ("DAX", "^GDAXI"), ("Euro Stoxx 50", "^STOXX50E"), ("S&P 500", "^GSPC"),
    ("Nasdaq 100", "^NDX"), ("Dow Jones", "^DJI"), ("Nikkei 225", "^N225"),
    ("VIX", "^VIX"), ("Brent-Öl", "BZ=F"), ("WTI-Öl", "CL=F"), ("Gold", "GC=F"),
    ("Kupfer", "HG=F"), ("EUR/USD", "EURUSD=X"), ("US-10J-Rendite", "^TNX"),
    ("Bitcoin (USD)", "BTC-USD"), ("Ethereum (USD)", "ETH-USD"),
]


def _chart(symbol: str) -> dict | None:
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?interval=1d&range=1y")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    res = (data.get("chart", {}).get("result") or [None])[0]
    return res


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def snapshot_row(name: str, symbol: str) -> str | None:
    try:
        res = _chart(symbol)
        if not res:
            return None
        q = res["indicators"]["quote"][0]
        pairs = [(t, c, v) for t, c, v in zip(res["timestamp"], q["close"], q.get("volume") or [None] * len(q["close"]))
                 if c is not None]
        if len(pairs) < 25:
            return None
        closes = [c for _, c, _ in pairs]
        last_ts = pairs[-1][0]
        last = float(res["meta"].get("regularMarketPrice") or closes[-1])
        stamp = datetime.fromtimestamp(res["meta"].get("regularMarketTime", last_ts)).strftime("%d.%m. %H:%M")
        prev = closes[-2]
        chg = lambda ref: (last / ref - 1) * 100
        sma20 = sum(closes[-20:]) / 20
        hi = max(closes)
        rsi = _rsi(closes)
        vols = [v for _, _, v in pairs if v]
        vol_txt = ""
        if len(vols) > 21 and vols[-1]:
            vol_txt = f", Vol {vols[-1] / (sum(vols[-21:-1]) / 20):.1f}x Ø20"
        # 60-Handelstage-Lauf wie R-A1 in portfolio.runup_check(): nur Schlusskurse vor heute (Berlin)
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(tz).strftime("%Y-%m-%d")
        past = [c for t, c, _ in pairs if datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d") < today]
        run_txt = ""
        if len(past) > 60:
            run = (past[-1] / past[-61] - 1) * 100
            run_txt = f" | 60T {run:+.1f}%" + (" ⛔ R-A1-Kaufsperre (>+30 %)" if run > 30 else "")
        return (f"- {name} ({symbol}): {last:,.2f} [Stand {stamp}] | Tag {chg(prev):+.2f}% | "
                f"5T {chg(closes[-6]):+.1f}% | 1M {chg(closes[-22]):+.1f}% | "
                f"RSI {rsi:.0f} | vs. 20T-Linie {chg(sma20):+.1f}% | Abstand 52W-Hoch {chg(hi):+.1f}%{vol_txt}"
                f"{run_txt}")
    except Exception as exc:
        logger.warning("Marktdaten %s fehlgeschlagen: %s", symbol, exc)
        return None


def build_market_data_section() -> str:
    rows = [r for r in (snapshot_row(n, s) for n, s in SYMBOLS) if r]
    if not rows:
        return ""
    return ("\n## Aktuelle Marktdaten (Yahoo Finance, automatisch abgerufen – verlässliche Quelle, "
            "NICHT per Websuche nachrecherchieren)\n" + "\n".join(rows) +
            "\nHinweis: Diese Werte gelten als belastbare Tageskurse; nutze sie für Makroteil und "
            "technische Pflicht-Checks. Für einzelne Kandidaten-Aktien kannst du die Kennzahlen via "
            "`python3 /home/pi/nova/market_data.py SYMBOL` abrufen (z.B. SAP.DE, NVDA). "
            "Die Ausgabe enthält auch den 60-Tage-Lauf für R-A1 – prüfe jeden Kaufkandidaten "
            "damit, bevor du einen BUY-Marker setzt.\n")


def build_holdings_section(portfolio) -> str:
    """Technische Kennzahlen der offenen Positionen (über price_symbol aus portfolio.json)."""
    rows = []
    for h in portfolio._portfolio.get("holdings", []):
        sym = h.get("price_symbol")
        if not sym:
            continue
        r = snapshot_row(h.get("name", sym), sym)
        if r:
            rows.append(r)
    if not rows:
        return ""
    return ("\n## Technik deiner offenen Positionen (Yahoo, automatisch – nicht nachrecherchieren)\n"
            + "\n".join(rows) + "\n")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for s in sys.argv[1:]:
            print(snapshot_row(s, s) or f"- {s}: keine Daten")
    else:
        print(build_market_data_section())
