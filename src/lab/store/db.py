"""SQLite schema, migrations, and writers (single file data/lab.db, WAL mode).

The forecasts table is append-only: the guarded connection installs SQLite
authorizer callbacks that hard-fail any UPDATE or DELETE against it
(guardrail: immutable forecast ledger).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lab.util import PROJECT_ROOT, now_utc_iso

SCHEMA_VERSION = "6"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

-- v1.9 multi-venue foundation (Phase 10, brief section 5). `markets` itself
-- gains venue/venue_native_id/event_id via ALTER in _migrate_multi_venue()
-- below -- CREATE TABLE IF NOT EXISTS can't add columns to an existing table.
CREATE TABLE IF NOT EXISTS venues (
  venue TEXT PRIMARY KEY,
  trust_tier TEXT CHECK(trust_tier IN ('money','reputation','play')),
  forecastable INTEGER DEFAULT 0,
  in_m7_pool INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  title TEXT, created_ts TEXT
);

CREATE TABLE IF NOT EXISTS markets (
  condition_id TEXT PRIMARY KEY,
  slug TEXT, question TEXT, category TEXT,
  description TEXT,
  end_date_iso TEXT,
  token_id_yes TEXT, token_id_no TEXT,
  neg_risk INTEGER DEFAULT 0,
  active INTEGER, closed INTEGER,
  liquidity_num REAL, volume_num REAL,
  tier TEXT CHECK(tier IN ('liquid','tail','ignored')),
  first_seen_ts TEXT, last_synced_ts TEXT
);

CREATE TABLE IF NOT EXISTS resolutions (
  condition_id TEXT PRIMARY KEY REFERENCES markets(condition_id),
  resolved_ts TEXT,
  payout_yes REAL CHECK(payout_yes IN (0.0, 1.0)),
  disputed INTEGER DEFAULT 0,
  source TEXT
);

-- append-only. NEVER UPDATE OR DELETE ROWS.
CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  condition_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  p_yes REAL NOT NULL CHECK(p_yes > 0 AND p_yes < 1),
  p_market_at_ts REAL NOT NULL,
  spread_at_ts REAL,
  inputs_hash TEXT,
  evidence_run_id INTEGER,
  cost_usd REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS evidence_runs (
  id INTEGER PRIMARY KEY,
  ts TEXT, condition_id TEXT,
  dossier_json TEXT,
  llm_model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL
);

CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY,
  ts TEXT, model_id TEXT, window_label TEXT,
  n INTEGER,
  brier REAL, brier_market REAL, skill REAL,
  skill_ci_lo REAL, skill_ci_hi REAL,
  log_loss REAL, log_loss_market REAL,
  calibration_json TEXT
);

CREATE TABLE IF NOT EXISTS shadow_trades (
  id INTEGER PRIMARY KEY,
  opened_ts TEXT, condition_id TEXT, token_side TEXT CHECK(token_side IN ('YES','NO')),
  entry_price REAL, p_model REAL, p_market REAL, edge REAL,
  stake_sim REAL, kelly_frac REAL,
  exit_ts TEXT, exit_price REAL, pnl_sim REAL,
  status TEXT CHECK(status IN ('open','resolved','abandoned'))
);

CREATE TABLE IF NOT EXISTS postmortems (
  id INTEGER PRIMARY KEY,
  ts TEXT, condition_id TEXT, model_id TEXT,
  kind TEXT CHECK(kind IN ('miss','win')),
  brier_model REAL, brier_market REAL,
  analysis_json TEXT,
  llm_model TEXT, cost_usd REAL
);

-- daily LLM spend ledger (guardrail 10: budget enforced before each call)
CREATE TABLE IF NOT EXISTS llm_spend (
  date TEXT NOT NULL,             -- YYYY-MM-DD UTC
  purpose TEXT NOT NULL,          -- 'm3_extraction', 'postmortem', ...
  cost_usd REAL NOT NULL,
  ts TEXT NOT NULL
);

-- append-only. Rollback = repoint is_active, never rewrite a row (brief section 5/6).
-- Coexists with data/models/*.json artifact files (Phase 2): this table owns
-- VERSIONING/active/rollback state; artifact_path points at the file rather than
-- duplicating it. data/models/ACTIVE.json is a generated pointer written by
-- registry.py whenever is_active changes -- a cache of this table, never hand-edited.
CREATE TABLE IF NOT EXISTS model_versions (
  id INTEGER PRIMARY KEY,
  model_id TEXT NOT NULL,           -- artifact key ('m1_curves', ...) or ledger id ('m3_evidence@deepseek')
  version_tag TEXT NOT NULL,        -- e.g. 'v3'; human-readable, not semver-enforced
  artifact_path TEXT NOT NULL,      -- e.g. 'data/models/m1_curves_v3.json'; content immutable once written
  params_hash TEXT NOT NULL,        -- sha256 of the artifact file, for integrity verification
  fit_window_start TEXT, fit_window_end TEXT,   -- walk-forward train window; NULL for hand-set v1 defaults
  registered_ts TEXT NOT NULL,      -- challengers earn track record only from forecasts after this
  promoted_ts TEXT,                 -- NULL while still a challenger
  retired_ts TEXT,                  -- NULL while active
  retired_reason TEXT CHECK(retired_reason IN ('replaced','rollback') OR retired_reason IS NULL),
  is_active INTEGER DEFAULT 0       -- exactly one active row per model_id; enforced in registry.py + index
);

-- derived from forecasts + resolutions; always recomputable, not a backup-critical table.
-- Phase 14 (brief section 6/14): the Kelly-fraction wealth process already used by the
-- shadow portfolio (Phase 6), generalized to every model as a scoring/selection layer --
-- NOT a second trading simulation. Unlike shadow_trades (M4-only, entry-filtered, "would
-- we have traded"), this table scores EVERY resolved forecast from EVERY model
-- unconditionally, maximizing n for comparison purposes. log_wealth_delta is the Kelly
-- log-growth for a binary bet, same side rule as shadow_trades (YES if p_model > p_market,
-- else NO). forecast_id is additive beyond the brief's literal DDL -- an idempotency key
-- (every other column keeps its documented meaning) since a model can forecast the same
-- market many times before resolution and cum_log_wealth/n_forecasts are running sums.
CREATE TABLE IF NOT EXISTS wealth_ledger (
  id INTEGER PRIMARY KEY,
  model_id TEXT NOT NULL,
  category TEXT NOT NULL,
  condition_id TEXT NOT NULL,
  event_id TEXT,                     -- for event-level attribution, mirrors eval's clustering
  forecast_id INTEGER NOT NULL,      -- FK to forecasts.id; idempotency key
  ts TEXT NOT NULL,                  -- resolution timestamp
  kelly_fraction REAL NOT NULL,      -- same 0.2x-capped fraction as shadow_trades
  log_wealth_delta REAL NOT NULL,    -- log(1 - f + f/price) if the bet won, log(1 - f) if lost
  cum_log_wealth REAL NOT NULL,      -- running sum for this (model_id, category)
  n_forecasts INTEGER NOT NULL       -- running count; cum_log_wealth / n_forecasts is the fair,
                                      -- coverage-normalized comparison metric (sleeping-expert rule)
);

CREATE INDEX IF NOT EXISTS idx_forecasts_condition ON forecasts(condition_id);
CREATE INDEX IF NOT EXISTS idx_forecasts_model_ts ON forecasts(model_id, ts);
CREATE INDEX IF NOT EXISTS idx_markets_tier ON markets(tier);
CREATE INDEX IF NOT EXISTS idx_llm_spend_date ON llm_spend(date);
CREATE INDEX IF NOT EXISTS idx_model_versions_model ON model_versions(model_id);
-- DB-level backstop for the single-active invariant (registry.py also enforces it).
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_versions_active
  ON model_versions(model_id) WHERE is_active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_wealth_ledger_forecast ON wealth_ledger(forecast_id);
CREATE INDEX IF NOT EXISTS idx_wealth_ledger_model_category ON wealth_ledger(model_id, category);

-- Phase 17 (v2.4) item 1: every venue-native tag/series that didn't match any
-- entry in data/categories.yaml's taxonomy and fell back to 'unknown' -- makes
-- taxonomy drift (a renamed Gamma tag, a new Kalshi series) visible instead of
-- silently diluting the 'unknown' bucket.
CREATE TABLE IF NOT EXISTS category_drift_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  venue TEXT NOT NULL,
  raw_tag TEXT NOT NULL,
  fallback_category TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_category_drift_venue ON category_drift_log(venue, raw_tag);

-- Phase 15 (v2.3/v2.7): every market considered and excluded from the universe,
-- with a reason code -- answers "why isn't X in the ledger" and defends against
-- selection-bias claims in review (brief section 5/15). No CHECK constraint on
-- reason_code: it is deliberately an open, non-exhaustive enum (see
-- collect/universe.py for which codes are actually populated today).
CREATE TABLE IF NOT EXISTS universe_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  venue TEXT NOT NULL, venue_native_id TEXT NOT NULL,
  reason_code TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_universe_log_ts ON universe_log(ts);
CREATE INDEX IF NOT EXISTS idx_universe_log_reason ON universe_log(reason_code);
CREATE INDEX IF NOT EXISTS idx_universe_log_venue_native ON universe_log(venue, venue_native_id);
"""


