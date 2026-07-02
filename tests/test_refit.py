"""M1/M2 fitting: recovery, monotonicity, artifact versioning."""

from __future__ import annotations

import numpy as np
import pytest

from lab.learn.refit import (
    bucket_for_days,
    fit_logistic_recalibration,
    fit_m1_curves,
    fit_m2_baserates,
    isotonic_fit,
    load_active_artifact,
    logit,
    save_artifact,
    sigmoid,
)

RNG = np.random.default_rng(7)


def _synthetic(n: int, alpha: float, beta: float) -> tuple[np.ndarray, np.ndarray]:
    """Outcomes drawn from sigmoid(alpha + beta*logit(p)): the market is
    miscalibrated exactly the way M1 models."""
    p_market = RNG.uniform(0.05, 0.95, n)
    p_true = sigmoid(alpha + beta * logit(p_market))
    y = (RNG.uniform(size=n) < p_true).astype(float)
    return p_market, y


def test_recovers_underconfident_market():
    p, y = _synthetic(20000, alpha=0.0, beta=1.5)
    fit = fit_logistic_recalibration(p, y)
    assert fit["beta"] == pytest.approx(1.5, abs=0.15)
    assert fit["alpha"] == pytest.approx(0.0, abs=0.1)


def test_recalibration_curve_is_monotone():
    """Acceptance criterion: each fitted recalibration must be monotone."""
    p, y = _synthetic(5000, alpha=0.2, beta=1.3)
    fit = fit_logistic_recalibration(p, y)
    grid = np.linspace(0.01, 0.99, 500)
    curve = sigmoid(fit["alpha"] + fit["beta"] * logit(grid))
    assert np.all(np.diff(curve) > 0)


def test_isotonic_output_is_monotone():
    p, y = _synthetic(3000, alpha=0.0, beta=1.2)
    bins = isotonic_fit(p, y)
    iso = [b["isotonic"] for b in bins]
    assert all(a <= b + 1e-9 for a, b in zip(iso, iso[1:]))


def test_m1_curves_per_bucket_and_monotone():
    obs = []
    for days, beta in [(3, 1.05), (15, 1.2), (60, 1.4), (200, 1.6)]:
        p, y = _synthetic(3000, alpha=0.0, beta=beta)
        obs += [
            {"p_market": pi, "outcome": yi, "days_to_resolution": days}
            for pi, yi in zip(p, y)
        ]
    artifact = fit_m1_curves(obs)
    assert set(artifact["buckets"]) == {"lt7d", "7to30d", "30to90d", "gt90d"}
    grid = np.linspace(0.01, 0.99, 200)
    for fit in artifact["buckets"].values():
        curve = sigmoid(fit["alpha"] + fit["beta"] * logit(grid))
        assert np.all(np.diff(curve) > 0)


def test_bucket_for_days():
    assert bucket_for_days(2) == "lt7d"
    assert bucket_for_days(7) == "7to30d"
    assert bucket_for_days(45) == "30to90d"
    assert bucket_for_days(400) == "gt90d"


def test_m2_baserates_respects_min_n():
    rows = [{"category": "politics", "outcome": 1.0}] * 40 + [
        {"category": "politics", "outcome": 0.0}
    ] * 60 + [{"category": "rare", "outcome": 1.0}] * 5
    art = fit_m2_baserates(rows, min_n=50)
    assert art["categories"]["politics"]["base_rate"] == pytest.approx(0.4)
    assert "rare" not in art["categories"]


def test_artifact_versioning_and_promotion(tmp_path):
    config = {"storage": {"models_dir": str(tmp_path)}}
    save_artifact(config, "m1_curves", {"kind": "m1_curves", "buckets": {}})
    save_artifact(config, "m1_curves", {"kind": "m1_curves", "buckets": {"lt7d": {}}})
    active = load_active_artifact(config, "m1_curves")
    assert active["version"] == 2

    # A challenger fit is written but NOT promoted: active stays at v2.
    save_artifact(config, "m1_curves", {"kind": "m1_curves"}, promote=False)
    assert load_active_artifact(config, "m1_curves")["version"] == 2
    assert (tmp_path / "m1_curves_v3.json").exists()
