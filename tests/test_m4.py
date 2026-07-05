"""M4 ensemble pooling and weight fitting."""

from __future__ import annotations

import pytest

from lab.learn.refit import logit, sigmoid
from lab.models.base import MarketState
from lab.models.m4_ensemble import M4Ensemble, fit_m4_weights
from lab.store import db
from lab.util import load_config, now_utc


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lab.db")
    yield c
    c.close()


def _state(cid="0x1", category="politics") -> MarketState:
    return MarketState(
        condition_id=cid, question="Q?", category=category, description=None,
        end_date_iso=None, tier="liquid", p_market=0.5, spread=0.02,
        snapshot_ts="2026-07-02T12:00:00+00:00", days_to_resolution=30.0,
    )


def _add_forecast(conn, cid, model_id, p_yes, ts=None):
    db.append_forecast(conn, {
        "ts": ts or now_utc().isoformat(timespec="seconds"),
        "condition_id": cid, "model_id": model_id,
        "p_yes": p_yes, "p_market_at_ts": 0.5,
    })


def test_equal_weight_pool_is_logodds_mean(conn):
    _add_forecast(conn, "0x1", "m0_market", 0.5)
    _add_forecast(conn, "0x1", "m1_debiased", 0.7)
    conn.commit()
    res = M4Ensemble(conn, None).forecast(_state(), {})
    expected = float(sigmoid((logit(0.5) + logit(0.7)) / 2))
    assert res.p_yes == pytest.approx(expected)
    assert res.meta["weighted"] is False


def test_abstains_with_fewer_than_two_members(conn):
    _add_forecast(conn, "0x1", "m0_market", 0.5)
    conn.commit()
    assert M4Ensemble(conn, None).forecast(_state(), {}) is None


def test_fitted_weights_favor_better_model(conn):
    config = load_config()
    # 120 resolved politics markets: m1 always closer to truth than m0.
    for i in range(120):
        cid = f"0x{i}"
        conn.execute(
            "INSERT INTO markets (condition_id, category, tier, active, closed) "
            "VALUES (?, 'politics', 'liquid', 1, 1)", (cid,))
        outcome = float(i % 2)
        db.record_resolution(conn, cid, "2026-07-01T00:00:00+00:00", outcome, False, "gamma")
        _add_forecast(conn, cid, "m0_market", 0.5)
        _add_forecast(conn, cid, "m1_debiased", 0.8 if outcome else 0.2)
    conn.commit()
    art = fit_m4_weights(conn, config)
    weights = art["categories"]["politics"]["weights"]
    assert weights["m1_debiased"] > weights["m0_market"]
    assert sum(weights.values()) == pytest.approx(1.0)


# --- Phase 13: extremization applied at forecast time -----------------------

def test_no_extremization_artifact_is_byte_identical_to_today(conn):
    """Regression guard: omitting extremization_artifact (the default) must
    reproduce today's plain log-odds pool exactly -- no behavior change for
    existing deployments until a fit actually runs."""
    _add_forecast(conn, "0x1", "m0_market", 0.5)
    _add_forecast(conn, "0x1", "m1_debiased", 0.7)
    conn.commit()
    without = M4Ensemble(conn, None).forecast(_state(), {})
    with_none = M4Ensemble(conn, None, None).forecast(_state(), {})
    assert without.p_yes == pytest.approx(with_none.p_yes)
    assert with_none.meta["extremization_a_eff"] == pytest.approx(1.0)


def test_extremization_shifts_pool_away_from_plain_average(conn):
    _add_forecast(conn, "0x1", "m0_market", 0.5)
    _add_forecast(conn, "0x1", "m1_debiased", 0.7)
    conn.commit()
    plain = M4Ensemble(conn, None).forecast(_state(), {})
    ext_artifact = {"categories": {"politics": {"a": 2.0, "rho_bar": 0.0}}}
    extremized = M4Ensemble(conn, None, ext_artifact).forecast(_state(), {})

    assert extremized.meta["extremization_a_eff"] == pytest.approx(2.0)  # rho_bar=0 -> full a
    # Both members agree YES-leaning (0.5, 0.7) -> extremizing pushes further
    # from 0.5 in the same direction, not toward it.
    assert extremized.p_yes > plain.p_yes > 0.5


def test_extremization_fully_correlated_pair_collapses_to_identity(conn):
    """rho_bar=1.0 with n=2 members -> n_eff=1 -> a_eff=1.0 regardless of the
    fitted a (Phase 13's "duplicating a source suppresses extremization")."""
    _add_forecast(conn, "0x1", "m0_market", 0.5)
    _add_forecast(conn, "0x1", "m1_debiased", 0.7)
    conn.commit()
    plain = M4Ensemble(conn, None).forecast(_state(), {})
    ext_artifact = {"categories": {"politics": {"a": 2.5, "rho_bar": 1.0}}}
    extremized = M4Ensemble(conn, None, ext_artifact).forecast(_state(), {})
    assert extremized.meta["extremization_a_eff"] == pytest.approx(1.0)
    assert extremized.p_yes == pytest.approx(plain.p_yes)