# venue -> (trust_tier, forecastable, in_m7_pool). Brief section 5/16: Polymarket
# and Kalshi are real-money and forecastable; Metaculus (reputation-scored) feeds
# M7's external pool but is never itself a forecast target; Manifold (play-money)
# feeds event mapping and M2 base rates only -- excluded from M7 and forecasting.
VENUE_SEEDS: tuple[tuple[str, str, int, int], ...] = (
    ("polymarket", "money", 1, 0),
    ("kalshi", "money", 1, 1),
    ("metaculus", "reputation", 0, 1),
    ("manifold", "play", 0, 0),
)


def venue_condition_id(venue: str, native_id: str) -> str:
    """Synthesized market key for non-Polymarket rows (brief section 5):
    condition_id stays the universal key so every existing FK, the forecasts
    ledger, and the snapshot layout keep working unchanged. Polymarket's own
    condition_id (its native hash) is used as-is, never prefixed."""
    return native_id if venue == "polymarket" else f"{venue}:{native_id}"


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def migrate_multi_venue(conn: sqlite3.Connection) -> dict[str, bool]:
    """Idempotent v1.9 migration (Phase 10): ALTER `markets` with venue columns
    (CREATE TABLE IF NOT EXISTS above can't add columns to a table that already
    exists) and seed the `venues` table. Safe to call on every connect() --
    each step checks before acting, so a second run is a no-op. Never rewrites
    or drops anything; a fresh DB gets the columns from the first connect()
    with no separate migration event.
    """
    applied = {"venue_column": False, "venue_native_id_column": False, "event_id_column": False}
    if not _column_exists(conn, "markets", "venue"):
        conn.execute("ALTER TABLE markets ADD COLUMN venue TEXT DEFAULT 'polymarket'")
        applied["venue_column"] = True
    if not _column_exists(conn, "markets", "venue_native_id"):
        conn.execute("ALTER TABLE markets ADD COLUMN venue_native_id TEXT")
        applied["venue_native_id_column"] = True
        # Backfill: for pre-existing (Polymarket) rows, the native id IS the
        # condition_id -- new venues populate this at insert time instead.
        conn.execute(
            "UPDATE markets SET venue_native_id = condition_id WHERE venue_native_id IS NULL"
        )
    if not _column_exists(conn, "markets", "event_id"):
        conn.execute("ALTER TABLE markets ADD COLUMN event_id TEXT")
        applied["event_id_column"] = True
    conn.execute("CREATE INDEX IF NOT EXISTS idx_markets_venue ON markets(venue)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_id)")
    for venue, trust_tier, forecastable, in_m7_pool in VENUE_SEEDS:
        conn.execute(
            "INSERT OR IGNORE INTO venues(venue, trust_tier, forecastable, in_m7_pool) "
            "VALUES (?, ?, ?, ?)",
            (venue, trust_tier, forecastable, in_m7_pool),
        )
    conn.commit()
    return applied


