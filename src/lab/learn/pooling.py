"""Extremized, correlation-aware pooling (Phase 13, CLAUDE.md M4/M7 extremization).

A plain log-odds pool (M4's weighted average, M7's `pool_log_odds`) is known to
be systematically underconfident when its sources carry distinct information
(Satopää et al.; Neyman-Roughgarden) -- extremizing the pooled logit corrects
for this, but only to the degree the sources are actually independent. M4's
members mostly derive from the same underlying Polymarket price; M7's external
venues price the same real-world event. Extremizing as if `n` members were
independent would overstate confidence, so the fitted exponent `a` is
discounted by the correlation-adjusted effective source count `n_eff` before
it's ever applied.
"""

from __future__ import annotations

import itertools
import logging
from datetime import timedelta
from typing import Any

import numpy as np

from lab.learn.refit import assert_walk_forward, logit, sigmoid

log = logging.getLogger(__name__)

EXTREMIZATION_GRID: tuple[float, ...] = (1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5)


def effective_source_count(n: int, rho_bar: float) -> float:
    """n_eff = n / (1 + (n-1)*rho_bar) -- the standard poll-aggregation
    effective-N discount for correlated sources.

    rho_bar is clamped to [0, 1]: a negative correlation would push n_eff
    above n, which extremization has no use for here (the a<=2.5 cap already
    bounds the upside) -- this keeps n_eff's role as "fraction of full
    independence" clean. n_eff itself is clamped to [1, n].
    """
    if n <= 1:
        return float(max(n, 0))
    rho_bar = float(np.clip(rho_bar, 0.0, 1.0))
    n_eff = n / (1 + (n - 1) * rho_bar)
    return float(np.clip(n_eff, 1.0, n))


def discount_extremization_exponent(a_raw: float, n: int, rho_bar: float) -> float:
    """a_eff = 1 + (a_raw - 1) * (n_eff - 1)/(n - 1) for n > 1, else 1.0.

    a_raw=1.0 -> a_eff=1.0 always, regardless of n/rho_bar (acceptance
    criterion: "a=1.0 reproduces current pooling exactly"). rho_bar->1 (e.g. a
    duplicated source) drives n_eff->1, which drives a_eff->1.0 regardless of
    a_raw (acceptance criterion: duplication suppresses extremization).
    """
    if n <= 1:
        return 1.0
    n_eff = effective_source_count(n, rho_bar)
    frac = (n_eff - 1) / (n - 1)
    return float(1.0 + (a_raw - 1.0) * frac)


def extremize_logit(pooled_logit: float, a_eff: float) -> float:
    """a_eff * pooled_logit -- applied AFTER pooling (CLAUDE.md: "applied to
    the pooled logit"), so a_eff=1.0 is a bit-exact identity transform."""
    return float(a_eff * pooled_logit)


def fit_extremization_exponent(
    train: list[dict], validation: list[dict], grid: tuple[float, ...] = EXTREMIZATION_GRID
) -> dict[str, Any]:
    """Walk-forward grid search over the raw extremization exponent `a`
    (same shape as learn/loop.py's M3_GRID search): for each candidate a,
    replay Brier of sigmoid(a * logit(raw_pooled_p)) against train, pick the
    best, report its validation Brier too.

    train/validation: [{"p_pooled": raw pooled probability (pre-extremization,
    i.e. today's plain pool), "outcome": ...}, ...].
    """
    assert_walk_forward(train, validation)

    def _brier(rows: list[dict], a: float) -> float:
        p_pool = np.array([r["p_pooled"] for r in rows], dtype=float)
        y = np.array([r["outcome"] for r in rows], dtype=float)
        p_ext = sigmoid(a * logit(p_pool))
        return float(np.mean((p_ext - y) ** 2))

    best_a, best_score = 1.0, float("inf")
    for a in grid:
        score = _brier(train, a)
        if score < best_score:
            best_a, best_score = a, score

    return {
        "a": float(best_a),
        "train_brier": best_score,
        "validation_brier": _brier(validation, best_a),
        "n_train": len(train),
        "n_validation": len(validation),
    }


