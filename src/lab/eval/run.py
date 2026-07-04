"""`lab eval`: score resolved forecasts per model and window, persist eval_runs.

Phase 11 (brief section 7/11): grouped per (model_id, window_label, venue,
category) -- each forecast is scored against its OWN venue's price, forecastable
venues read from the `venues` table. The cluster bootstrap resamples by
event_id (falling back to condition_id); the anytime-valid confidence sequence
and the precision-weighted stratified estimator are computed alongside the
classical bootstrap CI and persisted as secondary columns.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import numpy as np

from lab.eval.anytime import confidence_sequence
from lab.eval.calibration import calibration_bins
from lab.eval.scoring import brier, paired_skill
from lab.eval.stratified import precision_weighted_skill
from lab.store import db as dbmod
from lab.util import now_utc, now_utc_iso

log = logging.getLogger(__name__)

WINDOWS = {"all_time": None, "trailing_90d": 90}

# Sentinel category value for the "all categories pooled, this venue" row --
# distinct from a legacy pre-Phase-11 eval_runs row's NULL category.
ALL_CATEGORIES = "ALL"


def challenger_registered_ts(conn, model_id: str) -> str | None:
    """Earliest registration timestamp for a versioned/challenger model_id.

    Forward-only rule (brief section 6/guardrail 15): a registered challenger
    earns skill only from forecasts made after its own registration -- it is
    never scored on history predating its existence. Legacy model_ids with no
    model_versions row are unaffected (returns None).
    """
    row = conn.execute(
        "SELECT MIN(registered_ts) AS ts FROM model_versions WHERE model_id = ?", (model_id,)
    ).fetchone()
    return row["ts"] if row is not None else None


def resolved_forecast_rows(
    conn, model_id: str, window_days: int | None,
    venue: str | None = None, category: str | None = None,
    null_control_ids: set[str] | None = None, invert_null_control: bool = False,
) -> list[dict]:
    """Paired rows: forecast + resolution outcome + venue/category/event_id
    for one model, optionally scoped to one venue and/or one category."""
    query = """
        SELECT f.condition_id, f.p_yes, f.p_market_at_ts, r.payout_yes, r.resolved_ts,
               m.venue AS venue, m.category AS category, m.event_id AS event_id
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id = ? AND r.disputed = 0
    """
    params: list[Any] = [model_id]
    if venue is not None:
        query += " AND m.venue = ?"
        params.append(venue)
    if category is not None:
        query += " AND m.category = ?"
        params.append(category)
    if window_days is not None:
        query += " AND f.ts >= ?"
        params.append((now_utc() - timedelta(days=window_days)).isoformat(timespec="seconds"))
    registered_ts = challenger_registered_ts(conn, model_id)
    if registered_ts is not None:
        query += " AND f.ts >= ?"
        params.append(registered_ts)
    rows = [dict(r) for r in conn.execute(query, params)]
    if null_control_ids is not None:
        if invert_null_control:
            rows = [r for r in rows if r["condition_id"] in null_control_ids]
        else:
            rows = [r for r in rows if r["condition_id"] not in null_control_ids]
    return rows


def _event_cluster_ids(rows: list[dict]) -> np.ndarray:
    return np.array([r["event_id"] or r["condition_id"] for r in rows])


def _per_cluster_diffs_in_resolution_order(
    diffs: np.ndarray, cluster_ids: np.ndarray, resolved_ts: list[str]
) -> np.ndarray:
    """One mean diff per event-cluster, ordered by each cluster's earliest
    resolution -- what the anytime-valid CS treats as its sequential sample
    (brief section 7: "n counts resolved event clusters, not venue-market
    rows")."""
    order = np.argsort(resolved_ts)
    first_seen: dict[str, int] = {}
    ordered_clusters: list[str] = []
    for idx in order:
        cid = cluster_ids[idx]
        if cid not in first_seen:
            first_seen[cid] = len(ordered_clusters)
            ordered_clusters.append(cid)
    buckets: list[list[float]] = [[] for _ in ordered_clusters]
    for cid, d in zip(cluster_ids, diffs):
        buckets[first_seen[cid]].append(d)
    return np.array([float(np.mean(b)) for b in buckets])


def evaluate_model(
    conn, model_id: str, window_label: str, rows: list[dict], config: dict[str, Any],
    venue: str | None = None, category: str | None = None,
) -> dict[str, Any] | None:
    if not rows:
        return None
    p_model = np.array([r["p_yes"] for r in rows])
    p_market = np.array([r["p_market_at_ts"] for r in rows])
    y = np.array([r["payout_yes"] for r in rows])
    cluster_ids = _event_cluster_ids(rows)

    result = paired_skill(
        p_model=p_model, p_market=p_market, y=y, condition_ids=cluster_ids,
        iterations=config["eval"]["bootstrap_iterations"],
    )
    bins = calibration_bins(p_model, y, n_bins=config["eval"]["calibration_bins"])

    diffs = brier(p_market, y) - brier(p_model, y)
    resolved_ts = [r["resolved_ts"] for r in rows]
    per_cluster_diffs = _per_cluster_diffs_in_resolution_order(diffs, cluster_ids, resolved_ts)
    cs = confidence_sequence(
        per_cluster_diffs, alpha=config["eval"]["confidence_sequence"]["alpha"]
    )

    stratified = precision_weighted_skill(
        diffs, p_market, cluster_ids, iterations=config["eval"]["bootstrap_iterations"]
    )

    conn.execute(
        """
        INSERT INTO eval_runs (ts, model_id, window_label, n, brier, brier_market,
                               skill, skill_ci_lo, skill_ci_hi, log_loss,
                               log_loss_market, calibration_json,
                               venue, category, skill_pw, skill_pw_ci_lo, skill_pw_ci_hi,
                               n_strata_pw, cs_lo, cs_hi, cs_covers_zero, n_event_clusters)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (now_utc_iso(), model_id, window_label, result.n, result.brier_model,
         result.brier_market, result.skill, result.skill_ci_lo, result.skill_ci_hi,
         result.log_loss_model, result.log_loss_market, json.dumps(bins),
         venue, category, stratified.skill_pw, stratified.ci_lo, stratified.ci_hi,
         stratified.n_strata, cs.lo, cs.hi, int(cs.covers_zero), result.n_markets),
    )
    return {
        "model_id": model_id, "window": window_label, "venue": venue, "category": category,
        "result": result, "bins": bins, "cs": cs, "stratified": stratified,
    }


def run_eval(conn, config: dict[str, Any]) -> list[dict[str, Any]]:
    from lab.forecast import null_control_ids_by_venue

    nc_ids_by_venue = null_control_ids_by_venue(conn, config)
    model_ids = [r["model_id"] for r in conn.execute(
        "SELECT DISTINCT model_id FROM forecasts ORDER BY model_id"
    )]
    venue_categories: dict[str, list[str]] = {}
    for r in conn.execute(
        """
        SELECT DISTINCT venue, category FROM markets
        WHERE venue IN (SELECT venue FROM venues WHERE forecastable = 1)
        ORDER BY venue, category
        """
    ):
        venue_categories.setdefault(r["venue"], []).append(r["category"])

    out: list[dict[str, Any]] = []
    for model_id in model_ids:
        for venue, categories in venue_categories.items():
            nc_ids = nc_ids_by_venue.get(venue)
            for category in categories:
                for label, days in WINDOWS.items():
                    rows = resolved_forecast_rows(
                        conn, model_id, days, venue=venue, category=category,
                        null_control_ids=nc_ids,
                    )
                    summary = evaluate_model(
                        conn, model_id, label, rows, config, venue=venue, category=category
                    )
                    if summary:
                        out.append(summary)
            # "ALL categories" aggregate row per venue -- per-category n stays
            # sparse for months (brief section 11 timelines), this keeps a
            # non-sparse view available from day one.
            for label, days in WINDOWS.items():
                rows = resolved_forecast_rows(
                    conn, model_id, days, venue=venue, null_control_ids=nc_ids,
                )
                summary = evaluate_model(
                    conn, model_id, label, rows, config, venue=venue, category=ALL_CATEGORIES
                )
                if summary:
                    out.append(summary)
            # Null control scored separately, same math, shown side by side --
            # one venue-scoped sample per forecastable venue.
            nc_rows = resolved_forecast_rows(
                conn, model_id, None, venue=venue, null_control_ids=nc_ids,
                invert_null_control=True,
            )
            nc_summary = evaluate_model(
                conn, model_id, "null_control", nc_rows, config,
                venue=venue, category=ALL_CATEGORIES,
            )
            if nc_summary:
                out.append(nc_summary)
    conn.commit()
    log.info("eval complete", extra={"ctx": {"summaries": len(out)}})
    return out
