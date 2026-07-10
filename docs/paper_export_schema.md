# Paper replication export schema

`lab export --paper --out <path>` writes two files:

- `<path>` — one JSON object per line (JSONL), one row per resolved forecast
  from every model. Reuses the same paired forecast+resolution+market query
  `lab eval` scores on (`eval/run.py::resolved_forecast_rows`), including its
  forward-only challenger filter (a versioned model never leaks rows from
  before its own `registered_ts`) and its `resolutions.disputed = 0`
  exclusion — this export is provably consistent with what was actually
  scored, not a separate reconstruction.
- `<path>.meta.json` — a manifest: the exact code version and schema version
  that produced the export, when it was generated, and how many rows it
  contains, so a reviewer can verify what they are re-analyzing.

No PII exists anywhere in this schema (there are no user/account records at
all), so "anonymized" here means exactly one thing: internal/operational
fields (`cost_usd`, `evidence_run_id`, `inputs_hash`, and anything from
`evidence_runs`, which holds scraped article text) are excluded — nothing
else needs redacting.

## Row fields

| Field | Type | Meaning |
|---|---|---|
| `condition_id` | string | Market key (synthesized `{venue}:{native_id}` for non-Polymarket rows). |
| `venue` | string | `polymarket`, `kalshi`, `metaculus`, `manifold`. |
| `category` | string | Internal taxonomy category (`data/categories.yaml`). |
| `tier` | string | `liquid`, `tail`, or `ignored` at forecast time. |
| `model_id` | string | Forecaster identity, e.g. `m0_market`, `m1_hier@kalshi`, `m3_evidence@deepseek`. |
| `forecast_ts` | string (ISO 8601 UTC) | When this forecast was frozen in the ledger. |
| `p_yes` | float (0,1) | The model's forecast probability. |
| `p_market_at_ts` | float (0,1) | The market's own price at the same freeze moment. |
| `spread_at_ts` | float or null | Bid/ask spread at freeze time, if available. |
| `resolved_ts` | string (ISO 8601 UTC) | When the market resolved. |
| `payout_yes` | float (0.0 or 1.0) | The resolved outcome. |
| `event_id` | string or null | Cross-venue/negRisk event cluster id, for event-level clustering (null if this market was never linked to one). |
| `m3_randomized` | int (0 or 1) | Phase 15 boundary-randomization tag: 1 iff this M3 forecast was a coin-flip member of the K-10..K+10 liquidity band. Always 0 for non-M3 models. |
| `m3_random_seed` | string or null | The seed used, when `m3_randomized = 1`; null otherwise. |

## Manifest fields (`<path>.meta.json`)

| Field | Meaning |
|---|---|
| `code_version` | `process_guard.code_version()` — a deterministic sha1 (first 12 hex chars) over every `.py` file under `src/lab` plus `config.yaml`. Identical across two checkouts with identical bytes; changes whenever the code that produced the export changes. |
| `schema_version` | The database schema version (`meta.schema_version`) at export time. |
| `generated_at` | ISO 8601 UTC timestamp of the export run. |
| `row_count` | Number of rows in the JSONL file. |
| `fields` | The exact field list above, for a quick sanity check against this document. |

## Automated weekly snapshot

Since v2.8, `docs/paper_exports/YYYY-MM-DD.jsonl` (+ matching
`YYYY-MM-DD.jsonl.meta.json`) is produced automatically every week
(`paper_export.cron`, default Sunday 05:00 UTC) and committed to this public
repo, using the exact schema and manifest fields documented above — the CLI's
manual `lab export --paper --out <path>` flow is unaffected and remains
available for one-off exports. See `src/lab/paper_export.py`.