def migrate_eval_measurement_upgrade(conn: sqlite3.Connection) -> dict[str, bool]:
    """Idempotent v2.1 migration (Phase 11): ALTER `eval_runs` with the venue/
    category dimension, the anytime-valid confidence sequence columns, and the
    precision-weighted stratified estimator columns (CREATE TABLE IF NOT EXISTS
    in SCHEMA can't add columns to a table that already has rows). Safe to call
    on every connect() -- each column checks before adding, so a second run is
    a no-op. Old rows keep venue/category NULL (legacy pooled snapshots from
    before this migration), never rewritten.
    """
    applied = {
        "venue_column": False, "category_column": False,
        "skill_pw_column": False, "skill_pw_ci_lo_column": False,
        "skill_pw_ci_hi_column": False, "n_strata_pw_column": False,
        "cs_lo_column": False, "cs_hi_column": False,
        "cs_covers_zero_column": False, "n_event_clusters_column": False,
    }
    columns = {
        "venue_column": ("venue", "TEXT"),
        "category_column": ("category", "TEXT"),
        "skill_pw_column": ("skill_pw", "REAL"),
        "skill_pw_ci_lo_column": ("skill_pw_ci_lo", "REAL"),
        "skill_pw_ci_hi_column": ("skill_pw_ci_hi", "REAL"),
        "n_strata_pw_column": ("n_strata_pw", "INTEGER"),
        "cs_lo_column": ("cs_lo", "REAL"),
        "cs_hi_column": ("cs_hi", "REAL"),
        "cs_covers_zero_column": ("cs_covers_zero", "INTEGER"),
        "n_event_clusters_column": ("n_event_clusters", "INTEGER"),
    }
    for key, (column, sql_type) in columns.items():
        if not _column_exists(conn, "eval_runs", column):
            conn.execute(f"ALTER TABLE eval_runs ADD COLUMN {column} {sql_type}")
            applied[key] = True
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_runs_venue_category ON eval_runs(venue, category)"
    )
    conn.commit()
    return applied


