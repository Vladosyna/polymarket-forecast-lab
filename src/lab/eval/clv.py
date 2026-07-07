"""CLV-style early signal: does the market move toward the model's view?

For each forecast, take the market mid at t+24h / t+72h and measure drift
signed in the direction of the model's disagreement at freeze time:
  signed_drift = sign(p_model - p_market_at_ts) * (mid_later - p_market_at_ts)
Positive mean drift = the model tends to be early, not wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import polars as pl

from lab.store.snapshots import SnapshotStore, utc_date_str


def _mid_at(df: pl.DataFrame, condition_id: str, target_ts: datetime,
            tolerance_hours: float = 3.0) -> float | None:
    subset = df.filter(pl.col("condition_id") == condition_id)
    if subset.is_empty():
        return None
    target = target_ts.isoformat(timespec="seconds")
    lo = (target_ts - timedelta(hours=tolerance_hours)).isoformat(timespec="seconds")
    hi = (target_ts + timedelta(hours=tolerance_hours)).isoformat(timespec="seconds")
    window = subset.filter((pl.col("ts") >= lo) & (pl.col("ts") <= hi))
    if window.is_empty():
        return None
    closest = window.with_columns(
        (pl.col("ts").str.to_datetime(time_zone="UTC")
         - pl.lit(target_ts)).abs().alias("_dist")
    ).sort("_dist")
    return float(closest["mid"][0])


def _overlaps_gap(window_start: datetime, window_end: datetime,
                  gaps: list[tuple[datetime, datetime]]) -> bool:
    return any(window_start < g_end and g_start < window_end for g_start, g_end in gaps)


def clv_drift(forecasts: list[dict], store: SnapshotStore, horizons_hours: list[int],
              gap_windows: list[tuple[datetime, datetime]] | None = None,
              ) -> dict[int, dict[str, float]]:
    """forecasts: rows with ts, condition_id, model_id, p_yes, p_market_at_ts.

    `gap_windows` (Phase 17 item 5, `collect/status.py::gap_windows`): a
    forecast's drift window [ts, ts+horizon] overlapping any recorded
    collection gap is excluded from the mean and counted separately
    (`dropped_for_gap`) rather than silently folded into "no data" -- a gap
    is a known-missing measurement, not the same failure mode as no snapshot
    existing near the target time at all.
    """
    if not forecasts:
        return {}
    gaps = gap_windows or []
    all_dates: set[str] = set()
    parsed = []
    for f in forecasts:
        ts = datetime.fromisoformat(f["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        parsed.append((f, ts))
        for h in horizons_hours:
            all_dates.add(utc_date_str(ts + timedelta(hours=h)))
            all_dates.add(utc_date_str(ts + timedelta(hours=h) - timedelta(days=1)))
    df = store.read_range(sorted(all_dates))

    out: dict[int, dict[str, float]] = {}
    for h in horizons_hours:
        drifts: list[float] = []
        dropped_for_gap = 0
        for f, ts in parsed:
            disagreement = f["p_yes"] - f["p_market_at_ts"]
            if abs(disagreement) < 1e-9:
                continue
            window_end = ts + timedelta(hours=h)
            if gaps and _overlaps_gap(ts, window_end, gaps):
                dropped_for_gap += 1
                continue
            later_mid = _mid_at(df, f["condition_id"], window_end)
            if later_mid is None:
                continue
            drifts.append(float(np.sign(disagreement)) * (later_mid - f["p_market_at_ts"]))
        out[h] = {
            "n": len(drifts),
            "mean_signed_drift": float(np.mean(drifts)) if drifts else float("nan"),
            "dropped_for_gap": dropped_for_gap,
        }
    return out
