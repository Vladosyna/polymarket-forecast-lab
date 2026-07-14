"""Metaculus collector (Phase 10): confirmed-pairs-only snapshotting and the
resolution watcher. No real network calls -- a fake client stubs `.question()`
and the raw-JSON path the resolution watcher needs. Fixture-driven per the
brief's Phase 10 acceptance criterion (0 confirmed pairs today is correct and
expected; a hidden Metaculus CP must be stored as NULL, not skipped).

No pytest-asyncio plugin is installed in this project (confirmed at
implementation time), so async entry points are driven with `asyncio.run(...)`
inside plain `def test_...` functions -- matching test_manifold_collector.py's
working convention, not the (currently broken, unrunnable) bare
`async def test_...` style seen in test_kalshi_collector.py.
"""

from __future__ import annotations

import asyncio

import pytest

from lab.collect.metaculus_collector import (
    _extract_resolution,
    snapshot_metaculus,
    unresolved_metaculus_pairs,
    watch_metaculus_resolutions,
)
from lab.models.m7_crossvenue import save_markets_map
from lab.store import db
from lab.store.snapshots import SnapshotStore, utc_date_str
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


class FakeMetaculusQuestion:
    def __init__(self, id_, community_prediction):
        self.id = id_
        self.title = f"Question {id_}"
        self.community_prediction = community_prediction


class _FakeHttpClient:
    """Stand-in for httpx.AsyncClient's `.headers` dict access."""

    def __init__(self):
        self.headers: dict[str, str] = {}


class FakeMetaculusClient:
    """Stub for MetaculusClient: canned .question() results by question_id,
    plus a raw-JSON map for the resolution watcher's direct-fetch path
    (mirrors the real client's `._token` / `._client.headers` / `.get_json`
    shape that `_fetch_raw_post` relies on)."""

    def __init__(self, questions: dict[str, FakeMetaculusQuestion | None],
                raw_by_id: dict[str, dict] | None = None, token: str | None = "fake-token"):
        self.questions = questions
        self.raw_by_id = raw_by_id or {}
        self.calls: list[int] = []
        self._token = token
        self._client = _FakeHttpClient()

    async def question(self, question_id: int):
        self.calls.append(question_id)
        return self.questions.get(str(question_id))

    async def get_json(self, path: str, params=None):
        # path looks like "/posts/{id}/"
        qid = path.strip("/").split("/")[1]
        return self.raw_by_id.get(qid, {})


def _seed_market(conn, condition_id: str):
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num, venue, venue_native_id)
           VALUES (?, ?, ?, 'politics', 'd', '2026-12-31T00:00:00+00:00', NULL,
                   'ignored', 1, 0, 0, 0, 'metaculus', ?)""",
        (condition_id, condition_id, f"Question for {condition_id}?", condition_id),
    )


def _map_with_pairs(tmp_path, pairs: list[tuple[str, str]]):
    path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {
            "confirmed": [
                {"condition_id": cid, "venue": "metaculus", "external_id": qid}
                for cid, qid in pairs
            ],
            "proposed": [],
        },
        path,
    )
    return path


# --- snapshot_metaculus -----------------------------------------------------

def test_snapshot_metaculus_no_confirmed_pairs_writes_nothing(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    map_path = _map_with_pairs(tmp_path, [])
    client = FakeMetaculusClient({})

    written = asyncio.run(
        snapshot_metaculus(client, conn, store, config, markets_map_path=map_path)
    )

    assert written == 0
    assert client.calls == []
    conn.close()


def test_snapshot_metaculus_writes_null_mid_when_hidden(config, tmp_path):
    """The core acceptance criterion: a hidden/unavailable community
    prediction is stored as an explicit NULL row, not skipped or errored."""
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_market(conn, "0xHIDDEN")
    conn.commit()
    map_path = _map_with_pairs(tmp_path, [("0xHIDDEN", "111")])
    client = FakeMetaculusClient({"111": FakeMetaculusQuestion(111, None)})

    written = asyncio.run(
        snapshot_metaculus(client, conn, store, config, markets_map_path=map_path)
    )

    assert written == 1
    df = store.read_range([utc_date_str(now_utc())])
    row = df.filter(df["condition_id"] == "0xHIDDEN").to_dicts()[0]
    assert row["mid"] is None
    assert row["venue"] == "metaculus"
    conn.close()


def test_snapshot_metaculus_writes_visible_mid(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_market(conn, "0xVISIBLE")
    conn.commit()
    map_path = _map_with_pairs(tmp_path, [("0xVISIBLE", "222")])
    client = FakeMetaculusClient({"222": FakeMetaculusQuestion(222, 0.37)})

    written = asyncio.run(
        snapshot_metaculus(client, conn, store, config, markets_map_path=map_path)
    )

    assert written == 1
    df = store.read_range([utc_date_str(now_utc())])
    row = df.filter(df["condition_id"] == "0xVISIBLE").to_dicts()[0]
    assert row["mid"] == pytest.approx(0.37)
    conn.close()


def test_snapshot_metaculus_skips_pair_with_unknown_condition_id(config, tmp_path):
    """condition_id not yet present in markets -- fail soft, skip, don't crash."""
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    map_path = _map_with_pairs(tmp_path, [("0xNOTYET", "333")])
    client = FakeMetaculusClient({"333": FakeMetaculusQuestion(333, 0.5)})

    written = asyncio.run(
        snapshot_metaculus(client, conn, store, config, markets_map_path=map_path)
    )

    assert written == 0
    assert client.calls == []  # never even fetched -- skipped before the call
    conn.close()


