"""`lab export`: latest forecast per (market, model) + metadata as JSONL.

This is the downstream integration point (brief section 13). Schema per line:
{condition_id, slug, question, category, end_date_iso, tier, model_id, ts,
 p_yes, p_market_at_ts, spread_at_ts}

`lab export --paper` (Phase 15) is a second, independent export: the full
resolved-forecast replication dataset for the eventual paper, plus a manifest
(code version hash, schema version, row count) so a reviewer can verify what
they're re-analyzing. See EXPORT_PAPER_FIELDS/export_paper_rows below.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from lab.store import db as dbmod
from lab.util import now_utc_iso

EXPORT_FIELDS = [
    "condition_id", "slug", "question", "category", "end_date_iso", "tier",
    "model_id", "ts", "p_yes", "p_market_at_ts", "spread_at_ts",
]

# Phase 15 replication export. Deliberately excludes cost_usd, evidence_run_id,
# and inputs_hash (internal/operational, not needed to re-analyze results) and
# never joins evidence_runs (holds scraped article text) -- this schema has no
# PII anywhere, so "anonymized" means exactly this: operational fields out,
# nothing else to redact.
EXPORT_PAPER_FIELDS = [
    "condition_id", "venue", "category", "tier", "model_id", "forecast_ts",
    "p_yes", "p_market_at_ts", "spread_at_ts", "resolved_ts", "payout_yes",
    "event_id", "m3_randomized", "m3_random_seed",
]


def export_rows(conn) -> Iterator[dict]:
    rows = conn.execute(
        """
        SELECT m.condition_id, m.slug, m.question, m.category, m.end_date_iso, m.tier,
               f.model_id, f.ts, f.p_yes, f.p_market_at_ts, f.spread_at_ts
        FROM forecasts f
        JOIN markets m ON m.condition_id = f.condition_id
        JOIN (SELECT condition_id, model_id, MAX(ts) AS ts FROM forecasts
              GROUP BY condition_id, model_id) latest
          ON latest.condition_id = f.condition_id AND latest.model_id = f.model_id
             AND latest.ts = f.ts
        ORDER BY m.condition_id, f.model_id
        """
    )
    for r in rows:
        yield {k: r[k] for k in EXPORT_FIELDS}


def export_jsonl(conn) -> Iterator[str]:
    for row in export_rows(conn):
        yield json.dumps(row, ensure_ascii=False)


def export_paper_rows(conn) -> Iterator[dict]:
    """Every resolved forecast from every model, projected to
    EXPORT_PAPER_FIELDS -- the paper-grade replication dataset (Phase 15).

    Reuses `resolved_forecast_rows` (the same paired forecast+resolution+
    market query `lab eval` scores on) per model_id rather than reinventing
    the join, so this export is provably consistent with what was actually
    scored -- including its forward-only challenger filter (a version never
    leaks rows from before its own registered_ts) and its `disputed = 0`
    exclusion.
    """
    from lab.eval.run import resolved_forecast_rows

    model_ids = [r["model_id"] for r in conn.execute(
        "SELECT DISTINCT model_id FROM forecasts ORDER BY model_id"
    )]
    for model_id in model_ids:
        for row in resolved_forecast_rows(conn, model_id, None):
            row["model_id"] = model_id
            yield {k: row[k] for k in EXPORT_PAPER_FIELDS}


def export_paper_jsonl(conn) -> Iterator[str]:
    for row in export_paper_rows(conn):
        yield json.dumps(row, ensure_ascii=False)


def paper_export_manifest(conn, row_count: int) -> dict[str, Any]:
    """Code version hash + schema version + row count -- lets a reviewer
    verify what they're re-analyzing (brief section 15's "code version hash
    + schema documentation"). Reuses process_guard.code_version(), the same
    deterministic, content-based hash `lab ps` already reports -- not a new
    git-based hash."""
    from lab.process_guard import code_version

    return {
        "code_version": code_version(),
        "schema_version": dbmod.SCHEMA_VERSION,
        "generated_at": now_utc_iso(),
        "row_count": row_count,
        "fields": EXPORT_PAPER_FIELDS,
    }
