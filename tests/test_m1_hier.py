"""Phase 12 -- M1.x hierarchical recalibration (CLAUDE.md M1.x, brief section 6/10).

Covers the headline acceptance criterion: on synthetic multi-venue data, a
small-n venue's offset shrinks toward the global curve while a large-n venue's
offset diverges where its own data demands it.
"""

from __future__ import annotations

import numpy as np
import pytest

from lab.learn import registry
from lab.learn.loop import _process_challenger, fit_m1_hier_walk_forward
from lab.learn.refit import (
    WalkForwardError,
    assert_walk_forward,
    fit_m1_hier_curves,
    logit,
    save_artifact,
    sigmoid,
)
from lab.models.base import MarketState
from lab.models.m1_hier import M1Hier, apply_hier_curve
from lab.store import db
from lab.util import load_config


@pytest.fixture()
def config(tmp_path):
    cfg = load_config()
    cfg["storage"] = {
        "db_path": str(tmp_path / "lab.db"),
        "snapshots_dir": str(tmp_path / "snapshots"),
        "models_dir": str(tmp_path / "models"),
        "logs_dir": str(tmp_path / "logs"),
        "reports_dir": str(tmp_path / "reports"),
    }
    return cfg


def _generate_venue_obs(rng, n, alpha_g, beta_g, alpha_v, beta_v, venue, days=100.0):
    p_market = rng.uniform(0.05, 0.95, size=n)
    z = (alpha_g + alpha_v) + (beta_g + beta_v) * logit(p_market)
    y = rng.binomial(1, sigmoid(z))
    return [
        {"p_market": float(p), "outcome": float(o), "days_to_resolution": days, "venue": venue}
        for p, o in zip(p_market, y)
    ]


def test_small_n_venue_shrinks_large_n_venue_diverges():
    """Phase 12's headline acceptance criterion, run against real data shapes:
    a small-n venue's fitted offset stays close to zero (shrunk toward the
    global curve) while a large-n venue's fitted offset tracks its true
    generating divergence."""
    rng = np.random.default_rng(42)
    alpha_g, beta_g = 0.0, 1.3

    # Bulk of data: plain global curve, no venue offset -- anchors the fit.
    poly_obs = _generate_venue_obs(rng, 5000, alpha_g, beta_g, 0.0, 0.0, "polymarket")
    # Large-n venue: true offset is real and should be recoverable.
    kalshi_true = (0.5, 0.4)
    kalshi_obs = _generate_venue_obs(rng, 3000, alpha_g, beta_g, *kalshi_true, "kalshi")
    # Small-n venue: true offset is even LARGER than Kalshi's, but with only
    # 15 observations the ridge penalty (scaled bucket_n/n_v) should crush it.
    metaculus_true = (-0.8, -0.6)
    metaculus_obs = _generate_venue_obs(rng, 15, alpha_g, beta_g, *metaculus_true, "metaculus")

    artifact = fit_m1_hier_curves(poly_obs + kalshi_obs + metaculus_obs)

    bucket = artifact["buckets"]["gt90d"]
    kalshi_fit = bucket["venues"]["kalshi"]
    metaculus_fit = bucket["venues"]["metaculus"]

    kalshi_mag = abs(kalshi_fit["alpha_offset"]) + abs(kalshi_fit["beta_offset"])
    metaculus_mag = abs(metaculus_fit["alpha_offset"]) + abs(metaculus_fit["beta_offset"])
    kalshi_true_mag = abs(kalshi_true[0]) + abs(kalshi_true[1])
    metaculus_true_mag = abs(metaculus_true[0]) + abs(metaculus_true[1])

    # Small-n venue: fitted offset is shrunk to well under half its true
    # generating divergence (heavy ridge penalty: bucket_n/n_v ~ 200x).
    assert metaculus_mag < metaculus_true_mag * 0.5
    # Large-n venue: fitted offset lands close to its true generating
    # divergence (light ridge penalty: bucket_n/n_v ~ 1x) -- it "diverges when
    # its data demands it," unlike the small-n venue.
    assert abs(kalshi_mag - kalshi_true_mag) < 0.3
    # The large-n venue's offset magnitude clearly exceeds the small-n venue's,
    # even though the small-n venue's TRUE generating offset was larger.
    assert kalshi_mag > metaculus_mag


def test_fit_m1_hier_curves_requires_min_observations_per_bucket():
    artifact = fit_m1_hier_curves([
        {"p_market": 0.5, "outcome": 1.0, "days_to_resolution": 100.0, "venue": "polymarket"},
    ])
    assert artifact["buckets"] == {}


def test_fit_m1_hier_curves_drops_disallowed_venues():
    rng = np.random.default_rng(1)
    obs = _generate_venue_obs(rng, 200, 0.0, 1.0, 0.0, 0.0, "manifold")
    artifact = fit_m1_hier_curves(obs)
    # All rows were manifold-tagged and filtered out (guardrail 16) -> no bucket fit at all.
    assert artifact["buckets"] == {}


