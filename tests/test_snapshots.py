"""Parquet snapshot store: dedup on (ts, condition_id), bucket flooring."""

from __future__ import annotations

from datetime import datetime, timezone

from lab.store.snapshots import SNAPSHOT_SCHEMA, SnapshotStore, floor_ts_bucket


def _row(ts: str, cid: str, mid: float = 0.5) -> dict:
    return {
        "ts": ts,
        "condition_id": cid,
        "token_id_yes": "tok-" + cid,
        "best_bid": mid - 0.01,
        "best_ask": mid + 0.01,
        "mid": mid,
        "spread": 0.02,
        "bid_depth_usd": 1000.0,
        "ask_depth_usd": 900.0,
        "last_trade_price": None,
    }


def test_floor_ts_bucket():
    ts = datetime(2026, 7, 2, 13, 47, 33, tzinfo=timezone.utc)
    assert floor_ts_bucket(ts, 5) == "2026-07-02T13:45:00+00:00"
    assert floor_ts_bucket(ts, 60) == "2026-07-02T13:00:00+00:00"


def test_append_dedups_on_ts_and_market(tmp_path):
    store = SnapshotStore(tmp_path)
    ts = "2026-07-02T13:45:00+00:00"

    assert store.append([_row(ts, "a"), _row(ts, "b")]) == 2
    # Restart replays the same bucket: nothing new may be written.
    assert store.append([_row(ts, "a", mid=0.9), _row(ts, "b")]) == 0
    # New bucket writes normally.
    assert store.append([_row("2026-07-02T13:50:00+00:00", "a")]) == 1

    df = store.read_range(["2026-07-02"])
    assert len(df) == 3
    # Original row survived the duplicate with a different mid.
    kept = df.filter((df["ts"] == ts) & (df["condition_id"] == "a"))
    assert kept["mid"].item() == 0.5


def test_append_straddles_midnight(tmp_path):
    store = SnapshotStore(tmp_path)
    rows = [
        _row("2026-07-02T23:55:00+00:00", "a"),
        _row("2026-07-03T00:00:00+00:00", "a"),
    ]
    assert store.append(rows) == 2
    assert len(store.read_range(["2026-07-02"])) == 1
    assert len(store.read_range(["2026-07-03"])) == 1
    assert len(store.read_range(["2026-07-02", "2026-07-03"])) == 2


def test_latest_per_market(tmp_path):
    store = SnapshotStore(tmp_path)
    store.append([
        _row("2026-07-02T13:45:00+00:00", "a", mid=0.4),
        _row("2026-07-02T13:50:00+00:00", "a", mid=0.6),
    ])
    latest = store.latest_per_market(["2026-07-02"])
    assert latest["mid"].item() == 0.6


def test_depth_columns_roundtrip(tmp_path):
    import json

    store = SnapshotStore(tmp_path)
    row = _row("2026-07-02T13:45:00+00:00", "a")
    row["bids_json"] = json.dumps([[0.49, 100.0], [0.48, 250.0]])
    row["asks_json"] = json.dumps([[0.51, 80.0], [0.53, 300.0]])
    store.append([row])
    df = store.read_range(["2026-07-02"])
    bids = json.loads(df["bids_json"].item())
    assert bids == [[0.49, 100.0], [0.48, 250.0]]  # best-first, lossless


def test_old_partitions_without_depth_still_merge(tmp_path):
    """Partitions written before the depth columns existed read as nulls."""
    import polars as pl

    store = SnapshotStore(tmp_path)
    # Simulate a pre-depth partition: same layout minus the new columns.
    old = _row("2026-07-02T13:40:00+00:00", "a")
    old.pop("bids_json", None), old.pop("asks_json", None)
    legacy_schema = {k: v for k, v in SNAPSHOT_SCHEMA.items()
                     if k not in ("bids_json", "asks_json")}
    path = tmp_path / "date=2026-07-02" / "snapshots.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame([old], schema=legacy_schema).write_parquet(path)

    # New-schema rows append into the same partition without breaking.
    assert store.append([_row("2026-07-02T13:45:00+00:00", "a")]) == 1
    df = store.read_range(["2026-07-02"]).sort("ts")
    assert len(df) == 2
    assert df["bids_json"][0] is None      # legacy row -> null depth
    assert df["mid"][0] == 0.5             # legacy data intact
