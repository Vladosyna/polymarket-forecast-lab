"""Phase 15: venue fee-schedule lookup (src/lab/shadow/fees.py)."""

from __future__ import annotations

import pytest

from lab.shadow.fees import fee_usd_for, load_fee_schedule, taker_rate_for

SCHEDULE = {
    "schedule": [
        {"effective_from": "2020-01-01", "venue": "polymarket", "category": "default", "taker_rate": 0.04},
        {"effective_from": "2020-01-01", "venue": "polymarket", "category": "crypto", "taker_rate": 0.07},
        {"effective_from": "2020-01-01", "venue": "kalshi", "category": "default", "taker_rate": 0.05},
        {"effective_from": "2026-07-07", "venue": "kalshi", "category": "default", "taker_rate": 0.07},
    ]
}


def test_taker_rate_picks_category_specific_entry():
    assert taker_rate_for(SCHEDULE, "polymarket", "crypto", "2026-01-01") == pytest.approx(0.07)


def test_taker_rate_falls_back_to_default_category():
    assert taker_rate_for(SCHEDULE, "polymarket", "sports", "2026-01-01") == pytest.approx(0.04)


def test_taker_rate_selects_latest_entry_as_of_date():
    """Kalshi's rate changed from 0.05 to 0.07 effective 2026-07-07 -- a
    lookup before that date gets the old rate, on/after gets the new one."""
    assert taker_rate_for(SCHEDULE, "kalshi", "default", "2026-07-06") == pytest.approx(0.05)
    assert taker_rate_for(SCHEDULE, "kalshi", "default", "2026-07-07") == pytest.approx(0.07)
    assert taker_rate_for(SCHEDULE, "kalshi", "default", "2026-12-31") == pytest.approx(0.07)


def test_taker_rate_unknown_venue_defaults_to_zero():
    assert taker_rate_for(SCHEDULE, "manifold", "sports", "2026-01-01") == 0.0


def test_fee_usd_for_matches_derived_formula():
    # fee = stake * rate * (1 - entry_price)
    fee = fee_usd_for(SCHEDULE, "polymarket", "crypto", entry_price=0.60, stake_usd=1000.0,
                      as_of_ts="2026-01-01")
    assert fee == pytest.approx(1000.0 * 0.07 * 0.40)


def test_load_fee_schedule_missing_file_returns_empty(tmp_path):
    data = load_fee_schedule(tmp_path / "does_not_exist.yaml")
    assert data == {"version": 0, "schedule": []}


def test_load_fee_schedule_reads_real_file():
    data = load_fee_schedule()
    assert data["schedule"]  # the committed data/fee_schedule.yaml is non-empty
    assert any(e["venue"] == "polymarket" for e in data["schedule"])
    assert any(e["venue"] == "kalshi" for e in data["schedule"])
