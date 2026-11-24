#!/usr/bin/env python3
"""
portfolio.py – NOVA Virtuelles Wertpapierdepot

Verwaltet Novas virtuelles Portfolio mit 10.000 € Startkapital.
Alle Asset-Klassen erlaubt (Aktien, ETFs, Krypto, Rohstoffe, Derivate, Anleihen).
Keine Handelsgebühren.

Identifier-Konvention:
  Deutsche Aktien  → WKN (z.B. "710000")
  Internationale   → ISIN (z.B. "US0378331005")
  Krypto           → Ticker (z.B. "BTC", "ETH")
  ETFs             → ISIN

Datenpfade:
  personality/memory/portfolio/portfolio.json  ← aktueller State
  personality/memory/portfolio/trades.json     ← Trade-Ledger

Verwendung:
  python portfolio.py       # Initialisiert und zeigt aktuellen Status
  python portfolio.py --status   # Nur Status anzeigen
"""

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PORTFOLIO_DIR = SCRIPT_DIR / "personality" / "memory" / "portfolio"
PORTFOLIO_FILE = PORTFOLIO_DIR / "portfolio.json"
TRADES_FILE = PORTFOLIO_DIR / "trades.json"

INITIAL_CAPITAL = 10_000.00
MAX_POSITION_PCT = 0.20  # Max 20% des Gesamtdepots pro Position
MAX_SECTOR_PCT = 0.40  # Max 40% des Gesamtdepots in einem Sektor/Themencluster

# Bekannte Krypto-Ticker – bewusst als Whitelist statt Regex, da viele
# US-Aktien-Ticker (MELI, SPCX, TSLA, AAPL, ...) ebenfalls 2-6 Großbuchstaben
# ohne Ziffern sind und sonst fälschlich als Krypto (z.B. "MELI-EUR") behandelt würden.
CRYPTO_TICKERS = {
    'BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'DOGE', 'DOT', 'LTC',
    'BNB', 'AVAX', 'MATIC', 'LINK', 'TRX', 'ATOM', 'XLM', 'UNI',
}

logger = logging.getLogger("portfolio")

# ---------------------------------------------------------------------------
# Live-Kursabfrage (Yahoo Finance)
# ---------------------------------------------------------------------------

def get_current_price(identifier: str, name: str = '',
                      price_symbol: str = '') -> dict | None:
    """Fragt den aktuellen EUR-Kurs bei Yahoo Finance ab.

    Crypto-Ticker (2–6 Großbuchstaben): direkt als {id}-EUR gesucht.
    Aktien / ETFs: Suche per price_symbol (falls bekannt), dann per Name,
    dann Name ohne erstes Wort (für "HANetf ...", "iShares ..." etc.),
    dann per Identifier.

    Gibt {'price': float, 'symbol': str} zurück oder None wenn nicht verfügbar.
    """
    ua = 'Mozilla/5.0 (compatible; NOVA-Portfolio/1.0)'

    def _fetch_json(url: str) -> dict:
        req = urllib.request.Request(url, headers={'User-Agent': ua})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))

    # EUR-Börsen nach Präferenz: Xetra (GER/.DE) > Frankfurt (FRA/.F) > Andere EU
    _EUR_EXCHANGES = {'GER', 'FRA', 'MUN', 'HAN', 'HAM', 'DUS', 'STU', 'BER', 'AMS', 'VIE'}
    _EUR_SUFFIXES = ('.DE', '.AS', '.VI', '.PA', '.MI', '.BR', '.F')

    def _search_symbol(query: str) -> str | None:
        url = (
            'https://query1.finance.yahoo.com/v1/finance/search'
            f'?q={urllib.parse.quote(query)}&quotesCount=5&newsCount=0'
        )
        data = _fetch_json(url)
        quotes = data.get('quotes', [])
        eur_q = next(
            (q for q in quotes
             if any(q.get('symbol', '').endswith(s) for s in _EUR_SUFFIXES)
             or q.get('exchange') in _EUR_EXCHANGES),
            None,
        )
        return (eur_q or (quotes[0] if quotes else None) or {}).get('symbol')

    def _fetch_price(symbol: str) -> float | None:
        url = (
            'https://query1.finance.yahoo.com/v8/finance/chart/'
            f'{urllib.parse.quote(symbol)}?interval=1d&range=1d'
        )
        data = _fetch_json(url)
        meta = (data.get('chart', {}).get('result') or [{}])[0].get('meta', {})
        px, cur = meta.get('regularMarketPrice'), meta.get('currency')
        if px is None:
            return None
        if cur == 'EUR':
            return float(px)
        # 2026-11-21: Nicht-EUR-Kurs (Glencore → GLEN.L in GBp) in EUR umrechnen,
        # statt auf Novas Schätzung zurückzufallen (19.11.: 5,64 € gebucht, Live ≈ 5,53 €).
        factor = 1.0
        if cur in ('GBp', 'GBX'):
            cur, factor = 'GBP', 0.01
        if not cur or not re.fullmatch(r'[A-Z]{3}', cur):
            return None
        fx_meta = (_fetch_json(
            'https://query1.finance.yahoo.com/v8/finance/chart/'
            f'{cur}EUR=X?interval=1d&range=1d'
        ).get('chart', {}).get('result') or [{}])[0].get('meta', {})
        fx = fx_meta.get('regularMarketPrice')
        if not fx:
            return None
        eur = float(px) * factor * float(fx)
        logger.info("Umrechnung %s: %.4f %s × %.5f = %.4f €", symbol, float(px) * factor, cur, fx, eur)
        return eur

    try:
        # price_symbol hat Vorrang – auch vor Krypto-Pattern
        if price_symbol:
            symbol = price_symbol
        elif identifier in CRYPTO_TICKERS:
            symbol = f'{identifier}-EUR'
        else:
            # Suche: voller Name → Name ohne erstes Wort → Identifier
            symbol = _search_symbol(name) if name else None
            if not symbol and name:
                words = name.split()
                if len(words) >= 3:
                    symbol = _search_symbol(' '.join(words[1:]))
            if not symbol:
                symbol = _search_symbol(identifier)

        if not symbol:
            logger.warning("Kein Yahoo-Symbol für %s (%s) gefunden.", identifier, name)
            return None

        try:
            price = _fetch_price(symbol)
        except Exception as exc:
            if not price_symbol:
                raise
            logger.warning("Gespeichertes Symbol %s für %s fehlgeschlagen: %s", symbol, identifier, exc)
            price = None
        if price is None and price_symbol:
            # Gespeichertes Symbol liefert nichts → normale Suche als Rückfall
            alt = (_search_symbol(name) if name else None) or _search_symbol(identifier)
            if alt and alt != symbol:
                symbol, price = alt, _fetch_price(alt)
        if price is not None:
            logger.info("Aktueller Kurs %s: %.4f € (Symbol: %s)", identifier, price, symbol)
            return {'price': price, 'symbol': symbol}
        logger.warning("Kein EUR-Kurs für Symbol %s.", symbol)
        return None
    except Exception as exc:
        logger.warning("Kursabfrage %s (%s) fehlgeschlagen: %s", identifier, name, exc)
        return None


