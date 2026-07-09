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


# --- v1.9 multi-venue migration (Phase 10) ----------------------------------

def test_migrate_multi_venue_is_idempotent(conn):
    # db.connect() (the `conn` fixture) already ran the migration once.
    applied_again = db.migrate_multi_venue(conn)
    assert applied_again == {
        "venue_column": False, "venue_native_id_column": False, "event_id_column": False,
    }
    cols = {r[1] for r in conn.execute("PRAGMA table_info(markets)")}
    assert {"venue", "venue_native_id", "event_id"} <= cols


def test_migrate_eval_measurement_upgrade_is_idempotent(conn):
    # db.connect() (the `conn` fixture) already ran the migration once.
    applied_again = db.migrate_eval_measurement_upgrade(conn)
    assert applied_again == {
        "venue_column": False, "category_column": False,
        "skill_pw_column": False, "skill_pw_ci_lo_column": False,
        "skill_pw_ci_hi_column": False, "n_strata_pw_column": False,
        "cs_lo_column": False, "cs_hi_column": False,
        "cs_covers_zero_column": False, "n_event_clusters_column": False,
    }
    cols = {r[1] for r in conn.execute("PRAGMA table_info(eval_runs)")}
    assert {"venue", "category", "skill_pw", "skill_pw_ci_lo", "skill_pw_ci_hi",
           "n_strata_pw", "cs_lo", "cs_hi", "cs_covers_zero", "n_event_clusters"} <= cols


def test_migrate_eval_measurement_upgrade_legacy_row_gets_null_new_columns(conn):
    # A pre-Phase-11 style row (only the original columns populated) must
    # survive the migration untouched, with new columns reading NULL --
    # never backfilled/guessed.
    conn.execute(
        "INSERT INTO eval_runs (ts, model_id, window_label, n, brier, brier_market, skill, "
        "skill_ci_lo, skill_ci_hi, log_loss, log_loss_market) "
        "VALUES ('2026-01-01T00:00:00Z', 'm0_market', 'all_time', 10, 0.2, 0.25, 0.05, 0.01, 0.09, 0.4, 0.5)"
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM eval_runs WHERE model_id = 'm0_market'"
    ).fetchone()
    assert row["skill"] == pytest.approx(0.05)
    assert row["venue"] is None and row["category"] is None
    assert row["skill_pw"] is None and row["cs_lo"] is None


def test_venues_seeded_with_brief_flags(conn):
    rows = {r["venue"]: dict(r) for r in conn.execute("SELECT * FROM venues")}
    assert rows["polymarket"] == {"venue": "polymarket", "trust_tier": "money", "forecastable": 1, "in_m7_pool": 0}
    assert rows["kalshi"] == {"venue": "kalshi", "trust_tier": "money", "forecastable": 1, "in_m7_pool": 1}
    assert rows["metaculus"] == {"venue": "metaculus", "trust_tier": "reputation", "forecastable": 0, "in_m7_pool": 1}
    assert rows["manifold"] == {"venue": "manifold", "trust_tier": "play", "forecastable": 0, "in_m7_pool": 0}


def test_universe_log_table_exists_and_accepts_rows(conn):
    """Phase 15: universe_log is a brand-new table (CREATE TABLE IF NOT
    EXISTS), no ALTER migration needed -- just confirm it's created and
    accepts a row with the expected columns."""
    conn.execute(
        "INSERT INTO universe_log (ts, venue, venue_native_id, reason_code) VALUES (?, ?, ?, ?)",
        ("2026-07-09T00:00:00+00:00", "polymarket", "0xabc", "low_liquidity"),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM universe_log").fetchone()
    assert row["venue"] == "polymarket"
    assert row["venue_native_id"] == "0xabc"
    assert row["reason_code"] == "low_liquidity"


def test_venue_condition_id():
    assert db.venue_condition_id("polymarket", "0xabc") == "0xabc"
    assert db.venue_condition_id("kalshi", "KXFOO-26") == "kalshi:KXFOO-26"
    assert db.venue_condition_id("metaculus", "12345") == "metaculus:12345"


def test_upsert_market_defaults_venue_polymarket(conn):
    db.upsert_market(conn, MARKET)
    row = conn.execute("SELECT venue, venue_native_id, event_id FROM markets WHERE condition_id = ?",
                       (MARKET["condition_id"],)).fetchone()
    assert row["venue"] == "polymarket"
    assert row["venue_native_id"] == MARKET["condition_id"]
    assert row["event_id"] is None


def test_upsert_market_resync_never_overwrites_event_id(conn):
    kalshi_row = {**MARKET, "condition_id": "kalshi:KXFOO-26", "venue": "kalshi", "venue_native_id": "KXFOO-26"}
    db.upsert_market(conn, kalshi_row)
    db.link_event(conn, MARKET["condition_id"], kalshi_row["condition_id"])
    db.upsert_market(conn, kalshi_row)  # a routine re-sync of the same market
    row = conn.execute("SELECT event_id FROM markets WHERE condition_id = ?",
                       (kalshi_row["condition_id"],)).fetchone()
    assert row["event_id"] is not None


def test_link_event_mints_and_links_both_sides(conn):
    db.upsert_market(conn, MARKET)
    kalshi_cid = "kalshi:KXFOO-26"
    event_id = db.link_event(conn, MARKET["condition_id"], kalshi_cid, title="Will X happen?")
    rows = {r["condition_id"]: r["event_id"]
           for r in conn.execute("SELECT condition_id, event_id FROM markets WHERE condition_id IN (?, ?)",
                                 (MARKET["condition_id"], kalshi_cid))}
    assert rows[MARKET["condition_id"]] == event_id
    assert rows[kalshi_cid] == event_id
    ev = conn.execute("SELECT title FROM events WHERE event_id = ?", (event_id,)).fetchone()
    assert ev["title"] == "Will X happen?"


def test_link_event_idempotent_reuses_same_event(conn):
    db.upsert_market(conn, MARKET)
    kalshi_cid = "kalshi:KXFOO-26"
    first = db.link_event(conn, MARKET["condition_id"], kalshi_cid)
    second = db.link_event(conn, MARKET["condition_id"], kalshi_cid)
    assert first == second
    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 1


def test_link_event_upserts_placeholder_for_unsynced_venue_market(conn):
    """Acceptance criterion (Phase 10): a confirmed match creates an event
    linking >=2 venue-markets, even if the external venue's own collector
    hasn't synced that market yet."""
    db.upsert_market(conn, MARKET)
    kalshi_cid = "kalshi:KXNOTSYNCED-26"
    assert conn.execute("SELECT COUNT(*) AS n FROM markets WHERE condition_id = ?",
                        (kalshi_cid,)).fetchone()["n"] == 0
    event_id = db.link_event(conn, MARKET["condition_id"], kalshi_cid)
    linked = conn.execute("SELECT condition_id FROM markets WHERE event_id = ?", (event_id,)).fetchall()
    assert {r["condition_id"] for r in linked} == {MARKET["condition_id"], kalshi_cid}
