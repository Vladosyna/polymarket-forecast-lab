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
from lab.collect.categories import (
    category_from_polymarket_tags,
    load_categories,
    log_unrecognized_tag,
)
from lab.store import db
from lab.store.snapshots import SnapshotStore, utc_date_str
from lab.util import now_utc

log = logging.getLogger(__name__)

CRYPTO_HINTS = ("crypto", "bitcoin", "btc", "ethereum", "eth ", "solana", "dogecoin", "xrp")


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


def assign_tier_with_category(m: GammaMarket, category: str, config: dict[str, Any],
                              depth_usd: float | None = None) -> str:
    """Phase 17 item 2: once we have our own collected order-book depth for a
    market, tier on THAT (depth_usd = bid_depth_usd + ask_depth_usd from the
    latest snapshot) instead of Gamma's self-reported liquidity_num/volume_num
    -- volume in particular is wash-trading-contaminated (concentrated in
    sports) and was never a sound tiering signal, just the only one available
    pre-collection. A brand-new market has no snapshot yet (depth_usd is
    None); it falls back to the liquidity/volume path below until one exists.
    """
    if m.enable_order_book is False or m.accepting_orders is False:
        return "ignored"
    if category in set(config["universe"]["excluded_categories"]) or _looks_crypto(m):
        return "ignored"
    tiers = config["universe"]["tiers"]
    if depth_usd is not None:
        if depth_usd >= tiers["liquid"]["min_depth_usd"]:
            return "liquid"
        if depth_usd >= tiers["tail"]["min_depth_usd"]:
            return "tail"
        return "ignored"
    liq = m.liquidity_num or 0.0
    vol = m.volume_num or 0.0
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
        # Covariate only, not used for tiering (Phase 17 item 2): Gamma's
        # volume_num is wash-trading-contaminated, concentrated in sports.
        "volume_num": m.volume_num,
        "tier": tier,
    }


def _link_negrisk_legs(conn, condition_ids: list[str], title: str | None) -> None:
    """Phase 16: chain-link a negRisk event's legs into one shared event_id
    via the same mechanism the cross-venue matcher uses (`db.link_event`),
    just applied at sync time to same-venue legs instead of at human-confirm
    time. `link_event` already reuses an existing event_id from either side
    of a pair, so calling it pairwise (leg[0]-leg[1], leg[0]-leg[2], ...)
    correctly propagates ONE shared id across all N legs -- no new N-ary
    linking primitive needed. This is what lets Phase 16's RPS scoring find
    "which legs belong to one bucketed numeric question" on RESOLVED
    forecasts later -- Gamma's own event grouping is live-only (gone once a
    market closes), so it must be persisted here, not reconstructed after
    the fact. Idempotent: re-sync of an already-linked event is a no-op.
    """
    first = condition_ids[0]
    for other in condition_ids[1:]:
        db.link_event(conn, first, other, title=title)


def _depth_lookup(store: SnapshotStore, now: datetime, days_back: int = 3) -> dict[str, float]:
    """condition_id -> most recent (bid_depth_usd + ask_depth_usd), Phase 17
    item 2. A market with no snapshot in the window is simply absent from the
    returned dict -- callers treat that as "no depth data yet" (falls back to
    liquidity/volume tiering), not as zero depth."""
    dates = [utc_date_str(now - timedelta(days=d)) for d in range(days_back + 1)]
    df = store.latest_per_market(dates)
    if df.is_empty():
        return {}
    depth = df["bid_depth_usd"].fill_null(0) + df["ask_depth_usd"].fill_null(0)
    return dict(zip(df["condition_id"].to_list(), depth.to_list()))


async def sync_universe(gamma: GammaClient, conn, store: SnapshotStore,
                        config: dict[str, Any]) -> dict[str, int]:
    """Fetch active events (markets + tags) from Gamma, tier and upsert (idempotent).

    Events are the source of truth because market-level `category` is no
    longer populated -- categories come from event tags.
    """
    now = now_utc()
    taxonomy = load_categories()
    depth_by_market = _depth_lookup(store, now)
    events = await gamma.iter_events(active="true", closed="false")
    counts = {"events": len(events), "seen": 0, "binary": 0,
              "liquid": 0, "tail": 0, "ignored": 0, "skipped": 0}
    seen_ids: set[str] = set()
    for ev in events:
        category = category_from_polymarket_tags(ev.tag_slugs, taxonomy)
        if category == "unknown" and ev.tag_slugs:
            log_unrecognized_tag(conn, "polymarket", ",".join(sorted(ev.tag_slugs)))
        event_leg_ids: list[str] = []
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
            depth_usd = depth_by_market.get(effective.condition_id)
            tier = assign_tier_with_category(effective, category, config, depth_usd=depth_usd)
            counts[tier] += 1
            db.upsert_market(conn, market_row(effective, tier, category))
            if ev.neg_risk:
                event_leg_ids.append(effective.condition_id)
        if ev.neg_risk and len(event_leg_ids) >= 2:
            _link_negrisk_legs(conn, event_leg_ids, title=ev.slug)
    conn.commit()
    log.info("universe sync complete", extra={"ctx": counts})
    return counts