def known_price_symbol(identifier: str) -> str:
    """Gespeichertes Yahoo-Symbol einer Depot-Position (leer, wenn unbekannt).

    2026-11-21: Die Analyse-Skripte suchten bei SELL neu per Name und fanden für
    Glencore nur GLEN.L, obwohl portfolio.json 8GC.MU kannte.
    """
    try:
        data = json.loads(PORTFOLIO_FILE.read_text(encoding='utf-8'))
        ident = (identifier or '').lower()
        for h in data.get('holdings', []):
            if h.get('identifier', '').lower() == ident:
                return h.get('price_symbol') or ''
    except Exception as exc:
        logger.warning("known_price_symbol(%s): %s", identifier, exc)
    return ''


# ---------------------------------------------------------------------------
# R-A1: Sperre gegen Käufe nach parabolischem Lauf (seit 2026-11-03)
# ---------------------------------------------------------------------------
# Beleg: personality/ideas/2026-11-02-narrativ-regel.md – Käufe nach > +30 %
# in 60 Handelstagen lagen 40 Tage später 0/4 vor EUNL (Ø -24,5 %).
# Modus: 'off' | 'shadow' (nur protokollieren, Kauf läuft) | 'enforce' (Kauf abgelehnt).
# Wochenreflexion 2026-11-07: 'enforce' (Rückwärtstest 4/4 Hype-Käufe getroffen, 0 Fehlgriffe
# unter 20 übrigen; blockierte Kandidaten stehen weiter im Log und bleiben nachrechenbar).
# Prüfdatum: 2027-01-31 (Z1-Ende).
RUNUP_GUARD_MODE = 'enforce'
RUNUP_LIMIT = 0.30
RUNUP_BARS = 60
RUNUP_LOG = SCRIPT_DIR / "personality" / "memory" / "r_a1_checks.jsonl"


