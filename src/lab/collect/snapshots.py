"""Order-book snapshot loop: liquid tier every 5 min, tail every 60 min.

Rows are keyed by (ts floored to the tier cadence, condition_id); the store
drops duplicates, so restarts and overlapping runs cannot double-write.
Full book depth (top-N levels) is stored alongside top-of-book because the
historical depth cannot be re-collected later; current models don't consume
it, future microstructure work might.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from lab.api.clob import ClobClient
from lab.store.snapshots import SnapshotStore, floor_ts_bucket
from lab.util import now_utc

log = logging.getLogger(__name__)


def tracked_markets(conn, tier: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT condition_id, token_id_yes FROM markets
        WHERE tier = ? AND active = 1 AND closed = 0 AND token_id_yes IS NOT NULL
        """,
        (tier,),
    ).fetchall()
    return [dict(r) for r in rows]


async def snapshot_tier(
    clob: ClobClient, conn, store: SnapshotStore, tier: str, config: dict[str, Any]
) -> int:
    """One snapshot round for a tier. Returns rows written (post-dedup)."""
    markets = tracked_markets(conn, tier)
    if not markets:
        log.info("snapshot round: no markets", extra={"ctx": {"tier": tier}})
        return 0
    bucket_minutes = config["collect"]["snapshot_interval_minutes"][tier]
    ts_bucket = floor_ts_bucket(now_utc(), bucket_minutes)
    depth_levels = config["collect"].get("book_depth_levels", 10)

    rows: list[dict] = []
    for m in markets:
        try:
            book = await clob.book(m["token_id_yes"])
        except Exception:
            # Fail soft: one bad market never kills the round (guardrail 9).
            log.warning("snapshot: book fetch failed",
                        extra={"ctx": {"condition_id": m["condition_id"]}})
            continue
        if book.mid is None:
            continue
        rows.append({
            "ts": ts_bucket,
            "condition_id": m["condition_id"],
            "token_id_yes": m["token_id_yes"],
            "best_bid": book.best_bid,
            "best_ask": book.best_ask,
            "mid": book.mid,
            "spread": book.spread,
            "bid_depth_usd": book.depth_usd("bid"),
            "ask_depth_usd": book.depth_usd("ask"),
            "last_trade_price": None,  # populated later if a model needs it
            "bids_json": json.dumps(book.top_levels("bid", depth_levels)),
            "asks_json": json.dumps(book.top_levels("ask", depth_levels)),
        })
    written = store.append(rows)
    log.info("snapshot round done",
             extra={"ctx": {"tier": tier, "markets": len(markets), "written": written}})
    return written
