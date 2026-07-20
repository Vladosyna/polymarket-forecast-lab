"""CLV drift signal: hand-computed values and Phase 17 item 5's gap-aware
exclusion (a drift window overlapping a recorded collection gap is dropped
and counted, not silently treated as ordinary missing data).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lab.eval.clv import (
    CLV_SNAPSHOT_COLUMNS,
    clv_dates,
    clv_drift,
    clv_validity_check,
    update_clv_trust_flag,
)
from lab.store import db
from lab.store.db import get_meta
from lab.store.snapshots import SnapshotStore
from lab.util import load_config


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


def test_clv_drift_shared_snapshots_matches_internal_read(tmp_path):
    """Passing a pre-loaded `snapshots=` frame (the report render's read-once
    optimization) yields byte-identical results to letting clv_drift read the
    store itself -- and `clv_dates` covers exactly the partitions it needs."""
    store = SnapshotStore(tmp_path / "snapshots")
    ts = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    _seed_mid(store, ts + timedelta(hours=24), "0x1", 0.65)
    forecasts = [{"ts": ts.isoformat(timespec="seconds"), "condition_id": "0x1",
                 "model_id": "m1", "p_yes": 0.7, "p_market_at_ts": 0.5}]

    # Read once for the union of dates clv_dates reports, then share it.
    shared = store.read_range(sorted(clv_dates(forecasts, [24])), columns=CLV_SNAPSHOT_COLUMNS)
    out_shared = clv_drift(forecasts, store, [24], snapshots=shared)
    out_internal = clv_drift(forecasts, store, [24])
    assert out_shared == out_internal
    assert out_shared[24]["mean_signed_drift"] == pytest.approx(0.15)


# --- clv_validity_check / update_clv_trust_flag (Phase 17 item 4) ---------

def _config(tmp_path, **eval_overrides):
    cfg = load_config()
    cfg["storage"] = {
        "db_path": str(tmp_path / "lab.db"),
        "snapshots_dir": str(tmp_path / "snapshots"),
        "models_dir": str(tmp_path / "models"),
        "logs_dir": str(tmp_path / "logs"),
        "reports_dir": str(tmp_path / "reports"),
    }
    cfg["eval"] = {**cfg["eval"], "clv_null_control_min_n": 2, **eval_overrides}
    return cfg


def _seed_null_control_case(conn, store, cid, ts, p_yes, p_market, payout_yes, later_mid, horizon):
    conn.execute(
        """INSERT OR IGNORE INTO markets (condition_id, question, category, venue, tier, active, closed)
           VALUES (?, ?, 'sports', 'polymarket', 'liquid', 1, 1)""",
        (cid, f"Q {cid}?"),
    )
    ts_iso = ts.isoformat(timespec="seconds")
    db.append_forecast(conn, {"ts": ts_iso, "condition_id": cid, "model_id": "m0_market",
                             "p_yes": p_yes, "p_market_at_ts": p_market})
    db.record_resolution(conn, cid, ts_iso, payout_yes, False, "gamma")
    store.append([{
        "ts": (ts + timedelta(hours=horizon)).isoformat(timespec="seconds"),
        "condition_id": cid, "mid": later_mid,
    }])
    conn.commit()


# Shared across both fixtures below: y=1, p_market_at_ts=0.5 for every case,
# only p_yes varies -> skill = 0.25 - (p_yes-1)**2, hand-computed:
#   p_yes=0.8 -> skill= 0.21 (disagreement +0.3, sign +1)
#   p_yes=0.6 -> skill= 0.09 (disagreement +0.1, sign +1)
#   p_yes=0.1 -> skill=-0.56 (disagreement -0.4, sign -1)
#   p_yes=0.4 -> skill=-0.11 (disagreement -0.1, sign -1)
_CASES = [("0x1", 0.8, 0.21, 1), ("0x2", 0.6, 0.09, 1), ("0x3", 0.1, -0.56, -1), ("0x4", 0.4, -0.11, -1)]


def test_clv_validity_check_stays_trusted_when_drift_has_no_variance(tmp_path):
    """Uncorrelated (here: literally constant) drift across varying realized
    skill -- there is nothing for a correlation to detect, so the check must
    not raise a false alarm."""
    config = _config(tmp_path)
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    horizon = config["eval"]["clv_horizons_hours"][0]

    for cid, p_yes, _skill, sign in _CASES:
        later_mid = 0.5 + 0.1 * sign  # constant drift = +0.1 regardless of sign/skill
        _seed_null_control_case(conn, store, cid, ts, p_yes, 0.5, 1.0, later_mid, horizon)

    result = clv_validity_check(conn, config, store)
    assert result["n"] == 4
    assert result["trusted"] is True
    assert result["reason"] == "zero_variance"
    conn.close()


def test_clv_validity_check_flags_untrusted_when_drift_tracks_skill(tmp_path):
    """Drift set to an exact positive multiple of realized skill (perfect
    correlation) on the null control -- must be flagged untrusted."""
    config = _config(tmp_path)
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    horizon = config["eval"]["clv_horizons_hours"][0]

    for cid, p_yes, skill, sign in _CASES:
        drift = 0.3 * skill
        later_mid = 0.5 + sign * drift  # drift = sign*(later_mid-0.5) = drift by construction
        _seed_null_control_case(conn, store, cid, ts, p_yes, 0.5, 1.0, later_mid, horizon)

    result = clv_validity_check(conn, config, store)
    assert result["n"] == 4
    assert result["correlation"] == pytest.approx(1.0)
    assert result["trusted"] is False
    conn.close()


def test_update_clv_trust_flag_persists_and_abstention_does_not_clear_it(tmp_path):
    config = _config(tmp_path)
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    horizon = config["eval"]["clv_horizons_hours"][0]

    for cid, p_yes, skill, sign in _CASES:
        drift = 0.3 * skill
        later_mid = 0.5 + sign * drift
        _seed_null_control_case(conn, store, cid, ts, p_yes, 0.5, 1.0, later_mid, horizon)

    result = update_clv_trust_flag(conn, config, store)
    assert result["trusted"] is False
    assert get_meta(conn, "clv_trusted") == "0"

    # A later run with insufficient data (an abstention) must not silently
    # clear the flag -- it re-verified nothing.
    empty_config = _config(tmp_path, clv_null_control_min_n=1000)
    abstained = update_clv_trust_flag(conn, empty_config, store)
    assert "correlation" not in abstained
    assert get_meta(conn, "clv_trusted") == "0"
    conn.close()
