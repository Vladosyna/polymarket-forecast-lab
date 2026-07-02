"""`lab status` gap detection on fixture data with a synthetic gap."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from lab.collect.status import snapshot_gaps
from lab.store.snapshots import SNAPSHOT_SCHEMA, floor_ts_bucket


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


def test_no_tracked_markets_reports_zero():
    assert snapshot_gaps(_frame([]), [], 5,
                         datetime(2026, 7, 2, tzinfo=timezone.utc),
                         datetime(2026, 7, 2, 1, tzinfo=timezone.utc)) == 0
