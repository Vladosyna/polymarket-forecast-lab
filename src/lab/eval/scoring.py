"""Scoring: paired Brier / log loss, skill with cluster bootstrap CIs.

Sign convention (brief section 7): skill = mean(brier_market - brier_model);
positive skill means the model beats the market.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

EPS = 1e-6


def brier(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    return (np.asarray(p, dtype=float) - np.asarray(y, dtype=float)) ** 2


def log_loss(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    y = np.asarray(y, dtype=float)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


@dataclass
class SkillResult:
    n: int
    n_markets: int
    brier_model: float
    brier_market: float
    skill: float
    skill_ci_lo: float
    skill_ci_hi: float
    log_loss_model: float
    log_loss_market: float
    mde: float  # minimum detectable effect at current n (80% power, alpha=0.05)


def cluster_bootstrap_ci(
    diffs: np.ndarray,
    clusters: np.ndarray,
    iterations: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile CI for mean(diffs), resampling whole clusters (condition_ids).

    Multiple forecasts on the same market are correlated; resampling rows
    would understate the variance.
    """
    unique = np.unique(clusters)
    by_cluster = {c: diffs[clusters == c] for c in unique}
    rng = np.random.default_rng(seed)
    means = np.empty(iterations)
    for i in range(iterations):
        picked = rng.choice(unique, size=len(unique), replace=True)
        means[i] = float(np.mean(np.concatenate([by_cluster[c] for c in picked])))
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def paired_skill(
    p_model: np.ndarray,
    p_market: np.ndarray,
    y: np.ndarray,
    condition_ids: np.ndarray,
    iterations: int = 2000,
) -> SkillResult:
    p_model = np.asarray(p_model, dtype=float)
    p_market = np.asarray(p_market, dtype=float)
    y = np.asarray(y, dtype=float)
    condition_ids = np.asarray(condition_ids)

    b_model = brier(p_model, y)
    b_market = brier(p_market, y)
    diffs = b_market - b_model  # positive = model beats market
    ci_lo, ci_hi = cluster_bootstrap_ci(diffs, condition_ids, iterations=iterations)

    # MDE from the empirical sd of per-market mean paired differences.
    unique = np.unique(condition_ids)
    per_market = np.array([float(np.mean(diffs[condition_ids == c])) for c in unique])
    n_markets = len(unique)
    sd = float(np.std(per_market, ddof=1)) if n_markets > 1 else float("nan")
    mde = 2.8 * sd / np.sqrt(n_markets) if n_markets > 1 else float("nan")

    return SkillResult(
        n=len(y),
        n_markets=n_markets,
        brier_model=float(np.mean(b_model)),
        brier_market=float(np.mean(b_market)),
        skill=float(np.mean(diffs)),
        skill_ci_lo=ci_lo,
        skill_ci_hi=ci_hi,
        log_loss_model=float(np.mean(log_loss(p_model, y))),
        log_loss_market=float(np.mean(log_loss(p_market, y))),
        mde=float(mde),
    )


def honesty_tier(n_markets: int, n_insufficient: int = 200, n_preliminary: int = 500) -> str:
    if n_markets < n_insufficient:
        return "INSUFFICIENT DATA"
    if n_markets < n_preliminary:
        return "PRELIMINARY"
    return "STANDARD"
