"""`lab export --paper` (Phase 15): replication dataset + manifest."""

from __future__ import annotations

import json

import pytest

from lab.export import EXPORT_PAPER_FIELDS, export_paper_jsonl, paper_export_manifest
from lab.store import db
from lab.util import PROJECT_ROOT, load_config


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


def _seed(conn, n_markets: int = 4, disputed: bool = False):
    for i in range(n_markets):
        cid = f"0x{i}"
        conn.execute(
            """INSERT INTO markets (condition_id, slug, question, category, tier,
                                    active, closed, end_date_iso, venue, event_id)
               VALUES (?, ?, ?, 'politics', 'liquid', 1, 1, '2026-12-31T00:00:00+00:00',
                       'polymarket', ?)""",
            (cid, f"market-{i}", f"Question {i}?", f"evt-{i}"),
        )
        outcome = float(i % 2)
        db.record_resolution(conn, cid, "2026-07-01T00:00:00+00:00", outcome, disputed, "gamma")
        db.append_forecast(conn, {
            "ts": "2026-06-01T00:00:00+00:00",
            "condition_id": cid,
            "model_id": "m_test",
            "p_yes": 0.7 if outcome else 0.3,
            "p_market_at_ts": 0.5,
            "spread_at_ts": 0.02,
        })
    conn.commit()


def test_paper_export_roundtrip_lossless(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed(conn)
    lines = list(export_paper_jsonl(conn))
    assert len(lines) == 4
    for line in lines:
        parsed = json.loads(line)
        assert list(parsed.keys()) == EXPORT_PAPER_FIELDS
        assert parsed["model_id"] == "m_test"
        assert parsed["venue"] == "polymarket"
        assert parsed["m3_randomized"] == 0
        assert parsed["m3_random_seed"] is None
        assert json.loads(json.dumps(parsed)) == parsed
    conn.close()


def test_paper_export_excludes_disputed_resolutions(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed(conn, n_markets=2, disputed=True)
    lines = list(export_paper_jsonl(conn))
    assert lines == []  # every seeded resolution here is disputed
    conn.close()


def test_paper_export_manifest_shape(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed(conn)
    rows = list(export_paper_jsonl(conn))
    manifest = paper_export_manifest(conn, len(rows))

    assert manifest["row_count"] == 4
    assert manifest["fields"] == EXPORT_PAPER_FIELDS
    assert isinstance(manifest["code_version"], str) and len(manifest["code_version"]) == 12
    assert manifest["schema_version"] == db.SCHEMA_VERSION
    assert "generated_at" in manifest
    conn.close()


def test_paper_export_code_version_stable_across_two_calls(config):
    """The hash is content-based, not a fixed literal -- assert stability
    across two calls in the same process, not a hardcoded value that would
    break on every future code change."""
    conn = db.connect(config["storage"]["db_path"])
    m1 = paper_export_manifest(conn, 0)
    m2 = paper_export_manifest(conn, 0)
    assert m1["code_version"] == m2["code_version"]
    conn.close()


def test_paper_export_schema_doc_mentions_every_field():
    doc = (PROJECT_ROOT / "docs" / "paper_export_schema.md").read_text(encoding="utf-8")
    for field in EXPORT_PAPER_FIELDS:
        assert field in doc, f"{field} missing from paper_export_schema.md"
