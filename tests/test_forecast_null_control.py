"""Per-venue null-control sampling (brief section 7, Phase 11)."""

from __future__ import annotations

import pytest

from lab.forecast import null_control_ids_by_venue
from lab.store import db
from lab.util import load_config


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lab.db")
    yield c
    c.close()


def _seed_market(conn, cid, venue, category="sports"):
    db.upsert_market(conn, {
        "condition_id": cid, "venue": venue, "venue_native_id": cid,
        "slug": None, "question": f"q {cid}", "category": category, "description": "d",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 0, "liquidity_num": 1.0, "volume_num": 1.0,
        "tier": "tail",
    })


def test_null_control_ids_by_venue_scoped_per_venue(conn):
    config = load_config()
    for i in range(5):
        _seed_market(conn, f"poly_{i}", "polymarket")
    for i in range(5):
        _seed_market(conn, f"kalshi:T{i}", "kalshi")
    _seed_market(conn, "metaculus:Q1", "metaculus")  # not forecastable -- excluded
    conn.commit()

    result = null_control_ids_by_venue(conn, config)

    assert set(result.keys()) == {"polymarket", "kalshi"}  # forecastable venues only
    assert result["polymarket"] <= {f"poly_{i}" for i in range(5)}
    assert result["kalshi"] <= {f"kalshi:T{i}" for i in range(5)}
    # No cross-venue leakage: a kalshi id never lands in the polymarket sample.
    assert result["polymarket"].isdisjoint(result["kalshi"])
