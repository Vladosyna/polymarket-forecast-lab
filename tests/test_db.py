"""Store tests: idempotent upserts, immutable forecast ledger, resolutions."""

from __future__ import annotations

import sqlite3

import pytest

from lab.store import db


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lab.db")
    yield c
    c.close()


MARKET = {
    "condition_id": "0xabc",
    "slug": "will-x-happen",
    "question": "Will X happen?",
    "category": "politics",
    "description": "Resolves YES if X happens by DATE.",
    "end_date_iso": "2026-12-31T00:00:00+00:00",
    "token_id_yes": "111",
    "token_id_no": "222",
    "neg_risk": 0,
    "active": 1,
    "closed": 0,
    "liquidity_num": 50000.0,
    "volume_num": 100000.0,
    "tier": "liquid",
}


def test_upsert_market_idempotent_preserves_first_seen(conn):
    db.upsert_market(conn, MARKET)
    first = conn.execute("SELECT first_seen_ts, last_synced_ts FROM markets").fetchone()

    db.upsert_market(conn, {**MARKET, "liquidity_num": 60000.0})
    rows = conn.execute("SELECT * FROM markets").fetchall()
    assert len(rows) == 1
    assert rows[0]["liquidity_num"] == 60000.0
    assert rows[0]["first_seen_ts"] == first["first_seen_ts"]
    assert rows[0]["last_synced_ts"] >= first["last_synced_ts"]


FORECAST = {
    "ts": "2026-07-01T00:00:00+00:00",
    "condition_id": "0xabc",
    "model_id": "m0_market",
    "p_yes": 0.62,
    "p_market_at_ts": 0.62,
}


def test_forecast_append_and_update_denied(conn):
    row_id = db.append_forecast(conn, FORECAST)
    assert row_id == 1

    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE forecasts SET p_yes = 0.99 WHERE id = 1")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM forecasts WHERE id = 1")

    stored = conn.execute("SELECT p_yes FROM forecasts WHERE id = 1").fetchone()
    assert stored["p_yes"] == 0.62


def test_resolution_idempotent(conn):
    db.upsert_market(conn, MARKET)
    for _ in range(2):  # at-least-once delivery must be safe
        db.record_resolution(conn, "0xabc", "2026-07-01T12:00:00+00:00", 1.0, False, "gamma")
    rows = conn.execute("SELECT * FROM resolutions").fetchall()
    assert len(rows) == 1
    assert rows[0]["payout_yes"] == 1.0


def test_llm_spend_ledger(conn):
    assert db.llm_spend_today(conn, "2026-07-01") == 0.0
    db.record_llm_spend(conn, "2026-07-01", "m3_extraction", 0.12)
    db.record_llm_spend(conn, "2026-07-01", "m3_extraction", 0.08)
    assert db.llm_spend_today(conn, "2026-07-01") == pytest.approx(0.20)