def runup_check(symbol: str, today: str | None = None) -> dict | None:
    """Rendite über die letzten RUNUP_BARS Handelstage bis zum Vortagsschluss.

    Währungsunabhängig (Verhältnis zweier Schlusskurse). Gibt None zurück,
    wenn keine Daten vorliegen – die Sperre greift dann nicht (fail-open).
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo('Europe/Berlin')
    today = today or datetime.now(tz).strftime('%Y-%m-%d')
    url = ('https://query1.finance.yahoo.com/v8/finance/chart/'
           f'{urllib.parse.quote(symbol)}?interval=1d&range=1y')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; NOVA-Portfolio/1.0)'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode('utf-8'))['chart']['result'][0]
        ts = res.get('timestamp') or []
        closes = res['indicators']['quote'][0]['close']
    except Exception as exc:
        logger.warning("R-A1: Kursverlauf %s nicht abrufbar (%s) – keine Prüfung.", symbol, exc)
        return None
    bars = [(datetime.fromtimestamp(t, tz).strftime('%Y-%m-%d'), c)
            for t, c in zip(ts, closes) if c is not None]
    bars = [b for b in bars if b[0] < today]          # nur, was vor dem Kauf bekannt war
    if len(bars) <= RUNUP_BARS:
        logger.warning("R-A1: zu wenig Historie für %s (%d Tage) – keine Prüfung.", symbol, len(bars))
        return None
    (d0, c0), (d1, c1) = bars[-1 - RUNUP_BARS], bars[-1]
    runup = c1 / c0 - 1
    return {'symbol': symbol, 'from': d0, 'to': d1, 'runup': round(runup, 4),
            'over_limit': runup > RUNUP_LIMIT}


def _runup_guard(name: str, identifier: str, price_symbol: str | None, source: str) -> bool:
    """True = Kauf blockieren. Protokolliert jede Prüfung in RUNUP_LOG."""
    if RUNUP_GUARD_MODE == 'off':
        return False
    try:
        symbol = price_symbol
        if not symbol:
            found = get_current_price(identifier, name)
            symbol = found['symbol'] if found else None
        chk = runup_check(symbol) if symbol else None
        entry = {'ts': _now_iso(), 'mode': RUNUP_GUARD_MODE, 'name': name,
                 'identifier': identifier, 'source': source, **(chk or {'symbol': symbol, 'runup': None})}
        RUNUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RUNUP_LOG.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
        if not chk or not chk['over_limit']:
            return False
        if RUNUP_GUARD_MODE == 'enforce':
            logger.warning("BUY abgelehnt (R-A1): %s +%.0f %% in %d Handelstagen > +%.0f %%.",
                           name, chk['runup'] * 100, RUNUP_BARS, RUNUP_LIMIT * 100)
            return True
        logger.warning("R-A1 (Schatten): %s +%.0f %% in %d Handelstagen – würde blockiert, Kauf läuft.",
                       name, chk['runup'] * 100, RUNUP_BARS)
        return False
    except Exception as exc:  # Die Sperre darf den Handel nie zum Absturz bringen.
        logger.warning("R-A1-Prüfung fehlgeschlagen (%s) – Kauf läuft.", exc)
        return False


# Stop-Live-Check (seit 2026-11-07, nur Messung): Nennt die Begründung eines
# SELL einen Stop, liegt der Ausführungskurs (Live) aber über dem gespeicherten
# Stop, war der Stop gar nicht gerissen (Fall GE Vernova 06.11.: Text 686 €,
# live 708 €, Stop 703 €). 'shadow' = protokollieren, Verkauf läuft.
# Keine Sperre: Bis 07.11. 1 Fall in 17 Stop-Verkäufen, Halten wäre -24 €.
# Auswertung: ideas/2026-11-07-stop-live-check.md.
STOP_CHECK_MODE = 'shadow'  # 'off' | 'shadow'
STOP_CHECK_LOG = SCRIPT_DIR / "personality" / "memory" / "stop_live_checks.jsonl"
_STOP_WORD_RE = re.compile(r'stop', re.IGNORECASE)


def _stop_live_check(name: str, identifier: str, price: float, stop: float | None,
                     source: str, rationale: str) -> None:
    """Protokolliert Stop-Verkäufe, deren Ausführungskurs über dem Stop liegt. Wirft nie."""
    if STOP_CHECK_MODE == 'off' or not _STOP_WORD_RE.search(rationale or ''):
        return
    try:
        above = stop is not None and price > stop
        entry = {'ts': _now_iso(), 'name': name, 'identifier': identifier, 'source': source,
                 'price': price, 'stop_loss': stop, 'above_stop': above,
                 'gap_pct': round((price / stop - 1) * 100, 2) if stop else None}
        STOP_CHECK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with STOP_CHECK_LOG.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
        if above:
            logger.warning("Stop-Check (Schatten): %s verkauft zu %.2f € > Stop %.2f € (+%.1f %%) – "
                           "Stop war zum Live-Kurs nicht gerissen.", name, price, stop, entry['gap_pct'])
    except Exception as exc:  # Messung darf den Handel nie stören.
        logger.warning("Stop-Check fehlgeschlagen (%s).", exc)


# ---------------------------------------------------------------------------
# Trade-Marker Parser
# ---------------------------------------------------------------------------

_ACTION_BLOCK_RE = re.compile(
    r'<!--\s*PORTFOLIO_ACTION\s*(.*?)-->',
    re.DOTALL | re.IGNORECASE,
)
_LINE_RE = re.compile(r'^\s*([A-Z_]+)\s*:\s*(.+?)\s*$', re.MULTILINE)


def _parse_float(s: str) -> float:
    """Parst europäisches und angloamerikanisches Zahlenformat."""
    s = s.strip()
    # Entferne Tausender-Trennzeichen: 85.000,50 → 85000.50 oder 85,000.50 → 85000.50
    if ',' in s and '.' in s:
        if s.rindex('.') < s.rindex(','):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        s = s.replace(',', '.')
    return float(s)


def parse_portfolio_actions(content: str) -> list[dict]:
    """
    Sucht alle <!-- PORTFOLIO_ACTION ... --> Blöcke im Content.
    Gibt Liste valider Trade-Dicts zurück. Invalide Blöcke werden übersprungen.
    """
    actions = []
    for match in _ACTION_BLOCK_RE.finditer(content):
        block = match.group(1)
        fields = {k.upper(): v for k, v in _LINE_RE.findall(block)}

        if 'ACTION' not in fields:
            logger.warning("Portfolio-Marker ohne ACTION-Feld – übersprungen.")
            continue
        action = fields['ACTION'].upper()

        if action in ('BUY', 'SELL'):
            required = ['ASSET', 'IDENTIFIER', 'QUANTITY', 'PRICE_PER_UNIT', 'TOTAL']
            if action == 'BUY':
                required = required + ['SECTOR']
            missing = [f for f in required if f not in fields]
            if missing:
                logger.warning("Portfolio-Marker unvollständig – fehlt: %s", missing)
                continue

            try:
                qty = _parse_float(fields['QUANTITY'])
                price = _parse_float(fields['PRICE_PER_UNIT'])
                total = _parse_float(fields['TOTAL'])
            except (ValueError, KeyError) as exc:
                logger.warning("Zahlen-Parsing fehlgeschlagen: %s", exc)
                continue

            if qty <= 0 or price <= 0:
                logger.warning("Ungültige Werte: qty=%s price=%s", qty, price)
                continue

            parsed = {
                'action': action,
                'name': fields['ASSET'],
                'identifier': fields['IDENTIFIER'],
                'quantity': qty,
                'price_per_unit': price,
                'total': total,
                'rationale': fields.get('RATIONALE', ''),
            }
            for opt_field in ('TARGET', 'STOP_LOSS'):
                if opt_field in fields:
                    try:
                        parsed[opt_field.lower()] = _parse_float(fields[opt_field])
                    except ValueError:
                        logger.warning("Konnte %s nicht parsen: %s", opt_field, fields[opt_field])
            if action == 'BUY':
                parsed['sector'] = fields['SECTOR']
            actions.append(parsed)

        elif action == 'UPDATE_STOP':
            required = ['ASSET', 'IDENTIFIER', 'NEW_STOP_LOSS']
            missing = [f for f in required if f not in fields]
            if missing:
                logger.warning("Portfolio-Marker (UPDATE_STOP) unvollständig – fehlt: %s", missing)
                continue

            try:
                new_stop_loss = _parse_float(fields['NEW_STOP_LOSS'])
            except ValueError as exc:
                logger.warning("Konnte NEW_STOP_LOSS nicht parsen: %s", exc)
                continue

            if new_stop_loss <= 0:
                logger.warning("Ungültiger NEW_STOP_LOSS: %s", new_stop_loss)
                continue

            actions.append({
                'action': action,
                'name': fields['ASSET'],
                'identifier': fields['IDENTIFIER'],
                'new_stop_loss': new_stop_loss,
                'rationale': fields.get('RATIONALE', ''),
            })

        else:
            logger.warning("Unbekannte Aktion: %s", action)
            continue

    return actions


def strip_portfolio_actions(content: str) -> str:
    """Entfernt die PORTFOLIO_ACTION-Marker (für den Versand an Telegram)."""
    return _ACTION_BLOCK_RE.sub('', content).rstrip()


# Formulierungen, mit denen eine Analyse einen eigenen Trade ankündigt.
# Bewusst eng gefasst: "kaufe nicht", "Kein Neukauf" etc. sollen nicht anschlagen.
_TRADE_INTENT_PATTERNS = [
    re.compile(r'\b(?:Kauf|Verkauf|Teilverkauf|Trades? heute)\s*:', re.IGNORECASE),
    re.compile(r'\bsteige\b[^.\n]{0,80}\bein\b', re.IGNORECASE),
    # "Ich kaufe erst, wenn …" ist eine Bedingung, kein Trade (Fehlalarm Mittagsanalyse 13.11.).
    # Seit 20.11. außerdem: "nicht kaufe" (Verneinung davor), "kaufe ich nichts/keinen/weder"
    # und "Fällt er, verkaufe ich" (Verb direkt nach Komma = Bedingungssatz, Stop-Ankündigung).
    re.compile(r'(?<!nicht )(?<!, )\b(?:kaufe|verkaufe)\b(?!\s+erst\b)'
               r'(?![^.\n]{0,40}\b(?:nicht|nichts|kein\w*|weder)\b)', re.IGNORECASE),
    re.compile(r'\b(?:vollständig|komplett)\s+verkauft\b', re.IGNORECASE),
    re.compile(r'\bNeu kommt\b[^.\n]{0,80}\bPosition\b', re.IGNORECASE),
]


# Verneinte Ankündigungen ("Heute kein neuer Kauf: ...", "Keine Trades heute: ...") sind keine Trades.
# Markdown-Kursiv/Fett ("_Kein Kauf:_") vorher entfernen: '_' ist ein Wortzeichen, sonst greift \b nicht.
_TRADE_NEGATION = re.compile(r'\bkeine?[nr]?\b[^:\n]{0,40}\b(?:Kauf|Käufe|Verkauf|Verkäufe|Teilverkauf|Trades?)\b', re.IGNORECASE)


def find_trade_intents(content: str) -> list[str]:
    """
    Sucht im Analysetext nach angekündigten Käufen/Verkäufen.
    Dient als Konsistenz-Check: Kündigt der Text einen Trade an, ohne dass ein
    PORTFOLIO_ACTION-Marker geparst wurde, wird der Trade nie ausgeführt.
    Gibt die betroffenen Zeilen zurück (max. 5, gekürzt).
    """
    text = _ACTION_BLOCK_RE.sub('', content).replace('\\', '')
    hits = []
    for line in text.splitlines():
        if _TRADE_NEGATION.search(line.replace('_', ' ').replace('*', ' ')):
            continue
        if any(p.search(line) for p in _TRADE_INTENT_PATTERNS):
            hits.append(line.strip()[:200])
    return hits[:5]


# ---------------------------------------------------------------------------
# Portfolio-Klasse
# ---------------------------------------------------------------------------

class Portfolio:
    def __init__(self):
        self._portfolio: dict = {}
        self._trades: dict = {}
        self._load()

    def _load(self):
        if PORTFOLIO_FILE.exists():
            try:
                self._portfolio = json.loads(PORTFOLIO_FILE.read_text(encoding='utf-8'))
            except json.JSONDecodeError as exc:
                logger.error("portfolio.json korrupt: %s", exc)
                raise
        else:
            self._portfolio = {
                'initial_capital': INITIAL_CAPITAL,
                'cash': INITIAL_CAPITAL,
                'created': _now_iso(),
                'last_updated': _now_iso(),
                'holdings': [],
            }

        if TRADES_FILE.exists():
            try:
                self._trades = json.loads(TRADES_FILE.read_text(encoding='utf-8'))
            except json.JSONDecodeError as exc:
                logger.error("trades.json korrupt: %s", exc)
                raise
        else:
            self._trades = {'trades': []}

    def _save(self):
        PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)
        self._portfolio['last_updated'] = _now_iso()

        for path, data in [(PORTFOLIO_FILE, self._portfolio), (TRADES_FILE, self._trades)]:
            tmp = path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(tmp, path)

    def _total_value(self) -> float:
        holdings_val = sum(
            h['quantity'] * h['avg_buy_price']
            for h in self._portfolio['holdings']
        )
        return self._portfolio['cash'] + holdings_val

    def _sector_invested(self, sector: str, exclude_identifier: str | None = None) -> float:
        """Summe total_invested aller Holdings im selben Sektor/Themencluster
        (case-insensitiv). exclude_identifier klammert eine Position aus
        (z.B. die gerade aufgestockte, deren total_invested schon vorher gilt)."""
        sector_key = sector.strip().lower()
        exclude = exclude_identifier.lower() if exclude_identifier else None
        return sum(
            h['total_invested'] for h in self._portfolio['holdings']
            if h.get('sector', '').strip().lower() == sector_key
            and h['identifier'].lower() != exclude
        )

    def buy(self, name: str, identifier: str, quantity: float,
            price_per_unit: float, source: str = '', rationale: str = '',
            target: float | None = None, stop_loss: float | None = None,
            price_symbol: str | None = None, sector: str | None = None) -> dict | None:
        if quantity <= 0 or price_per_unit <= 0:
            logger.warning("BUY abgelehnt: ungültige Parameter qty=%s price=%s", quantity, price_per_unit)
            return None

        total_price = quantity * price_per_unit

        if total_price > self._portfolio['cash']:
            logger.warning(
                "BUY abgelehnt: nicht genug Cash. Benötigt %.2f €, verfügbar %.2f €",
                total_price, self._portfolio['cash'],
            )
            return None

        if total_price > MAX_POSITION_PCT * self._total_value():
            logger.warning(
                "BUY abgelehnt: 20%%-Limit überschritten. %.2f € > 20%% von %.2f €",
                total_price, self._total_value(),
            )
            return None

        if sector:
            sector_invested = self._sector_invested(sector, exclude_identifier=identifier)
            projected = sector_invested + total_price
            sector_limit = MAX_SECTOR_PCT * self._total_value()
            if projected > sector_limit:
                logger.warning(
                    "BUY abgelehnt: 40%%-Sektor-Limit für '%s' überschritten. "
                    "%.2f € (bestehend %.2f € + neu %.2f €) > 40%% von %.2f €",
                    sector, projected, sector_invested, total_price, self._total_value(),
                )
                return None

        if _runup_guard(name, identifier, price_symbol, source):
            return None

        self._portfolio['cash'] -= total_price

        ident_lower = identifier.lower()
        existing = next(
            (h for h in self._portfolio['holdings'] if h['identifier'].lower() == ident_lower),
            None
        )
        if existing:
            old_qty = existing['quantity']
            old_total = existing['total_invested']
            new_qty = old_qty + quantity
            existing['avg_buy_price'] = (old_total + total_price) / new_qty
            existing['quantity'] = new_qty
            existing['total_invested'] = old_total + total_price
            if target is not None:
                existing['target'] = target
            if stop_loss is not None:
                existing['stop_loss'] = stop_loss
            if sector:
                existing['sector'] = sector
        else:
            holding = {
                'identifier': identifier,
                'name': name,
                'quantity': quantity,
                'avg_buy_price': price_per_unit,
                'total_invested': total_price,
            }
            if target is not None:
                holding['target'] = target
            if stop_loss is not None:
                holding['stop_loss'] = stop_loss
            if price_symbol:
                holding['price_symbol'] = price_symbol
            if sector:
                holding['sector'] = sector
            self._portfolio['holdings'].append(holding)

        trade = self._record_trade('BUY', name, identifier, quantity, price_per_unit, total_price, source, rationale, target=target, stop_loss=stop_loss, sector=sector)
        self._save()
        return trade

    def sell(self, name: str, identifier: str, quantity: float,
             price_per_unit: float, source: str = '', rationale: str = '') -> dict | None:
        if quantity <= 0 or price_per_unit <= 0:
            logger.warning("SELL abgelehnt: ungültige Parameter qty=%s price=%s", quantity, price_per_unit)
            return None

        ident_lower = identifier.lower()
        existing = next(
            (h for h in self._portfolio['holdings'] if h['identifier'].lower() == ident_lower),
            None
        )
        if not existing:
            logger.warning("SELL abgelehnt: Position '%s' nicht im Depot.", identifier)
            return None

        if quantity > existing['quantity'] + 1e-8:
            logger.warning(
                "SELL: Menge %.6f > Bestand %.6f – wird auf Bestand gekappt.",
                quantity, existing['quantity'],
            )
        quantity = min(quantity, existing['quantity'])
        _stop_live_check(name, identifier, price_per_unit, existing.get('stop_loss'), source, rationale)

        total_price = quantity * price_per_unit
        self._portfolio['cash'] += total_price

        existing['quantity'] -= quantity
        existing['total_invested'] -= quantity * existing['avg_buy_price']

        if existing['quantity'] < 1e-8:
            self._portfolio['holdings'] = [
                h for h in self._portfolio['holdings']
                if h['identifier'].lower() != ident_lower
            ]

        trade = self._record_trade('SELL', name, identifier, quantity, price_per_unit, total_price, source, rationale)
        self._save()
        return trade

    def update_stop_loss(self, identifier: str, new_stop_loss: float,
                         source: str = '', rationale: str = '') -> dict | None:
        """Zieht den Stop-Loss einer bestehenden Position nach (Trailing-Stop).

        Erlaubt nur ein Anheben, nie ein Absenken – ein Trailing-Stop sichert
        Gewinne ab, darf das Risiko der Position aber nicht nachträglich erhöhen.
        """
        if new_stop_loss <= 0:
            logger.warning("UPDATE_STOP abgelehnt: ungültiger Wert %s", new_stop_loss)
            return None

        ident_lower = identifier.lower()
        existing = next(
            (h for h in self._portfolio['holdings'] if h['identifier'].lower() == ident_lower),
            None
        )
        if not existing:
            logger.warning("UPDATE_STOP abgelehnt: Position '%s' nicht im Depot.", identifier)
            return None

        old_stop_loss = existing.get('stop_loss')
        if old_stop_loss is not None and new_stop_loss <= old_stop_loss:
            logger.warning(
                "UPDATE_STOP abgelehnt: neuer Stop-Loss %.2f € nicht über bisherigem %.2f €.",
                new_stop_loss, old_stop_loss,
            )
            return None

        existing['stop_loss'] = new_stop_loss

        trade_id = len(self._trades['trades']) + 1
        trade = {
            'id': trade_id,
            'timestamp': _now_iso(),
            'action': 'UPDATE_STOP',
            'name': existing['name'],
            'identifier': identifier,
            'old_stop_loss': old_stop_loss,
            'stop_loss': new_stop_loss,
            'source': source,
            'rationale': rationale,
        }
        self._trades['trades'].append(trade)
        self._save()
        return trade

    def _record_trade(self, action, name, identifier, quantity, price_per_unit,
                      total_price, source, rationale,
                      target=None, stop_loss=None, sector=None) -> dict:
        trade_id = len(self._trades['trades']) + 1
        trade = {
            'id': trade_id,
            'timestamp': _now_iso(),
            'action': action,
            'name': name,
            'identifier': identifier,
            'quantity': quantity,
            'price_per_unit': price_per_unit,
            'total_price': total_price,
            'cash_after': self._portfolio['cash'],
            'source': source,
            'rationale': rationale,
        }
        if target is not None:
            trade['target'] = target
        if stop_loss is not None:
            trade['stop_loss'] = stop_loss
        if sector:
            trade['sector'] = sector
        self._trades['trades'].append(trade)
        return trade

    def get_value(self) -> dict:
        holdings_val = sum(
            h['quantity'] * h['avg_buy_price']
            for h in self._portfolio['holdings']
        )
        total = self._portfolio['cash'] + holdings_val
        initial = self._portfolio['initial_capital']
        pnl = total - initial
        pnl_pct = (pnl / initial * 100) if initial > 0 else 0.0
        return {
            'cash': self._portfolio['cash'],
            'holdings_value': holdings_val,
            'total': total,
            'initial_capital': initial,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
        }

    def get_holdings_table(self) -> str:
        holdings = self._portfolio['holdings']
        if not holdings:
            return '_Keine offenen Positionen._'

        val = self.get_value()
        total = val['total'] if val['total'] > 0 else 1.0

        lines = ['| Position | Identifier | Sektor | Menge | Avg Kauf | Kursziel | Stop-Loss | Investiert | % Depot |',
                 '|----------|-----------|--------|------:|----------:|---------:|----------:|----------:|-------:|']
        for h in holdings:
            invested = h['quantity'] * h['avg_buy_price']
            pct = invested / total * 100
            target_str = f"{h['target']:,.2f} €" if h.get('target') else '–'
            sl_str = f"{h['stop_loss']:,.2f} €" if h.get('stop_loss') else '–'
            sector_str = h.get('sector') or '–'
            lines.append(
                f"| {h['name']} | {h['identifier']} | {sector_str} | {h['quantity']:g} "
                f"| {h['avg_buy_price']:,.2f} € | {target_str} | {sl_str} "
                f"| {invested:,.2f} € | {pct:.1f}% |"
            )
        return '\n'.join(lines)

    def get_live_holdings_table(self) -> str:
        """Holdings-Tabelle mit Live-Kursen und P&L.

        Speichert gefundene price_symbols zurück in portfolio.json für zukünftige
        Abrufe. Fällt auf avg_buy_price zurück wenn kein Live-Kurs verfügbar.
        """
        holdings = self._portfolio['holdings']
        if not holdings:
            return '_Keine offenen Positionen._'

        changed = False
        lines = [
            '| Position | Identifier | Menge | Avg Kauf | Live-Kurs | P&L | Kursziel | Stop-Loss |',
            '|----------|-----------|------:|----------:|----------:|----:|---------:|----------:|',
        ]
        for h in holdings:
            result = get_current_price(
                h['identifier'], h.get('name', ''), h.get('price_symbol', '')
            )
            if result is not None:
                live = result['price']
                if not h.get('price_symbol') and result.get('symbol'):
                    h['price_symbol'] = result['symbol']
                    changed = True
                pnl_pct = (live - h['avg_buy_price']) / h['avg_buy_price'] * 100
                live_str = f"{live:,.2f} €"
                pnl_str = f"{pnl_pct:+.1f}%"
            else:
                live_str = f"{h['avg_buy_price']:,.2f} € *(kein Live-Kurs)*"
                pnl_str = "–"

            target_str = f"{h['target']:,.2f} €" if h.get('target') else '–'
            sl_str = f"{h['stop_loss']:,.2f} €" if h.get('stop_loss') else '–'
            lines.append(
                f"| {h['name']} | {h['identifier']} | {h['quantity']:g} "
                f"| {h['avg_buy_price']:,.2f} € | {live_str} | {pnl_str} "
                f"| {target_str} | {sl_str} |"
            )

        if changed:
            self._save()

        return '\n'.join(lines)

    def get_trade_history(self, last_n: int = 5) -> str:
        trades = self._trades['trades'][-last_n:]
        if not trades:
            return '_Noch keine Trades._'
        lines = []
        for t in reversed(trades):
            ts = t['timestamp'][:10]
            if t['action'] == 'UPDATE_STOP':
                old_s = f"{t['old_stop_loss']:,.2f} €" if t.get('old_stop_loss') else '–'
                lines.append(
                    f"- {ts} **STOP ANGEPASST** {t['name']} ({t['identifier']}) – "
                    f"{old_s} → {t['stop_loss']:,.2f} €"
                )
            else:
                lines.append(
                    f"- {ts} **{t['action']}** {t['name']} ({t['identifier']}) – "
                    f"{t['quantity']:g} × {t['price_per_unit']:,.2f} € = {t['total_price']:,.2f} €"
                )
        return '\n'.join(lines)

    def format_trade_for_telegram(self, trade: dict) -> str:
        action_emoji = '💹' if trade['action'] == 'BUY' else '📤'
        action_label = 'KAUF' if trade['action'] == 'BUY' else 'VERKAUF'

        def esc(s: str) -> str:
            return re.sub(r'([\\`\[\]()~>#+\-=|{}.!])', r'\\\1', str(s))

        val = self.get_value()
        qty_str = esc(f"{trade['quantity']:g}")
        price_str = esc(f"{trade['price_per_unit']:,.2f} €")
        total_str = esc(f"{trade['total_price']:,.2f} €")
        cash_str = esc(f"{trade['cash_after']:,.2f} €")
        depot_str = esc(f"{val['total']:,.2f} €")
        pnl_str = esc(f"{val['pnl_pct']:+.1f}%")
        lines = [
            f"{action_emoji} *NOVA Portfolio – {action_label}*",
            "",
            f"*Asset:* {esc(trade['name'])}",
            f"*Identifier:* {esc(trade['identifier'])}",
            f"*Menge:* {qty_str}",
            f"*Kurs:* {price_str}",
            f"*Gesamt:* {total_str}",
        ]
        if trade.get('target'):
            target_s = esc(f"{trade['target']:,.2f} €")
            lines.append(f"*Kursziel:* {target_s}")
        if trade.get('stop_loss'):
            sl_s = esc(f"{trade['stop_loss']:,.2f} €")
            lines.append(f"*Stop\\-Loss:* {sl_s}")
        lines += [
            f"*Cash danach:* {cash_str}",
            f"*Depot gesamt:* {depot_str} \\({pnl_str}\\)",
            "",
        ]
        if trade.get('rationale'):
            rationale_esc = esc(trade['rationale'][:200])
            lines.append(f"_{rationale_esc}_")
            lines.append("")
        lines.append("Nova \U0001f916 | @novas\\_crypto\\_signals")
        return '\n'.join(lines)

    def format_trade_for_blog(self, trade: dict) -> str:
        action_label = 'Kauf' if trade['action'] == 'BUY' else 'Verkauf'
        val = self.get_value()
        ts = trade['timestamp'][:16].replace('T', ' ')

        target_line = f"**Kursziel:** {trade['target']:,.2f} €\n" if trade.get('target') else ''
        sl_line = f"**Stop-Loss:** {trade['stop_loss']:,.2f} €\n" if trade.get('stop_loss') else ''
        return f"""## Nova Portfolio – {action_label}: {trade['name']}

