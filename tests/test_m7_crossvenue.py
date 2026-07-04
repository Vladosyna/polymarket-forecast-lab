"""M7 cross-venue signal (Phase 9): mapping propose-then-confirm flow, the
deterministic log-odds pool, and the ledger writer -- fixture-driven per the
brief's Phase 9 acceptance criterion ("fixtures acceptable")."""

from __future__ import annotations

import json

import pytest

from lab.models.base import ForecastResult
from lab.models.m7_crossvenue import (
    confirm_match,
    confirmed_by_condition,
    link_confirmed_event,
    load_markets_map,
    pool_log_odds,
    propose_matches,
    save_markets_map,
    write_m7_forecasts,
)
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


def test_pool_log_odds_averages_in_logit_space():
    # Two venues agreeing exactly: pool equals that shared value.
    assert pool_log_odds([0.7, 0.7]) == pytest.approx(0.7)
    # One confident YES, one confident NO -> pools back toward 0.5.
    assert pool_log_odds([0.9, 0.1]) == pytest.approx(0.5, abs=1e-9)
    with pytest.raises(ValueError):
        pool_log_odds([])


def test_markets_map_roundtrip(tmp_path):
    path = tmp_path / "markets_map.yaml"
    data = {"confirmed": [{"condition_id": "0x1", "venue": "kalshi", "external_id": "T1"}],
            "proposed": []}
    save_markets_map(data, path)
    loaded = load_markets_map(path)
    assert loaded["confirmed"] == data["confirmed"]
    assert loaded["proposed"] == []


def test_load_markets_map_missing_file_returns_empty(tmp_path):
    loaded = load_markets_map(tmp_path / "does_not_exist.yaml")
    assert loaded == {"confirmed": [], "proposed": []}


def test_confirm_match_moves_proposed_to_confirmed():
    data = {
        "confirmed": [],
        "proposed": [{"condition_id": "0x1", "question": "q", "venue": "kalshi",
                     "external_id": "T1", "external_question": "eq",
                     "rationale": "same event", "confidence": 0.9,
                     "proposed_ts": "2026-07-01T00:00:00+00:00"}],
    }
    assert confirm_match(data, "0x1", "kalshi") is True
    assert data["proposed"] == []
    assert len(data["confirmed"]) == 1
    entry = data["confirmed"][0]
    assert entry["condition_id"] == "0x1" and entry["external_id"] == "T1"
    assert "confirmed_ts" in entry
    # LLM-only fields don't belong on a confirmed (human-owned) entry.
    assert "rationale" not in entry and "confidence" not in entry


def test_confirm_match_idempotent():
    data = {"confirmed": [{"condition_id": "0x1", "venue": "kalshi", "external_id": "T1",
                          "confirmed_ts": "t"}], "proposed": []}
    assert confirm_match(data, "0x1", "kalshi") is True
    assert len(data["confirmed"]) == 1  # no duplicate


def test_confirm_match_hand_curated_without_prior_proposal():
    """A human can confirm a Metaculus pair directly -- `propose` can't reach
    Metaculus (api/metaculus.py), so this is the only path for that venue."""
    data = {"confirmed": [], "proposed": []}
    assert confirm_match(data, "0x1", "metaculus", external_id="12345") is True
    assert data["confirmed"][0]["external_id"] == "12345"


def test_confirm_match_returns_false_with_nothing_to_confirm():
    data = {"confirmed": [], "proposed": []}
    assert confirm_match(data, "0x1", "kalshi") is False
    assert data["confirmed"] == []


def test_link_confirmed_event_creates_event_linking_both_markets(config):
    """Phase 10 acceptance: a confirmed match creates an event linking >=2
    venue-markets, using the Polymarket market's own question as the title."""
    conn = db.connect(config["storage"]["db_path"])
    db.upsert_market(conn, {
        "condition_id": "0x1", "venue": "polymarket", "venue_native_id": "0x1",
        "slug": "s", "question": "Will X happen?", "category": "politics",
        "description": "d", "end_date_iso": "2026-12-31T00:00:00+00:00",
        "token_id_yes": "1", "token_id_no": "2", "neg_risk": 0,
        "active": 1, "closed": 0, "liquidity_num": 1.0, "volume_num": 1.0,
        "tier": "liquid",
    })
    conn.commit()

    event_id = link_confirmed_event(conn, "0x1", "kalshi", "T1")

    rows = {r["condition_id"]: r["event_id"]
           for r in conn.execute("SELECT condition_id, event_id FROM markets")}
    assert rows["0x1"] == event_id
    assert rows["kalshi:T1"] == event_id
    ev = conn.execute("SELECT title FROM events WHERE event_id = ?", (event_id,)).fetchone()
    assert ev["title"] == "Will X happen?"
    conn.close()


