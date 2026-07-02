"""Phase 7: challenger promotion mechanics and post-mortems on fixtures."""

from __future__ import annotations

import json

import pytest

from lab.learn.loop import fit_m3_aggregator, maybe_promote, promote
from lab.learn.postmortem import lessons_digest, run_postmortems, select_candidates
from lab.learn.refit import load_active_artifact, save_artifact
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


def test_better_challenger_promoted_worse_rejected(config):
    champion = {"kind": "x", "quality": 0.20}
    path = save_artifact(config, "x", champion, promote=True)
    assert load_active_artifact(config, "x")["version"] == 1

    # Score = the artifact's own quality field (lower is better).
    score = lambda art: art["quality"]

    better = {"kind": "x", "quality": 0.10}
    better_path = save_artifact(config, "x", better, promote=False)
    assert load_active_artifact(config, "x")["version"] == 1  # untouched until promotion
    assert maybe_promote(config, "x", better, better_path, score, min_n=10, n_available=50)
    assert load_active_artifact(config, "x")["version"] == 2

    worse = {"kind": "x", "quality": 0.30}
    worse_path = save_artifact(config, "x", worse, promote=False)
    assert not maybe_promote(config, "x", worse, worse_path, score, min_n=10, n_available=50)
    assert load_active_artifact(config, "x")["version"] == 2  # champion survives


def test_promotion_blocked_below_min_n(config):
    champ_path = save_artifact(config, "y", {"quality": 0.2}, promote=True)
    better = {"quality": 0.1}
    better_path = save_artifact(config, "y", better, promote=False)
    assert not maybe_promote(config, "y", better, better_path,
                             lambda a: a["quality"], min_n=200, n_available=50)
    assert load_active_artifact(config, "y")["version"] == 1


def test_m3_fit_gated_on_min_resolved(config):
    conn = db.connect(config["storage"]["db_path"])
    assert fit_m3_aggregator(conn, config) is None  # zero resolved runs
    conn.close()


def _seed_resolved_forecasts(conn, n=40):
    ts = now_utc().isoformat(timespec="seconds")
    for i in range(n):
        cid = f"0x{i}"
        conn.execute(
            "INSERT INTO markets (condition_id, question, category, tier, active, closed) "
            "VALUES (?, ?, 'politics', 'liquid', 1, 1)", (cid, f"Q{i}?"))
        outcome = float(i % 2)
        db.record_resolution(conn, cid, ts, outcome, False, "gamma")
        # Forecast error varies with i so deciles are well defined.
        err = (i % 10) / 10 * 0.5
        p = min(0.99, max(0.01, abs(outcome - err)))
        db.append_forecast(conn, {
            "ts": ts, "condition_id": cid, "model_id": "m1_debiased",
            "p_yes": p, "p_market_at_ts": 0.5,
        })
    conn.commit()


class FakeLlm:
    model = "fake"

    def complete(self, system, prompt, purpose, max_tokens=600):
        return (json.dumps({"error_source": "evidence", "evidence_quality": 2,
                            "resolution_reading": "correct", "notes": "test note"}),
                {"tokens_in": 100, "tokens_out": 50, "cost_usd": 0.001})


def test_postmortems_generate_and_digest(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed_resolved_forecasts(conn)

    cands = select_candidates(conn, window_days=30, decile=0.1)
    assert len(cands["miss"]) == 4 and len(cands["win"]) == 4
    # Misses really are worse than wins under the paired sign convention.
    assert cands["miss"][0]["diff"] < cands["win"][-1]["diff"]

    written = run_postmortems(conn, config, FakeLlm())
    assert written == 8
    # Idempotent: re-running writes nothing new.
    assert run_postmortems(conn, config, FakeLlm()) == 0

    digest = lessons_digest(conn)
    assert digest["n"] == 8
    assert digest["miss_error_sources"] == {"evidence": 4}
    assert digest["sample_notes"]
    conn.close()


def test_postmortems_skip_without_llm(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed_resolved_forecasts(conn, n=10)
    assert run_postmortems(conn, config, None) == 0
    conn.close()
