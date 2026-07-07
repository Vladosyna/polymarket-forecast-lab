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


def clv_validity_check(conn, config: dict[str, Any], store: SnapshotStore) -> dict[str, Any]:
    """Phase 17 item 4: is the CLV signal itself trustworthy?

    Correlates per-forecast signed drift against per-forecast realized
    paired-Brier skill contribution (brier_market - brier_model) on the
    sports null control -- a sample that should show ~zero real skill for
    any model (brief section 3/7). If CLV drift correlates with realized
    skill even here, the two are confounded by something other than genuine
    predictive information (e.g. a shared stale-price artifact), and the CLV
    metric must not be trusted lab-wide until investigated.
    """
    from lab.forecast import null_control_ids_by_venue

    nc_ids_by_venue = null_control_ids_by_venue(conn, config)
    all_nc_ids: set[str] = set()
    for ids in nc_ids_by_venue.values():
        all_nc_ids |= ids
    if not all_nc_ids:
        return {"trusted": True, "reason": "no_null_control_data", "n": 0}

    horizon = config["eval"]["clv_horizons_hours"][0]
    placeholders = ",".join("?" * len(all_nc_ids))
    rows = conn.execute(
        f"""
        SELECT f.ts, f.condition_id, f.p_yes, f.p_market_at_ts, r.payout_yes
        FROM forecasts f JOIN resolutions r ON r.condition_id = f.condition_id
        WHERE f.condition_id IN ({placeholders})
        """,
        tuple(all_nc_ids),
    ).fetchall()
    if not rows:
        return {"trusted": True, "reason": "no_resolved_null_control_forecasts", "n": 0}

    all_dates: set[str] = set()
    parsed = []
    for row in rows:
        ts = datetime.fromisoformat(row["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        parsed.append((dict(row), ts))
        all_dates.add(utc_date_str(ts + timedelta(hours=horizon)))
        all_dates.add(utc_date_str(ts + timedelta(hours=horizon) - timedelta(days=1)))
    df = store.read_range(sorted(all_dates))

    drifts: list[float] = []
    skills: list[float] = []
    for f, ts in parsed:
        disagreement = f["p_yes"] - f["p_market_at_ts"]
        if abs(disagreement) < 1e-9:
            continue
        later_mid = _mid_at(df, f["condition_id"], ts + timedelta(hours=horizon))
        if later_mid is None:
            continue
        y = f["payout_yes"]
        drifts.append(float(np.sign(disagreement)) * (later_mid - f["p_market_at_ts"]))
        skills.append((f["p_market_at_ts"] - y) ** 2 - (f["p_yes"] - y) ** 2)

    min_n = config["eval"].get("clv_null_control_min_n", 30)
    if len(drifts) < min_n:
        return {"trusted": True, "reason": "insufficient_n", "n": len(drifts)}

    with np.errstate(invalid="ignore"):
        correlation = float(np.corrcoef(drifts, skills)[0, 1])
    if np.isnan(correlation):  # zero variance in one series -- nothing to detect
        return {"trusted": True, "reason": "zero_variance", "n": len(drifts)}

    max_corr = config["eval"].get("clv_null_control_max_corr", 0.15)
    return {"trusted": abs(correlation) <= max_corr, "correlation": correlation, "n": len(drifts)}


def update_clv_trust_flag(conn, config: dict[str, Any], store: SnapshotStore) -> dict[str, Any]:
    """Run the validity check and persist the result -- only when the check
    actually ran with enough data (an abstention must not silently clear a
    previously-raised untrusted flag, since it re-verified nothing)."""
    from lab.store.db import set_meta

    result = clv_validity_check(conn, config, store)
    if "correlation" in result:
        set_meta(conn, "clv_trusted", "1" if result["trusted"] else "0")
        conn.commit()
    return result