**Zeitpunkt:** {ts}
**Aktion:** {trade['action']}
**Asset:** {trade['name']}
**Identifier:** {trade['identifier']}
**Menge:** {trade['quantity']:g}
**Kurs:** {trade['price_per_unit']:,.2f} €
**Gesamtbetrag:** {trade['total_price']:,.2f} €
{target_line}{sl_line}**Cash nach Trade:** {trade['cash_after']:,.2f} €
**Depot gesamt:** {val['total']:,.2f} € ({val['pnl_pct']:+.1f}%)

**Begründung:** {trade.get('rationale', '–')}

---
*Nova handelt rein algorithmisch auf Basis täglicher Marktanalysen. Keine Anlageberatung.*
"""

    def format_stop_update_for_telegram(self, trade: dict) -> str:
        def esc(s: str) -> str:
            return re.sub(r'([\\`\[\]()~>#+\-=|{}.!])', r'\\\1', str(s))

        old_s = esc(f"{trade['old_stop_loss']:,.2f} €") if trade.get('old_stop_loss') else esc('–')
        new_s = esc(f"{trade['stop_loss']:,.2f} €")
        lines = [
            f"🔼 *NOVA Portfolio – Stop\\-Loss nachgezogen*",
            "",
            f"*Asset:* {esc(trade['name'])}",
            f"*Identifier:* {esc(trade['identifier'])}",
            f"*Alter Stop\\-Loss:* {old_s}",
            f"*Neuer Stop\\-Loss:* {new_s}",
            "",
        ]
        if trade.get('rationale'):
            lines.append(f"_{esc(trade['rationale'][:200])}_")
            lines.append("")
        lines.append("Nova \U0001f916 | @novas\\_crypto\\_signals")
        return '\n'.join(lines)

    def format_stop_update_for_blog(self, trade: dict) -> str:
        ts = trade['timestamp'][:16].replace('T', ' ')
        old_s = f"{trade['old_stop_loss']:,.2f} €" if trade.get('old_stop_loss') else '–'
        return f"""## Nova Portfolio – Stop-Loss nachgezogen: {trade['name']}

