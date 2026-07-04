"""Anytime-valid confidence sequence for the mean paired Brier difference
(brief section 7, Phase 11).

Implements the two-sided NORMAL-MIXTURE uniform boundary of Howard, Ramdas,
McAuliffe & Sekhon, "Time-uniform, nonparametric, nonasymptotic confidence
sequences," Annals of Statistics 49(2):1055-1080, 2021 (arXiv:1810.08240),
Proposition 5 / Eq. (14):

    u(v) = sqrt( (v + rho) * log( l0**2 * (v + rho) / (alpha**2 * rho) ) )

with l0 = 1 (their Definition 1: "In scalar cases, we always have l0 = 1").
This is applied to the process S_t = t * (mean(diffs[:t]) - mu) with variance
process V_t = t * sample_variance(diffs[:t]) -- an ASYMPTOTIC (CLT-justified)
construction, since it plugs in the running SAMPLE variance rather than a
value known a priori or a strictly predictable/sequential estimate. This
matches the brief's explicit "asymptotic CS" wording, distinct from
Waudby-Smith & Ramdas (2020, arXiv:2010.09686)'s own nonasymptotic
betting-based CS, which this project does not implement.

The intrinsic-time tuning parameter rho is chosen via their Proposition 3(a)
to minimize the boundary at a target sample size (defaults to the full input
length -- "optimize tightness for the horizon we actually expect to look at"):

    m / rho = -W_{-1}(-alpha**2 / e) - 1

where W_{-1} is the lower branch of the Lambert W function.

Brier differences live in [-1, 1]; the construction above is valid for any
sub-Gaussian (in particular, any bounded, hence sub-Gaussian) mean-zero
process, so no rescaling to [0, 1] is required.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.special import lambertw


@dataclass
class ConfidenceSequence:
    center: float
    radius: float
    lo: float
    hi: float
    n: int
    covers_zero: bool


def _optimal_rho(alpha: float, target_n: int) -> float:
    """Proposition 3(a): rho minimizing the normal-mixture boundary at sample
    size `target_n` (l0 = 1, scalar case)."""
    target_n = max(target_n, 1)
    w = lambertw(-(alpha**2) / np.e, k=-1).real
    return target_n / (-w - 1)


def _normal_mixture_radius(v: float, n: int, alpha: float, rho: float) -> float:
    """u(v)/n -- half-width of the CS at variance-process value v, sample size n."""
    if n == 0:
        return float("inf")
    u = np.sqrt((v + rho) * np.log((v + rho) / (alpha**2 * rho)))
    return float(u / n)


def cs_sequence_over_time(
    diffs_in_order: np.ndarray, alpha: float = 0.05
) -> list[ConfidenceSequence]:
    """CS at every prefix of `diffs_in_order` (caller must presort by
    resolution time -- a confidence sequence is a martingale construction
    over the order observations actually arrive in, one value per
    event-cluster per brief section 7)."""
    diffs = np.asarray(diffs_in_order, dtype=float)
    n_total = len(diffs)
    if n_total == 0:
        return []
    rho = _optimal_rho(alpha, n_total)
    out: list[ConfidenceSequence] = []
    for t in range(1, n_total + 1):
        prefix = diffs[:t]
        center = float(np.mean(prefix))
        var = float(np.var(prefix, ddof=1)) if t > 1 else 0.0
        v = t * var
        radius = _normal_mixture_radius(v, t, alpha, rho)
        lo, hi = center - radius, center + radius
        out.append(
            ConfidenceSequence(
                center=center, radius=radius, lo=lo, hi=hi, n=t,
                covers_zero=(lo <= 0.0 <= hi),
            )
        )
    return out


def confidence_sequence(diffs: np.ndarray, alpha: float = 0.05) -> ConfidenceSequence:
    """The CS evaluated at the current (full) sample -- the single snapshot
    used for a nightly report / promotion-gate check."""
    diffs = np.asarray(diffs, dtype=float)
    if len(diffs) == 0:
        return ConfidenceSequence(
            center=0.0, radius=float("inf"), lo=-float("inf"), hi=float("inf"),
            n=0, covers_zero=True,
        )
    return cs_sequence_over_time(diffs, alpha=alpha)[-1]
