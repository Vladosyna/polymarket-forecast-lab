"""Kalshi collector: market discovery/tiering, snapshot loop, resolution
watcher -- the Kalshi analog of universe.py / snapshots.py / resolutions.py
(brief section 3, Phase 10).

Assumptions (guardrail 1 -- stated rather than silently picked):

1. Discovery is deliberately NOT a bulk `/markets` scan. The unfiltered feed is
   flooded with sub-24h crypto price-target markets and multivariate combo
   markets that the brief's universe policy excludes outright (martingale
   underlyings; "ALL crypto/equity price-target markets at any horizon").
   Instead we walk `config['venues']['kalshi']['category_map']` -> per-category
   `/series` -> per-series `/markets`, explicitly skipping
   `excluded_series_categories` even if such a category were reachable another
   way, and capping total series processed per cycle via `max_series_per_sync`.
2. Tiering mirrors `assign_tier_with_category` in collect/universe.py exactly,
   just against Kalshi's own config['venues']['kalshi']['tiers'] thresholds
   (liquid requires BOTH liquidity and volume to clear; tail requires BOTH to
   clear the (lower) tail thresholds; else ignored).
3. Kalshi's `/markets` response gives no order-book depth (unlike Polymarket's
   `/book`), so `bid_depth_usd`/`ask_depth_usd` are always None for Kalshi
   snapshot rows. Nothing in the current model set reads book depth for
   non-Polymarket venues; if a future model needs it, it would come from a
   separate orderbook endpoint, not from here.
4. Kalshi has no UMA-style dispute window. A market is treated as finally
   resolved exactly when `status == 'finalized'` and `result` is 'yes' or
   'no' (empty string means "not yet settled" per the live-verified shape);
   `disputed` is always recorded False for Kalshi rows.
"""

from __future__ import annotations

import logging
from typing import Any

from lab.api.kalshi import KalshiClient, KalshiMarket
from lab.collect.categories import load_categories
from lab.store import db
from lab.store.snapshots import SnapshotStore, floor_ts_bucket
from lab.util import now_utc, now_utc_iso

log = logging.getLogger(__name__)

_OPEN_STATUSES = ("active", "initialized")
_CLOSED_STATUSES = ("finalized", "closed")


def assign_kalshi_tier(m: KalshiMarket, config: dict[str, Any]) -> str:
    tiers = config["venues"]["kalshi"]["tiers"]
    liq = m.liquidity_dollars or 0.0
    vol = m.volume_fp or 0.0
    if liq >= tiers["liquid"]["min_liquidity"] and vol >= tiers["liquid"]["min_volume"]:
        return "liquid"
    if liq >= tiers["tail"]["min_liquidity"] and vol >= tiers["tail"]["min_volume"]:
        return "tail"
    return "ignored"


def kalshi_market_row(m: KalshiMarket, category: str) -> dict[str, Any]:
    return {
        "condition_id": db.venue_condition_id("kalshi", m.ticker),
        "venue": "kalshi",
        "venue_native_id": m.ticker,
        "slug": None,
        "question": m.title,
        "category": category,
        "description": m.rules_primary,
        "end_date_iso": m.close_time or m.expiration_time,
        "token_id_yes": None,
        "token_id_no": None,
        "neg_risk": 0,
        "active": int(m.status in _OPEN_STATUSES),
        "closed": int(m.status in _CLOSED_STATUSES),
        "liquidity_num": m.liquidity_dollars,
        "volume_num": m.volume_fp,
    }


async def sync_kalshi_universe(
    kalshi: KalshiClient, conn, config: dict[str, Any]
) -> dict[str, int]:
    """Fetch open markets per configured category (deterministic, bounded fan-out)
    and upsert (idempotent). Returns a summary counts dict, mirroring
    sync_universe()'s shape/logging in collect/universe.py."""
    kalshi_cfg = config["venues"]["kalshi"]
    category_map: dict[str, str] = load_categories()["kalshi_series"]
    excluded = set(kalshi_cfg.get("excluded_series_categories", []))
    max_series = kalshi_cfg.get("max_series_per_sync", 40)

    counts = {"series": 0, "markets_seen": 0, "liquid": 0, "tail": 0, "ignored": 0, "skipped_category": 0}
    series_processed = 0

    for kalshi_category, our_category in category_map.items():
        if kalshi_category in excluded:
            counts["skipped_category"] += 1
            continue
        if series_processed >= max_series:
            break
        try:
            series_list = await kalshi.series_by_category(kalshi_category)
        except Exception:
            log.warning("kalshi universe: series fetch failed",
                        extra={"ctx": {"category": kalshi_category}})
            continue
        for s in series_list:
            if series_processed >= max_series:
                break
            ticker = s.get("ticker")
            if not ticker:
                continue
            series_processed += 1
            counts["series"] += 1
            try:
                markets = await kalshi.markets_for_series(ticker, status="open")
            except Exception:
                log.warning("kalshi universe: markets fetch failed",
                            extra={"ctx": {"series_ticker": ticker}})
                continue
            for m in markets:
                counts["markets_seen"] += 1
                tier = assign_kalshi_tier(m, config)
                counts[tier] += 1
                db.upsert_market(conn, {**kalshi_market_row(m, our_category), "tier": tier})
            conn.commit()

    log.info("kalshi universe sync complete", extra={"ctx": counts})
    return counts


