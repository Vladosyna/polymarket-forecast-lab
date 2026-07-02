"""`lab export`: latest forecast per (market, model) + metadata as JSONL.

This is the downstream integration point (brief section 13). Schema per line:
{condition_id, slug, question, category, end_date_iso, tier, model_id, ts,
 p_yes, p_market_at_ts, spread_at_ts}
"""

from __future__ import annotations

import json
from typing import Iterator

EXPORT_FIELDS = [
    "condition_id", "slug", "question", "category", "end_date_iso", "tier",
    "model_id", "ts", "p_yes", "p_market_at_ts", "spread_at_ts",
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
