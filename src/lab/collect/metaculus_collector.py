"""Metaculus collection (Phase 10, brief section 3/6, v1.9): community-prediction
snapshots + resolution watcher for CONFIRMED cross-venue pairs only.

Scope assumption (brief Phase 10 task text is explicit about this, not a gap):
unlike Kalshi, Metaculus gets no broad universe sync here. There is no
"discover Metaculus questions" step -- this module only ever touches questions
that already appear in `data/markets_map.yaml`'s `confirmed` list with
venue == 'metaculus' (the same propose-then-confirm gate M7 uses, reused
rather than re-built: `lab.models.m7_crossvenue.load_markets_map` /
`confirmed_by_condition`). A confirmed pair's `condition_id` is expected to
already exist as a `markets` row (minted at match-confirmation time by
Phase 10's event step, wired up elsewhere); if it doesn't yet, we skip that
pair and log it rather than upserting a placeholder ourselves -- guardrail 9
fail-soft, and no FK is enforced at the SQLite level for this column anyway.

Resolution-field assumption (guardrail 1 -- stated, not guessed): the shared
`MetaculusClient.question()` currently extracts only `community_prediction`
from `GET /api/posts/{id}/`; it does not parse resolution status. Live
verification of the exact resolution JSON shape was not available in this
session's environment/token tier (see api/metaculus.py's module docstring:
even community_prediction itself came back null for every question tested
with a basic account token). Rather than guess a schema and risk silently
mis-reading it, `_extract_resolution` below reads the raw response
defensively: it looks for `question.resolution` (the field name used by
Metaculus's real-time and public post-resolution endpoints per their own
forecasting-tools client) and, as a fallback, a top-level `resolved`/
`actual_resolve_time` pair. Any shape not matching returns None (still open /
unrecognized) rather than raising or guessing a payout. This mirrors the
Gamma resolution watcher's finality-over-speed stance: better to under-detect
than to record a wrong payout. It is expected and acceptable to record zero
resolutions right now, since zero confirmed Metaculus pairs exist yet.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from lab.models.m7_crossvenue import confirmed_by_condition, load_markets_map
from lab.store import db
from lab.store.snapshots import SnapshotStore, floor_ts_bucket
from lab.util import now_utc, now_utc_iso

log = logging.getLogger(__name__)


def _metaculus_pairs(markets_map_path: Path | None) -> list[tuple[str, str]]:
    """(condition_id, question_id) for every confirmed Metaculus pair. Empty
    list is the expected, correct result while no pairs have been confirmed."""
    data = load_markets_map(markets_map_path)
    by_cid = confirmed_by_condition(data)
    return [
        (cid, entry["external_id"])
        for cid, entries in by_cid.items()
        for entry in entries
        if entry["venue"] == "metaculus"
    ]


def _known_condition_ids(conn, condition_ids: list[str]) -> set[str]:
    if not condition_ids:
        return set()
    rows = conn.execute(
        "SELECT condition_id FROM markets WHERE condition_id IN ({})".format(
            ",".join("?" for _ in condition_ids)
        ),
        condition_ids,
    ).fetchall()
    return {r["condition_id"] for r in rows}


async def snapshot_metaculus(
    metaculus_client, conn, store: SnapshotStore, config: dict[str, Any],
    markets_map_path: Path | None = None,
) -> int:
    """One snapshot round: one row per confirmed Metaculus pair. `mid` is the
    community prediction, or explicitly None when Metaculus hides/omits it --
    that NULL is the correct, expected value (abstain, don't guess), not an
    error path. Returns rows written (post-dedup, per SnapshotStore.append)."""
    pairs = _metaculus_pairs(markets_map_path)
    if not pairs:
        log.info("metaculus snapshot: no confirmed pairs", extra={"ctx": {}})
        return 0

    known = _known_condition_ids(conn, [cid for cid, _ in pairs])
    bucket_minutes = config["venues"]["metaculus"]["snapshot_interval_minutes"]
    ts_bucket = floor_ts_bucket(now_utc(), bucket_minutes)

    rows: list[dict] = []
    for condition_id, question_id in pairs:
        if condition_id not in known:
            log.info("metaculus snapshot: condition_id not in markets yet, skipping",
                     extra={"ctx": {"condition_id": condition_id, "question_id": question_id}})
            continue
        try:
            q = await metaculus_client.question(int(question_id))
        except Exception:
            log.warning("metaculus snapshot: question fetch failed",
                       extra={"ctx": {"condition_id": condition_id, "question_id": question_id}})
            continue
        rows.append({
            "ts": ts_bucket,
            "condition_id": condition_id,
            "token_id_yes": None,
            "best_bid": None,
            "best_ask": None,
            "mid": q.community_prediction if q else None,
            "spread": None,
            "bid_depth_usd": None,
            "ask_depth_usd": None,
            "last_trade_price": None,
            "bids_json": None,
            "asks_json": None,
            "venue": "metaculus",
        })
    written = store.append(rows)
    log.info("metaculus snapshot round done",
             extra={"ctx": {"pairs": len(pairs), "written": written}})
    return written


def _extract_resolution(raw: dict[str, Any]) -> tuple[float, bool] | None:
    """Best-effort, defensive extraction of a final payout from the raw
    `GET /api/posts/{id}/` response. Returns None (treat as still open /
    unrecognized shape) rather than guessing -- see module docstring."""
    if not isinstance(raw, dict):
        return None
    q = raw.get("question")
    if isinstance(q, dict):
        resolution = q.get("resolution")
        if resolution is not None:
            # Binary questions resolve to "yes"/"no" (observed shape in
            # Metaculus's own forecasting-tools client) or a bare 0.0/1.0.
            if isinstance(resolution, str):
                lowered = resolution.strip().lower()
                if lowered == "yes":
                    return 1.0, False
                if lowered == "no":
                    return 0.0, False
                return None  # e.g. "ambiguous"/"annulled" -- not a binary payout
            try:
                payout = float(resolution)
            except (TypeError, ValueError):
                return None
            if payout in (0.0, 1.0):
                return payout, False
            return None
    # Fallback: a top-level resolved flag with an explicit payout, in case a
    # future/alternate response shape surfaces it outside `question`.
    if raw.get("resolved") and raw.get("actual_resolve_time"):
        payout = raw.get("resolution") or raw.get("payout")
        try:
            payout = float(payout)
        except (TypeError, ValueError):
            return None
        if payout in (0.0, 1.0):
            return payout, False
    return None


def unresolved_metaculus_pairs(conn, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Scoped to the confirmed-pairs list itself (not a venue-tagged markets
    query) -- per assumption note in the module docstring, Metaculus markets
    rows may not carry meaningful active/closed flags the way Kalshi/Polymarket
    do, so 'unresolved' here means 'in the confirmed list, no resolutions row'."""
    if not pairs:
        return []
    condition_ids = [cid for cid, _ in pairs]
    rows = conn.execute(
        "SELECT condition_id FROM resolutions WHERE condition_id IN ({})".format(
            ",".join("?" for _ in condition_ids)
        ),
        condition_ids,
    ).fetchall()
    already_resolved = {r["condition_id"] for r in rows}
    return [(cid, qid) for cid, qid in pairs if cid not in already_resolved]


async def _fetch_raw_post(metaculus_client, question_id: int) -> dict[str, Any] | None:
    """Fetch the raw `GET /api/posts/{id}/` JSON directly.

    `MetaculusClient.question()` (api/metaculus.py, not ours to modify) parses
    this same endpoint down to `MetaculusQuestion` and discards the raw dict,
    so resolution status -- which it was never built to extract -- isn't
    reachable through it. We reuse the client's own auth header + rate-limited
    `get_json` (inherited from BaseClient) rather than re-implementing HTTP
    plumbing, mirroring exactly what `.question()` does internally.
    """
    token = getattr(metaculus_client, "_token", None)
    if not token:
        log.warning("metaculus resolutions: no METACULUS_API_KEY -- abstaining",
                   extra={"ctx": {"question_id": question_id}})
        return None
    try:
        metaculus_client._client.headers["Authorization"] = f"Token {token}"
        metaculus_client._client.headers["Accept-Language"] = "en"
        raw = await metaculus_client.get_json(f"/posts/{question_id}/")
    except Exception:
        log.warning("metaculus resolutions: fetch failed", extra={"ctx": {"question_id": question_id}})
        return None
    return raw if isinstance(raw, dict) else None


async def watch_metaculus_resolutions(
    metaculus_client, conn, markets_map_path: Path | None = None,
) -> int:
    """One poll round over confirmed Metaculus pairs with no resolutions row
    yet. Returns number of resolutions recorded (0 is expected and correct
    while no confirmed pairs, or none has resolved, exist)."""
    pairs = _metaculus_pairs(markets_map_path)
    pending = unresolved_metaculus_pairs(conn, pairs)
    recorded = 0
    for condition_id, question_id in pending:
        raw = await _fetch_raw_post(metaculus_client, int(question_id))
        final = _extract_resolution(raw) if raw is not None else None
        if final is None:
            continue
        payout_yes, disputed = final
        db.record_resolution(
            conn, condition_id,
            resolved_ts=now_utc_iso(),
            payout_yes=payout_yes,
            disputed=disputed,
            source="metaculus",
        )
        recorded += 1
        # Commit per-candidate, not once at the end (resolutions.py's own
        # documented lesson): avoids holding a write transaction open across
        # a long backlog scan and locking out other connections meanwhile.
        conn.commit()
    if recorded:
        log.info("metaculus resolutions recorded", extra={"ctx": {"count": recorded}})
    return recorded
