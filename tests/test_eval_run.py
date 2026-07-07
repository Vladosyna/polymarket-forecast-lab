"""Per-venue x per-category eval grouping (brief section 7/11, Phase 11)."""

from __future__ import annotations

import pytest

from lab.eval.run import ALL_CATEGORIES, run_eval
from lab.store import db
from lab.util import load_config, now_utc


@pytest.fixture()
def config(tmp_path):
    cfg = load_config()
    cfg["storage"] = {
        "db_path": str(tmp_path / "lab.db"),
        "snapshots_dir": str(tmp_path / "snapshots"),
        "models_dir": str(tmp_path / "models"),
        "logs_dir": str(tmp_path / "logs"),
        "reports_dir": str(tmp_path / "reports"),
    }
    return cfg


def _seed(conn, cid, venue, category, n=1):
    db.upsert_market(conn, {
        "condition_id": cid, "venue": venue, "venue_native_id": cid,
        "slug": None, "question": f"q {cid}", "category": category, "description": "d",
        "end_date_iso": "2026-12-31T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "liquid",
    })
    ts = now_utc().isoformat(timespec="seconds")
    db.append_forecast(conn, {
        "ts": ts, "condition_id": cid, "model_id": "m0_market",
        "p_yes": 0.6, "p_market_at_ts": 0.5,
    })
    db.record_resolution(conn, cid, ts, 1.0, False, "gamma")


def test_run_eval_produces_rows_per_venue_and_category(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed(conn, "poly_econ", "polymarket", "economics")
    _seed(conn, "poly_pol", "polymarket", "politics")
    _seed(conn, "kalshi:econ", "kalshi", "economics")
    conn.commit()

    run_eval(conn, config)

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM eval_runs WHERE model_id = 'm0_market' AND window_label = 'all_time'"
    )]
    keys = {(r["venue"], r["category"]) for r in rows}
    assert ("polymarket", "economics") in keys
    assert ("polymarket", "politics") in keys
    assert ("kalshi", "economics") in keys
    # ALL-categories aggregate row exists per venue.
    assert ("polymarket", ALL_CATEGORIES) in keys
    assert ("kalshi", ALL_CATEGORIES) in keys
    # Metaculus/Manifold are not forecastable -> no rows for them.
    assert not any(r["venue"] == "metaculus" for r in rows)
    assert not any(r["venue"] == "manifold" for r in rows)

    # New Phase 11 columns are populated, not left NULL, for a fresh row.
    poly_econ = next(r for r in rows if (r["venue"], r["category"]) == ("polymarket", "economics"))
    assert poly_econ["n_event_clusters"] == 1
    assert poly_econ["cs_lo"] is not None and poly_econ["cs_hi"] is not None
    conn.close()


def _seed_bucket_event(conn, event_id, model_id, ts, category="economics", true_idx=1):
    """One resolved 3-leg negRisk-style bucketed event, bucket-orderable via
    each question's numeric value (Phase 16). Legs are also ordinary resolved
    forecast rows, so they double as `evaluate_model`'s binary paired input."""
    for i, order in enumerate([3.0, 3.5, 4.0]):
        cid = f"{event_id}_{i}"
        db.upsert_market(conn, {
            "condition_id": cid, "venue": "polymarket", "venue_native_id": cid,
            "slug": None, "question": f"Will CPI be {order}%?", "category": category,
            "description": "d", "end_date_iso": "2026-12-31T00:00:00Z",
            "token_id_yes": None, "token_id_no": None, "neg_risk": 1,
            "active": 0, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
            "tier": "liquid", "event_id": event_id,
        })
        payout = 1.0 if i == true_idx else 0.0
        db.append_forecast(conn, {
            "ts": ts, "condition_id": cid, "model_id": model_id,
            "p_yes": 0.6 if i == true_idx else 0.2, "p_market_at_ts": 0.33,
        })
        db.record_resolution(conn, cid, ts, payout, False, "gamma")


def test_eval_runs_rps_columns_populate_only_with_enough_bucketed_events(config):
    """Phase 16 wiring: rps/rps_market on an eval_runs row stay NULL below
    config's min_bucketed_events (20), and populate once that many bucketed
    events exist for that model/venue/category/window."""
    conn = db.connect(config["storage"]["db_path"])
    ts = now_utc().isoformat(timespec="seconds")
    for i in range(19):
        _seed_bucket_event(conn, f"evt{i}", "m0_market", ts)
    conn.commit()

    run_eval(conn, config)
    row = conn.execute(
        "SELECT rps, rps_market FROM eval_runs WHERE model_id='m0_market' "
        "AND window_label='all_time' AND venue='polymarket' AND category='economics' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["rps"] is None
    assert row["rps_market"] is None

    _seed_bucket_event(conn, "evt19", "m0_market", ts)  # 20th event crosses the threshold
    conn.commit()
    run_eval(conn, config)
    row = conn.execute(
        "SELECT rps, rps_market FROM eval_runs WHERE model_id='m0_market' "
        "AND window_label='all_time' AND venue='polymarket' AND category='economics' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["rps"] is not None
    assert row["rps_market"] is not None
    conn.close()
