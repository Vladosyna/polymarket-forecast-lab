"""Deterministic M3 aggregator: direction signs, decay, caps, no look-ahead."""

from __future__ import annotations

import math

import pytest

from lab.learn.refit import logit, sigmoid
from lab.news.aggregate import aggregate, item_delta

TS = "2026-07-02T12:00:00+00:00"


def _item(direction="for_yes", strength=2, reliability=2, relevance=1.0,
          published="2026-07-02T11:00:00+00:00") -> dict:
    return {"claim": "c", "direction": direction, "strength": strength,
            "source_reliability": reliability, "relevance": relevance,
            "published_ts": published}


def test_direction_signs():
    assert item_delta(_item("for_yes"), TS, k=0.15, tau_days=5) > 0
    assert item_delta(_item("for_no"), TS, k=0.15, tau_days=5) < 0
    assert item_delta(_item("neutral"), TS, k=0.15, tau_days=5) == 0.0


def test_delta_hand_computed():
    # k*strength*reliability*relevance*exp(-age/tau); age = 1 hour = 1/24 day
    expected = 0.15 * 2 * 2 * 1.0 * math.exp(-(1 / 24) / 5)
    assert item_delta(_item(), TS, k=0.15, tau_days=5) == pytest.approx(expected)


def test_recency_decay():
    fresh = item_delta(_item(published="2026-07-02T11:00:00+00:00"), TS, 0.15, 5)
    stale = item_delta(_item(published="2026-06-22T11:00:00+00:00"), TS, 0.15, 5)
    assert 0 < stale < fresh
    assert stale / fresh == pytest.approx(math.exp(-10 / 5), rel=1e-2)


def test_no_look_ahead_future_article_is_zero():
    future = _item(published="2026-07-02T13:00:00+00:00")  # after forecast ts
    assert item_delta(future, TS, 0.15, 5) == 0.0
    assert item_delta(_item(published=None), TS, 0.15, 5) == 0.0


def test_total_shift_capped():
    items = [_item(relevance=1.0, strength=3, reliability=3) for _ in range(20)]
    out = aggregate(0.5, items, TS, k=0.15, tau_days=5, max_shift=0.8)
    assert out["total_shift"] > 0.8
    assert out["clipped_shift"] == 0.8
    assert out["p_yes"] == pytest.approx(float(sigmoid(logit(0.5) + 0.8)))


def test_cap_symmetric_for_no():
    items = [_item("for_no", strength=3, reliability=3) for _ in range(20)]
    out = aggregate(0.5, items, TS, max_shift=0.8)
    assert out["clipped_shift"] == -0.8


def test_no_items_returns_market_prior():
    out = aggregate(0.62, [], TS)
    assert out["p_yes"] == pytest.approx(0.62, abs=1e-9)
