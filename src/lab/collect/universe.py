"""Market discovery & tiering (one sync per hour).

Tiering assumption (brief leaves the combination open): a market is `liquid`
when BOTH liquidity and volume clear the liquid thresholds, `tail` when both
clear the tail thresholds, else `ignored`. Markets without a live order book
are `ignored` regardless. Excluded categories (crypto/equities) stay in the
DB as `ignored` so calibration stats can still use them, except sub-24h
crypto "pulse" markets which are skipped entirely.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from lab.api.gamma import GammaClient, GammaMarket
from lab.store import db
from lab.util import now_utc

log = logging.getLogger(__name__)

CRYPTO_HINTS = ("crypto", "bitcoin", "btc", "ethereum", "eth ", "solana", "dogecoin", "xrp")

# Gamma stopped populating markets.category; tags on the parent event are the
# live category signal. First matching group wins (ordered by specificity).
TAG_CATEGORY_MAP: list[tuple[str, tuple[str, ...]]] = [
    ("crypto", ("crypto", "bitcoin", "ethereum", "solana", "memecoins", "defi")),
    ("equities", ("stocks", "equities", "wall-street", "big-tech-stocks")),
    ("weather", ("weather", "climate", "temperature", "hurricanes")),
    ("economics", ("economy", "economics", "fed", "fed-rates", "inflation",
                    "macro", "interest-rates", "gdp", "jobs-report", "cpi")),
    ("sports", ("sports", "soccer", "nba", "nfl", "mlb", "nhl", "tennis", "golf",
                 "f1", "ufc", "boxing", "cricket", "esports", "olympics",
                 "fifa-world-cup", "college-football", "college-basketball")),
    ("entertainment", ("entertainment", "movies", "awards", "oscars", "music",
                        "pop-culture", "celebrities", "tv", "grammys")),
    ("geopolitics", ("geopolitics", "world", "ukraine", "israel", "china",
                      "middle-east", "war")),
    ("politics", ("politics", "elections", "us-politics", "trump", "congress",
                   "us-election", "supreme-court")),
]


def category_from_tags(tag_slugs: list[str]) -> str:
    slugs = set(tag_slugs)
    for category, tags in TAG_CATEGORY_MAP:
        if slugs & set(tags):
            return category
    return "unknown"


def _category(m: GammaMarket) -> str:
    return (m.category or "unknown").strip().lower() or "unknown"


def _looks_crypto(m: GammaMarket) -> bool:
    text = " ".join(filter(None, [_category(m), m.slug or "", (m.question or "").lower()])).lower()
    return any(h in text for h in CRYPTO_HINTS)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_sub_24h_crypto(m: GammaMarket, now: datetime) -> bool:
    if not _looks_crypto(m):
        return False
    end = _parse_iso(m.end_date_iso)
    start = _parse_iso(m.start_date_iso)
    if end and start and end - start <= timedelta(hours=24):
        return True
    return bool(end and end - now <= timedelta(hours=24))


def assign_tier(m: GammaMarket, config: dict[str, Any]) -> str:
    return assign_tier_with_category(m, _category(m), config)


def assign_tier_with_category(m: GammaMarket, category: str, config: dict[str, Any]) -> str:
    if m.enable_order_book is False or m.accepting_orders is False:
        return "ignored"
    if category in set(config["universe"]["excluded_categories"]) or _looks_crypto(m):
        return "ignored"
    liq = m.liquidity_num or 0.0
    vol = m.volume_num or 0.0
    tiers = config["universe"]["tiers"]
    if liq >= tiers["liquid"]["min_liquidity"] and vol >= tiers["liquid"]["min_volume"]:
        return "liquid"
    if liq >= tiers["tail"]["min_liquidity"] and vol >= tiers["tail"]["min_volume"]:
        return "tail"
    return "ignored"


def market_row(m: GammaMarket, tier: str, category: str | None = None) -> dict[str, Any]:
    return {
        "condition_id": m.condition_id,
        "slug": m.slug,
        "question": m.question,
        "category": category or _category(m),
        "description": m.description,
        "end_date_iso": m.end_date_iso,
        "token_id_yes": m.token_id_yes,
        "token_id_no": m.token_id_no,
        "neg_risk": int(m.neg_risk),
        "active": int(bool(m.active)),
        "closed": int(bool(m.closed)),
        "liquidity_num": m.liquidity_num,
        "volume_num": m.volume_num,
        "tier": tier,
    }


async def sync_universe(gamma: GammaClient, conn, config: dict[str, Any]) -> dict[str, int]:
    """Fetch active events (markets + tags) from Gamma, tier and upsert (idempotent).

    Events are the source of truth because market-level `category` is no
    longer populated -- categories come from event tags.
    """
    now = now_utc()
    events = await gamma.iter_events(active="true", closed="false")
    counts = {"events": len(events), "seen": 0, "binary": 0,
              "liquid": 0, "tail": 0, "ignored": 0, "skipped": 0}
    seen_ids: set[str] = set()
    for ev in events:
        category = category_from_tags(ev.tag_slugs)
        for m in ev.markets:
            if m.condition_id in seen_ids:
                continue
            seen_ids.add(m.condition_id)
            counts["seen"] += 1
            if not m.is_binary:
                continue
            counts["binary"] += 1
            if config["universe"]["skip_sub_24h_crypto"] and is_sub_24h_crypto(m, now):
                counts["skipped"] += 1
                continue
            effective = m if not ev.neg_risk else m.model_copy(update={"neg_risk": True})
            tier = assign_tier_with_category(effective, category, config)
            counts[tier] += 1
            db.upsert_market(conn, market_row(effective, tier, category))
    conn.commit()
    log.info("universe sync complete", extra={"ctx": counts})
    return counts
