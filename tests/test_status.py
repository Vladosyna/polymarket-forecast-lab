"""`lab status` gap detection on fixture data with a synthetic gap."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from lab.collect.status import gap_windows, gather_status, snapshot_gaps
from lab.store import db
from lab.store.snapshots import SNAPSHOT_SCHEMA, SnapshotStore, floor_ts_bucket
from lab.util import load_config


def _frame(timestamps: list[str]) -> pl.DataFrame:
    rows = [
        {
            "ts": ts, "condition_id": "a", "token_id_yes": "tok",
            "best_bid": 0.49, "best_ask": 0.51, "mid": 0.5, "spread": 0.02,
            "bid_depth_usd": 1000.0, "ask_depth_usd": 900.0, "last_trade_price": None,
        }
        for ts in timestamps
    ]
    return pl.DataFrame(rows, schema=SNAPSHOT_SCHEMA)


def test_flags_synthetic_gap():
    start = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)  # 12 buckets at 5-min cadence
    buckets = [start + timedelta(minutes=5 * i) for i in range(12)]
    # Drop two buckets in the middle: the synthetic gap.
    present = [b for i, b in enumerate(buckets) if i not in (5, 6)]
    df = _frame([floor_ts_bucket(b, 5) for b in present])

    assert snapshot_gaps(df, ["a"], 5, start, end) == 2


def test_full_coverage_has_no_gaps():
    start = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    df = _frame([floor_ts_bucket(start + timedelta(minutes=5 * i), 5) for i in range(12)])
    assert snapshot_gaps(df, ["a"], 5, start, end) == 0


def test_gap_windows_returns_the_actual_missing_intervals():
    """Phase 17 item 5: snapshot_gaps' count is now derived FROM gap_windows,
    which callers like eval/clv.py's gap-aware drift need the actual
    intervals from, not just a count."""
    start = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)  # 12 buckets at 5-min cadence
    buckets = [start + timedelta(minutes=5 * i) for i in range(12)]
    present = [b for i, b in enumerate(buckets) if i not in (5, 6)]
    df = _frame([floor_ts_bucket(b, 5) for b in present])

    windows = gap_windows(df, ["a"], 5, start, end)
    assert windows == [
        (start + timedelta(minutes=25), start + timedelta(minutes=30)),
        (start + timedelta(minutes=30), start + timedelta(minutes=35)),
    ]
    assert snapshot_gaps(df, ["a"], 5, start, end) == len(windows) == 2


def test_no_tracked_markets_reports_zero():
    assert snapshot_gaps(_frame([]), [], 5,
                         datetime(2026, 7, 2, tzinfo=timezone.utc),
                         datetime(2026, 7, 2, 1, tzinfo=timezone.utc)) == 0


# --- per-venue status lines (Phase 10) --------------------------------------

def test_gather_status_reports_per_venue_health(tmp_path):
    config = load_config()
    config = {
        **config,
        "storage": {
            **config["storage"],
            "db_path": str(tmp_path / "lab.db"),
            "snapshots_dir": str(tmp_path / "snapshots"),
        },
    }
    conn = db.connect(config["storage"]["db_path"])
    db.upsert_market(conn, {
        "condition_id": "kalshi:T1", "venue": "kalshi", "venue_native_id": "T1",
        "slug": None, "question": "q", "category": "economics", "description": "d",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 0, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "tail",
    })
    db.record_resolution(conn, "kalshi:T1", "2026-01-02T00:00:00Z", 1.0, False, "kalshi")
    db.upsert_market(conn, {
        "condition_id": "kalshi:T2", "venue": "kalshi", "venue_native_id": "T2",
        "slug": None, "question": "q2", "category": "economics", "description": "d2",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 0, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "tail",
    })  # closed, unresolved -- should count toward kalshi's closed_unresolved
    db.upsert_market(conn, {
        "condition_id": "manifold:M1", "venue": "manifold", "venue_native_id": "M1",
        "slug": None, "question": "q3", "category": "unknown", "description": None,
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 0, "liquidity_num": None, "volume_num": 5.0,
        "tier": "ignored",
    })
    conn.commit()
    conn.close()

    store = SnapshotStore(config["storage"]["snapshots_dir"])
    store.append([{
        "ts": floor_ts_bucket(datetime.now(timezone.utc), 5), "condition_id": "kalshi:T1",
        "token_id_yes": None, "best_bid": 0.5, "best_ask": 0.52, "mid": 0.51, "spread": 0.02,
        "bid_depth_usd": None, "ask_depth_usd": None, "last_trade_price": None,
        "bids_json": None, "asks_json": None, "venue": "kalshi",
    }])

    status = gather_status(config)
    assert status["venues"]["kalshi"]["markets"] == 2
    assert status["venues"]["kalshi"]["resolutions"] == 1
    assert status["venues"]["kalshi"]["closed_unresolved"] == 1
    assert status["venues"]["kalshi"]["last_snapshot_age_min"] is not None
    assert status["venues"]["metaculus"]["markets"] == 0
    assert status["venues"]["metaculus"]["last_snapshot_age_min"] is None
    assert status["venues"]["manifold"]["markets"] == 1
    assert "last_snapshot_age_min" not in status["venues"]["manifold"]
