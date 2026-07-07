"""Phase 16 (v2.4): event-distribution assembly for RPS scoring.

Many Kalshi/Polymarket "markets" are one numeric question split into
mutually exclusive buckets (CPI ranges, temperature bands). Scoring each
bucket as an isolated binary discards the cross-bucket structure; this
module assembles the per-model implied distribution over an event's buckets
so `eval/scoring.py::rps` can score the whole shape at once.

Scope (guardrail 1 -- stated, not silently picked): same-venue negRisk
groups only, linked at sync time by `collect/universe.py::_link_negrisk_legs`
into a shared `markets.event_id` (persisted, unlike Gamma's own live-only
event grouping -- this module reads what was already linked, it never
re-derives grouping from Gamma). Cross-venue bucket-to-bucket matching is
out of scope for this pass. RPS requires ORDERED buckets, which this module
determines by extracting the first numeric value from each leg's question
text; a leg that doesn't yield a parseable number takes the whole event out
of RPS scoring (logged, not raised) -- M6's coherence check is unaffected.

One RPS observation per event: only the model's LATEST forecast on each leg
is used (not every historical forecast on that leg), since aligning
multiple daily forecasts across legs at arbitrary, possibly-differing
timestamps has no single obviously-correct answer and CLAUDE.md doesn't
specify one; "latest known distributional view per event" is the simplest
defensible choice.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

_NUMBER_RE = re.compile(r"-?\$?\d[\d,]*\.?\d*")


def parse_bucket_order(question: str | None) -> float | None:
    """First numeric value in a question's text (handles $, %, commas), or
    None if nothing parses -- e.g. "Will CPI be between 3.0% and 3.5%?" -> 3.0.
    """
    if not question:
        return None
    match = _NUMBER_RE.search(question)
    if not match:
        return None
    raw = match.group().lstrip("$").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def implied_cdf(legs_p_yes: list[float]) -> np.ndarray:
    """Renormalize a leg-probability vector (already bucket-ordered) into a
    proper PMF summing to 1 -- the model's implied distribution over
    mutually exclusive buckets. Falls back to a uniform PMF if the legs sum
    to zero or less (degenerate input, never a real forecast in practice).
    """
    p = np.asarray(legs_p_yes, dtype=float)
    total = p.sum()
    if total <= 0:
        return np.full(len(p), 1.0 / len(p))
    return p / total


def coherence_deviation(legs_p_yes: list[float]) -> float:
    """abs(sum(p) - 1) -- the M6 coherence-deviation covariate the phase
    text calls for logging (not scoring) alongside the implied CDF. Same
    quantity `m6_consistency.scan_negrisk_event` already computes; exposed
    here directly so callers don't need to reconstruct legs into that
    function's own {condition_id, p_yes} dict shape just for this number.
    """
    return abs(float(np.asarray(legs_p_yes, dtype=float).sum()) - 1.0)


def bucketed_resolved_events(conn, model_id: str, category: str | None = None,
                             window_days: int | None = None) -> list[dict[str, Any]]:
    """Resolved, bucket-ordered events for one model: one row per event_id
    with >=2 resolved legs, exactly one resolving true, all legs' questions
    yielding a parseable bucket order. Malformed or unparseable groups are
    skipped and logged, never coerced. `category`/`window_days` mirror
    `eval/run.py::resolved_forecast_rows`'s own filters (window_days against
    `resolved_ts`, since RPS scores already-resolved distributional events --
    a stated, defensible choice where CLAUDE.md doesn't specify one).
    """
    rows = [dict(r) for r in conn.execute(
        """
        SELECT f.condition_id, f.p_yes, f.p_market_at_ts, f.ts, r.payout_yes, r.resolved_ts,
               m.event_id AS event_id, m.category AS category, m.question AS question
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id = ? AND r.disputed = 0 AND m.event_id IS NOT NULL
        ORDER BY f.ts ASC
        """,
        (model_id,),
    )]

    cutoff = None
    if window_days is not None:
        from lab.util import now_utc
        from datetime import timedelta
        cutoff = (now_utc() - timedelta(days=window_days)).isoformat(timespec="seconds")

    by_event: dict[str, dict[str, dict]] = {}
    for r in rows:
        # ORDER BY f.ts ASC above means the last write per condition_id here
        # is that leg's latest forecast -- exactly the dedup this module's
        # docstring states.
        by_event.setdefault(r["event_id"], {})[r["condition_id"]] = r

    events: list[dict[str, Any]] = []
    for event_id, legs_by_cid in by_event.items():
        legs = list(legs_by_cid.values())
        if len(legs) < 2:
            continue
        if category is not None and legs[0]["category"] != category:
            continue
        if cutoff is not None and legs[0]["resolved_ts"] < cutoff:
            continue
        true_legs = [l for l in legs if l["payout_yes"] == 1.0]
        if len(true_legs) != 1:
            log.warning("distributional: malformed bucketed event skipped (not exactly one "
                       "leg resolved true)", extra={"ctx": {"event_id": event_id,
                                                            "model_id": model_id,
                                                            "n_true": len(true_legs)}})
            continue
        orders = [parse_bucket_order(l["question"]) for l in legs]
        if any(o is None for o in orders):
            log.info("distributional: unparseable bucket order, event skipped",
                     extra={"ctx": {"event_id": event_id, "model_id": model_id}})
            continue
        order_idx = list(np.argsort(orders))
        legs_sorted = [legs[i] for i in order_idx]
        y_bucket_idx = next(i for i, l in enumerate(legs_sorted) if l["payout_yes"] == 1.0)
        events.append({
            "event_id": event_id,
            "category": legs_sorted[0]["category"],
            "p_model": [l["p_yes"] for l in legs_sorted],
            "p_market": [l["p_market_at_ts"] for l in legs_sorted],
            "y_bucket_idx": y_bucket_idx,
            "condition_ids": [l["condition_id"] for l in legs_sorted],
            "resolved_ts": legs_sorted[0]["resolved_ts"],
        })
    return events