**Zeitpunkt:** {ts}
**Asset:** {trade['name']}
**Identifier:** {trade['identifier']}
**Alter Stop-Loss:** {old_s}
**Neuer Stop-Loss:** {trade['stop_loss']:,.2f} €

**Begründung:** {trade.get('rationale', '–')}

---
*Nova handelt rein algorithmisch auf Basis täglicher Marktanalysen. Keine Anlageberatung.*
"""

    def format_status(self) -> str:
        val = self.get_value()
        lines = [
            f"## 📊 NOVA Portfolio-Status",
            f"",
            f"**Startkapital:** {val['initial_capital']:,.2f} €",
            f"**Aktueller Wert:** {val['total']:,.2f} € ({val['pnl_pct']:+.1f}%)",
            f"**Cash:** {val['cash']:,.2f} €",
            f"**Positionen:** {val['holdings_value']:,.2f} €",
            f"",
            f"### Offene Positionen",
            self.get_holdings_table(),
            f"",
            f"### Letzte Trades",
            self.get_trade_history(10),
        ]
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


# ---------------------------------------------------------------------------
# Main – Initialisierung und Status
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="NOVA Portfolio – Status und Initialisierung")
    parser.add_argument('--status', action='store_true', help='Nur Status anzeigen')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-8s  %(message)s')

    PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)

    if not PORTFOLIO_FILE.exists():
        p = Portfolio()
        p._save()
        print(f"✅ Portfolio initialisiert: {PORTFOLIO_FILE}")
        print(f"✅ Trade-Ledger initialisiert: {TRADES_FILE}")
    else:
        print(f"Portfolio vorhanden: {PORTFOLIO_FILE}")

    p = Portfolio()
    print()
    print(p.format_status())


if __name__ == '__main__':
    main()