def tracked_kalshi_markets(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT condition_id, venue_native_id FROM markets "
        "WHERE venue = 'kalshi' AND active = 1 AND closed = 0"
    ).fetchall()
    return [dict(r) for r in rows]


def tracked_kalshi_markets_by_ids(conn, condition_ids: list[str]) -> list[dict]:
    """Phase 17 item 3: an explicit, small set of Kalshi markets (confirmed
    cross-venue pairs) rather than every open Kalshi market."""
    if not condition_ids:
        return []
    placeholders = ",".join("?" * len(condition_ids))
    rows = conn.execute(
        f"SELECT condition_id, venue_native_id FROM markets "
        f"WHERE venue = 'kalshi' AND condition_id IN ({placeholders})",
        tuple(condition_ids),
    ).fetchall()
    return [dict(r) for r in rows]


async def snapshot_kalshi_markets(kalshi: KalshiClient, store: SnapshotStore,
                                 markets: list[dict], ts_bucket: str) -> int:
    """Snapshot an explicit set of Kalshi markets. Shared by snapshot_kalshi
    (every open Kalshi market) and Phase 17 item 3's per-confirmed-pair
    high-frequency job (a small, explicit condition_id list)."""
    rows: list[dict] = []
    for row in markets:
        ticker = row["venue_native_id"]
        try:
            m = await kalshi.market(ticker)
        except Exception:
            log.warning("kalshi snapshot: market fetch failed",
                        extra={"ctx": {"condition_id": row["condition_id"]}})
            continue
        if m is None or m.yes_price is None:
            continue
        spread = None
        if m.yes_bid_dollars is not None and m.yes_ask_dollars is not None:
            spread = m.yes_ask_dollars - m.yes_bid_dollars
        rows.append({
            "ts": ts_bucket,
            "condition_id": row["condition_id"],
            "token_id_yes": None,
            "best_bid": m.yes_bid_dollars,
            "best_ask": m.yes_ask_dollars,
            "mid": m.yes_price,
            "spread": spread,
            "bid_depth_usd": None,
            "ask_depth_usd": None,
            "last_trade_price": m.last_price_dollars,
            "bids_json": None,
            "asks_json": None,
            "venue": "kalshi",
        })
    return store.append(rows)


async def snapshot_kalshi(kalshi: KalshiClient, conn, store: SnapshotStore, config: dict[str, Any]) -> int:
    """Single-tier snapshot round for Kalshi markets. Returns rows written (post-dedup)."""
    markets = tracked_kalshi_markets(conn)
    if not markets:
        log.info("kalshi snapshot round: no markets", extra={"ctx": {}})
        return 0
    bucket_minutes = config["venues"]["kalshi"]["snapshot_interval_minutes"]
    ts_bucket = floor_ts_bucket(now_utc(), bucket_minutes)

    written = await snapshot_kalshi_markets(kalshi, store, markets, ts_bucket)
    log.info("kalshi snapshot round done",
             extra={"ctx": {"markets": len(markets), "written": written}})
    return written


def unresolved_kalshi_markets(conn, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """
        SELECT m.condition_id, m.venue_native_id FROM markets m
        LEFT JOIN resolutions r ON r.condition_id = m.condition_id
        WHERE r.condition_id IS NULL AND m.venue = 'kalshi'
          AND (m.closed = 1 OR (m.end_date_iso IS NOT NULL AND m.end_date_iso < ?))
        LIMIT ?
        """,
        (now_utc_iso(), limit),
    ).fetchall()
    return [dict(r) for r in rows]


def extract_kalshi_payout(m: KalshiMarket) -> float | None:
    """Return payout_yes if the market has a final Kalshi settlement, else None."""
    if m.status != "finalized":
        return None
    if m.result == "yes":
        return 1.0
    if m.result == "no":
        return 0.0
    return None


async def watch_kalshi_resolutions(kalshi: KalshiClient, conn, limit: int = 200) -> int:
    """One poll round over unresolved Kalshi markets already in our DB. Returns
    number of resolutions recorded. Mirrors collect/resolutions.py's pattern."""
    recorded = 0
    for row in unresolved_kalshi_markets(conn, limit=limit):
        condition_id, ticker = row["condition_id"], row["venue_native_id"]
        try:
            m = await kalshi.market(ticker)
        except Exception:
            log.warning("kalshi resolutions: fetch failed",
                        extra={"ctx": {"condition_id": condition_id}})
            continue
        if m is None:
            conn.commit()
            continue
        payout_yes = extract_kalshi_payout(m)
        if payout_yes is None:
            conn.commit()
            continue
        db.record_resolution(
            conn, condition_id,
            resolved_ts=now_utc_iso(),
            payout_yes=payout_yes,
            disputed=False,
            source="kalshi",
        )
        recorded += 1
        # Commit per-candidate: avoids holding one long write transaction open
        # across a large backlog scan (same rationale as resolutions.py).
        conn.commit()
    if recorded:
        log.info("kalshi resolutions recorded", extra={"ctx": {"count": recorded}})
    return recorded
