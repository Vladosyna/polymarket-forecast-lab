"""Manifold Markets collector: markets + resolutions only (guardrail 16).

Manifold is play-money -- collected strictly for event mapping and M2 base
rates. There is deliberately no `snapshot_manifold()` function in this module
and no Parquet writer call anywhere here: Manifold prices are never a time
series in this system and must never touch store/snapshots.py.

There is also deliberately no separate resolution-watcher module/loop (unlike
Polymarket's resolutions.py or a Kalshi analog). Polymarket and Kalshi require
a second fetch of a market to learn whether it has since settled; Manifold's
own `/search-markets?filter=resolved` call already returns `isResolved`,
`resolution`, and `resolutionTime` directly in the same list response used for
the sync itself, so `record_manifold_resolution` is invoked inline per item
during `sync_manifold_markets` -- one fewer function than the Kalshi/Polymarket
shape, not a missing piece.

Tiering assumption (guardrail 1): every Manifold market is tier='ignored',
unconditionally. This isn't a placeholder pending real tiering logic --
Manifold markets are never forecast targets (play-money, guardrail 16), so
'liquid'/'tail' would be actively misleading labels for anything that reads
the tier column downstream.

Category assumption (guardrail 1, per orchestrator instructions): Manifold's
topic/group taxonomy is inconsistently present on markets and not modeled by
ManifoldMarket in api/manifold.py. category='unknown' for every row is
deliberate and acceptable per guardrail 2 (simplicity first) -- building
topic-tag scraping is out of scope for this pass.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from lab.api.manifold import ManifoldClient, ManifoldMarket
from lab.store import db
from lab.util import now_utc_iso

log = logging.getLogger(__name__)

_USABLE_RESOLUTIONS = ("YES", "NO")


def _iso_from_ms(ms: int | None) -> str | None:
    """Pure data conversion of an already-fetched epoch-millisecond value into
    an ISO-8601 UTC string -- not a clock read, so it doesn't violate
    guardrail 6 (now_utc() is the only clock call allowed)."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def manifold_market_row(m: ManifoldMarket) -> dict[str, Any]:
    return {
        "condition_id": db.venue_condition_id("manifold", m.id),
        "venue": "manifold",
        "venue_native_id": m.id,
        "slug": m.slug,
        "question": m.question,
        "category": "unknown",
        "description": None,
        "end_date_iso": _iso_from_ms(m.close_time_ms),
        "token_id_yes": None,
        "token_id_no": None,
        "neg_risk": 0,
        "active": 0 if m.is_resolved else 1,
        "closed": 1 if m.is_resolved else 0,
        "liquidity_num": None,
        "volume_num": m.volume,
        "tier": "ignored",
    }


def record_manifold_resolution(conn, m: ManifoldMarket) -> bool:
    """Record a final payout if `m` carries a usable binary resolution.

    Returns True if a resolution was recorded, False otherwise (not yet
    resolved, or resolved to something that isn't a clean binary outcome --
    'MKT' (resolved to a probability) or 'CANCEL' must not be forced into
    payout_yes 0.0/1.0)."""
    if not m.is_resolved or m.resolution not in _USABLE_RESOLUTIONS:
        return False
    resolved_ts = _iso_from_ms(m.resolution_time_ms) or now_utc_iso()
    db.record_resolution(
        conn,
        db.venue_condition_id("manifold", m.id),
        resolved_ts=resolved_ts,
        payout_yes=1.0 if m.resolution == "YES" else 0.0,
        disputed=False,
        source="manifold",
    )
    return True


async def sync_manifold_markets(manifold_client: ManifoldClient, conn, config: dict[str, Any]) -> dict[str, int]:
    """Paginate open + resolved binary markets up to the configured per-cycle
    cap, upsert rows, and record any usable resolutions inline.

    Bounded per cycle for politeness + runtime (brief/orchestrator note); the
    cap is split evenly between the two filters. Truncation against the cap is
    logged, never silent.
    """
    max_total = config["venues"]["manifold"]["max_markets_per_sync"]
    per_filter_cap = max_total // 2
    page_limit = 200

    counts = {"open_fetched": 0, "resolved_fetched": 0, "upserted": 0, "resolutions_recorded": 0}

    for filter_name, cap_key in (("open", "open_fetched"), ("resolved", "resolved_fetched")):
        offset = 0
        fetched_this_filter = 0
        while fetched_this_filter < per_filter_cap:
            remaining = per_filter_cap - fetched_this_filter
            page = await manifold_client.search_markets(
                filter=filter_name, offset=offset, limit=min(page_limit, remaining)
            )
            if not page:
                break
            for m in page:
                db.upsert_market(conn, manifold_market_row(m))
                counts["upserted"] += 1
                if record_manifold_resolution(conn, m):
                    counts["resolutions_recorded"] += 1
                conn.commit()
            fetched_this_filter += len(page)
            counts[cap_key] += len(page)
            offset += len(page)
            if len(page) < min(page_limit, remaining):
                break  # short page -- reached the end of this filter's results

        if fetched_this_filter >= per_filter_cap:
            log.info(
                "manifold: per-cycle cap reached for filter, truncating (not silent)",
                extra={"ctx": {"filter": filter_name, "cap": per_filter_cap, "fetched": fetched_this_filter}},
            )

    log.info("manifold sync complete", extra={"ctx": counts})
    return counts
