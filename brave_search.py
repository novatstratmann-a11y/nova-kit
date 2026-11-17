#!/usr/bin/env python3
"""
brave_search.py – Brave Search API Utility

Wrapper für die Brave Web Search API. Kann als Modul importiert oder
direkt ausgeführt werden.

API-Key: .env → BRAVE_SEARCH_KEY
Endpoint: https://api.search.brave.com/res/v1/web/search

Verwendung als Modul:
    from brave_search import search, search_news

    results = search("Bitcoin RSI Analyse")
    for r in results:
        print(r["title"], r["url"])
        print(r["description"])

    news = search_news("KI Neuigkeiten", count=5)

Verwendung als Script:
    python brave_search.py "Suchbegriff"
    python brave_search.py "KI Neuigkeiten" --count 5 --freshness pd
    python brave_search.py "Krypto" --json
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from env_utils import get_env


def _load_api_key() -> str:
    """Liest den Brave-API-Key aus .env (BRAVE_SEARCH_KEY)."""
    key = get_env("BRAVE_SEARCH_KEY")
    if not key:
        raise RuntimeError("BRAVE_SEARCH_KEY nicht in .env gefunden.")
    return key


def search(
    query: str,
    count: int = 10,
    freshness: Optional[str] = None,
    country: str = "DE",
    search_lang: str = "de",
    ui_lang: str = "de-DE",
) -> list[dict]:
    """
    Führt eine Brave-Web-Suche durch.

    Args:
        query:       Suchbegriff
        count:       Anzahl Ergebnisse (1-20)
        freshness:   Zeitfilter: 'pd' (24h), 'pw' (7 Tage), 'pm' (30 Tage), None (alle)
        country:     Ländercode (ISO 3166-1 alpha-2), z.B. 'DE', 'US'
        search_lang: Sprachcode, z.B. 'de', 'en'
        ui_lang:     UI-Sprache (BCP 47), z.B. 'de-DE', 'en-US'

    Returns:
        Liste von Dicts mit keys: title, url, description, age
    """
    api_key = _load_api_key()

    params = {
        "q": query,
        "count": min(max(1, count), 20),
        "country": country,
        "search_lang": search_lang,
        "ui_lang": ui_lang,
        "text_decorations": "false",
        "result_filter": "web",
    }
    if freshness:
        params["freshness"] = freshness

    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Encoding", "gzip")
    req.add_header("X-Subscription-Token", api_key)

    # Rate-Limit (Free-Plan: 1 Anfrage/s) → bei HTTP 429 mit Pause erneut versuchen
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                # Gzip-Dekomprimierung wenn nötig
                if resp.info().get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                data = json.loads(raw.decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"Brave API HTTP-Fehler {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Brave API Verbindungsfehler: {e.reason}") from e

    results = []
    for item in data.get("web", {}).get("results", []):
        results.append({
            "title":       item.get("title", ""),
            "url":         item.get("url", ""),
            "description": item.get("description", ""),
            "age":         item.get("age", ""),
        })
    return results


def search_news(query: str, count: int = 5) -> list[dict]:
    """
    Sucht nach aktuellen Nachrichten (letzten 24 Stunden).
    Kurzform für search(..., freshness='pd').
    """
    return search(query, count=count, freshness="pd")


def format_results(results: list[dict], max_chars: int = 3000) -> str:
    """
    Formatiert Suchergebnisse als lesbaren Text (für Prompts oder Ausgabe).
    Kürzt automatisch wenn max_chars überschritten.
    """
    lines = []
    total = 0
    for i, r in enumerate(results, 1):
        entry = f"{i}. **{r['title']}**\n   {r['url']}"
        if r.get("description"):
            entry += f"\n   {r['description']}"
        if r.get("age"):
            entry += f" ({r['age']})"
        entry += "\n"
        if total + len(entry) > max_chars:
            break
        lines.append(entry)
        total += len(entry)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Brave Web Search")
    parser.add_argument("query", help="Suchbegriff")
    parser.add_argument("--count", type=int, default=10, help="Anzahl Ergebnisse (max 20)")
    parser.add_argument("--freshness", choices=["pd", "pw", "pm"], default=None,
                        help="Zeitfilter: pd=24h, pw=7 Tage, pm=30 Tage")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Ergebnisse als JSON ausgeben")
    args = parser.parse_args()

    try:
        results = search(args.query, count=args.count, freshness=args.freshness)
    except RuntimeError as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print("Keine Ergebnisse gefunden.")
        sys.exit(0)

    if args.as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"\n{len(results)} Ergebnisse für: {args.query!r}\n")
        print(format_results(results))


if __name__ == "__main__":
    main()
