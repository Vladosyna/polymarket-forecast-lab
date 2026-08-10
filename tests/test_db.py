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


def test_migrate_m3_boundary_randomization_is_idempotent(conn):
    # db.connect() (the `conn` fixture) already ran the migration once.
    applied_again = db.migrate_m3_boundary_randomization(conn)
    assert applied_again == {
        "m3_randomized_column": False, "m3_random_seed_column": False,
    }
    cols = {r[1] for r in conn.execute("PRAGMA table_info(forecasts)")}
    assert {"m3_randomized", "m3_random_seed"} <= cols


def test_migrate_shadow_fees_is_idempotent(conn):
    # db.connect() (the `conn` fixture) already ran the migration once.
    applied_again = db.migrate_shadow_fees(conn)
    assert applied_again == {
        "fee_paid_sim_column": False, "effective_spread_sim_column": False,
    }
    cols = {r[1] for r in conn.execute("PRAGMA table_info(shadow_trades)")}
    assert {"fee_paid_sim", "effective_spread_sim"} <= cols


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


# --- 2026-07-20 universe_log dedup migration --------------------------------

def test_migrate_universe_log_dedup_is_idempotent(conn):
    # db.connect() (the `conn` fixture) already ran the migration once, so the
    # unique index already exists -- a second call must be a full no-op (in
    # particular it must NOT attempt CREATE UNIQUE INDEX again and error).
    applied_again = db.migrate_universe_log_dedup(conn)
    assert applied_again == {"deduped_existing_rows": False, "unique_index": False}
    assert db._index_exists(conn, "idx_universe_log_dedup")


def test_migrate_universe_log_dedup_collapses_pre_existing_duplicates(tmp_path):
    """The realistic legacy scenario: a database created before this fix has
    ~20x same-day duplicate rows already on disk (one per hourly sync). The
    migration must collapse each (venue, venue_native_id, reason_code, day)
    group to exactly one row -- keeping the earliest (lowest id) -- without
    touching rows that differ in venue, market, reason, or day."""
    import sqlite3

    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE universe_log (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, "
        "venue TEXT NOT NULL, venue_native_id TEXT NOT NULL, reason_code TEXT NOT NULL)"
    )
    # Three same-day duplicate syncs for 0x1/low_liquidity...
    for hour in ("00", "01", "02"):
        raw.execute(
            "INSERT INTO universe_log (ts, venue, venue_native_id, reason_code) VALUES (?,?,?,?)",
            (f"2026-07-19T{hour}:00:00+00:00", "polymarket", "0x1", "low_liquidity"),
        )
    # ...a genuinely different day for the same market/reason...
    raw.execute(
        "INSERT INTO universe_log (ts, venue, venue_native_id, reason_code) VALUES (?,?,?,?)",
        ("2026-07-20T00:00:00+00:00", "polymarket", "0x1", "low_liquidity"),
    )
    # ...and one unrelated row (different market) that must survive untouched.
    raw.execute(
        "INSERT INTO universe_log (ts, venue, venue_native_id, reason_code) VALUES (?,?,?,?)",
        ("2026-07-19T00:30:00+00:00", "polymarket", "0x2", "no_orderbook"),
    )
    raw.commit()
    raw.close()

    conn = db.connect(path)  # connect() runs the migration as part of its chain
    rows = conn.execute(
        "SELECT ts, venue_native_id, reason_code FROM universe_log ORDER BY id"
    ).fetchall()
    assert len(rows) == 3  # 3 same-day dupes -> 1, plus the distinct day, plus the other market
    by_native_id_and_date = {(r["venue_native_id"], r["ts"][:10]) for r in rows}
    assert by_native_id_and_date == {
        ("0x1", "2026-07-19"),  # the 3 same-day dupes, collapsed to the earliest (00:00)
        ("0x1", "2026-07-20"),  # a genuinely different day -- untouched
        ("0x2", "2026-07-19"),  # a different market -- untouched
    }
    kept = next(r for r in rows if r["venue_native_id"] == "0x1" and r["ts"][:10] == "2026-07-19")
    assert kept["ts"] == "2026-07-19T00:00:00+00:00"  # earliest (lowest id) of the 3 dupes
    conn.close()


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


# --- connect() must not need the write lock (2026-08-01..05 lock storm) ----

