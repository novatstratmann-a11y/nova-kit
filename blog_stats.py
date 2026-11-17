#!/usr/bin/env python3
"""Auswertung des anonymen Seitenaufruf-Zählers im Blog (Tabelle page_views).

    python blog_stats.py            # letzte 14 Tage
    python blog_stats.py --days 30
    python blog_stats.py --unknown  # unbekannte Pfade (Tabelle unknown_paths, seit 2026-11-15)

Liest blog/blog_data/blog.db read-only. Es gibt nur Aggregate (Tag, Pfad, Art, Anzahl).
"""
import argparse
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB = Path(__file__).parent / "blog" / "blog_data" / "blog.db"
METHOD_CHANGE = "2026-11-05"  # ab hier: human braucht Accept-Language, unbekannte Pfade = scan


def _no_db() -> str:
    return f"Keine Zählerdaten: {DB} fehlt (Blog noch nicht gestartet?)."


def stats(days: int = 14) -> str:
    if not DB.exists():
        return _no_db()
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            # Altdaten bereinigen: '(sonstige)' war vor METHOD_CHANGE als human gezählt → als scan werten
            "SELECT day, CASE WHEN path='(sonstige)' THEN 'scan' ELSE kind END k, SUM(hits) "
            "FROM page_views WHERE day >= ? GROUP BY day, k ORDER BY day",
            (since,),
        ).fetchall()
        top = con.execute(
            "SELECT path, SUM(hits) h FROM page_views WHERE day >= ? AND kind='human' AND path != '(sonstige)' "
            "GROUP BY path ORDER BY h DESC LIMIT 10",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        return f"Keine Zählerdaten ({e})."
    finally:
        con.close()

    per_day: dict[str, dict[str, int]] = {}
    for d, kind, n in rows:
        per_day.setdefault(d, {"human": 0, "bot": 0, "scan": 0})[kind] = n
    total_h = sum(v["human"] for v in per_day.values())
    total_b = sum(v["bot"] for v in per_day.values())
    total_s = sum(v["scan"] for v in per_day.values())

    out = [f"# Blog-Aufrufe seit {since} ({days} Tage)", "",
           f"Menschen (Obergrenze): **{total_h}** · Bots: {total_b} · Scanner (unbekannte Pfade): {total_s}", "",
           f"Achtung: Vor {METHOD_CHANGE} zählten unbekannte Pfade („(sonstige)“) und Browser ohne "
           "Accept-Language als Mensch. Für diese Tage ist „(sonstige)“ unten abgezogen.", "",
           "| Tag | Menschen | Bots | Scanner |", "|---|---:|---:|---:|"]
    out += [f"| {d} | {v['human']} | {v['bot']} | {v['scan']} |" for d, v in sorted(per_day.items())]
    out += ["", "Top-Pfade (Menschen):", ""]
    out += [f"- {p}: {h}" for p, h in top] or ["- (keine)"]
    return "\n".join(out)


def unknown(days: int = 14) -> str:
    """Unbekannte Pfade: '?' = passt zu keinem Scanner-Muster (Kandidat für echte Nutzerpfade)."""
    if not DB.exists():
        return _no_db()
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT cls, path, SUM(hits) h, MIN(day), MAX(day) FROM unknown_paths WHERE day >= ? "
            "GROUP BY cls, path ORDER BY cls, h DESC",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        return f"Keine Pfaddaten ({e})."
    finally:
        con.close()
    out = [f"# Unbekannte Pfade seit {since} (Limit/Tag: 10 scan, 100 ?)", ""]
    for cls in ("?", "scan"):
        sub = [r for r in rows if r[0] == cls]
        out += [f"## {cls} ({len(sub)} Pfade, {sum(r[2] for r in sub)} Treffer)", ""]
        out += [f"- `{p}`: {h} ({d1}–{d2})" for _, p, h, d1, d2 in sub] or ["- (keine)"]
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--unknown", action="store_true", help="unbekannte Pfade statt Aufrufzahlen")
    a = ap.parse_args()
    print(unknown(a.days) if a.unknown else stats(a.days))
