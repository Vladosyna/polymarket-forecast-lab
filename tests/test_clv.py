"""CLV drift signal: hand-computed values and Phase 17 item 5's gap-aware
exclusion (a drift window overlapping a recorded collection gap is dropped
and counted, not silently treated as ordinary missing data).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lab.eval.clv import clv_drift
from lab.store.snapshots import SnapshotStore


def _seed_mid(store, ts: datetime, condition_id: str, mid: float) -> None:
    store.append([{
        "ts": ts.isoformat(timespec="seconds"), "condition_id": condition_id,
        "token_id_yes": "tok", "best_bid": mid - 0.01, "best_ask": mid + 0.01,
        "mid": mid, "spread": 0.02,
    }])


def test_clv_drift_hand_computed(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    ts = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    _seed_mid(store, ts + timedelta(hours=24), "0x1", 0.65)
    forecasts = [{"ts": ts.isoformat(timespec="seconds"), "condition_id": "0x1",
                 "model_id": "m1", "p_yes": 0.7, "p_market_at_ts": 0.5}]
    out = clv_drift(forecasts, store, [24])
    # disagreement=0.2 -> sign=+1; drift = +1 * (0.65 - 0.5) = 0.15.
    assert out[24]["n"] == 1
    assert out[24]["mean_signed_drift"] == pytest.approx(0.15)
    assert out[24]["dropped_for_gap"] == 0


def test_clv_drift_excludes_and_counts_windows_overlapping_a_gap(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    ts_a = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    ts_b = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)
    # Both forecasts have a real snapshot near their target time -- proves
    # forecast A is excluded because of the gap, not for lack of data.
    _seed_mid(store, ts_a + timedelta(hours=24), "0x1", 0.65)
    _seed_mid(store, ts_b + timedelta(hours=24), "0x2", 0.65)
    forecasts = [
        {"ts": ts_a.isoformat(timespec="seconds"), "condition_id": "0x1",
         "model_id": "m1", "p_yes": 0.7, "p_market_at_ts": 0.5},
        {"ts": ts_b.isoformat(timespec="seconds"), "condition_id": "0x2",
         "model_id": "m1", "p_yes": 0.7, "p_market_at_ts": 0.5},
    ]
    # A synthetic outage overlapping only forecast A's [ts, ts+24h] window.
    gaps = [(ts_a + timedelta(hours=10), ts_a + timedelta(hours=11))]

    out = clv_drift(forecasts, store, [24], gap_windows=gaps)
    assert out[24]["n"] == 1                 # only forecast B's drift counted
    assert out[24]["mean_signed_drift"] == pytest.approx(0.15)
    assert out[24]["dropped_for_gap"] == 1    # forecast A excluded, not silently dropped

    # Without the gap, both would have contributed (proves the exclusion is
    # specifically the gap check, not some other filter).
    out_no_gap = clv_drift(forecasts, store, [24])
    assert out_no_gap[24]["n"] == 2
    assert out_no_gap[24]["dropped_for_gap"] == 0
