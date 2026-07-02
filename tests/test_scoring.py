"""Scoring math on hand-computed fixtures (test-critical per the brief)."""

from __future__ import annotations

import numpy as np
import pytest

from lab.eval.scoring import (
    brier,
    cluster_bootstrap_ci,
    honesty_tier,
    log_loss,
    paired_skill,
)


def test_brier_hand_computed():
    # (0.8 - 1)^2 = 0.04 ; (0.3 - 0)^2 = 0.09
    assert brier(np.array([0.8, 0.3]), np.array([1.0, 0.0])).tolist() == pytest.approx(
        [0.04, 0.09]
    )


def test_log_loss_hand_computed():
    # -ln(0.8) for y=1 at p=0.8 ; -ln(0.7) for y=0 at p=0.3
    out = log_loss(np.array([0.8, 0.3]), np.array([1.0, 0.0]))
    assert out.tolist() == pytest.approx([-np.log(0.8), -np.log(0.7)])


def test_paired_skill_sign_convention():
    """Model closer to truth than market -> POSITIVE skill."""
    y = np.array([1.0, 1.0, 0.0, 0.0])
    p_model = np.array([0.9, 0.8, 0.1, 0.2])   # good
    p_market = np.array([0.6, 0.6, 0.4, 0.4])  # mediocre
    cids = np.array(["a", "b", "c", "d"])
    res = paired_skill(p_model, p_market, y, cids, iterations=200)
    # hand-computed: brier_model = mean(.01,.04,.01,.04)=.025
    #                brier_market = mean(.16,.16,.16,.16)=.16 ; skill=.135
    assert res.brier_model == pytest.approx(0.025)
    assert res.brier_market == pytest.approx(0.16)
    assert res.skill == pytest.approx(0.135)

    # Swap roles: worse model must have the mirrored negative skill.
    res_bad = paired_skill(p_market, p_model, y, cids, iterations=200)
    assert res_bad.skill == pytest.approx(-0.135)


def test_cluster_bootstrap_wider_than_naive():
    """Correlated rows within a market must widen the CI vs treating rows as iid."""
    rng = np.random.default_rng(3)
    n_markets = 40
    market_effect = rng.normal(0, 0.1, n_markets)
    diffs, clusters = [], []
    for i in range(n_markets):
        for _ in range(10):  # 10 highly correlated rows per market
            diffs.append(market_effect[i] + rng.normal(0, 0.001))
            clusters.append(f"m{i}")
    diffs = np.array(diffs)
    clusters = np.array(clusters)

    lo_c, hi_c = cluster_bootstrap_ci(diffs, clusters, iterations=500)
    lo_n, hi_n = cluster_bootstrap_ci(diffs, np.arange(len(diffs)).astype(str), iterations=500)
    assert (hi_c - lo_c) > 2 * (hi_n - lo_n)


def test_mde_scales_with_n():
    rng = np.random.default_rng(5)
    y = (rng.uniform(size=400) < 0.5).astype(float)
    p_model = np.clip(y * 0.7 + 0.15 + rng.normal(0, 0.05, 400), 0.01, 0.99)
    p_market = np.clip(y * 0.6 + 0.2 + rng.normal(0, 0.05, 400), 0.01, 0.99)
    cids = np.array([f"m{i}" for i in range(400)])
    full = paired_skill(p_model, p_market, y, cids, iterations=100)
    small = paired_skill(p_model[:100], p_market[:100], y[:100], cids[:100], iterations=100)
    assert full.mde < small.mde  # more markets -> smaller detectable effect


def test_honesty_tiers():
    assert honesty_tier(50) == "INSUFFICIENT DATA"
    assert honesty_tier(300) == "PRELIMINARY"
    assert honesty_tier(700) == "STANDARD"
