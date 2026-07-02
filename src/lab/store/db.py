"""SQLite schema, migrations, and writers (single file data/lab.db, WAL mode).

The forecasts table is append-only: the guarded connection installs SQLite
authorizer callbacks that hard-fail any UPDATE or DELETE against it
(guardrail: immutable forecast ledger).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lab.util import PROJECT_ROOT, now_utc_iso

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
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

CREATE INDEX IF NOT EXISTS idx_forecasts_condition ON forecasts(condition_id);
CREATE INDEX IF NOT EXISTS idx_forecasts_model_ts ON forecasts(model_id, ts);
CREATE INDEX IF NOT EXISTS idx_markets_tier ON markets(tier);
CREATE INDEX IF NOT EXISTS idx_llm_spend_date ON llm_spend(date);
"""


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
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (now_utc_iso(),)
    )
    conn.commit()
    conn.set_authorizer(_authorizer)
    return conn


def upsert_market(conn: sqlite3.Connection, row: dict) -> None:
    """Idempotent market upsert; preserves first_seen_ts across re-syncs."""
    conn.execute(
        """
        INSERT INTO markets (condition_id, slug, question, category, description,
                             end_date_iso, token_id_yes, token_id_no, neg_risk,
                             active, closed, liquidity_num, volume_num, tier,
                             first_seen_ts, last_synced_ts)
        VALUES (:condition_id, :slug, :question, :category, :description,
                :end_date_iso, :token_id_yes, :token_id_no, :neg_risk,
                :active, :closed, :liquidity_num, :volume_num, :tier,
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
