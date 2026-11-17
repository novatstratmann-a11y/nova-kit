"""
blog_client.py – REST-API-Client für den NOVA Blog.

Sendet eine .md-Datei an den NOVA Blog (http://localhost:3000 by default).
Blog-URL über Umgebungsvariable NOVA_BLOG_URL konfigurierbar.

Kategorien:
  ki-news    → personality/memory/ki-news-YYYY-MM-DD.md
  ki-weekly  → personality/memory/ki-weekly-YYYY-WNN.md
  market     → personality/memory/market-analysis-YYYY-MM-DD.md
  midday     → personality/memory/midday-analysis-YYYY-MM-DD.md
  nova       → personality/memory/nova-gedanken-YYYY-WNN.md
  weltlage   → personality/memory/weltlage/weltlage-YYYY-MM-DD.md
  weltlage-lagebild → personality/memory/weltlage/lagebild.md (Upsert, lebendes Dokument)

Verwendung:
  from blog_client import post_to_blog
  ok = post_to_blog(category="ki-news", md_file=Path("personality/memory/ki-news-2026-02-23.md"), date="2026-02-23")
"""

import logging
import os
import re
import urllib.request
import urllib.error
import json
from pathlib import Path

logger = logging.getLogger("blog_client")

BLOG_URL = os.environ.get("NOVA_BLOG_URL", "http://localhost:3000").rstrip("/")

# Mapping: category → human-readable section name (for logging only)
CATEGORY_LABELS = {
    "ki-news":      "KI-News (Daily)",
    "ki-weekly":    "KI-News (Weekly)",
    "market":       "Marktanalyse (Daily)",
    "midday":       "Marktanalyse (Langfrist)",
    "head-follies": "Head-Follies (Daily)",
    "erklaer-mir-kurz": "Erklär mir kurz (Daily)",
    "portfolio":    "Portfolio-Trade",
    "strategy":     "Strategie",
    "nova":         "Nova (Gedanken)",
    "weltlage":     "Weltlage (Bericht)",
    "weltlage-lagebild": "Weltlage (Lagebild)",
}


def _extract_title(content: str) -> str:
    """Extract the first non-empty line as title, strip Telegram markdown."""
    for line in content.splitlines():
        line = line.strip()
        if line:
            # Remove Telegram bold (*text*) and leading emoji
            line = re.sub(r'^#+\s*', '', line)  # Markdown-Überschrift
            title = re.sub(r'\*([^*]+)\*', r'\1', line)
            title = re.sub(r'^[\U0001F300-\U0001FAFF\U00002600-\U000027BF\uFE0F\u200D\s]+', '', title).strip()
            return title or line
    return "Kein Titel"


def post_to_blog(category: str, md_file: Path, date: str) -> bool:
    """
    POST the markdown file to the NOVA Blog REST API.

    Args:
        category: One of 'ki-news', 'ki-weekly', 'market', 'midday'
        md_file:  Path to the .md file to upload
        date:     ISO date string (YYYY-MM-DD)

    Returns:
        True on success, False on failure.
    """
    if not md_file.exists():
        logger.warning("Blog: Datei nicht gefunden: %s", md_file)
        return False

    content = md_file.read_text(encoding="utf-8").strip()
    # Telegram MarkdownV2 escapes entfernen (\.  \-  \(  \)  \!  \*  etc.)
    content = re.sub(r'\\([_*\[\]()~`#+\-=|{}.!])', r'\1', content)
    if len(content) < 50:
        logger.warning("Blog: Datei zu kurz (%d Zeichen), übersprungen.", len(content))
        return False

    payload = {
        "category": category,
        "title":    _extract_title(content),
        "content":  content,
        "date":     date,
        "filename": md_file.name,
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BLOG_URL}/api/posts",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            logger.info(
                "Blog: %s veröffentlicht (id=%s).",
                CATEGORY_LABELS.get(category, category),
                result.get("id", "?"),
            )
            return True
    except urllib.error.URLError as e:
        logger.warning("Blog: Nicht erreichbar (%s) – wird übersprungen.", e.reason)
        return False
    except Exception as e:
        logger.warning("Blog: Fehler beim Senden – %s", e)
        return False
