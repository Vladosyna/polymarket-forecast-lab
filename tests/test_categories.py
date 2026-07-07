"""Phase 17 (v2.4) item 1: stable internal category taxonomy.

Covers the tag/series -> category mapping (moved from universe.py's old
hardcoded TAG_CATEGORY_MAP into data/categories.yaml), the remap-safety
property downstream fits rely on, and the unrecognized-tag drift log.
"""

from __future__ import annotations

from lab.collect.categories import (
    category_for_kalshi_series,
    category_from_polymarket_tags,
    load_categories,
    log_unrecognized_tag,
)
from lab.store import db

TAXONOMY = load_categories()


def test_tag_mapping():
    assert category_from_polymarket_tags(["soccer", "sports", "fifa-world-cup"], TAXONOMY) == "sports"
    assert category_from_polymarket_tags(["economy", "fed-rates"], TAXONOMY) == "economics"
    assert category_from_polymarket_tags(["politics", "us-election"], TAXONOMY) == "politics"
    assert category_from_polymarket_tags(["geopolitics", "ukraine"], TAXONOMY) == "geopolitics"
    assert category_from_polymarket_tags(["movies", "oscars"], TAXONOMY) == "entertainment"
    assert category_from_polymarket_tags(["weather"], TAXONOMY) == "weather"
    assert category_from_polymarket_tags(["bitcoin"], TAXONOMY) == "crypto"
    assert category_from_polymarket_tags([], TAXONOMY) == "unknown"
    assert category_from_polymarket_tags(["some-new-tag"], TAXONOMY) == "unknown"


def test_exclusions_win_over_broader_tags():
    # A crypto event also tagged 'economy' must classify as crypto (excluded),
    # since crypto is listed before economics in categories.yaml's ordering.
    assert category_from_polymarket_tags(["crypto", "economy"], TAXONOMY) == "crypto"


def test_v2_equity_index_forex_commodity_tags_map_to_excluded_equities():
    """Real drift-log tag combos (v2 taxonomy expansion): index/forex/
    commodity/bank-stock price-target markets all resolve to 'equities',
    which universe.excluded_categories already excludes wholesale."""
    assert category_from_polymarket_tags(["finance", "spx", "hide-from-new"], TAXONOMY) == "equities"
    assert category_from_polymarket_tags(
        ["commodities", "finance", "finance-updown", "hit-price", "oil", "pyth-finance"], TAXONOMY
    ) == "equities"
    assert category_from_polymarket_tags(
        ["banking", "banks", "finance", "goldman-sachs", "gs", "kpis"], TAXONOMY
    ) == "equities"
    assert category_from_polymarket_tags(["dollar", "forex", "fx"], TAXONOMY) == "equities"


def test_v2_ipo_and_acquisition_tags_map_to_business_not_excluded():
    """Real drift-log finding: 'ipo'/'ipos' tags appear on BOTH pure timing
    questions and valuation-threshold questions -- the tag alone can't tell
    them apart, so this category must NOT be excluded (the separate
    _looks_equity_price_target() question-text check in universe.py handles
    the valuation subset). Confirms tag-based categorization stays permissive
    for corporate-event markets."""
    assert category_from_polymarket_tags(["ai", "anthropic", "anthropic-ipo", "finance", "ipo", "ipos"], TAXONOMY) == "business"
    assert category_from_polymarket_tags(["acquisitions", "finance"], TAXONOMY) == "business"
    assert category_from_polymarket_tags(["business", "finance", "privates"], TAXONOMY) == "business"


def test_v2_ai_tech_tags_map_to_tech_not_equities():
    """AI/tech product-news tags stay forecastable under a distinct 'tech'
    category rather than falling into 'unknown' or wrongly matching
    'equities' via a shared 'finance' co-occurrence in a different market."""
    assert category_from_polymarket_tags(["ai", "big-tech", "openai", "sam-altman", "gpt-5pt6"], TAXONOMY) == "tech"
    assert category_from_polymarket_tags(["ai", "gemini", "google", "tech"], TAXONOMY) == "tech"


def test_v2_health_and_weather_tags():
    assert category_from_polymarket_tags(["disease", "pandemics"], TAXONOMY) == "health"
    assert category_from_polymarket_tags(["climate-science", "natural-disasters", "tornadoes"], TAXONOMY) == "weather"


def test_kalshi_series_mapping():
    assert category_for_kalshi_series("Economics", TAXONOMY) == "economics"
    assert category_for_kalshi_series("Climate and Weather", TAXONOMY) == "weather"
    assert category_for_kalshi_series("Some New Series", TAXONOMY) == "unknown"


def test_load_categories_missing_file_falls_back_safely(tmp_path):
    taxonomy = load_categories(tmp_path / "does_not_exist.yaml")
    assert taxonomy["categories"] == ["unknown"]
    assert category_from_polymarket_tags(["anything"], taxonomy) == "unknown"


def test_remap_changes_only_the_mapped_category_string():
    """A retagged/remapped taxonomy changes which category string a market
    gets, but per-category downstream fits/weights key purely on that
    string -- proven by two different taxonomies producing two different,
    internally-consistent groupings from the SAME raw tags, with nothing
    else (no hidden tag-specific state) carried between them."""
    taxonomy_v1 = {"polymarket_tags": {"economics": ["fed-rates"]}}
    taxonomy_v2 = {"polymarket_tags": {"politics": ["fed-rates"]}}  # hypothetical remap

    markets = [{"tags": ["fed-rates"]}, {"tags": ["fed-rates"]}]

    def _group(taxonomy):
        grouped: dict[str, list] = {}
        for m in markets:
            cat = category_from_polymarket_tags(m["tags"], taxonomy)
            grouped.setdefault(cat, []).append(m)
        return grouped

    grouped_v1 = _group(taxonomy_v1)
    grouped_v2 = _group(taxonomy_v2)
    assert set(grouped_v1) == {"economics"}
    assert len(grouped_v1["economics"]) == 2
    assert set(grouped_v2) == {"politics"}
    assert len(grouped_v2["politics"]) == 2


def test_log_unrecognized_tag_writes_drift_row(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    log_unrecognized_tag(conn, "polymarket", "some-new-tag")
    conn.commit()
    rows = conn.execute(
        "SELECT venue, raw_tag, fallback_category FROM category_drift_log"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["venue"] == "polymarket"
    assert rows[0]["raw_tag"] == "some-new-tag"
    assert rows[0]["fallback_category"] == "unknown"
    conn.close()
