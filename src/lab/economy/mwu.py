"""Shadow MWU ensemble weighting (Phase 14.1, CLAUDE.md section 6/14.1,
guardrail 17 -- the one narrow, explicit exception to guardrail 14).

A new `m4_ensemble@mwu` challenger derives per-category M4 weights from
relative wealth (the wealth_ledger Phase 14 already maintains) via a
Hedge/multiplicative-weights update, cluster-aware floor/ceiling clamped so
correlated high-wealth models can't jointly dominate. It registers under the
SAME `model_versions.model_id = "m4_weights"` key the incumbent monthly fit
uses -- MWU never forecasts anything on its own, it only proposes an
alternative weight vector for the pooling logic M4Ensemble already runs, so
promoting it is exactly repointing which "m4_weights" version is active
(zero changes needed in M4Ensemble or the forecast job). It computes nightly
inside the existing `lab eval` step, gated by a 90-day/n>=200-per-category
probation before it is even eligible for the standard anytime-valid CI-gated
promotion, with its own nightly (not monthly) rollback check.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import numpy as np

from lab.learn.refit import logit, sigmoid
from lab.util import now_utc, now_utc_iso

log = logging.getLogger(__name__)


def mwu_learning_rate(n_models: int, t: int) -> float:
    """eta_t = sqrt(8 * ln(N) / t) -- the standard regret-bound-optimal
    schedule (Cesa-Bianchi & Lugosi Theorem 2.2), shrinking as t (resolved-
    forecast count) grows rather than a hand-tuned rate. t is clamped to
    >=1 and n_models to >=2 so the formula never divides by zero or takes
    ln(<=1)."""
    t = max(t, 1)
    n_models = max(n_models, 2)
    return float(np.sqrt(8 * np.log(n_models) / t))


def mwu_raw_weights(avg_log_wealth: dict[str, float], eta: float) -> dict[str, float]:
    """w_i propto exp(eta * avg_log_wealth_i), normalized -- wealth-based
    Hedge/MWU (brief section 6: "w_i propto exp(eta_t * cum_log_wealth_i)"),
    using Phase 14's own sleeping-expert-normalized avg_log_wealth
    (cum_log_wealth/n_forecasts) rather than the raw cumulative total (see
    the Phase 14.1 plan's assumption 1 for why). Numerically stabilized by
    subtracting the max before exponentiating -- doesn't change the
    normalized result.
    """
    models = sorted(avg_log_wealth)
    vals = np.array([avg_log_wealth[m] for m in models], dtype=float)
    vals = vals - vals.max()
    w = np.exp(eta * vals)
    w = w / w.sum()
    return dict(zip(models, w.tolist()))


def fit_mwu_weights(conn, config: dict[str, Any]) -> dict[str, Any]:
    """Per category: raw MWU weights from wealth_ledger -> cluster-aware
    floor/ceiling clamp (the same discount machinery Phase 13 introduced,
    generalized to per-pair -- see learn/pooling.py). Skips a category below
    the same m4_min_resolved_per_category threshold the incumbent fit uses,
    or with fewer than 2 POOLABLE members present.
    """
    from lab.eval.wealth_plots import sleeping_expert_rankings
    from lab.learn.pooling import (
        clamp_weights_with_cluster_ceiling,
        cluster_correlated_models,
        estimate_pairwise_rho_m4,
    )
    from lab.models.m4_ensemble import MIN_RESOLVED_PER_CATEGORY, POOLABLE

    learn_cfg = config.get("learn", {})
    floor = float(learn_cfg.get("m4_weight_floor", 0.02))
    ceiling = float(learn_cfg.get("m4_weight_ceiling", 0.60))
    threshold = float(learn_cfg.get("mwu", {}).get("correlation_cluster_threshold", 0.8))

    by_cat: dict[str, dict[str, dict]] = {}
    for r in sleeping_expert_rankings(conn):
        if r["model_id"] in POOLABLE:
            by_cat.setdefault(r["category"], {})[r["model_id"]] = r

    artifact: dict[str, Any] = {"kind": "m4_weights", "fitted_at": now_utc_iso(),
                                "source": "mwu", "categories": {}}
    for cat, members in by_cat.items():
        if len(members) < 2:
            continue
        total_n = sum(v["n_forecasts"] for v in members.values())
        if total_n < MIN_RESOLVED_PER_CATEGORY:
            continue

        avg_log_wealth = {m: v["avg_log_wealth"] for m, v in members.items()}
        t = max(v["n_forecasts"] for v in members.values())
        eta = mwu_learning_rate(len(members), t)
        raw = mwu_raw_weights(avg_log_wealth, eta)

        pairwise = estimate_pairwise_rho_m4(conn, config, POOLABLE, cat)
        clusters = cluster_correlated_models(pairwise, sorted(members), threshold=threshold)
        weights = clamp_weights_with_cluster_ceiling(raw, clusters, floor, ceiling)

        artifact["categories"][cat] = {"weights": weights, "n_resolved": total_n}
    return artifact


def _mwu_rollback_check(conn, config: dict[str, Any], *, apply: bool) -> dict[str, Any] | None:
    """Nightly (not monthly) rollback specific to 'm4_weights' -- mirrors
    learn.loop.run_rollback_checks' logic exactly, but is callable outside
    the monthly lab learn cycle (guardrail 17's explicit exception: this
    challenger may update between lab learn cycles, so its safety net has
    to run on the same nightly cadence, not wait for the next month)."""
    from lab.learn import registry
    from lab.learn.loop import _cs_alpha, _iterations, _load_artifact_file, _m4_weights_predict, m4_pool_rows
    from lab.learn.loop import passes_ci_gate

    active = registry.active_version(conn, "m4_weights")
    if not active or not active.get("promoted_ts"):
        return None
    prev = registry.previous_promotable(conn, "m4_weights", exclude_id=active["id"])
    if not prev:
        return None
    holdout = m4_pool_rows(conn)
    if len(holdout) < 2:
        return None
    active_art = _load_artifact_file(active["artifact_path"])
    prev_art = _load_artifact_file(prev["artifact_path"])
    if active_art is None or prev_art is None:
        return None

    cids = np.array([r.get("event_id") or r["condition_id"] for r in holdout])
    active_b = np.array([(_m4_weights_predict(active_art, r) - r["outcome"]) ** 2 for r in holdout])
    prev_b = np.array([(_m4_weights_predict(prev_art, r) - r["outcome"]) ** 2 for r in holdout])
    # champ=active, challenger=prev: positive skill => prev beats current => degraded.
    degraded, stats = passes_ci_gate(active_b, prev_b, cids, iterations=_iterations(config),
                                     min_n=2, alpha=_cs_alpha(config))
    entry: dict[str, Any] = {"model": "m4_weights", "degraded": degraded, "window": len(holdout), **stats}
    if degraded and apply:
        restored = registry.rollback(conn, config, "m4_weights", reason="rollback")
        entry["rolled_back_to"] = restored["version_tag"] if restored else None
    return entry


def update_mwu_challenger(conn, config: dict[str, Any], *, apply: bool = True) -> dict[str, Any]:
    """The guardrail-17 nightly cycle: rollback-check the current active
    'm4_weights' first (mirrors run_learn's own "rollback -> refit" order),
    then fit -> register as 'mwu-v{n}' -> probation gate (90d / n>=200 per
    category, measured from the FIRST mwu-tagged version's registered_ts,
    separate from the incumbent monthly fit's own version history of the
    same key) -> if cleared, CI-gate via the standard anytime-valid
    machinery against the current incumbent, replaying m4_pool_rows()
    through _m4_weights_predict -> promote on pass.
    """
    from lab.learn import registry
    from lab.learn.loop import _cs_alpha, _iterations, _m4_weights_predict, _promotion_min_n, m4_pool_rows
    from lab.learn.loop import passes_ci_gate
    from lab.learn.refit import load_active_artifact, save_artifact

    result: dict[str, Any] = {"rollback": _mwu_rollback_check(conn, config, apply=apply)}

    mwu_cfg = config.get("learn", {}).get("mwu", {})
    probation_days = int(mwu_cfg.get("probation_days", 90))
    probation_min_n = int(mwu_cfg.get("probation_min_n", 200))

    artifact = fit_mwu_weights(conn, config)
    if not artifact["categories"]:
        result["challenger"] = {"skipped": "insufficient_data"}
        return result

    history = registry.history(conn, "m4_weights")
    mwu_versions = [v for v in history if v["version_tag"].startswith("mwu-v")]
    version_tag = f"mwu-v{len(mwu_versions) + 1}"

    if mwu_versions:
        first_registered = min(v["registered_ts"] for v in mwu_versions)
        age_days = (now_utc() - datetime.fromisoformat(first_registered)).days
    else:
        age_days = 0  # this would be the very first mwu version -- certainly not clear of probation
    min_n_over_categories = min(spec["n_resolved"] for spec in artifact["categories"].values())
    probation_cleared = age_days >= probation_days and min_n_over_categories >= probation_min_n

    decision: dict[str, Any] = {"model": "m4_weights", "version_tag": version_tag, "promoted": False,
                               "probation_cleared": probation_cleared, "age_days": age_days,
                               "min_n_over_categories": min_n_over_categories}
    champ = load_active_artifact(config, "m4_weights")
    holdout = m4_pool_rows(conn)
    if not probation_cleared:
        decision["reason"] = "probation"
    elif champ is None:
        decision.update(promoted=True, reason="first")
    elif not holdout:
        decision["reason"] = "no_holdout"
    else:
        cids = np.array([r.get("event_id") or r["condition_id"] for r in holdout])
        champ_b = np.array([(_m4_weights_predict(champ, r) - r["outcome"]) ** 2 for r in holdout])
        chall_b = np.array([(_m4_weights_predict(artifact, r) - r["outcome"]) ** 2 for r in holdout])
        ok, stats = passes_ci_gate(champ_b, chall_b, cids, iterations=_iterations(config),
                                   min_n=_promotion_min_n(config), alpha=_cs_alpha(config))
        decision.update(promoted=ok, **stats)

    if apply:
        path = save_artifact(config, "m4_weights", artifact, promote=False)
        vid = registry.register_version(conn, config, "m4_weights", path, version_tag=version_tag)
        decision["version_id"] = vid
        if decision["promoted"]:
            registry.set_active(conn, config, "m4_weights", vid)

    result["challenger"] = decision
    return result
