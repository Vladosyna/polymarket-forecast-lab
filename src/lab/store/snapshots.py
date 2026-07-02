"""Parquet snapshot store, partitioned data/snapshots/date=YYYY-MM-DD/*.parquet.

Dedup key is (ts_bucket, condition_id): ts is floored to the tier's cadence
bucket before writing, and appends drop rows whose key already exists in the
partition -- restart-safe by construction (guardrail 7).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from lab.util import PROJECT_ROOT

log = logging.getLogger(__name__)

SNAPSHOT_SCHEMA = {
    "ts": pl.String,            # ISO-8601 UTC, floored to cadence bucket
    "condition_id": pl.String,
    "token_id_yes": pl.String,
    "best_bid": pl.Float64,
    "best_ask": pl.Float64,
    "mid": pl.Float64,
    "spread": pl.Float64,
    "bid_depth_usd": pl.Float64,
    "ask_depth_usd": pl.Float64,
    "last_trade_price": pl.Float64,
}


def floor_ts_bucket(ts: datetime, bucket_minutes: int) -> str:
    """Floor a UTC datetime to the cadence bucket, ISO-8601."""
    minute = (ts.minute // bucket_minutes) * bucket_minutes if bucket_minutes < 60 else 0
    floored = ts.replace(minute=minute, second=0, microsecond=0)
    if bucket_minutes >= 60:
        hour = (ts.hour // (bucket_minutes // 60)) * (bucket_minutes // 60)
        floored = floored.replace(hour=hour)
    return floored.isoformat(timespec="seconds")


class SnapshotStore:
    def __init__(self, snapshots_dir: str | Path) -> None:
        base = Path(snapshots_dir)
        self.base = base if base.is_absolute() else PROJECT_ROOT / base
        self.base.mkdir(parents=True, exist_ok=True)

    def _partition(self, date_str: str) -> Path:
        return self.base / f"date={date_str}" / "snapshots.parquet"

    def append(self, rows: list[dict]) -> int:
        """Append rows, deduplicating on (ts, condition_id). Returns rows written."""
        if not rows:
            return 0
        df = pl.DataFrame(rows, schema=SNAPSHOT_SCHEMA)
        written = 0
        # A batch can straddle midnight; group rows into their date partitions.
        df = df.with_columns(pl.col("ts").str.slice(0, 10).alias("_date"))
        for (date_str,), part in df.partition_by("_date", as_dict=True).items():
            part = part.drop("_date")
            path = self._partition(date_str)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                existing = pl.read_parquet(path)
                keys = existing.select("ts", "condition_id")
                part = part.join(keys, on=["ts", "condition_id"], how="anti")
                if part.is_empty():
                    continue
                merged = pl.concat([existing, part], how="vertical")
            else:
                merged = part
            merged.write_parquet(path)
            written += len(part)
        return written

    def read_range(self, dates: list[str]) -> pl.DataFrame:
        """Read snapshot partitions for the given YYYY-MM-DD dates (missing ones skipped)."""
        frames = [
            pl.read_parquet(self._partition(d)) for d in dates if self._partition(d).exists()
        ]
        if not frames:
            return pl.DataFrame(schema=SNAPSHOT_SCHEMA)
        return pl.concat(frames, how="vertical")

    def latest_per_market(self, dates: list[str]) -> pl.DataFrame:
        df = self.read_range(dates)
        if df.is_empty():
            return df
        return df.sort("ts").group_by("condition_id").last()


def utc_date_str(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
