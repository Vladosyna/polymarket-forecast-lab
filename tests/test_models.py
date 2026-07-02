"""Baseline models M0/M1/M2."""

from __future__ import annotations

import pytest

from lab.models.base import MarketState
from lab.models.m0_market import M0Market
from lab.models.m1_debiased import M1Debiased
from lab.models.m2_baserate import M2BaseRate


def _state(p_market=0.6, category="politics", days=45.0) -> MarketState:
    return MarketState(
        condition_id="0x1", question="Q?", category=category, description="d",
        end_date_iso="2026-12-31T00:00:00+00:00", tier="liquid",
        p_market=p_market, spread=0.02, snapshot_ts="2026-07-02T12:00:00+00:00",
        days_to_resolution=days,
    )


def test_m0_returns_market_mid():
    assert M0Market().forecast(_state(0.6), {}).p_yes == pytest.approx(0.6)


def test_m0_clamps_extremes():
    assert M0Market().forecast(_state(0.999), {}).p_yes == 0.99
    assert M0Market().forecast(_state(0.001), {}).p_yes == 0.01


M1_ART = {"version": 1, "buckets": {"30to90d": {"alpha": 0.0, "beta": 1.5, "n": 500}}}


def test_m1_extremizes_when_beta_above_one():
    res = M1Debiased(M1_ART).forecast(_state(0.7, days=45.0), {})
    assert res.p_yes > 0.7
    assert res.meta["horizon_bucket"] == "30to90d"

    res_low = M1Debiased(M1_ART).forecast(_state(0.3, days=45.0), {})
    assert res_low.p_yes < 0.3


def test_m1_abstains_without_curve():
    assert M1Debiased(M1_ART).forecast(_state(days=2.0), {}) is None       # no lt7d curve
    assert M1Debiased(M1_ART).forecast(_state(days=None), {}) is None      # unknown horizon


M2_ART = {"version": 1, "categories": {"politics": {"base_rate": 0.2, "n": 300}}}


def test_m2_blends_toward_base_rate():
    res = M2BaseRate(M2_ART).forecast(_state(0.6, category="politics"), {})
    assert 0.2 < res.p_yes < 0.6  # pulled toward the low base rate, gently


def test_m2_abstains_on_unknown_category():
    assert M2BaseRate(M2_ART).forecast(_state(category="weather"), {}) is None