def migrate_distributional_scoring(conn: sqlite3.Connection) -> dict[str, bool]:
    """Idempotent v2.4 migration (Phase 16): ALTER `eval_runs` with nullable
    RPS columns. RPS is a secondary outcome on the SAME row `evaluate_model`
    already writes (venue/category/window) -- not a new table -- populated
    only for (model, venue, category, window) combinations with enough
    resolved bucketed events; NULL everywhere else, same convention as every
    other optional metric column already in this table.
    """
    applied = {"rps_column": False, "rps_market_column": False}
    for key, (column, sql_type) in {
        "rps_column": ("rps", "REAL"),
        "rps_market_column": ("rps_market", "REAL"),
    }.items():
        if not _column_exists(conn, "eval_runs", column):
            conn.execute(f"ALTER TABLE eval_runs ADD COLUMN {column} {sql_type}")
            applied[key] = True
    conn.commit()
    return applied


class ForecastLedgerViolation(RuntimeError):
    """Raised on any attempt to UPDATE or DELETE a forecast row."""


def _authorizer(action: int, arg1: str | None, arg2, db_name, trigger) -> int:
    if arg1 == "forecasts" and action in (sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the lab database with schema and guards applied."""
    path = Path(db_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Let the collector and orchestrator analytics connections wait on each
    # other instead of failing with "database is locked" under WAL.
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    migrate_multi_venue(conn)
    migrate_eval_measurement_upgrade(conn)
    migrate_distributional_scoring(conn)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (now_utc_iso(),)
    )
    # Forward migration: new tables above are created idempotently; bump the
    # recorded schema_version on pre-existing databases (INSERT OR IGNORE above
    # never updates it). No destructive change -- data is untouched.
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is not None and row[0] != SCHEMA_VERSION:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'", (SCHEMA_VERSION,)
        )
    conn.commit()
    conn.set_authorizer(_authorizer)
    return conn


def upsert_market(conn: sqlite3.Connection, row: dict) -> None:
    """Idempotent market upsert; preserves first_seen_ts across re-syncs.

    Venue-aware (v1.9, Phase 10) but fully backward-compatible: callers that
    don't pass venue/venue_native_id (every pre-Phase-10 call site) default to
    'polymarket' with venue_native_id = condition_id. `event_id` is deliberately
    excluded from the ON CONFLICT UPDATE -- a cross-venue link minted by
    `lab map confirm` must survive the next routine universe re-sync.
    """
    row = {"venue": "polymarket", "venue_native_id": row.get("condition_id"), "event_id": None, **row}
    conn.execute(
        """
        INSERT INTO markets (condition_id, slug, question, category, description,
                             end_date_iso, token_id_yes, token_id_no, neg_risk,
                             active, closed, liquidity_num, volume_num, tier,
                             venue, venue_native_id, event_id,
                             first_seen_ts, last_synced_ts)
        VALUES (:condition_id, :slug, :question, :category, :description,
                :end_date_iso, :token_id_yes, :token_id_no, :neg_risk,
                :active, :closed, :liquidity_num, :volume_num, :tier,
                :venue, :venue_native_id, :event_id,
                :now, :now)
        ON CONFLICT(condition_id) DO UPDATE SET
            slug=excluded.slug, question=excluded.question, category=excluded.category,
            description=excluded.description, end_date_iso=excluded.end_date_iso,
            token_id_yes=excluded.token_id_yes, token_id_no=excluded.token_id_no,
            neg_risk=excluded.neg_risk, active=excluded.active, closed=excluded.closed,
            liquidity_num=excluded.liquidity_num, volume_num=excluded.volume_num,
            tier=excluded.tier, last_synced_ts=excluded.last_synced_ts
        """,
        {**row, "now": now_utc_iso()},
    )


def link_event(conn: sqlite3.Connection, condition_id_a: str, condition_id_b: str,
              title: str | None = None) -> str:
    """Mint (or reuse) an event linking two venue-markets on human confirmation
    (brief section 5/Phase 10: "event_id minted on first human-confirmed
    cross-venue match"). Idempotent: re-linking the same pair is a no-op.
    Upserts a minimal placeholder row for either side not yet synced by its
    venue's own collector, so the link always succeeds.
    """
    import uuid

    for cid in (condition_id_a, condition_id_b):
        conn.execute(
            "INSERT OR IGNORE INTO markets (condition_id, venue, venue_native_id, tier, "
            "active, closed, first_seen_ts, last_synced_ts) VALUES (?, ?, ?, 'ignored', 0, 0, ?, ?)",
            (cid, cid.split(":", 1)[0] if ":" in cid else "polymarket",
             cid.split(":", 1)[1] if ":" in cid else cid, now_utc_iso(), now_utc_iso()),
        )
    rows = conn.execute(
        "SELECT condition_id, event_id FROM markets WHERE condition_id IN (?, ?)",
        (condition_id_a, condition_id_b),
    ).fetchall()
    existing = next((r["event_id"] for r in rows if r["event_id"]), None)
    event_id = existing or f"evt_{uuid.uuid4().hex[:16]}"
    if existing is None:
        conn.execute(
            "INSERT OR IGNORE INTO events(event_id, title, created_ts) VALUES (?, ?, ?)",
            (event_id, title, now_utc_iso()),
        )
    conn.execute(
        "UPDATE markets SET event_id = ? WHERE condition_id IN (?, ?) AND (event_id IS NULL OR event_id = ?)",
        (event_id, condition_id_a, condition_id_b, event_id),
    )
    conn.commit()
    return event_id


def record_resolution(
    conn: sqlite3.Connection,
    condition_id: str,
    resolved_ts: str,
    payout_yes: float,
    disputed: bool,
    source: str,
) -> None:
    """At-least-once, idempotent: replays of the same final payout are no-ops."""
    conn.execute(
        """
        INSERT INTO resolutions (condition_id, resolved_ts, payout_yes, disputed, source)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(condition_id) DO UPDATE SET
            resolved_ts=excluded.resolved_ts, payout_yes=excluded.payout_yes,
            disputed=excluded.disputed, source=excluded.source
        """,
        (condition_id, resolved_ts, payout_yes, int(disputed), source),
    )


def append_forecast(conn: sqlite3.Connection, row: dict) -> int:
    """The ONLY write path into the forecasts ledger. Insert-only by design."""
    cur = conn.execute(
        """
        INSERT INTO forecasts (ts, condition_id, model_id, p_yes, p_market_at_ts,
                               spread_at_ts, inputs_hash, evidence_run_id, cost_usd)
        VALUES (:ts, :condition_id, :model_id, :p_yes, :p_market_at_ts,
                :spread_at_ts, :inputs_hash, :evidence_run_id, :cost_usd)
        """,
        {
            "spread_at_ts": None,
            "inputs_hash": None,
            "evidence_run_id": None,
            "cost_usd": 0.0,
            **row,
        },
    )
    return cur.lastrowid


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a key/value pair into the meta table (allowed by the authorizer)."""
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def llm_spend_today(conn: sqlite3.Connection, date_utc: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM llm_spend WHERE date = ?", (date_utc,)
    ).fetchone()
    return float(row["total"])


def record_llm_spend(
    conn: sqlite3.Connection, date_utc: str, purpose: str, cost_usd: float
) -> None:
    conn.execute(
        "INSERT INTO llm_spend (date, purpose, cost_usd, ts) VALUES (?, ?, ?, ?)",
        (date_utc, purpose, cost_usd, now_utc_iso()),
    )
