"""Precision-weighted stratified skill estimator (brief section 7, v2.1).

Replaces the vacuous v1.9 "control-variate" design (which centered its
covariate at its own in-sample mean, forcing the correction term to zero
regardless of beta -- the "corrected" estimate was numerically identical to
the raw mean, so an "agree in sign" check could never fail).

The fix: stratify resolved forecasts into price buckets on `p_market_at_ts`
(Brier-difference variance is driven directly by price level -- Bernoulli
variance ~= p*(1-p) -- so price buckets capture real, independently-known
heterogeneity rather than a circularly-defined one). Within each stratum
with n_s >= min_stratum_n, compute the stratum mean diff and the variance OF
THAT MEAN (Var(d_bar_s) = sample_var(diffs_in_s) / n_s), then pool via
inverse-variance weights:

    skill_pw = sum(w_s * d_bar_s) / sum(w_s),  w_s = 1 / Var(d_bar_s)

Under HOMOGENEOUS per-observation variance across strata (regardless of
differing stratum sizes), w_s = n_s / sigma**2, so the pooling collapses
exactly to sum(n_s * d_bar_s) / sum(n_s) == the raw pooled mean(diffs) --
this is a required, asserted unit-test invariant (it is precisely the
property the v1.9 design lacked). It only diverges, meaningfully, when
per-observation variance is genuinely heterogeneous across price buckets.

Fewer than `min_strata` qualifying strata (n_s >= min_stratum_n) -> report
"insufficient data" (skill_pw=None), never a computed value on too few cells.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEFAULT_BINS: tuple[float, ...] = (0.0, 0.05, 0.2, 0.4, 0.6, 0.8, 0.95, 1.0)


@dataclass
class StratifiedSkillResult:
    skill_pw: float | None
    ci_lo: float | None
    ci_hi: float | None
    n_strata: int


def _stratum_index(p_market: np.ndarray, bins: tuple[float, ...]) -> np.ndarray:
    """Bucket index per row: bins[i] <= p < bins[i+1] -> bucket i."""
    return np.digitize(p_market, bins[1:-1], right=False)


def _pooled_stratified_mean(
    diffs: np.ndarray, strata: np.ndarray, min_stratum_n: int, min_strata: int
) -> tuple[float | None, int]:
    weights, means = [], []
    for s in np.unique(strata):
        mask = strata == s
        n_s = int(mask.sum())
        if n_s < min_stratum_n:
            continue
        d_s = diffs[mask]
        var_d_bar_s = max(float(np.var(d_s, ddof=1)) / n_s, 1e-12)
        weights.append(1.0 / var_d_bar_s)
        means.append(float(np.mean(d_s)))
    n_qualify = len(means)
    if n_qualify < min_strata:
        return None, n_qualify
    weights_arr = np.array(weights)
    means_arr = np.array(means)
    return float(np.sum(weights_arr * means_arr) / np.sum(weights_arr)), n_qualify


def _bootstrap_stratified_ci(
    diffs: np.ndarray, p_market: np.ndarray, cluster_ids: np.ndarray,
    bins: tuple[float, ...], min_stratum_n: int, min_strata: int,
    iterations: int, alpha: float, seed: int = 0,
) -> tuple[float | None, float | None]:
    """Cluster-resample (mirrors scoring.cluster_bootstrap_ci's discipline),
    recomputing the full stratified pooling -- not a plain mean -- on each
    resample."""
    unique_clusters = np.unique(cluster_ids)
    idx_by_cluster = {c: np.where(cluster_ids == c)[0] for c in unique_clusters}
    rng = np.random.default_rng(seed)
    skills: list[float] = []
    for _ in range(iterations):
        picked = rng.choice(unique_clusters, size=len(unique_clusters), replace=True)
        idx = np.concatenate([idx_by_cluster[c] for c in picked])
        strata = _stratum_index(p_market[idx], bins)
        skill_pw, _ = _pooled_stratified_mean(diffs[idx], strata, min_stratum_n, min_strata)
        if skill_pw is not None:
            skills.append(skill_pw)
    if len(skills) < iterations // 2:
        return None, None
    arr = np.array(skills)
    return float(np.quantile(arr, alpha / 2)), float(np.quantile(arr, 1 - alpha / 2))


def precision_weighted_skill(
    diffs: np.ndarray,
    p_market: np.ndarray,
    cluster_ids: np.ndarray,
    iterations: int = 2000,
    alpha: float = 0.05,
    bins: tuple[float, ...] = DEFAULT_BINS,
    min_stratum_n: int = 30,
    min_strata: int = 3,
) -> StratifiedSkillResult:
    diffs = np.asarray(diffs, dtype=float)
    p_market = np.asarray(p_market, dtype=float)
    cluster_ids = np.asarray(cluster_ids)
    strata = _stratum_index(p_market, bins)
    skill_pw, n_qualify = _pooled_stratified_mean(diffs, strata, min_stratum_n, min_strata)
    if skill_pw is None:
        return StratifiedSkillResult(skill_pw=None, ci_lo=None, ci_hi=None, n_strata=n_qualify)
    ci_lo, ci_hi = _bootstrap_stratified_ci(
        diffs, p_market, cluster_ids, bins, min_stratum_n, min_strata, iterations, alpha
    )
    return StratifiedSkillResult(skill_pw=skill_pw, ci_lo=ci_lo, ci_hi=ci_hi, n_strata=n_qualify)
