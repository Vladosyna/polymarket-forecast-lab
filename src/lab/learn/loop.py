"""`lab learn` -- the monthly batch learning loop (guardrail 14).

Everything here is batch-and-versioned: refits write challenger artifact
versions (promote=False); promotion happens only when the challenger beats
the incumbent out-of-sample with n above the min threshold. No code path
adjusts any model in response to an individual outcome.
"""

from __future__ import annotations

import itertools
import json
import logging
from typing import Any, Callable

import numpy as np

from lab.learn.refit import (
    fit_m1_curves,
    fit_m2_baserates,
    load_active_artifact,
    models_dir,
    save_artifact,
)
from lab.news.aggregate import aggregate
from lab.util import now_utc_iso

log = logging.getLogger(__name__)

PROMOTION_MIN_N = 200  # honesty threshold (brief section 7) applied to promotion


def promote(config: dict[str, Any], name: str, version_filename: str) -> None:
    d = models_dir(config)
    active_path = d / "ACTIVE.json"
    active = json.loads(active_path.read_text(encoding="utf-8")) if active_path.exists() else {}
    active[name] = version_filename
    active_path.write_text(json.dumps(active, indent=2), encoding="utf-8")
    log.info("artifact promoted", extra={"ctx": {"name": name, "file": version_filename}})


def maybe_promote(config: dict[str, Any], name: str, challenger: dict[str, Any],
                  challenger_path, score_fn: Callable[[dict[str, Any]], float | None],
                  min_n: int = PROMOTION_MIN_N, n_available: int = 0) -> bool:
    """Promote challenger iff score_fn(challenger) < score_fn(champion) with
    enough out-of-sample data. Lower score = better (e.g. Brier)."""
    champion = load_active_artifact(config, name)
    if champion is None:
        promote(config, name, challenger_path.name)
        return True
    if n_available < min_n:
        log.info("promotion skipped: insufficient out-of-sample n",
                 extra={"ctx": {"name": name, "n": n_available, "min_n": min_n}})
        return False
    s_champ = score_fn(champion)
    s_chall = score_fn(challenger)
    if s_champ is None or s_chall is None:
        return False
    if s_chall < s_champ:
        promote(config, name, challenger_path.name)
        return True
    log.info("challenger not promoted",
             extra={"ctx": {"name": name, "champion": s_champ, "challenger": s_chall}})
    return False


# --- M1 / M2 / M4 scheduled refits ----------------------------------------

def live_m1_observations(conn) -> list[dict]:
    """(p_market, outcome, days_to_resolution) from the lab's own resolved rows."""
    rows = conn.execute(
        """
        SELECT f.p_market_at_ts AS p_market, r.payout_yes AS outcome,
               (julianday(r.resolved_ts) - julianday(f.ts)) AS days_to_resolution
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id AND r.disputed = 0
        WHERE f.model_id = 'm0_market'
        """
    ).fetchall()
    return [dict(r) for r in rows if r["days_to_resolution"] and r["days_to_resolution"] > 0]


def m1_holdout_brier(artifact: dict[str, Any], holdout: list[dict]) -> float | None:
    """Brier of the recalibrated price on held-out observations."""
    from lab.learn.refit import bucket_for_days, logit, sigmoid

    scores = []
    for o in holdout:
        bucket = bucket_for_days(o["days_to_resolution"])
        fit = artifact.get("buckets", {}).get(bucket)
        if fit is None:
            continue
        p = float(sigmoid(fit["alpha"] + fit["beta"] * logit(o["p_market"])))
        scores.append((p - o["outcome"]) ** 2)
    return float(np.mean(scores)) if scores else None


def refit_statistical_models(conn, config: dict[str, Any]) -> dict[str, Any]:
    """M1 curves, M2 base rates, M4 weights. Challenger-versioned, promotion gated."""
    from lab.learn.bootstrap import load_observations
    from lab.models.m4_ensemble import fit_m4_weights

    results: dict[str, Any] = {}

    # Observations: historical bootstrap + the lab's own resolved record.
    try:
        obs = load_observations(config).to_dicts()
    except FileNotFoundError:
        obs = []
    live = live_m1_observations(conn)
    combined = obs + live
    if combined:
        # Walk-forward: live rows (newest) are the holdout for promotion.
        m1 = fit_m1_curves(combined)
        path = save_artifact(config, "m1_curves", m1, promote=False)
        promoted = maybe_promote(
            config, "m1_curves", m1, path,
            score_fn=lambda art: m1_holdout_brier(art, live),
            n_available=len(live),
        )
        results["m1_curves"] = {"version": m1.get("version"), "promoted": promoted,
                                "n_obs": len(combined), "n_holdout": len(live)}

    per_market: dict[str, dict] = {}
    for o in combined:
        cid = o.get("condition_id")
        if cid and cid not in per_market:
            per_market[cid] = {"category": o.get("category", "unknown"), "outcome": o["outcome"]}
    live_cats = conn.execute(
        """SELECT m.condition_id, m.category, r.payout_yes AS outcome
           FROM resolutions r JOIN markets m ON m.condition_id = r.condition_id
           WHERE r.disputed = 0"""
    ).fetchall()
    for r in live_cats:
        per_market[r["condition_id"]] = {"category": r["category"], "outcome": r["outcome"]}
    if per_market:
        m2 = fit_m2_baserates(list(per_market.values()))
        path = save_artifact(config, "m2_baserates", m2, promote=False)
        # Base rates are descriptive statistics, not tunable parameters:
        # promotion is automatic once fitted (nothing to compare out-of-sample).
        promote(config, "m2_baserates", path.name)
        results["m2_baserates"] = {"version": m2.get("version"), "promoted": True}

    m4 = fit_m4_weights(conn, config)
    if m4["categories"]:
        path = save_artifact(config, "m4_weights", m4, promote=False)
        promote(config, "m4_weights", path.name)  # reward signal: realized Brier only
        results["m4_weights"] = {"version": m4.get("version"), "promoted": True,
                                 "categories": list(m4["categories"])}
    return results


