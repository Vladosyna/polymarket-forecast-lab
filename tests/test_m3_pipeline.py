"""M3 end-to-end on fixtures: evidence rows land, cost cap skips cleanly."""

from __future__ import annotations

import json

import pytest

from lab.forecast import run_forecasts
from lab.models.base import MarketState
from lab.models.m3_evidence import M3Evidence, m3_target_ids
from lab.news.extract import BudgetExceeded
from lab.news.providers import Article
from lab.store import db
from lab.store.snapshots import SnapshotStore, floor_ts_bucket
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


class FakeProvider:
    def fetch(self, query, max_items=20):
        return [Article(title="Positive development for X", url="http://n/1",
                        source="fake", published_ts="2026-07-01T00:00:00+00:00",
                        summary="X moved closer to happening.")]


class FakeLlm:
    """Deterministic LLM stub with a controllable budget."""

    model = "fake-model"

    def __init__(self, budget_calls: int = 100) -> None:
        self.calls = 0
        self.budget_calls = budget_calls

    def complete(self, system, prompt, purpose, max_tokens=2000):
        if self.calls >= self.budget_calls:
            raise BudgetExceeded("cap")
        self.calls += 1
        payload = json.dumps([
            {"claim": "X moved closer", "direction": "for_yes", "strength": 2,
             "source_reliability": 2, "relevance": 0.8, "article_index": 0},
        ])
        return payload, {"tokens_in": 500, "tokens_out": 100, "cost_usd": 0.003}


def _seed_markets(conn, store, n=3):
    ts_bucket = floor_ts_bucket(now_utc(), 5)
    for i in range(n):
        cid = f"0x{i}"
        conn.execute(
            """INSERT INTO markets (condition_id, slug, question, category, description,
                                    end_date_iso, token_id_yes, tier, active, closed,
                                    liquidity_num, volume_num)
               VALUES (?, ?, ?, 'politics', 'Resolves YES if X.', '2026-12-31T00:00:00+00:00',
                       ?, 'liquid', 1, 0, ?, 5000000)""",
            (cid, f"m-{i}", f"Will X{i} happen?", f"tok{i}", 1000000 - i),
        )
        store.append([{
            "ts": ts_bucket, "condition_id": cid, "token_id_yes": f"tok{i}",
            "best_bid": 0.58, "best_ask": 0.62, "mid": 0.60, "spread": 0.04,
            "bid_depth_usd": 1000.0, "ask_depth_usd": 1000.0, "last_trade_price": None,
        }])
    conn.commit()


def test_m3_end_to_end_writes_evidence_and_forecasts(config):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_markets(conn, store)

    targets = m3_target_ids(conn, config)
    assert targets == ["0x0", "0x1", "0x2"]  # ordered by liquidity desc

    m3 = M3Evidence(conn, FakeLlm(), [FakeProvider()], config, targets)
    counts = run_forecasts(conn, store, [m3], config)
    assert counts["written"] == 3

    ev = conn.execute("SELECT * FROM evidence_runs").fetchall()
    assert len(ev) == 3
    dossier = json.loads(ev[0]["dossier_json"])
    # Auditability: dossier carries the full trail start-to-finish.
    for key in ("question", "resolution_criteria", "p_market", "articles",
                "evidence_items", "aggregation"):
        assert key in dossier

    fc = conn.execute("SELECT * FROM forecasts WHERE model_id='m3_evidence'").fetchall()
    assert len(fc) == 3
    assert all(f["evidence_run_id"] is not None for f in fc)
    assert all(f["p_yes"] > 0.60 for f in fc)  # for_yes evidence shifts up
    conn.close()


def test_cost_cap_breach_skips_remaining(config, caplog):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_markets(conn, store)

    llm = FakeLlm(budget_calls=1)  # cap hits after the first market
    m3 = M3Evidence(conn, llm, [FakeProvider()], config, m3_target_ids(conn, config))
    counts = run_forecasts(conn, store, [m3], config)

    assert counts["written"] == 1
    assert llm.calls == 1  # no further calls after the breach
    assert any("cost cap hit" in r.message for r in caplog.records)
    conn.close()