def test_assert_walk_forward_still_gates_this_path():
    with pytest.raises(WalkForwardError):
        assert_walk_forward([], [{"x": 1}])


# --- M1Hier model class -----------------------------------------------------

HIER_ART = {
    "version": 1,
    "buckets": {
        "30to90d": {
            "global": {"alpha": 0.0, "beta": 1.3, "n": 1000},
            "venues": {"kalshi": {"alpha_offset": 0.4, "beta_offset": 0.2, "n": 500}},
        }
    },
}


def _state(p_market=0.6, days=45.0, venue="polymarket") -> MarketState:
    return MarketState(
        condition_id="0x1", question="Q?", category="politics", description="d",
        end_date_iso="2026-12-31T00:00:00+00:00", tier="liquid",
        p_market=p_market, spread=0.02, snapshot_ts="2026-07-02T12:00:00+00:00",
        days_to_resolution=days, venue=venue,
    )


def test_apply_hier_curve_uses_global_only_when_venue_missing():
    p_global = apply_hier_curve(HIER_ART, "polymarket", "30to90d", 0.6)
    expected = float(sigmoid(0.0 + 1.3 * logit(0.6)))
    assert p_global == pytest.approx(expected)


def test_apply_hier_curve_adds_venue_offset():
    p_kalshi = apply_hier_curve(HIER_ART, "kalshi", "30to90d", 0.6)
    expected = float(sigmoid((0.0 + 0.4) + (1.3 + 0.2) * logit(0.6)))
    assert p_kalshi == pytest.approx(expected)


def test_m1_hier_abstains_outside_declared_venue():
    model = M1Hier(HIER_ART, venue="kalshi")
    assert model.forecast(_state(venue="polymarket"), {}) is None
    assert model.model_id == "m1_hier@kalshi"


def test_m1_hier_forecasts_own_venue_and_falls_back_to_global():
    kalshi_model = M1Hier(HIER_ART, venue="kalshi")
    res = kalshi_model.forecast(_state(0.6, days=45.0, venue="kalshi"), {})
    assert res is not None
    assert res.p_yes == pytest.approx(apply_hier_curve(HIER_ART, "kalshi", "30to90d", 0.6))

    # Polymarket has no venue offset entry in HIER_ART -> falls back to the
    # global-only curve (the partial-pooling default, not a special case).
    poly_model = M1Hier(HIER_ART, venue="polymarket")
    res_poly = poly_model.forecast(_state(0.6, days=45.0, venue="polymarket"), {})
    assert res_poly.p_yes == pytest.approx(apply_hier_curve(HIER_ART, "polymarket", "30to90d", 0.6))


def test_m1_hier_abstains_without_bucket_coverage():
    model = M1Hier(HIER_ART, venue="kalshi")
    assert model.forecast(_state(days=2.0, venue="kalshi"), {}) is None      # no lt7d bucket
    assert model.forecast(_state(days=None, venue="kalshi"), {}) is None    # unknown horizon


# --- registration hygiene (Phase 12 acceptance: "without touching the incumbent") ---

def test_fit_m1_hier_walk_forward_requires_validation_window():
    train = [{"p_market": 0.6, "outcome": 1.0, "days_to_resolution": 3.0, "venue": "polymarket"}] * 200
    with pytest.raises(WalkForwardError):
        fit_m1_hier_walk_forward(train, [])
    with pytest.raises(WalkForwardError):
        fit_m1_hier_walk_forward([], train)


def test_registering_m1_hier_curves_does_not_touch_m1_curves_incumbent(config):
    """Phase 12's literal acceptance wording: registering m1_hier_curves for
    the first time leaves m1_curves' own active pointer completely untouched --
    they are independent artifact keys in model_versions."""
    conn = db.connect(config["storage"]["db_path"])

    # An incumbent m1_curves is already active, as if from an earlier `lab learn` run.
    path = save_artifact(config, "m1_curves", {"kind": "m1_curves", "buckets": {}}, promote=False)
    incumbent_id = registry.register_version(conn, config, "m1_curves", path)
    registry.set_active(conn, config, "m1_curves", incumbent_id)

    rng = np.random.default_rng(7)
    obs = _generate_venue_obs(rng, 500, 0.0, 1.0, 0.0, 0.0, "polymarket")
    live = _generate_venue_obs(rng, 200, 0.0, 1.0, 0.0, 0.0, "polymarket")
    artifact = fit_m1_hier_walk_forward(train=obs, validation=live)

    from lab.learn.loop import _m1_hier_predict

    decision = _process_challenger(
        conn, config, "m1_hier_curves", artifact, live, _m1_hier_predict, apply=True)
    assert decision["promoted"] is True  # no incumbent for this key yet -> "first", auto-promotes

    m1_curves_active = registry.active_version(conn, "m1_curves")
    assert m1_curves_active["id"] == incumbent_id
    assert m1_curves_active["retired_ts"] is None

    m1_hier_active = registry.active_version(conn, "m1_hier_curves")
    assert m1_hier_active is not None
    assert m1_hier_active["id"] != incumbent_id
    conn.close()
