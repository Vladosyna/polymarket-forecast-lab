"""CLV-style early signal: does the market move toward the model's view?

For each forecast, take the market mid at t+24h / t+72h and measure drift
signed in the direction of the model's disagreement at freeze time:
  signed_drift = sign(p_model - p_market_at_ts) * (mid_later - p_market_at_ts)
Positive mean drift = the model tends to be early, not wrong.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import polars as pl

from lab.store.snapshots import SnapshotStore, utc_date_str

# condition_id -> (sorted ts strings, parallel mid values). Built once per
# snapshot frame by `_build_mid_index` and reused across every `_mid_at` call
# against that frame -- was a fresh `df.filter(condition_id ==)` linear scan
# per call (up to ~30k calls/report render); a report-scale frame made this
# the single largest cost after gap_windows. See tests/test_clv.py for the
# differential proof against the prior per-call-filter implementation.
_MidIndex = dict[str, tuple[list[str], list[float]]]


def _build_mid_index(df: pl.DataFrame) -> _MidIndex:
    if df.is_empty():
        return {}
    grouped = df.sort("ts").group_by("condition_id").agg([pl.col("ts"), pl.col("mid")])
    return {row["condition_id"]: (row["ts"], row["mid"]) for row in grouped.iter_rows(named=True)}


def _mid_at(index: _MidIndex, condition_id: str, target_ts: datetime,
            tolerance_hours: float = 3.0) -> float | None:
    entry = index.get(condition_id)
    if entry is None:
        return None
    ts_list, mid_list = entry
    target = target_ts.isoformat(timespec="seconds")
    lo = (target_ts - timedelta(hours=tolerance_hours)).isoformat(timespec="seconds")
    hi = (target_ts + timedelta(hours=tolerance_hours)).isoformat(timespec="seconds")
    # [i, j) = indices with lo <= ts <= hi (ts_list is sorted, per-market
    # timestamps are unique -- SnapshotStore.append dedups on (ts, condition_id)).
    i = bisect_left(ts_list, lo)
    j = bisect_right(ts_list, hi)
    if i >= j:
        return None
    # The minimizer of |ts - target| within a sorted range is always the
    # insertion point or its immediate predecessor -- no need to scan the rest.
    k = bisect_left(ts_list, target, i, j)
    best_idx, best_dist = None, None
    for idx in (k - 1, k):
        if i <= idx < j:
            ts_dt = datetime.fromisoformat(ts_list[idx])
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            dist = abs((ts_dt - target_ts).total_seconds())
            # Strict '<': on an exact tie, keep the earlier (lower idx)
            # candidate -- k-1 is tried first, so this prefers it. The prior
            # per-call implementation used polars' unstable sort here, whose
            # tie-break was already undefined; this makes it deterministic
            # rather than changing any currently-reproducible output.
            if best_dist is None or dist < best_dist:
                best_idx, best_dist = idx, dist
    return float(mid_list[best_idx]) if best_idx is not None else None


def _overlaps_gap(window_start: datetime, window_end: datetime,
                  gaps: list[tuple[datetime, datetime]]) -> bool:
    return any(window_start < g_end and g_start < window_end for g_start, g_end in gaps)


# `_mid_at` only ever reads these three columns; every CLV read projects to
# them so a multi-week drift window never materializes the order-book blobs.
CLV_SNAPSHOT_COLUMNS = ["ts", "condition_id", "mid"]


def clv_dates(forecasts: list[dict], horizons_hours: list[int]) -> set[str]:
    """The set of YYYY-MM-DD snapshot partitions `clv_drift` needs for these
    forecasts and horizons. Exposed so a caller scoring many model_ids over the
    same recent window can union them and read the store ONCE, then hand the
    result to each `clv_drift` via `snapshots=` -- avoiding one full read per
    model (the report render's dominant redundant-I/O cost)."""
    dates: set[str] = set()
    for f in forecasts:
        ts = datetime.fromisoformat(f["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        for h in horizons_hours:
            dates.add(utc_date_str(ts + timedelta(hours=h)))
            dates.add(utc_date_str(ts + timedelta(hours=h) - timedelta(days=1)))
    return dates


def clv_drift(forecasts: list[dict], store: SnapshotStore, horizons_hours: list[int],
              gap_windows: list[tuple[datetime, datetime]] | None = None,
              snapshots: pl.DataFrame | None = None,
              ) -> dict[int, dict[str, float]]:
    """forecasts: rows with ts, condition_id, model_id, p_yes, p_market_at_ts.

    `gap_windows` (Phase 17 item 5, `collect/status.py::gap_windows`): a
    forecast's drift window [ts, ts+horizon] overlapping any recorded
    collection gap is excluded from the mean and counted separately
    (`dropped_for_gap`) rather than silently folded into "no data" -- a gap
    is a known-missing measurement, not the same failure mode as no snapshot
    existing near the target time at all.

    `snapshots`: an already-loaded (ts, condition_id, mid) frame covering at
    least this forecast set's `clv_dates`. When given, the internal store read
    is skipped -- callers scoring many models over one window read once and
    share it (see `clv_dates`). A superset frame is fine: `_mid_at` filters by
    condition_id and ts, so extra rows are simply ignored.
    """
    if not forecasts:
        return {}
    gaps = gap_windows or []
    parsed = []
    for f in forecasts:
        ts = datetime.fromisoformat(f["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        parsed.append((f, ts))
    if snapshots is not None:
        df = snapshots
    else:
        df = store.read_range(sorted(clv_dates(forecasts, horizons_hours)),
                              columns=CLV_SNAPSHOT_COLUMNS)
    mid_index = _build_mid_index(df)

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
            later_mid = _mid_at(mid_index, f["condition_id"], window_end)
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
    df = store.read_range(sorted(all_dates), columns=CLV_SNAPSHOT_COLUMNS)
    mid_index = _build_mid_index(df)

    drifts: list[float] = []
    skills: list[float] = []
    for f, ts in parsed:
        disagreement = f["p_yes"] - f["p_market_at_ts"]
        if abs(disagreement) < 1e-9:
            continue
        later_mid = _mid_at(mid_index, f["condition_id"], ts + timedelta(hours=horizon))
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
