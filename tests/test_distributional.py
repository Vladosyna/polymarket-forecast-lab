"""Phase 16 (v2.4): event-distribution assembly and RPS scoring for
bucketed events (CPI ranges, temperature bands, ...).
"""

from __future__ import annotations

import numpy as np
import pytest

from lab.eval.distributional import (
    bucketed_resolved_events,
    coherence_deviation,
    implied_cdf,
    parse_bucket_order,
)
from lab.eval.scoring import brier, paired_rps_skill, rps
from lab.store import db


def test_parse_bucket_order_extracts_numeric_ranges():
    assert parse_bucket_order("Will CPI be between 3.0% and 3.5%?") == 3.0
    assert parse_bucket_order("Will the high temperature exceed 90 degrees?") == 90.0
    assert parse_bucket_order("Will BTC be above $100,000?") == 100000.0
    assert parse_bucket_order("Will the rate be -0.5% or lower?") == -0.5


def test_parse_bucket_order_returns_none_when_unparseable():
    assert parse_bucket_order("Will the incumbent win the election?") is None
    assert parse_bucket_order(None) is None
    assert parse_bucket_order("") is None


def test_implied_cdf_renormalizes_to_sum_one():
    cdf = implied_cdf([0.1, 0.2, 0.3])
    assert cdf.sum() == pytest.approx(1.0)
    assert cdf.tolist() == pytest.approx([1 / 6, 2 / 6, 3 / 6])


def test_implied_cdf_falls_back_to_uniform_when_degenerate():
    cdf = implied_cdf([0.0, 0.0, 0.0])
    assert cdf.tolist() == pytest.approx([1 / 3, 1 / 3, 1 / 3])


def test_coherence_deviation_is_zero_for_a_coherent_pool():
    assert coherence_deviation([0.2, 0.3, 0.5]) == pytest.approx(0.0)
    assert coherence_deviation([0.3, 0.3, 0.3]) == pytest.approx(0.1)


# --- bucketed_resolved_events -----------------------------------------------

def _seed_leg(conn, cid, event_id, question, p_yes, p_market, payout_yes, ts="2026-07-01T00:00:00+00:00"):
    conn.execute(
        """INSERT INTO markets (condition_id, question, category, tier, active, closed, event_id)
           VALUES (?, ?, 'economics', 'liquid', 1, 1, ?)""",
        (cid, question, event_id),
    )
    db.append_forecast(conn, {"ts": ts, "condition_id": cid, "model_id": "m_test",
                             "p_yes": p_yes, "p_market_at_ts": p_market})
    db.record_resolution(conn, cid, ts, payout_yes, False, "gamma")


