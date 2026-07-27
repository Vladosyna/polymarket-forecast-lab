"""The sports null control: eligibility sampling and scoring membership.

The control is the harness's own placebo (§4.7) -- apparent skill on sports is
supposed to indicate a broken instrument. Until 2026-07-28 it was itself
broken: `null_control_ids` sampled from every sports market ever seen, ~80% of
them already resolved and ~93% in the `ignored` tier, while the forecast loop
only considers `tier IN ('liquid','tail') AND active = 1 AND closed = 0`. Of
30 ids drawn on production data, 27 were already closed and **none** survived
the loop, so the control produced zero forecasts for the whole collection
period while reporting as configured.
"""

from __future__ import annotations

import pytest

from lab.forecast import null_control_ids, null_control_ids_by_venue
from lab.store import db
from lab.util import load_config, now_utc_iso


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lab.db")
    yield c
    c.close()


def _seed_market(conn, cid, venue="polymarket", category="sports",
                 tier="tail", active=1, closed=0):
    db.upsert_market(conn, {
        "condition_id": cid, "venue": venue, "venue_native_id": cid,
        "slug": None, "question": f"q {cid}", "category": category, "description": "d",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": active, "closed": closed,
        "liquidity_num": 1.0, "volume_num": 1.0, "tier": tier,
    })


def _seed_forecast(conn, cid, model_id="m0_market"):
    db.append_forecast(conn, {
        "ts": now_utc_iso(), "condition_id": cid, "model_id": model_id,
        "p_yes": 0.5, "p_market_at_ts": 0.5,
    })


# --- eligibility sampling ---------------------------------------------------

def test_sample_only_contains_forecastable_markets(conn):
    """THE regression test. With ineligible markets vastly outnumbering
    eligible ones -- the production ratio -- an unfiltered sample draws
    almost nothing forecastable, which is how the control silently died."""
    config = load_config()
    for i in range(200):
        _seed_market(conn, f"closed_{i}", tier="ignored", active=0, closed=1)
    for i in range(5):
        _seed_market(conn, f"live_{i}", tier="liquid", active=1, closed=0)
    conn.commit()

    ids = null_control_ids(conn, config)

    assert ids, "sample is empty -- the control would forecast nothing"
    assert ids == {f"live_{i}" for i in range(5)}, (
        "sample contains markets the forecast loop will never consider"
    )


def test_sample_respects_configured_size(conn):
    config = load_config()
    size = config["universe"]["null_control"]["sample_size"]
    for i in range(size * 3):
        _seed_market(conn, f"live_{i}", tier="liquid")
    conn.commit()

    assert len(null_control_ids(conn, config)) == size


def test_sample_is_deterministic_for_a_fixed_pool(conn):
    """Same seed, same pool -> same cohort, so a day's coverage is
    reproducible from the committed seed (Phase 15)."""
    config = load_config()
    for i in range(100):
        _seed_market(conn, f"live_{i}", tier="liquid")
    conn.commit()

    assert null_control_ids(conn, config) == null_control_ids(conn, config)


def test_sample_excludes_non_sports(conn):
    config = load_config()
    for i in range(10):
        _seed_market(conn, f"sport_{i}", tier="liquid")
        _seed_market(conn, f"pol_{i}", category="politics", tier="liquid")
    conn.commit()

    assert all(cid.startswith("sport_") for cid in null_control_ids(conn, config))


# --- scoring membership -----------------------------------------------------

def test_scoring_membership_is_what_was_forecast(conn):
    """Membership is read off the ledger, not re-drawn: the eligible pool
    turns over, so a re-draw would both miss real observations and admit
    markets never forecast."""
    config = load_config()
    _seed_market(conn, "forecast_me", tier="liquid")
    _seed_market(conn, "never_forecast", tier="liquid")
    _seed_forecast(conn, "forecast_me")
    conn.commit()

    result = null_control_ids_by_venue(conn, config)
    assert result["polymarket"] == {"forecast_me"}


def test_scoring_membership_keeps_markets_that_since_closed(conn):
    """The case a re-draw gets wrong: forecast while eligible, resolved
    later. Those resolved rows ARE the null control's observations and must
    still be scored once the market can no longer be sampled."""
    config = load_config()
    _seed_market(conn, "was_live", tier="liquid", active=1, closed=0)
    _seed_forecast(conn, "was_live")
    conn.commit()
    # Market closes, as every sports market eventually does.
    _seed_market(conn, "was_live", tier="ignored", active=0, closed=1)
    conn.commit()

    assert null_control_ids(conn, config) == set(), "closed market is still sampled"
    assert null_control_ids_by_venue(conn, config)["polymarket"] == {"was_live"}


def test_scoring_membership_is_scoped_per_venue(conn):
    config = load_config()
    for i in range(3):
        _seed_market(conn, f"poly_{i}", "polymarket", tier="liquid")
        _seed_forecast(conn, f"poly_{i}")
        _seed_market(conn, f"kalshi:T{i}", "kalshi", tier="liquid")
        _seed_forecast(conn, f"kalshi:T{i}")
    _seed_market(conn, "metaculus:Q1", "metaculus", tier="liquid")
    _seed_forecast(conn, "metaculus:Q1")
    conn.commit()

    result = null_control_ids_by_venue(conn, config)

    assert set(result.keys()) == {"polymarket", "kalshi"}  # forecastable venues only
    assert result["polymarket"] == {f"poly_{i}" for i in range(3)}
    assert result["kalshi"] == {f"kalshi:T{i}" for i in range(3)}
    assert result["polymarket"].isdisjoint(result["kalshi"])


def test_scoring_membership_includes_m6_negrisk_sweep(conn):
    """M6 scans every negRisk event regardless of category (the documented
    §3.5 exception). A sports market it forecasts is equally a placebo
    observation and belongs in the control, not in the primary tables."""
    config = load_config()
    _seed_market(conn, "m6_only", tier="liquid")
    _seed_forecast(conn, "m6_only", model_id="m6_consistency")
    conn.commit()

    assert null_control_ids_by_venue(conn, config)["polymarket"] == {"m6_only"}
