"""`lab export` lossless roundtrip and report rendering on synthetic fixtures."""

from __future__ import annotations

import json

import pytest

from lab.eval.report import render_report
from lab.eval.run import run_eval
from lab.export import EXPORT_FIELDS, export_jsonl
from lab.learn.refit import save_artifact
from lab.store import db
from lab.store.snapshots import SnapshotStore
from lab.util import load_config


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
    cfg["eval"]["bootstrap_iterations"] = 100
    return cfg


def _seed(conn, n_markets: int = 6):
    for i in range(n_markets):
        cid = f"0x{i}"
        conn.execute(
            """INSERT INTO markets (condition_id, slug, question, category, tier,
                                    active, closed, end_date_iso)
               VALUES (?, ?, ?, 'politics', 'liquid', 1, 0, '2026-12-31T00:00:00+00:00')""",
            (cid, f"market-{i}", f"Question {i}?"),
        )
        outcome = float(i % 2)
        db.record_resolution(conn, cid, "2026-07-01T00:00:00+00:00", outcome, False, "gamma")
        for day in (1, 2):
            # Model slightly better than market on every row.
            p_model = 0.8 if outcome else 0.2
            p_market = 0.6 if outcome else 0.4
            db.append_forecast(conn, {
                "ts": f"2026-06-0{day}T00:00:00+00:00",
                "condition_id": cid,
                "model_id": "m_test",
                "p_yes": p_model,
                "p_market_at_ts": p_market,
                "spread_at_ts": 0.02,
            })
    conn.commit()


def test_export_roundtrip_lossless(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed(conn)
    lines = list(export_jsonl(conn))
    # 6 markets x 1 model, latest forecast only.
    assert len(lines) == 6
    for line in lines:
        parsed = json.loads(line)  # the downstream "test stub"
        assert list(parsed.keys()) == EXPORT_FIELDS
        assert parsed["ts"] == "2026-06-02T00:00:00+00:00"  # latest, not first
        assert 0 < parsed["p_yes"] < 1
        assert json.loads(json.dumps(parsed)) == parsed
    conn.close()


def test_eval_and_report_on_fixtures(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed(conn)
    summaries = run_eval(conn, config)
    assert summaries, "expected at least one eval summary"
    all_time = next(s for s in summaries if s["window"] == "all_time")
    assert all_time["result"].skill > 0  # model was built to beat the market

    store = SnapshotStore(config["storage"]["snapshots_dir"])
    path = render_report(conn, store, config)
    html = path.read_text(encoding="utf-8")
    assert "m_test" in html
    assert "INSUFFICIENT DATA" in html  # n=6 markets is far below 200
    conn.close()


def _seed_multi_venue(conn):
    """>=2 venues x >=2 categories, enough rows per stratum for skill_pw to
    qualify (min_stratum_n=30, min_strata=3) -- one price bucket per market
    below plus enough markets to reach 3+ qualifying strata overall."""
    combos = [
        ("polymarket", "economics"), ("polymarket", "politics"),
        ("kalshi", "economics"),
    ]
    prices = [0.2, 0.5, 0.8]  # 3 distinct price buckets -> 3 qualifying strata
    i = 0
    for venue, category in combos:
        for price in prices:
            for _ in range(30):  # >= min_stratum_n per (combo, price bucket)
                cid = f"{venue}:{i}" if venue != "polymarket" else f"0x{i}"
                i += 1
                db.upsert_market(conn, {
                    "condition_id": cid, "venue": venue, "venue_native_id": cid,
                    "slug": None, "question": f"q{i}?", "category": category,
                    "description": "d", "end_date_iso": "2026-12-31T00:00:00+00:00",
                    "token_id_yes": None, "token_id_no": None, "neg_risk": 0,
                    "active": 1, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
                    "tier": "liquid",
                })
                outcome = 1.0 if price >= 0.5 else 0.0
                db.record_resolution(conn, cid, "2026-07-01T00:00:00+00:00", outcome, False, "gamma")
                db.append_forecast(conn, {
                    "ts": "2026-06-01T00:00:00+00:00", "condition_id": cid,
                    "model_id": "m_multi", "p_yes": min(price + 0.1, 0.99),
                    "p_market_at_ts": price, "spread_at_ts": 0.02,
                })
    conn.commit()


def test_report_renders_venue_category_matrix_with_both_estimators(config):
    """Phase 11 acceptance criterion: the report renders the venue x category
    matrix with both the anytime-valid CS and the precision-weighted
    stratified estimator."""
    conn = db.connect(config["storage"]["db_path"])
    _seed_multi_venue(conn)
    run_eval(conn, config)

    store = SnapshotStore(config["storage"]["snapshots_dir"])
    path = render_report(conn, store, config)
    html = path.read_text(encoding="utf-8")

    assert "m_multi" in html
    assert "skill_pw" in html or "insufficient data" in html  # column present either way
    assert "anytime CS" in html
    for venue in ("polymarket", "kalshi"):
        assert venue in html
    for category in ("economics", "politics", "ALL"):
        assert category in html

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM eval_runs WHERE model_id = 'm_multi' AND window_label = 'all_time'"
    )]
    keys = {(r["venue"], r["category"]) for r in rows}
    assert ("polymarket", "economics") in keys
    assert ("polymarket", "politics") in keys
    assert ("kalshi", "economics") in keys
    # The economics/polymarket cell has 90 rows across 3 price buckets of 30
    # each -- enough for skill_pw to actually compute (not "insufficient data").
    econ_poly = next(r for r in rows if (r["venue"], r["category"]) == ("polymarket", "economics"))
    assert econ_poly["skill_pw"] is not None
    assert econ_poly["n_strata_pw"] >= 3
    conn.close()


def test_report_renders_pooling_diagnostics_section(config):
    """Phase 13 acceptance: fitted extremization exponents appear in the
    report with their n (n_members / n_eff)."""
    conn = db.connect(config["storage"]["db_path"])
    _seed(conn, n_markets=2)  # just enough for render_report to run end-to-end
    save_artifact(config, "m4_extremization", {
        "kind": "m4_extremization",
        "categories": {"politics": {"a": 1.8, "rho_bar": 0.4, "n_members_at_fit": 5,
                                    "n_train": 100, "n_validation": 25}},
    })
    save_artifact(config, "m7_extremization", {
        "kind": "m7_extremization",
        "categories": {"_all": {"a": 1.3, "rho_bar": 0.6, "n_members_at_fit": 2.0,
                                "n_train": 80, "n_validation": 20}},
    })

    store = SnapshotStore(config["storage"]["snapshots_dir"])
    path = render_report(conn, store, config)
    html = path.read_text(encoding="utf-8")

    assert "Pooling / extremization diagnostics" in html
    assert "M4 ensemble" in html and "M7 cross-venue" in html
    assert "1.800" in html  # M4's fitted a
    assert "1.300" in html  # M7's fitted a
    conn.close()