def test_connect_succeeds_while_another_connection_holds_the_write_lock(tmp_path):
    """THE regression test, reproducing the exact production failure.

    connect() used to run executescript(SCHEMA), seven migrations and two
    INSERT OR IGNOREs on EVERY call, so merely opening a connection to READ
    one value took the write lock. schedule_state.last_run_age_seconds does
    exactly that on every catch-up tick, so while a long job was writing, the
    open failed with "database is locked" -- which is how an hourly learn run
    turned into 84 unit restarts over three days.
    """
    import sqlite3

    path = tmp_path / "lab.db"
    db.connect(path).close()  # first open creates and migrates

    writer = sqlite3.connect(path)
    writer.execute("PRAGMA busy_timeout=0")
    writer.execute("BEGIN IMMEDIATE")  # hold the write lock
    writer.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('probe','1')")
    try:
        reader = db.connect(path)  # must not need the write lock
        try:
            assert reader.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 0
        finally:
            reader.close()
    finally:
        writer.rollback()
        writer.close()


def test_connect_still_migrates_a_new_database(tmp_path):
    conn = db.connect(tmp_path / "fresh.db")
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"markets", "forecasts", "model_versions", "wealth_ledger"} <= tables
        assert conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        conn.close()


def test_connect_reruns_migrations_when_the_version_is_stale(tmp_path):
    """The skip is version-gated, not unconditional: an out-of-date database
    must still take the full path."""
    path = tmp_path / "lab.db"
    db.connect(path).close()

    import sqlite3

    raw = sqlite3.connect(path)
    raw.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    raw.execute("DROP TABLE wealth_ledger")
    raw.commit()
    raw.close()

    conn = db.connect(path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "wealth_ledger" in tables, "stale schema was not re-migrated"
        assert conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        conn.close()


# --- a resolved market is closed (2026-08-10) -------------------------------

def test_recording_a_resolution_closes_the_market(tmp_path):
    """Nothing else clears the flag: a venue drops a settled market from its
    listing, so the universe sync never sees it again and cannot update it.
    1,772 Kalshi markets sat active=1/closed=0 with resolutions already
    recorded, and kept being snapshotted every 15 minutes."""
    conn = db.connect(tmp_path / "lab.db")
    try:
        conn.execute(
            "INSERT INTO markets (condition_id, question, venue, tier, active, closed)"
            " VALUES ('0x1','Q','kalshi','tail',1,0)")
        conn.commit()

        db.record_resolution(conn, "0x1", "2026-08-10T00:00:00+00:00", 1.0, False, "kalshi")
        conn.commit()

        row = conn.execute(
            "SELECT active, closed FROM markets WHERE condition_id='0x1'").fetchone()
        assert (row["active"], row["closed"]) == (0, 1)
    finally:
        conn.close()


def test_closing_on_resolution_does_not_starve_the_resolution_watcher(tmp_path):
    """Both watchers select markets with NO resolution row, so a market is out
    of their candidate set before this ever runs. Asserted rather than assumed:
    closing markets from under a watcher is exactly how the 2026-07-25 stall
    happened."""
    from lab.collect.resolutions import unresolved_closed_markets

    conn = db.connect(tmp_path / "lab.db")
    try:
        for cid in ("0xresolved", "0xpending"):
            conn.execute(
                "INSERT INTO markets (condition_id, question, venue, tier, active, closed)"
                " VALUES (?,'Q','polymarket','tail',0,1)", (cid,))
        conn.commit()
        db.record_resolution(conn, "0xresolved", "2026-08-10T00:00:00+00:00", 1.0, False, "gamma")
        conn.commit()

        pending = unresolved_closed_markets(conn, limit=50)
    finally:
        conn.close()

    assert "0xpending" in pending, "an unresolved closed market must stay a candidate"
    assert "0xresolved" not in pending, "a resolved one is already out of the queue"


def test_migration_closes_resolved_markets_and_only_those(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    try:
        conn.execute("INSERT INTO markets (condition_id, question, venue, tier, active, closed)"
                     " VALUES ('0xres','Q','kalshi','tail',1,0)")
        conn.execute("INSERT INTO markets (condition_id, question, venue, tier, active, closed)"
                     " VALUES ('0xopen','Q','kalshi','tail',1,0)")
        conn.execute("INSERT INTO resolutions (condition_id, resolved_ts, payout_yes, disputed)"
                     " VALUES ('0xres','2026-08-10T00:00:00+00:00',1.0,0)")
        conn.commit()

        db.migrate_close_resolved_markets(conn)

        rows = dict(conn.execute(
            "SELECT condition_id, closed FROM markets").fetchall())
        assert rows["0xres"] == 1
        assert rows["0xopen"] == 0, "a market without a resolution must not be closed"
        # idempotent
        assert db.migrate_close_resolved_markets(conn)["closed"] == 0
    finally:
        conn.close()