def estimate_rho_bar_m4(conn, config: dict[str, Any], model_ids: tuple[str, ...],
                        min_pairs_per_category: int = 30) -> dict[str, float]:
    """Per category: mean pairwise Pearson correlation of logit(p_yes) across
    `model_ids`, matched on same-day (condition_id, ts) forecasts. Categories
    with too few overlapping-pair observations are omitted entirely -- the
    caller treats a missing category as rho_bar=0.0 (no discount), the
    conservative default until enough data exists to estimate it."""
    rows = conn.execute(
        """
        SELECT f.condition_id, f.ts, f.model_id, f.p_yes, m.category AS category
        FROM forecasts f JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id IN ({})
        """.format(",".join("?" for _ in model_ids)),
        model_ids,
    ).fetchall()

    by_category: dict[str, dict[tuple[str, str], dict[str, float]]] = {}
    for r in rows:
        cat = r["category"] or "unknown"
        key = (r["condition_id"], r["ts"])
        by_category.setdefault(cat, {}).setdefault(key, {})[r["model_id"]] = float(logit(r["p_yes"]))

    out: dict[str, float] = {}
    for cat, points in by_category.items():
        pair_logits: dict[tuple[str, str], list[tuple[float, float]]] = {}
        for logits_by_model in points.values():
            present = sorted(logits_by_model)
            for a, b in itertools.combinations(present, 2):
                pair_logits.setdefault((a, b), []).append((logits_by_model[a], logits_by_model[b]))

        correlations = []
        for pair, values in pair_logits.items():
            if len(values) < min_pairs_per_category:
                continue
            xs = np.array([v[0] for v in values])
            ys = np.array([v[1] for v in values])
            if np.std(xs) == 0 or np.std(ys) == 0:
                continue
            correlations.append(float(np.corrcoef(xs, ys)[0, 1]))
        if correlations:
            out[cat] = float(np.mean(correlations))
    return out


def estimate_rho_bar_m7(conn, store, config: dict[str, Any],
                        markets_map_path=None, min_days: int = 10,
                        lookback_days: int = 90) -> float | None:
    """Single overall value (not per-category -- confirmed-pair count is
    small, Phase 9's own acceptance bar was only >=5 pairs): for each
    confirmed pair, pull each side's own snapshot mid history over the
    trailing `lookback_days` (a monthly-batch job, not latency sensitive, but
    still bounded rather than scanning the entire snapshot archive), resample
    to one observation per UTC date (last value that day), align by date,
    correlate logit(mid). Averaged across pairs with >= min_days overlapping
    days. Returns None if no pair has enough overlap yet (caller treats this
    as rho_bar=0.0, the conservative no-discount default)."""
    from lab.models.m7_crossvenue import confirmed_by_condition, load_markets_map
    from lab.store.snapshots import utc_date_str
    from lab.util import now_utc

    data = load_markets_map(markets_map_path)
    by_cid = confirmed_by_condition(data)
    if not by_cid:
        return None

    now = now_utc()
    dates = [utc_date_str(now - timedelta(days=d)) for d in range(lookback_days)]
    df = store.read_range(dates)
    if df.is_empty():
        return None

    from lab.store import db as dbmod

    correlations = []
    for cid, pairs in by_cid.items():
        external_cids = [dbmod.venue_condition_id(p["venue"], p["external_id"]) for p in pairs]
        all_cids = [cid, *external_cids]
        subset = df.filter(df["condition_id"].is_in(all_cids))
        if subset.is_empty():
            continue
        daily = (
            subset.with_columns(subset["ts"].str.slice(0, 10).alias("_date"))
            .sort("ts")
            .group_by(["condition_id", "_date"])
            .last()
        )
        by_cid_date: dict[str, dict[str, float]] = {}
        for row in daily.to_dicts():
            if row["mid"] is None:
                continue
            by_cid_date.setdefault(row["condition_id"], {})[row["_date"]] = float(row["mid"])

        for a, b in itertools.combinations(all_cids, 2):
            a_series, b_series = by_cid_date.get(a, {}), by_cid_date.get(b, {})
            shared_dates = sorted(set(a_series) & set(b_series))
            if len(shared_dates) < min_days:
                continue
            xs = logit(np.array([a_series[d] for d in shared_dates]))
            ys = logit(np.array([b_series[d] for d in shared_dates]))
            if np.std(xs) == 0 or np.std(ys) == 0:
                continue
            correlations.append(float(np.corrcoef(xs, ys)[0, 1]))

    return float(np.mean(correlations)) if correlations else None