def test_confirmed_by_condition_excludes_proposed():
    """The core of 'a proposed-but-unconfirmed pair is NOT forecast': the
    scan path only ever sees confirmed_by_condition()'s output."""
    data = {
        "confirmed": [{"condition_id": "0xA", "venue": "kalshi", "external_id": "T1"}],
        "proposed": [{"condition_id": "0xB", "venue": "kalshi", "external_id": "T2"}],
    }
    by_cid = confirmed_by_condition(data)
    assert set(by_cid) == {"0xA"}


def _seed_market_with_snapshot(conn, store, cid: str, mid: float = 0.5):
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES (?, ?, ?, 'politics', 'd', '2026-12-31T00:00:00+00:00', ?, 'liquid', 1, 0,
                   200000, 2000000)""",
        (cid, cid, f"Question for {cid}?", f"tok-{cid}"),
    )
    store.append([{
        "ts": floor_ts_bucket(now_utc(), 5), "condition_id": cid, "token_id_yes": f"tok-{cid}",
        "best_bid": mid - 0.02, "best_ask": mid + 0.02, "mid": mid, "spread": 0.04,
        "bid_depth_usd": 1000.0, "ask_depth_usd": 1000.0, "last_trade_price": None,
    }])


def test_write_m7_forecasts_writes_five_confirmed_pairs_and_skips_unconfirmed(config):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    cids = [f"0x{i}" for i in range(5)]
    for cid in cids:
        _seed_market_with_snapshot(conn, store, cid)
    # A 6th market stands in for a proposed-but-unconfirmed pair: it has its
    # own snapshot but no entry in `results`, so it must never be forecast.
    _seed_market_with_snapshot(conn, store, "0xUNCONFIRMED")
    conn.commit()

    results = {
        cid: ForecastResult(
            p_yes=0.6 + 0.01 * i,
            meta={"quotes": [{"venue": "kalshi", "external_id": f"T{i}", "price": 0.6 + 0.01 * i,
                              "fetched_ts": "2026-07-03T00:00:00+00:00"}], "n_pooled": 1},
        )
        for i, cid in enumerate(cids)
    }

    written = write_m7_forecasts(conn, store, results, config)
    assert written == 5

    rows = conn.execute("SELECT * FROM forecasts WHERE model_id='m7_crossvenue'").fetchall()
    assert len(rows) == 5
    assert {r["condition_id"] for r in rows} == set(cids)
    assert all(r["p_market_at_ts"] == pytest.approx(0.5) for r in rows)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM forecasts WHERE condition_id='0xUNCONFIRMED'"
    ).fetchone()["n"] == 0
    conn.close()


class FakeKalshiCandidate:
    def __init__(self, ticker, title):
        self.ticker = ticker
        self.title = title


class FakeLlm:
    def __init__(self, response: dict):
        self.response = response
        self.calls = 0

    def complete(self, system, prompt, purpose, max_tokens=2000):
        self.calls += 1
        return json.dumps(self.response), {"tokens_in": 100, "tokens_out": 50, "cost_usd": 0.001}


def test_propose_matches_appends_to_proposed_not_confirmed(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES ('0x1', 's', 'Will the Fed cut rates in March?', 'economics', 'd',
                   '2026-12-31T00:00:00+00:00', 'tok', 'liquid', 1, 0, 200000, 5000000)"""
    )
    conn.commit()
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)

    llm = FakeLlm({"matches": [{"external_id": "FEDMAR", "confidence": 0.85,
                               "rationale": "same FOMC meeting"}]})
    candidates = [FakeKalshiCandidate("FEDMAR", "Fed cuts rates in March FOMC meeting")]

    proposals = propose_matches(conn, config, candidates, llm, markets_map_path=map_path)
    assert len(proposals) == 1
    assert proposals[0]["external_id"] == "FEDMAR"
    assert proposals[0]["venue"] == "kalshi"

    data = load_markets_map(map_path)
    assert data["confirmed"] == []  # propose never writes to confirmed
    assert len(data["proposed"]) == 1
    conn.close()


def test_propose_matches_skips_already_proposed_pair(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES ('0x1', 's', 'Will the Fed cut rates in March?', 'economics', 'd',
                   '2026-12-31T00:00:00+00:00', 'tok', 'liquid', 1, 0, 200000, 5000000)"""
    )
    conn.commit()
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [], "proposed": [{"condition_id": "0x1", "venue": "kalshi",
                                       "external_id": "FEDMAR", "confidence": 0.5}]},
        map_path,
    )
    llm = FakeLlm({"matches": [{"external_id": "FEDMAR", "confidence": 0.85, "rationale": "x"}]})
    candidates = [FakeKalshiCandidate("FEDMAR", "Fed cuts rates in March FOMC meeting")]

    proposals = propose_matches(conn, config, candidates, llm, markets_map_path=map_path)
    assert proposals == []  # already proposed -- LLM not asked to re-propose it
    assert llm.calls == 0
    conn.close()
