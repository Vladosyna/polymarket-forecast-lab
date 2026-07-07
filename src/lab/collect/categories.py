"""Phase 17 (v2.4) item 1: stable internal category taxonomy.

Gamma event tags drift (a tag gets renamed or retired) and Kalshi series
names don't align with Polymarket's own tag vocabulary at all -- two
independently hand-maintained tag->category mappings with no shared enum
and no drift detection is the failure this replaces. `data/categories.yaml`
is now the single source of truth; every per-category fit/weight downstream
(M2 base rates, M4 weights, the wealth ledger) keys purely on the category
STRING these functions return, so a remap here changes nothing about how
those consumers group data -- only which string a given venue-native tag/
series maps to.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from lab.util import PROJECT_ROOT, now_utc_iso

log = logging.getLogger(__name__)

DEFAULT_CATEGORIES_PATH = PROJECT_ROOT / "data" / "categories.yaml"

_FALLBACK = "unknown"


def load_categories(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_CATEGORIES_PATH
    if not p.exists():
        return {"version": 0, "categories": [_FALLBACK], "polymarket_tags": {}, "kalshi_series": {}}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("categories", [_FALLBACK])
    data.setdefault("polymarket_tags", {})
    data.setdefault("kalshi_series", {})
    return data


def category_from_polymarket_tags(tag_slugs: list[str], taxonomy: dict[str, Any]) -> str:
    """First matching category wins (ordered by specificity in categories.yaml),
    mirroring the TAG_CATEGORY_MAP list this replaces."""
    slugs = set(tag_slugs)
    for category, tags in taxonomy.get("polymarket_tags", {}).items():
        if slugs & set(tags):
            return category
    return _FALLBACK


def category_for_kalshi_series(series: str, taxonomy: dict[str, Any]) -> str:
    return taxonomy.get("kalshi_series", {}).get(series, _FALLBACK)


def log_unrecognized_tag(conn, venue: str, raw_tag: str) -> None:
    """Record a venue-native tag/series that fell back to 'unknown' -- makes
    taxonomy drift visible (in lab status/report) instead of silently diluting
    the unknown bucket. One row per occurrence; cheap, append-only, not
    deduplicated (repeated drift on the same tag is itself a useful signal of
    how long it's gone unfixed)."""
    conn.execute(
        "INSERT INTO category_drift_log (ts, venue, raw_tag, fallback_category) VALUES (?, ?, ?, ?)",
        (now_utc_iso(), venue, raw_tag, _FALLBACK),
    )
