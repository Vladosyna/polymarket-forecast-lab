"""Precision-weighted stratified skill estimator (brief section 7, v2.1).

This is the test suite that would have caught the original v1.9 control-
variate bug: the homogeneous-variance invariant below is exactly the
property that design lacked (its "correction" was identically zero,
regardless of any real or imagined heterogeneity).
"""

from __future__ import annotations

import numpy as np
import pytest

from lab.eval.stratified import precision_weighted_skill


def _place_in_bucket(rng, bucket_lo: float, bucket_hi: float, n: int) -> np.ndarray:
    return rng.uniform(bucket_lo + 1e-4, bucket_hi - 1e-4, n)


def test_homogeneous_variance_collapses_exactly_to_raw_mean():
    """Required unit-test invariant (brief section 7): under HOMOGENEOUS
    per-observation variance across strata -- even with different stratum
    sizes and different stratum means -- the inverse-variance pooling must
    reduce exactly to the simple pooled mean of every individual diff.

    The collapse is exact only when each stratum's SAMPLE variance
    (ddof=1, as computed by the estimator) is identical, since that is what
    makes the inverse-variance weights exactly proportional to n_s. Random
    draws only match in expectation, not bit-for-bit, so this fixture solves
    for a symmetric two-point spread per stratum that pins the sample
    variance to the same constant regardless of n_s -- this is what makes
    the test exact rather than approximate."""
    rng = np.random.default_rng(0)
    target_var = 1.0  # common ddof=1 sample variance enforced in every stratum
    # 5 strata: different sizes, different true means, same sample variance.
    strata_specs = [
        (0.0, 0.05, 40, 0.30),
        (0.05, 0.2, 60, -0.10),
        (0.2, 0.4, 100, 0.05),
        (0.6, 0.8, 30, 0.00),
        (0.95, 1.0, 50, -0.20),
    ]
    all_diffs, all_p, cluster_ids = [], [], []
    next_id = 0
    for lo, hi, n, mu in strata_specs:
        assert n % 2 == 0
        d = np.sqrt(target_var * (n - 1) / n)
        diffs = np.concatenate([np.full(n // 2, mu + d), np.full(n - n // 2, mu - d)])
        rng.shuffle(diffs)  # order must not matter
        p = _place_in_bucket(rng, lo, hi, n)
        all_diffs.append(diffs)
        all_p.append(p)
        cluster_ids.extend(str(i) for i in range(next_id, next_id + n))
        next_id += n
    diffs = np.concatenate(all_diffs)
    p_market = np.concatenate(all_p)
    cluster_ids = np.array(cluster_ids)

    for d_arr in all_diffs:
        assert np.var(d_arr, ddof=1) == pytest.approx(target_var, abs=1e-9)

    result = precision_weighted_skill(diffs, p_market, cluster_ids, iterations=200)
    assert result.n_strata == 5
    assert result.skill_pw == pytest.approx(np.mean(diffs), abs=1e-9)


def test_heterogeneous_variance_diverges_from_raw_mean():
    """When per-observation variance genuinely differs across strata, the
    inverse-variance pool must diverge -- meaningfully -- from the raw
    pooled mean (favoring the low-variance, high-precision strata)."""
    rng = np.random.default_rng(1)
    # One very noisy stratum with a large mean, several precise/quiet ones
    # with a mean near zero -- the raw mean gets dragged toward the noisy
    # stratum's mean, but precision-weighting should resist that pull.
    noisy = rng.normal(0.5, 1.0, 60)
    noisy_p = _place_in_bucket(rng, 0.0, 0.05, 60)
    quiet_specs = [(0.05, 0.2, 60), (0.2, 0.4, 60), (0.6, 0.8, 60)]
    quiet_diffs, quiet_p, cluster_ids = [], [], []
    next_id = 0
    for lo, hi, n in quiet_specs:
        quiet_diffs.append(rng.normal(0.0, 0.02, n))
        quiet_p.append(_place_in_bucket(rng, lo, hi, n))
    diffs = np.concatenate([noisy] + quiet_diffs)
    p_market = np.concatenate([noisy_p] + quiet_p)
    cluster_ids = np.array([str(i) for i in range(len(diffs))])

    result = precision_weighted_skill(diffs, p_market, cluster_ids, iterations=200)
    assert result.n_strata == 4
    raw_mean = np.mean(diffs)
    assert result.skill_pw is not None
    assert abs(result.skill_pw - raw_mean) > 0.05  # materially different, not noise
    assert result.skill_pw < raw_mean  # pulled toward the quiet strata, away from the noisy one


def test_fewer_than_three_strata_is_insufficient_data():
    """Fewer than min_strata qualifying strata -> None, not a computed value."""
    rng = np.random.default_rng(2)
    # All observations land in just two price buckets.
    diffs = np.concatenate([rng.normal(0.1, 0.1, 40), rng.normal(-0.1, 0.1, 40)])
    p_market = np.concatenate([
        _place_in_bucket(rng, 0.0, 0.05, 40),
        _place_in_bucket(rng, 0.05, 0.2, 40),
    ])
    cluster_ids = np.array([str(i) for i in range(len(diffs))])

    result = precision_weighted_skill(diffs, p_market, cluster_ids, iterations=200)
    assert result.skill_pw is None
    assert result.ci_lo is None and result.ci_hi is None
    assert result.n_strata == 2


def test_stratum_below_min_n_does_not_qualify():
    """A stratum with n_s < min_stratum_n must not count toward n_strata."""
    rng = np.random.default_rng(3)
    specs = [(0.0, 0.05, 40), (0.05, 0.2, 40), (0.2, 0.4, 40), (0.6, 0.8, 5)]  # last too small
    diffs_parts, p_parts, cluster_ids = [], [], []
    for lo, hi, n in specs:
        diffs_parts.append(rng.normal(0.0, 0.1, n))
        p_parts.append(_place_in_bucket(rng, lo, hi, n))
    diffs = np.concatenate(diffs_parts)
    p_market = np.concatenate(p_parts)
    cluster_ids = np.array([str(i) for i in range(len(diffs))])

    result = precision_weighted_skill(diffs, p_market, cluster_ids, iterations=200, min_stratum_n=30)
    assert result.n_strata == 3  # the 5-row stratum excluded