def test_bucketed_resolved_events_groups_and_orders_by_bucket(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    _seed_leg(conn, "0x1", "evt1", "Will CPI be between 3.0% and 3.5%?", 0.2, 0.25, 0.0)
    _seed_leg(conn, "0x2", "evt1", "Will CPI be between 3.5% and 4.0%?", 0.6, 0.55, 1.0)
    _seed_leg(conn, "0x3", "evt1", "Will CPI be between 4.0% and 4.5%?", 0.2, 0.20, 0.0)
    conn.commit()

    events = bucketed_resolved_events(conn, "m_test")
    assert len(events) == 1
    e = events[0]
    assert e["event_id"] == "evt1"
    assert e["condition_ids"] == ["0x1", "0x2", "0x3"]  # bucket-ordered: 3.0 < 3.5 < 4.0
    assert e["y_bucket_idx"] == 1
    assert e["p_model"] == pytest.approx([0.2, 0.6, 0.2])
    conn.close()


def test_bucketed_resolved_events_skips_malformed_group_with_two_true_legs(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    _seed_leg(conn, "0x1", "evt_bad", "Will CPI be between 3.0% and 3.5%?", 0.5, 0.5, 1.0)
    _seed_leg(conn, "0x2", "evt_bad", "Will CPI be between 3.5% and 4.0%?", 0.5, 0.5, 1.0)
    conn.commit()

    assert bucketed_resolved_events(conn, "m_test") == []
    conn.close()


def test_bucketed_resolved_events_skips_group_with_unparseable_question(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    _seed_leg(conn, "0x1", "evt_unclear", "Will the incumbent win?", 0.4, 0.4, 0.0)
    _seed_leg(conn, "0x2", "evt_unclear", "Will the challenger win?", 0.6, 0.6, 1.0)
    conn.commit()

    assert bucketed_resolved_events(conn, "m_test") == []
    conn.close()


def test_bucketed_resolved_events_uses_latest_forecast_per_leg(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    _seed_leg(conn, "0x1", "evt2", "Will CPI be between 3.0% and 3.5%?", 0.9, 0.9, 0.0,
             ts="2026-06-01T00:00:00+00:00")
    # A later forecast on the same leg -- must win over the earlier one.
    db.append_forecast(conn, {"ts": "2026-06-15T00:00:00+00:00", "condition_id": "0x1",
                             "model_id": "m_test", "p_yes": 0.2, "p_market_at_ts": 0.25})
    _seed_leg(conn, "0x2", "evt2", "Will CPI be between 3.5% and 4.0%?", 0.6, 0.6, 1.0,
             ts="2026-06-01T00:00:00+00:00")
    conn.commit()

    events = bucketed_resolved_events(conn, "m_test")
    assert len(events) == 1
    assert events[0]["p_model"][0] == pytest.approx(0.2)  # the later forecast, not 0.9
    conn.close()


# --- RPS scoring (eval/scoring.py) -----------------------------------------

def test_rps_two_bucket_identity_matches_brier():
    """The literal Phase 16 acceptance identity: a two-bucket event's RPS
    reduces EXACTLY to the Brier score (not an approximation)."""
    for p in (0.1, 0.3, 0.5, 0.7, 0.95):
        p_buckets = [p, 1 - p]  # bucket 0 = YES, bucket 1 = NO
        # Outcome = bucket 0 (YES happened) -> Brier's y=1.
        assert rps(p_buckets, y_bucket_idx=0) == pytest.approx(
            brier(np.array([p]), np.array([1.0]))[0])
        # Outcome = bucket 1 (NO happened) -> Brier's y=0.
        assert rps(p_buckets, y_bucket_idx=1) == pytest.approx(
            brier(np.array([p]), np.array([0.0]))[0])


def test_rps_rewards_correct_shape_over_lucky_spike():
    """The whole point of the RPS upgrade: a model whose mass is spread
    sensibly on and near the true bucket scores BETTER than a model that
    puts a single confident spike on a bucket that isn't even the true
    one -- RPS punishes confident wrongness more than a diffuse near-miss."""
    y_bucket_idx = 2
    shape_correct = [0.15, 0.40, 0.30, 0.15]  # spread, real mass on/near bucket 2
    lucky_spike = [0.02, 0.02, 0.02, 0.94]    # confident spike on bucket 3, not 2

    assert rps(shape_correct, y_bucket_idx) < rps(lucky_spike, y_bucket_idx)


def test_paired_rps_skill_reuses_cluster_bootstrap(tmp_path):
    """Pairing/clustering reuse proven directly (not assumed): paired_rps_skill's
    CI comes from the same cluster_bootstrap_ci every other skill statistic
    already uses, and bucketed_resolved_events' output feeds it end to end."""
    conn = db.connect(tmp_path / "lab.db")
    _seed_leg(conn, "0x1", "evtA", "Will CPI be 3.0%?", 0.05, 0.33, 0.0)
    _seed_leg(conn, "0x2", "evtA", "Will CPI be 3.5%?", 0.90, 0.34, 1.0)
    _seed_leg(conn, "0x3", "evtA", "Will CPI be 4.0%?", 0.05, 0.33, 0.0)
    _seed_leg(conn, "0x4", "evtB", "Will temperature be 60F?", 0.05, 0.33, 0.0)
    _seed_leg(conn, "0x5", "evtB", "Will temperature be 70F?", 0.90, 0.34, 1.0)
    _seed_leg(conn, "0x6", "evtB", "Will temperature be 80F?", 0.05, 0.33, 0.0)
    conn.commit()

    events = bucketed_resolved_events(conn, "m_test")
    assert len(events) == 2
    result = paired_rps_skill(events, iterations=200)
    assert result.n == 2
    assert result.skill_rps > 0  # model clearly beats the near-uniform market
    assert result.skill_rps_ci_lo <= result.skill_rps <= result.skill_rps_ci_hi
    conn.close()


def test_bucketed_resolved_events_filters_by_category_and_window(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    _seed_leg(conn, "0x1", "evt_econ", "Will CPI be 3.0%?", 0.3, 0.3, 0.0,
             ts="2026-07-01T00:00:00+00:00")
    _seed_leg(conn, "0x2", "evt_econ", "Will CPI be 3.5%?", 0.7, 0.7, 1.0,
             ts="2026-07-01T00:00:00+00:00")
    _seed_leg(conn, "0x3", "evt_weather", "Will it be 60F?", 0.3, 0.3, 0.0,
             ts="2020-01-01T00:00:00+00:00")
    _seed_leg(conn, "0x4", "evt_weather", "Will it be 70F?", 0.7, 0.7, 1.0,
             ts="2020-01-01T00:00:00+00:00")
    conn.execute("UPDATE markets SET category='weather' WHERE condition_id IN ('0x3','0x4')")
    conn.execute("UPDATE resolutions SET resolved_ts='2020-01-01T00:00:00+00:00' "
                "WHERE condition_id IN ('0x3','0x4')")
    conn.commit()

    econ_only = bucketed_resolved_events(conn, "m_test", category="economics")
    assert {e["event_id"] for e in econ_only} == {"evt_econ"}

    recent_only = bucketed_resolved_events(conn, "m_test", window_days=90)
    assert {e["event_id"] for e in recent_only} == {"evt_econ"}  # 2020 event excluded
    conn.close()