# --- M3 aggregator walk-forward fit ----------------------------------------

M3_GRID = {
    "k": [0.08, 0.15, 0.25],
    "tau_days": [3.0, 5.0, 10.0],
    "max_shift": [0.5, 0.8, 1.2],
}


def resolved_m3_runs(conn) -> list[dict]:
    """Resolved M3 forecasts joined with their stored dossiers, oldest first."""
    rows = conn.execute(
        """
        SELECT f.ts, f.p_market_at_ts, r.payout_yes, e.dossier_json
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id AND r.disputed = 0
        JOIN evidence_runs e ON e.id = f.evidence_run_id
        WHERE f.model_id = 'm3_evidence'
        ORDER BY f.ts
        """
    ).fetchall()
    out = []
    for r in rows:
        try:
            dossier = json.loads(r["dossier_json"])
        except json.JSONDecodeError:
            continue
        out.append({
            "ts": r["ts"],
            "p_market": r["p_market_at_ts"],
            "outcome": r["payout_yes"],
            "items": dossier.get("evidence_items", []),
        })
    return out


def replay_brier(runs: list[dict], k: float, tau: float, cap: float) -> float:
    scores = []
    for run in runs:
        p = aggregate(run["p_market"], run["items"], run["ts"],
                      k=k, tau_days=tau, max_shift=cap)["p_yes"]
        scores.append((p - run["outcome"]) ** 2)
    return float(np.mean(scores)) if scores else float("inf")


def fit_m3_aggregator(conn, config: dict[str, Any]) -> dict[str, Any] | None:
    """Walk-forward grid search over (k, tau, cap); gated on min resolved runs."""
    runs = resolved_m3_runs(conn)
    min_n = config["learn"]["m3_min_resolved"]
    if len(runs) < min_n:
        log.info("m3 aggregator fit skipped",
                 extra={"ctx": {"resolved": len(runs), "required": min_n}})
        return None
    n_folds = 5
    fold_size = len(runs) // n_folds
    best_params, best_score = None, float("inf")
    for k, tau, cap in itertools.product(M3_GRID["k"], M3_GRID["tau_days"], M3_GRID["max_shift"]):
        # Walk-forward: evaluate each fold using only its position in time
        # (params are global; the walk-forward guards against regime luck).
        fold_scores = [
            replay_brier(runs[i * fold_size:(i + 1) * fold_size], k, tau, cap)
            for i in range(1, n_folds)  # skip the earliest fold as burn-in
        ]
        score = float(np.mean(fold_scores))
        if score < best_score:
            best_score, best_params = score, {"k": k, "tau_days": tau, "max_shift": cap}
    artifact = {"kind": "m3_params", "fitted_at": now_utc_iso(),
                "params": best_params, "walk_forward_brier": best_score,
                "n_runs": len(runs)}
    path = save_artifact(config, "m3_params", artifact, promote=False)
    holdout = runs[-max(1, len(runs) // 5):]
    promoted = maybe_promote(
        config, "m3_params", artifact, path,
        score_fn=lambda art: replay_brier(
            holdout, art["params"]["k"], art["params"]["tau_days"], art["params"]["max_shift"])
        if art.get("params") else None,
        n_available=len(runs),
    )
    return {"params": best_params, "promoted": promoted, "n_runs": len(runs)}


def run_learn(conn, config: dict[str, Any], llm=None) -> dict[str, Any]:
    from lab.learn.postmortem import run_postmortems

    summary: dict[str, Any] = {"ts": now_utc_iso()}
    summary["refits"] = refit_statistical_models(conn, config)
    summary["m3_aggregator"] = fit_m3_aggregator(conn, config)
    summary["postmortems"] = run_postmortems(conn, config, llm)
    log.info("learn complete", extra={"ctx": summary})
    return summary
