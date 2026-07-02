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