# --- resolution watcher ------------------------------------------------------

def test_extract_resolution_yes_string():
    raw = {"question": {"resolution": "yes"}}
    assert _extract_resolution(raw) == (1.0, False)


def test_extract_resolution_no_string():
    raw = {"question": {"resolution": "no"}}
    assert _extract_resolution(raw) == (0.0, False)


def test_extract_resolution_numeric():
    assert _extract_resolution({"question": {"resolution": 1.0}}) == (1.0, False)
    assert _extract_resolution({"question": {"resolution": 0.0}}) == (0.0, False)


def test_extract_resolution_ambiguous_returns_none():
    assert _extract_resolution({"question": {"resolution": "ambiguous"}}) is None


def test_extract_resolution_missing_field_returns_none():
    assert _extract_resolution({"question": {}}) is None
    assert _extract_resolution({}) is None
    assert _extract_resolution(None) is None


def test_extract_resolution_returns_none_for_group_of_questions_post():
    """Same group_of_questions/conditional scoping as api/metaculus.py's
    _extract_probability -- abstain rather than guess which sub-question's
    resolution applies."""
    raw = {"id": 17829, "group_of_questions": {"questions": [
        {"id": 17876, "resolution": "yes"},
    ]}}
    assert _extract_resolution(raw) is None


def test_unresolved_metaculus_pairs_excludes_already_resolved(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed_market(conn, "0xA")
    db.record_resolution(conn, "0xA", resolved_ts="2026-07-01T00:00:00+00:00",
                         payout_yes=1.0, disputed=False, source="metaculus")
    conn.commit()
    pending = unresolved_metaculus_pairs(conn, [("0xA", "1"), ("0xB", "2")])
    assert pending == [("0xB", "2")]
    conn.close()


def test_watch_metaculus_resolutions_empty_pairs_is_clean_noop(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    map_path = _map_with_pairs(tmp_path, [])
    client = FakeMetaculusClient({})

    recorded = asyncio.run(watch_metaculus_resolutions(client, conn, markets_map_path=map_path))

    assert recorded == 0
    conn.close()


def test_watch_metaculus_resolutions_records_final_payout(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    _seed_market(conn, "0xRESOLVED")
    conn.commit()
    map_path = _map_with_pairs(tmp_path, [("0xRESOLVED", "999")])
    client = FakeMetaculusClient({}, raw_by_id={"999": {"question": {"resolution": "yes"}}})

    recorded = asyncio.run(watch_metaculus_resolutions(client, conn, markets_map_path=map_path))

    assert recorded == 1
    row = conn.execute(
        "SELECT payout_yes, disputed, source FROM resolutions WHERE condition_id = ?",
        ("0xRESOLVED",),
    ).fetchone()
    assert row["payout_yes"] == 1.0
    assert row["disputed"] == 0
    assert row["source"] == "metaculus"
    conn.close()


def test_watch_metaculus_resolutions_is_idempotent(config, tmp_path):
    """Calling twice must not double-count or raise (append-only resolutions
    upsert is already idempotent at the db layer; this proves the collector
    doesn't fight that by e.g. re-querying stale pending lists)."""
    conn = db.connect(config["storage"]["db_path"])
    _seed_market(conn, "0xTWICE")
    conn.commit()
    map_path = _map_with_pairs(tmp_path, [("0xTWICE", "555")])
    client = FakeMetaculusClient({}, raw_by_id={"555": {"question": {"resolution": "no"}}})

    first = asyncio.run(watch_metaculus_resolutions(client, conn, markets_map_path=map_path))
    second = asyncio.run(watch_metaculus_resolutions(client, conn, markets_map_path=map_path))

    assert first == 1
    assert second == 0  # already has a resolutions row -- no longer "pending"
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM resolutions WHERE condition_id = ?", ("0xTWICE",)
    ).fetchone()["n"]
    assert count == 1
    conn.close()


def test_watch_metaculus_resolutions_still_open_records_nothing(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    _seed_market(conn, "0xOPEN")
    conn.commit()
    map_path = _map_with_pairs(tmp_path, [("0xOPEN", "777")])
    client = FakeMetaculusClient({}, raw_by_id={"777": {"question": {"resolution": None}}})

    recorded = asyncio.run(watch_metaculus_resolutions(client, conn, markets_map_path=map_path))

    assert recorded == 0
    conn.close()


def test_watch_metaculus_resolutions_no_token_abstains_cleanly(config, tmp_path):
    """No METACULUS_API_KEY on the client -- must abstain (0 recorded), not
    raise, matching the shared client's own no-token behavior."""
    conn = db.connect(config["storage"]["db_path"])
    _seed_market(conn, "0xNOTOKEN")
    conn.commit()
    map_path = _map_with_pairs(tmp_path, [("0xNOTOKEN", "888")])
    client = FakeMetaculusClient({}, raw_by_id={"888": {"question": {"resolution": "yes"}}}, token=None)

    recorded = asyncio.run(watch_metaculus_resolutions(client, conn, markets_map_path=map_path))

    assert recorded == 0
    conn.close()
