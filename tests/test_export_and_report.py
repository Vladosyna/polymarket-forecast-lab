"""`lab export` lossless roundtrip and report rendering on synthetic fixtures."""

from __future__ import annotations

import json

import pytest

from lab.eval.report import render_report
from lab.eval.run import run_eval
from lab.export import EXPORT_FIELDS, export_jsonl
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
