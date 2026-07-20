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


def test_read_range_skips_corrupt_partition(tmp_path):
    """A single truncated/0-byte partition (an interrupted write) must not
    abort the whole read -- it's skipped loudly so status/CLV/report keep
    working on the remaining days instead of the orchestrator crash-looping
    on one bad file."""
    store = SnapshotStore(tmp_path)
    store.append([_row("2026-07-02T13:45:00+00:00", "a", mid=0.4)])
    store.append([_row("2026-07-04T13:45:00+00:00", "b", mid=0.6)])
    # Corrupt the middle day the way an interrupted flush would: a 0-byte file.
    (tmp_path / "date=2026-07-03").mkdir(parents=True)
    (tmp_path / "date=2026-07-03" / "snapshots.parquet").write_bytes(b"")

    df = store.read_range(["2026-07-02", "2026-07-03", "2026-07-04"])
    assert set(df.get_column("condition_id").to_list()) == {"a", "b"}  # both good days survive
    # Same resilience on the projected path (which reads schema first).
    proj = store.read_range(["2026-07-02", "2026-07-03", "2026-07-04"],
                            columns=["ts", "condition_id", "mid"])
    assert set(proj.get_column("condition_id").to_list()) == {"a", "b"}


def test_append_is_atomic_no_tmp_left_behind(tmp_path):
    """append() writes via a temp file + atomic rename, so a completed append
    leaves exactly the partition file and no stray .tmp -- the mechanism that
    prevents the 0-byte/half-written partitions read_range now defends against."""
    store = SnapshotStore(tmp_path)
    store.append([_row("2026-07-02T13:45:00+00:00", "a")])
    store.append([_row("2026-07-02T13:50:00+00:00", "b")])  # read-modify-write path
    part_dir = tmp_path / "date=2026-07-02"
    files = sorted(p.name for p in part_dir.iterdir())
    assert files == ["snapshots.parquet"]  # no leftover .tmp
    assert len(store.read_range(["2026-07-02"])) == 2  # both rows intact


def test_read_range_column_projection(tmp_path):
    """Projected reads return exactly the requested columns with identical
    values, and never materialize the ones not asked for (the whole point:
    skipping the heavy bids_json/asks_json blobs)."""
    store = SnapshotStore(tmp_path)
    store.append([
        _row("2026-07-02T13:45:00+00:00", "a", mid=0.4),
        _row("2026-07-02T13:50:00+00:00", "b", mid=0.7),
    ])
    proj = store.read_range(["2026-07-02"], columns=["ts", "condition_id", "mid"])
    assert proj.columns == ["ts", "condition_id", "mid"]
    assert set(proj.get_column("condition_id").to_list()) == {"a", "b"}
    assert proj.filter(proj["condition_id"] == "a")["mid"].item() == 0.4
    # Same rows as an unprojected read -- projection changes width, not length.
    assert len(proj) == len(store.read_range(["2026-07-02"]))


def test_read_range_projection_missing_range_returns_projected_schema(tmp_path):
    """An empty range with a projection returns an empty frame carrying just
    the projected columns, not the full schema -- so callers can index the
    columns they asked for without a KeyError."""
    store = SnapshotStore(tmp_path)
    empty = store.read_range(["2099-01-01"], columns=["ts", "condition_id", "mid"])
    assert empty.is_empty()
    assert empty.columns == ["ts", "condition_id", "mid"]


def test_read_range_projection_tolerates_legacy_partition(tmp_path):
    """Projecting a column an older partition never had (columns are additive
    over time, e.g. `venue`) must not raise -- the absent column comes back
    null via the diagonal concat, exactly as a full read already handles it."""
    import polars as pl

    store = SnapshotStore(tmp_path)
    # A legacy partition with no `venue` column at all.
    legacy_schema = {k: v for k, v in SNAPSHOT_SCHEMA.items() if k != "venue"}
    old = {k: (_row("2026-07-02T13:40:00+00:00", "a").get(k)) for k in legacy_schema}
    path = tmp_path / "date=2026-07-02" / "snapshots.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame([old], schema=legacy_schema).write_parquet(path)
    # A newer partition that does carry `venue`.
    store.append([_row("2026-07-03T13:45:00+00:00", "a")])

    df = store.read_range(["2026-07-02", "2026-07-03"], columns=["ts", "condition_id", "venue"])
    assert "venue" in df.columns
    assert len(df) == 2
    legacy_row = df.filter(df["ts"] == "2026-07-02T13:40:00+00:00")
    assert legacy_row["venue"].item() is None       # absent -> null, not a crash
    new_row = df.filter(df["ts"] == "2026-07-03T13:45:00+00:00")
    assert new_row["venue"].item() == "polymarket"  # default applied on append


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
