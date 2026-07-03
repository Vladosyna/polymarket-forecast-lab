# Polymarket Forecast Lab — Implementation Brief for Claude Code

**v1.8** — cross-venue signal (M7): Kalshi public market-data endpoints verified (no account, no keys) and promoted to a concrete read-only source alongside the Metaculus public API; curated question-matching via `markets_map.yaml` (LLM proposes, human confirms); Phase 9, optional and gated on Phase 6. Also clarified where more external data helps (M2 base rates, forecast-time features) vs hurts (pooling foreign-venue biases into M1).

**Read this entire document before writing any code.** It is the single source of truth for this project. Work through the phases in order. Each phase has acceptance criteria — do not move to the next phase until they pass.

---

## 1. What we are building (and what we are NOT building)

**Goal.** A read-only research system that answers one question with statistical rigor:

> *Can we produce probability estimates for Polymarket event outcomes that are better calibrated than the market price itself, measured after resolution?*

The system continuously collects public market data, generates timestamped probability forecasts from several models, freezes them in an append-only ledger, waits for markets to resolve, and scores every model against the market baseline (Brier score, log loss, calibration curves). A simulated "shadow portfolio" translates any measured edge into hypothetical P&L. No real money is ever involved.

**Scope invariants of this repository (see §13 for the open-source model — these define what this repo is, not what its users may do):**

1. **This repository contains no execution code — by scope, not ideology.** It is a measurement instrument, published under MIT. Order placement, wallets, private keys, token allowances, L1/L2 CLOB authentication, EIP-712 signing all belong in downstream projects that consume this lab's forecast contract (§13). Inside this repo, "buy" and "sell" appear only in simulation code clearly labeled as such.
2. **Public endpoints only.** Use only unauthenticated, public REST/WebSocket endpoints. No account creation on Polymarket, no API keys for Polymarket (LLM API keys are fine).
3. **No geoblock circumvention.** No VPNs, no proxies, no routing tricks. If an endpoint is unreachable from the operator's network, log it and fall back to historical/offline data sources. Never work around access restrictions.
4. **Polite API citizenship.** Global rate limiter, exponential backoff on 429/5xx, conservative polling cadence, response caching where sensible.
5. **Immutable forecast ledger.** Once a forecast row is written, it is never updated or deleted. Scoring happens in separate tables. This is what makes the calibration measurement honest.

**Why this is legally clean (context, not legal advice):** reading public market data is research; forecasting is research; simulated positions involve no stake, so no gambling activity occurs; no Polymarket account or ToS acceptance is required for public data. This system is equivalent to what academic groups do with the same data.

---

## 2. Tech stack (decided — do not substitute)

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | |
| Env/deps | `uv` | `pyproject.toml`, locked |
| HTTP | `httpx` (async) + `tenacity` | thin hand-written clients; do NOT use `py-clob-client` (trading SDK, auth baggage we must not have) |
| Storage: state | SQLite via `sqlite3` stdlib | single file `data/lab.db`, WAL mode |
| Storage: time series | Parquet via `polars` | partitioned by date under `data/snapshots/` |
| Schemas | `pydantic` v2 | all API responses validated |
| Scheduling | `APScheduler` inside a long-running `collect` process | run under tmux/systemd |
| LLM | Anthropic API (`anthropic` SDK), model configurable, default a current Sonnet-class model | strict JSON outputs, daily cost cap |
| Math/eval | `numpy`, `scipy` | bootstrap CIs |
| Plots/report | `matplotlib` + `jinja2` → static HTML | no web server in core |
| CLI | `typer` | `lab <command>` |
| Tests | `pytest` | scoring math is test-critical |
| Dashboard (Phase 6, optional) | Streamlit | reads the same SQLite/Parquet |

Secrets: `.env` with `ANTHROPIC_API_KEY` and optional `NEWSAPI_KEY`. **A test must fail if any code imports web3/eth-account or references the CLOB `POST /order` path** (see §9).

---

## 3. Data sources — quick reference

All public, no auth. As of mid-2026 (post CLOB V2 migration, April 2026):

- **Gamma API** — `https://gamma-api.polymarket.com`
  Market/event metadata, resolution criteria, token IDs. Endpoints: `/markets`, `/events`. Pagination via `limit` + `offset` (limits reduced in 2026 — assume ≤100 per page and paginate). No free-text search; filter by `slug`, tag, `active`, `closed`.
- **CLOB API (public market data only)** — `https://clob.polymarket.com`
  `/book?token_id=` (order book), `/price?token_id=&side=`, `/midpoint?token_id=`, `/prices-history?market=<token_id>&interval=&fidelity=` (price time series).
- **Data API** — `https://data-api.polymarket.com`
  Public trades, holder/position aggregates.
- **WebSocket (market channel, public)** — `wss://ws-subscriptions-clob.polymarket.com/ws/`
  Real-time book updates. Use for the liquid tier if REST polling proves too coarse; start with REST.
- **Historical bootstrap** — HuggingFace dataset `SII-WANGZJ/Polymarket_data` (1.1B records, 268,706 markets; also mirrored on GitHub). Covers Polymarket's inception through end of 2025 — it is a static snapshot, not a live feed. 2026-onward data comes from our own Phase 1 collector, not this dataset. Pull only two of its five files:
  - `quant.parquet` (~21–27GB) — cleaned trades unified to the YES token; this is the "prices at multiple horizons" source for M1/M2.
  - `markets.parquet` (~68–165MB) — market metadata and resolution outcomes; joins to `quant.parquet` on market id.
  Skip `orderfilled.parquet`, `trades.parquet`, `users.parquet` (85GB combined, raw/user-level — not needed for fitting). Fetch via `huggingface_hub`:
  ```python
  from huggingface_hub import hf_hub_download
  hf_hub_download(repo_id="SII-WANGZJ/Polymarket_data", filename="quant.parquet", repo_type="dataset", local_dir="data/bootstrap/")
  hf_hub_download(repo_id="SII-WANGZJ/Polymarket_data", filename="markets.parquet", repo_type="dataset", local_dir="data/bootstrap/")
  ```
  Then filter with `polars.scan_parquet(...)` (lazy) down to resolved binary markets — never load either file fully into memory.
- **External model inputs (for M5):**
  - *Weather:* Open-Meteo **Ensemble API** — `https://api.open-meteo.com/v1/ensemble?latitude=&longitude=&hourly=<vars>&models=<model>` — returns per-member forecasts (up to 51 members), which is what P(threshold exceeded) actually needs. The plain `/v1/forecast` endpoint returns one deterministic value and is not sufficient here. No key, CC BY 4.0 (attribute in README), free tier caps at 10,000 calls/day — irrelevant at our scale. `api.weather.gov` (NWS) as a secondary cross-check for US station observations only.
  - *Macro:* **FRED API is primary**, not optional — it cleanly mirrors both Atlanta Fed nowcasts as documented series: `GDPNOW` (real GDP growth) and `PCENOW` (real PCE growth). Free key from `fredaccount.stlouisfed.org`, stored as `FRED_API_KEY`. Request pattern: `https://api.stlouisfed.org/fred/series/observations?series_id=GDPNOW&api_key=...&file_type=json`. Cleveland Fed's separate CPI/PCE inflation nowcast has no confirmed stable machine endpoint as of this writing — its page is `clevelandfed.org/indicators-and-data/inflation-nowcasting`; inspect its actual download mechanism at Phase 5 implementation time rather than hardcoding a guessed URL, and skip that specific sub-adapter if none is found. GDPNow alone already covers the growth side of the macro category.
  - *Cross-venue prices (for M7 — verified public, no accounts, no keys):* **Kalshi** — `https://external-api.kalshi.com/trade-api/v2` (`/series`, `/events`, `/markets`, `/markets/{ticker}/orderbook`); market data requires no auth; ~10 req/s — route through the same global rate limiter. **Metaculus** — official public API (`metaculus.com/api`), community prediction on public questions; a different crowd (reputation-scored forecasters, no money), strongest as an independent signal on long-horizon questions. Manifold is public too but play-money — skip. Betfair requires an account — out of scope.
  - *M1 warning:* never pool other venues' resolved markets into M1's recalibration fit — M1 models Polymarket-specific bias, and other venues carry different (even opposite) tail biases. External resolved questions may feed M2 base rates only.

Domain facts to encode:
- Every binary market has two outcome tokens (YES/NO); prices are in [0,1] and the pair sums to ≈ 1. Track the YES token; store its `token_id` and the `condition_id` of the market.
- Markets resolve via UMA optimistic oracle; there is a dispute window (~2h) and occasional contested resolutions. The resolution watcher must record final payout, not first report. Flag disputed markets.
- Metadata can exist for markets with no live book. Filter universe on `active`, `closed`, `enableOrderBook`/`accepting_orders`, and liquidity/volume fields.
- Rate limits are generous for reads (thousands per 10s) but poll far below them: default snapshot cadence 5 min for the liquid tier, 60 min for the tail. One universe sync per hour.

**Universe policy (from the edge research — encode in `config.yaml`):**

- **Priority tiers for forecasting:** (P1) economic data releases & central-bank decisions — model-drivable; (P2) weather markets where present — model-drivable; (P3) long-horizon (≥30 days to resolution) politics/geopolitics — documented long-horizon underconfidence makes recalibration the edge; (P4) entertainment/awards — base-rate drivable.
- **Excluded as structurally unforecastable:** ALL crypto/equity price-target markets at any horizon (martingale underlyings — the market price *is* the forecast); "will X say/tweet Y" novelty markets and anything with ambiguous resolution wording; markets already priced ≥ 0.95 or ≤ 0.05 (residual edge there is dominated by oracle/dispute tail risk — keep them in calibration stats, never as forecast targets).
- **Null control:** keep a small random sample of sports markets in the ledger, forecast by the cheap models only. Sports markets are near-efficient; if the lab "finds skill" there, the harness is broken, not the market. The weekly report prints null-control skill next to everything else.

---

## 4. Repository layout

```
forecast-lab/
├── pyproject.toml
├── .env.example
├── config.yaml                  # universe filters, cadences, thresholds, cost caps
├── README.md                    # generated in Phase 0: quickstart + commands
├── src/lab/
│   ├── api/                     # thin public-data clients
│   │   ├── gamma.py
│   │   ├── clob.py
│   │   └── dataapi.py
│   ├── store/
│   │   ├── db.py                # sqlite schema + migrations + writers
│   │   └── snapshots.py         # parquet append/read
│   ├── collect/
│   │   ├── universe.py          # market discovery & tiering
│   │   ├── snapshots.py         # book/price snapshot loop
│   │   └── resolutions.py       # resolution watcher
│   ├── models/
│   │   ├── base.py              # Forecaster protocol: forecast(market, context) -> p_yes
│   │   ├── m0_market.py
│   │   ├── m1_debiased.py
│   │   ├── m2_baserate.py
│   │   ├── m3_evidence.py
│   │   ├── m5_nowcast.py        # + thin per-category data adapters (weather, macro)
│   │   ├── m6_consistency.py
│   │   ├── m7_crossvenue.py     # + api/kalshi.py, api/metaculus.py thin read-only clients (Phase 9)
│   │   └── m4_ensemble.py
│   ├── news/
│   │   ├── providers.py         # RSS + Google News RSS per market; NewsAPI optional
│   │   ├── extract.py           # LLM evidence extraction (strict JSON)
│   │   └── aggregate.py         # deterministic log-odds aggregation
│   ├── eval/
│   │   ├── scoring.py           # brier, log loss, paired skill
│   │   ├── calibration.py       # reliability diagrams
│   │   ├── clv.py               # price-drift metric
│   │   └── report.py            # jinja2 HTML report
│   ├── learn/
│   │   ├── refit.py             # scheduled parameter refits (M1 curves, M2 rates, M3 aggregator, M5 error dists, M4 weights)
│   │   ├── registry.py          # model_versions CRUD, promotion gate, rollback circuit breaker
│   │   └── postmortem.py        # structured post-mortems on top-decile misses/wins
│   ├── shadow/
│   │   └── portfolio.py         # simulated positions
│   └── cli.py
├── tests/
├── reports/                     # generated HTML (gitignored)
└── data/                        # sqlite + parquet (gitignored)
```

---

## 5. Database schema (SQLite)

```sql
CREATE TABLE meta (
  key TEXT PRIMARY KEY,             -- 'schema_version', 'created_at', ...
  value TEXT
);

-- discovered markets (binary only, v1)
CREATE TABLE markets (
  condition_id TEXT PRIMARY KEY,
  slug TEXT, question TEXT, category TEXT,
  description TEXT,               -- verbatim resolution criteria; critical for M3
  end_date_iso TEXT,
  token_id_yes TEXT, token_id_no TEXT,
  neg_risk INTEGER DEFAULT 0,
  active INTEGER, closed INTEGER,
  liquidity_num REAL, volume_num REAL,
  tier TEXT CHECK(tier IN ('liquid','tail','ignored')),
  first_seen_ts TEXT, last_synced_ts TEXT
);

CREATE TABLE resolutions (
  condition_id TEXT PRIMARY KEY REFERENCES markets(condition_id),
  resolved_ts TEXT,
  payout_yes REAL CHECK(payout_yes IN (0.0, 1.0)),
  disputed INTEGER DEFAULT 0,
  source TEXT                      -- 'gamma' etc.
);

-- append-only. NEVER UPDATE OR DELETE ROWS.
CREATE TABLE forecasts (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,                -- UTC ISO, freeze moment
  condition_id TEXT NOT NULL,
  model_id TEXT NOT NULL,          -- 'm0_market', 'm1_debiased', ...
  p_yes REAL NOT NULL CHECK(p_yes > 0 AND p_yes < 1),
  p_market_at_ts REAL NOT NULL,    -- YES mid captured at the same moment
  spread_at_ts REAL,
  inputs_hash TEXT,                -- sha256 over: model code version, curve/artifact versions, config hash, input snapshot ids
  evidence_run_id INTEGER,         -- FK for m3
  cost_usd REAL DEFAULT 0
);

CREATE TABLE evidence_runs (
  id INTEGER PRIMARY KEY,
  ts TEXT, condition_id TEXT,
  dossier_json TEXT,               -- articles fetched, extraction output, aggregation trace
  llm_model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL
);

CREATE TABLE eval_runs (
  id INTEGER PRIMARY KEY,
  ts TEXT, model_id TEXT, window_label TEXT,
  n INTEGER,
  brier REAL, brier_market REAL, skill REAL,          -- skill = brier_market - brier (positive = we beat market)
  skill_ci_lo REAL, skill_ci_hi REAL,                  -- cluster bootstrap by condition_id
  log_loss REAL, log_loss_market REAL,
  calibration_json TEXT                                -- bins for reliability diagram
);

CREATE TABLE shadow_trades (
  id INTEGER PRIMARY KEY,
  opened_ts TEXT, condition_id TEXT, token_side TEXT CHECK(token_side IN ('YES','NO')),
  entry_price REAL, p_model REAL, p_market REAL, edge REAL,
  stake_sim REAL, kelly_frac REAL,
  exit_ts TEXT, exit_price REAL, pnl_sim REAL,
  status TEXT CHECK(status IN ('open','resolved','abandoned'))
);

CREATE TABLE postmortems (
  id INTEGER PRIMARY KEY,
  ts TEXT, condition_id TEXT, model_id TEXT,
  kind TEXT CHECK(kind IN ('miss','win')),
  brier_model REAL, brier_market REAL,
  analysis_json TEXT,              -- structured: error_source, evidence_quality, resolution_reading, notes
  llm_model TEXT, cost_usd REAL
);

-- append-only. Rollback = repoint is_active, never rewrite a row.
-- Coexists with the data/models/*.json artifact files from Phase 2: this table is authoritative
-- for VERSIONING (active/promoted/retired state, audit trail); the JSON files remain artifact
-- storage. artifact_path points at the existing file rather than duplicating its contents.
-- data/models/ACTIVE.json is a generated pointer, rewritten by registry.py whenever is_active
-- changes — it is a cache of this table, never edited independently.
CREATE TABLE model_versions (
  id INTEGER PRIMARY KEY,
  model_id TEXT NOT NULL,           -- 'm1_debiased', 'm3_evidence', 'm4_ensemble', ...
  version_tag TEXT NOT NULL,        -- e.g. 'v3'; human-readable, not semver-enforced
  artifact_path TEXT NOT NULL,      -- e.g. 'data/models/m1_debiased_v3.json'; file content immutable once written
  params_hash TEXT NOT NULL,        -- sha256 of the artifact file, for integrity verification
  fit_window_start TEXT, fit_window_end TEXT,   -- walk-forward train window; NULL for hand-set v1 defaults
  registered_ts TEXT NOT NULL,      -- challengers earn track record only from forecasts after this
  promoted_ts TEXT,                 -- NULL while still a challenger
  retired_ts TEXT,                  -- NULL while active
  retired_reason TEXT CHECK(retired_reason IN ('replaced','rollback') OR retired_reason IS NULL),
  is_active INTEGER DEFAULT 0       -- exactly one active row per model_id; enforced in registry.py
);
```

Snapshots go to Parquet, not SQLite: columns `ts, condition_id, token_id_yes, best_bid, best_ask, mid, spread, bid_depth_usd, ask_depth_usd, last_trade_price`, partitioned `data/snapshots/date=YYYY-MM-DD/*.parquet`.

---

## 6. Forecast models

All models implement `Forecaster.forecast(market, context) -> ForecastResult(p_yes, meta)`. Probabilities are clamped to [0.01, 0.99] before writing.

**M0 `m0_market` — the null model.** `p_yes = market mid`. This is the baseline every other model must beat. It is written to the ledger like any other model so paired comparison is trivial.

**M1 `m1_debiased` — horizon-aware market recalibration.** The strongest documented, implementable edge: prediction-market prices are systematically **underconfident at long horizons** (calibration slope > 1 far from resolution, converging to ≈1 near resolution), and recent Polymarket data shows a **reversed favorite-longshot bias** at the tails — so never hardcode a bias direction; fit it. Implementation: logistic recalibration `logit(p̂) = α_h + β_h · logit(p_market)` fitted per time-to-resolution bucket (<7d, 7–30d, 30–90d, >90d) and, once n allows, per category, on historical resolved markets from the bootstrap. β_h > 1 at long horizons = extremizing. Guards: isotonic sanity check, monthly refit, every curve versioned, horizon bucket stored with each forecast.

**M2 `m2_baserate` — category base rates.** For question templates that recur ("Will X happen by DATE"), compute historical base rates by category from resolved markets and blend with market prior in log-odds space with a small fixed weight. This is a sanity-check model, expected to be weak alone.

**M3 `m3_evidence` — LLM evidence pipeline.** The interesting one. Strict separation of roles:

1. **Dossier build** (deterministic): question, verbatim resolution criteria from `markets.description`, end date, current price, 7-day price path.
2. **Retrieval** (pluggable `NewsProvider`): Google News RSS query built from market keywords + configurable RSS feed list; optional NewsAPI if key present. Dedup by URL, keep publish timestamps.
3. **Extraction** (LLM, strict JSON): for each article, extract evidence items:
   `{claim, direction: "for_yes"|"for_no"|"neutral", strength: 1-3, source_reliability: 1-3, relevance: 0.0-1.0, published_ts}`.
   The prompt must include the resolution criteria and instruct the model to judge relevance *to the resolution criteria*, not to the topic. Reject/retry on invalid JSON.
4. **Aggregation** (deterministic, unit-tested — **the LLM never writes the final number**):
   - Start from market prior: `L = logit(p_market)`.
   - Each item contributes `Δ = k · strength · reliability · relevance · sign(direction)` with recency decay `exp(-age_days/τ)`; defaults `k=0.15`, `τ=5`.
   - Cap total shift per run at ±0.8 log-odds. `p_yes = sigmoid(L + clipped ΣΔ)`.
5. **Cost control**: per-day USD cap in config; when exceeded, M3 skips markets (log it). Every run stored in `evidence_runs` with full trace.

Optional experiment `m3b_direct`: the LLM states a probability directly (with a calibration-focused prompt). Logged like any model. The lab will then *measure* whether deterministic aggregation beats direct LLM estimates — a genuinely useful result either way.

**M5 `m5_nowcast` — structural models for model-drivable categories.** The highest-conviction edge class in the literature: where an external quantitative model publishes a distribution, map it onto the market's resolution criteria and output a probability directly.
- *Weather markets:* pull ensemble forecasts (open-meteo / NWS) for the referenced station and date; compute P(threshold exceeded) from ensemble spread.
- *Econ releases / Fed:* Cleveland Fed nowcast, GDPNow → P(print lands in the market's bucket) via an error distribution fitted on historical release surprises.
One thin adapter per category. Do not generalize prematurely — two adapters is the v1 scope.

**M6 `m6_consistency` — cross-market coherence scanner.** Deterministic, no LLM. Within a negRisk event, mutually exclusive YES prices must sum to ≈ 1 — record the deviation. For logically linked market pairs (curated `links.yaml`, starts empty), check conditional-probability bounds. Output is a signal attached to forecasts (direction + magnitude of incoherence); M6 emits standalone forecasts only for incoherent legs, pulling them toward the coherent joint solution.

**M7 `m7_crossvenue` — cross-venue signal on matched questions.** For markets that also trade on Kalshi or carry a Metaculus community prediction, output the log-odds pool of the *external* venues' probabilities — Polymarket's own price stays out (M0 already carries it; the ensemble learns how much to trust each source). Question matching is the failure point and is handled conservatively: a curated `data/markets_map.yaml` where the LLM may *propose* candidate matches (question texts + resolution criteria side by side), but a human confirms every pair before it goes live — one mismatched pair silently poisons the signal. External prices are snapshotted at forecast ts under the same freshness rule (§9.13). Deterministic at forecast time; no LLM call in the loop.

**M4 `m4_ensemble`.** Log-odds weighted pool of M0–M3, M5–M7; weights fit per category on a rolling window of resolved forecasts (equal weights until ≥100 resolved samples in that category). Where M5 exists it should dominate — the fit will discover that; don't hand-tune. (Model IDs are stable; the numbering is historical, ensemble stays last in the pipeline.)

**Forecast cadence:** once per market per day per model (config), plus an extra forecast when |24h price move| > 0.10 on a tracked market. M3 runs only on the liquid tier, selected by a **deterministic rule** (top-K by liquidity within priority categories) — never by perceived difficulty, or the skill measurement inherits selection bias. M5 runs on every market its adapters cover. M6 runs on every negRisk event in the universe.

**Learning & versioning (how the system improves from its own record — and the hard line around the LLM).** Learning happens in batches over resolved outcomes, never per decision — a single win or loss is noise, and a system that adjusts to individual outcomes learns the noise. All mechanisms run inside the monthly `lab learn` job, dry-run by default.

*The line that must never move:* the LLM's weights are never fine-tuned, and the LLM is never re-invoked against a historical dossier after the fact. "Training" in this codebase means exactly two safe things:

1. **Fitting closed-form parameters on frozen data** — M1 curves, M2 base rates, M5 surprise distributions, M4 weights, and the M3 **aggregator** knobs (k, τ, cap). All of these are pure arithmetic over numbers already sitting in the database (prices, outcomes, or — for M3 — the structured evidence objects `{direction, strength, reliability, relevance, published_ts}` extracted at forecast time and frozen in `evidence_runs`). No LLM call happens during any of these refits, so there is no channel for the model's post-hoc knowledge to leak in. This is safe on the same footing as M1/M2/M5.
2. **Forward-only challenger registration** — a new M3 extraction prompt, or a new `m3b_direct` variant, registers as a new `model_id` (`m3_evidence@v2`) with a `registered_ts` in `model_versions`. It earns forecasts, and skill, only from markets it forecasts *after* that timestamp. It is never scored against, or backtested on, history that predates its own existence — doing so would require the LLM to re-read old news today, when today's model may already know how those old questions resolved.

**Rule of thumb for any future change to this system:** if it requires a *new LLM inference call* on a market that has already resolved, it is forbidden, full stop. If it only requires re-doing arithmetic on numbers already in the database, it is a normal refit.

**Safeguards (so one bad month can't corrupt a good model):**
- **Walk-forward only.** Every refit fits on data up to cutoff T and validates on data after T. A refit function that accepts a single history window with no train/validation split is a bug — `tests/test_learning_safety.py` asserts the split exists on every refit path.
- **Bounded step per cycle.** No refit may move a live parameter (recalibration slope, ensemble weight, aggregator k/τ/cap) by more than `max_step_pct` (config, default 20% relative) in one monthly cycle. A refit wanting to move further logs the full proposed change and applies only the capped step; the next cycle continues the move if the evidence still supports it. One noisy month becomes a slow lean, not a lurch.
- **Promotion requires a confidence interval, not a point estimate.** A challenger is promoted only when its measured skill beats the champion's with the bootstrap CI (§7) excluding zero, reusing `eval/scoring.py` rather than a bespoke metric.
- **Automatic rollback — the actual safety valve.** After promotion, the new champion keeps being scored forward like anything else. If its trailing skill over the next `rollback_window` resolved forecasts (config, default 50) falls below the retired champion's historical skill under the same CI test, `lab learn` reverts the active pointer automatically and records `retired_reason='rollback'`. Learning that turns out to hurt undoes itself instead of relying solely on the entry gate having been right.
- **Append-only registry.** Every parameter set or prompt gets a new row in `model_versions`, never edited — but the row points at its artifact via `artifact_path` rather than duplicating it; the artifact itself (curve coefficients, base rates, prompt text) lives in `data/models/*.json` exactly as it has since Phase 2. Rollback means repointing `is_active` at a previous row, not recomputing and hoping.
- **One kill switch.** `lab learn` refuses to run while `data/PAUSE` exists — the same file the collector already respects (guardrail 8, §9). No second switch to remember.
- **Dry-run by default.** `lab learn` always produces a diff report first — what would change, by how much, on what n, for which models — and only writes to `model_versions` with an explicit `--apply` flag. Every learning cycle is a reviewable event, not a silent mutation.

**Post-mortems:** monthly, for the top decile of misses and of wins among resolved forecasts, the LLM produces a structured analysis (error source: evidence / weighting / resolution-criteria reading / category / horizon) stored in `postmortems`; the report carries a quarterly lessons digest. Lessons feed *versioned* changes a human decides to make — never an automatic parameter nudge.

---

## 7. Evaluation protocol (the heart of the system)

- **Freeze semantics.** A forecast is scored exactly as written at `ts`, against `p_market_at_ts` captured in the same row. No retroactive edits — enforced by an append-only writer and a test.
- **Scoring at resolution.** For resolved markets: Brier `(p − y)²` and log loss (with clamped p). Compute for the model and for `p_market_at_ts` on the *same rows* (paired).
- **Skill.** `skill = mean(brier_market − brier_model)` over paired rows. Positive = beating the market. Report with a **cluster bootstrap CI resampling by `condition_id`** (multiple forecasts on the same market are correlated; naive CIs would lie).
- **Calibration.** Reliability diagrams (10 bins) per model, plus per-category breakdown once n allows.
- **CLV-style early signal** (doesn't need resolution): for each forecast, measure whether the market price at t+24h / t+72h moved toward the model's view. Report mean signed drift in the direction of the model's disagreement. This detects information timing months before enough markets resolve.
- **Honesty thresholds & statistical power.** The report displays n everywhere. Tiers: n < 200 resolved markets → "INSUFFICIENT DATA"; 200 ≤ n < 500 → "PRELIMINARY"; n ≥ 500 → standard claim. Additionally the report computes the **minimum detectable effect** at current n from the empirical sd of per-market paired Brier differences (MDE ≈ 2.8 · σ_d / √n, for 80% power at α = 0.05, n = resolved markets) and prints it next to every skill number — a skill estimate smaller than its own MDE is noise by construction. No cherry-picked windows: all-time and trailing-90-days only.
- **Null control.** The sports control sample (§3 universe policy) is scored identically and shown in the same table. Statistically significant "skill" there invalidates the run pending investigation.
- **LLM models are live-only.** Never backtest M3/M3b on markets resolved before the LLM's training cutoff — training-data leakage makes such numbers meaningless. Statistical models (M1/M2/M5/M6) may be backtested; LLM skill accrues only forward. The one exception: the M3 **aggregator's** own parameters (k/τ/cap) may be fit on frozen historical evidence objects, because that fit is arithmetic, not a new LLM call (§6).

---

## 8. Shadow portfolio (simulation only)

Purpose: translate calibration edge into an interpretable number. Everything labeled `SIMULATION` in code, DB, and reports.

- Bankroll: simulated $10,000.
- Entry rule (evaluated daily on liquid tier, using M4): `edge = |p_model − p_market| ≥ 0.05` AND spread ≤ 0.03 AND top-of-book depth ≥ $500 on the entry side AND `0.05 < p_market < 0.95` (tail entries are oracle-risk bets, not forecasting bets).
- Side: buy YES if `p_model > p_market`, else buy NO (price `1 − mid`).
- Fill model: simulated fill at best ask for the chosen side plus a slippage haircut proportional to (stake / visible depth), capped; parameters in config.
- Sizing: fractional Kelly at 0.2× with per-market cap 5% and per-category cap 20% of bankroll.
- Exit: hold to resolution (v1). Mark-to-market daily for the open book.
- Report: realized sim P&L (resolved only) separately from unrealized; max drawdown; hit rate; comparison vs "bet nothing" and vs "always take market side".

---

## 9. Engineering guardrails

Derived from the Karpathy guidelines — they are binding for this project:

1. **Think before coding.** If this brief is ambiguous somewhere, state the assumption in a code comment and in the phase summary — don't silently pick.
2. **Simplicity first.** Minimum code that satisfies the phase's acceptance criteria. No speculative abstraction, no plugin systems beyond `NewsProvider`, no config options nobody asked for. If a module exceeds ~300 lines, justify or split.
3. **Surgical changes.** Later phases must not rewrite earlier phases unless an acceptance criterion breaks.
4. **Goal-driven execution.** Every phase ends with its acceptance checks actually executed, not assumed.

Domain-specific guardrails:

5. **The scope guard.** `tests/test_scope.py` greps `src/` and fails if it finds: imports of `web3`, `eth_account`, `py_clob_client`; the strings `private_key`, `POST /order`, `create_order`, `eip712` (case-insensitive). This is not a usage restriction — the MIT license grants forks every freedom, and removing this test is one commit. Its job is to keep the canonical repository exactly what it claims to be (a measurement instrument) and to prevent execution code from entering the maintainer's publication via contributions or automation, while the maintainer operates from a jurisdiction where Polymarket does not accept traders. Runs in every phase.
6. **UTC everywhere.** All timestamps ISO-8601 UTC. A helper `now_utc()` is the only clock call allowed.
7. **Idempotent collectors.** Restart-safe: universe sync upserts; snapshot writer dedups on (ts_bucket, condition_id); resolution watcher is at-least-once with idempotent writes.
8. **Rate limiting.** One global async token-bucket for all Polymarket hosts (default: 5 req/s, burst 10). Backoff with jitter on 429/5xx. Respect a kill file (`data/PAUSE`) that halts all polling when present.
9. **Fail soft, log loud.** Network failures degrade to skip-and-retry; the process never crashes on a single bad market. Structured logging (`logging` + JSON lines to `data/logs/`).
10. **LLM budget.** Hard daily USD cap enforced in code before each call; spend persisted; the report shows cumulative cost.
11. **No look-ahead.** Every M3 evidence item must satisfy `published_ts ≤ forecast ts`; the dossier stores both timestamps. LLM models are never scored on pre-cutoff history (§7).
12. **No selection bias.** Cheap models (M0/M1/M2/M6, and M5 where its adapters apply) forecast the ENTIRE eligible universe daily. Any subsetting (M3 cost cap) follows the deterministic rule in §6 — never editorial judgment about which markets look "forecastable".
13. **Price freshness.** A forecast row requires `p_market_at_ts` from a snapshot no older than 15 min (liquid tier) / 90 min (tail). If the latest snapshot is stale, skip the forecast and log it — a forecast paired against a stale price corrupts the skill comparison silently, which is the worst failure class in this system.
14. **Self-modification is scheduled and versioned.** Parameters and prompts change only via `lab learn` on its monthly schedule, only when min-n thresholds are met, only via walk-forward fitting, and only as challenger versions measured against the incumbent (§6). No code path may adjust any model in response to an individual outcome.
15. **Learning never re-invokes the LLM on resolved history.** Refits touching M3 are arithmetic over evidence already frozen in `evidence_runs`; new prompts/extraction logic earn skill only from forecasts made after their own `registered_ts`. Full safeguard list (bounded step, CI-gated promotion, automatic rollback, dry-run default) lives in §6 — this rule is the one that may never be relaxed for convenience.

---

## 10. Implementation phases

### Phase 0 — Scaffold
Tasks: repo init, `uv` project, `pyproject.toml`, `LICENSE` (MIT), `config.yaml` with documented defaults, `.env.example`, `typer` CLI skeleton (`lab sync`, `lab collect`, `lab forecast`, `lab eval`, `lab report`, `lab shadow`, `lab export`, `lab status`, `lab learn`), logging setup, `test_scope.py`, README quickstart + "Downstream use & license" section (§13).
**Accept when:** `uv run lab --help` works; `uv run pytest` green (includes the tripwire test).

### Phase 1 — Collection
Tasks: Gamma client + universe sync with tiering (config thresholds on liquidity/volume; binary markets only; skip sub-24h crypto "pulse" markets by default); CLOB client + snapshot loop (5 min liquid / 60 min tail) writing Parquet; resolution watcher polling closed markets → `resolutions` (respect dispute flag: only record when Gamma marks final payout); `lab status` implementation: last-snapshot age per tier, snapshot gaps over trailing 24h/7d, resolution-watcher lag, ledger row counts, today's LLM spend.
**Accept when:** after a 1-hour live run: ≥50 liquid markets tracked; Parquet contains multiple snapshot rounds; process restart produces no duplicate rows; universe re-sync updates `last_synced_ts` without churn; PAUSE file halts polling within one cycle; `lab status` reports correct freshness and flags a synthetic gap in fixture data.

### Phase 2 — Historical bootstrap & debiasing
Tasks: downloader for `quant.parquet` + `markets.parquet` from `SII-WANGZJ/Polymarket_data` (§3 has the exact fetch code); lazy-filter to resolved binary markets with prices at multiple horizons + outcomes; fit M1 logistic recalibration per horizon bucket (expect slope > 1 at long horizons; verify the tail-bias direction empirically — recent Polymarket data shows a *reversed* favorite-longshot bias) and M2 category base rates; persist versioned curve artifacts under `data/models/`; plot calibration-slope-by-horizon and price-vs-outcome curves into `reports/`.
**Accept when:** curve artifacts exist and load; unit test asserts monotonicity of each recalibration; the report artifact shows fitted curves per horizon bucket with sample sizes per bin.

### Phase 3 — Ledger, baseline models, evaluation
Tasks: append-only forecast writer; M0/M1/M2 wired to daily cadence; `eval/scoring.py` with paired Brier/log-loss + cluster bootstrap; calibration plots; nightly `lab eval && lab report` producing static HTML; `lab export` — latest forecast per (market, model) plus market metadata as JSONL to stdout or file (the downstream integration point, §13).
**Accept when:** scoring functions pass unit tests on hand-computed fixtures (including the paired-skill sign convention and bootstrap clustering); report renders on synthetic fixture data AND on whatever real data has accumulated; forecast writer raises on any UPDATE attempt; `lab export` emits schema-valid JSONL that a test stub parses back losslessly.

### Phase 4 — Evidence pipeline (M3)
Tasks: `NewsProvider` (Google News RSS + config feed list; NewsAPI optional), dossier builder, LLM extraction with strict-JSON retry, deterministic aggregator (unit-tested: caps, decay, direction signs), cost accounting + daily cap, `evidence_runs` persistence, optional `m3b_direct`.
**Accept when:** for 10 liquid markets, an end-to-end run produces evidence rows and M3 forecasts in the ledger; aggregator tests pass; simulated cost-cap breach cleanly skips remaining markets with a log line; a stored dossier is human-readable enough to audit one forecast start-to-finish.

### Phase 5 — Structural models & consistency (M5, M6)
Tasks: weather adapter using the Open-Meteo **Ensemble API** (not plain forecast — §3) → threshold probabilities from ensemble spread, for whatever weather markets exist in the universe; macro adapter using **FRED** (`GDPNOW`, `PCENOW` series) → bucket probabilities via a historical-surprise error distribution; if a stable Cleveland Fed inflation-nowcast endpoint is found at implementation time, add it as a second macro sub-adapter, otherwise skip it and note why; negRisk coherence scanner with deviation logging; curated `links.yaml` (empty at start) for pairwise logical constraints.
**Accept when:** for at least one live weather market and one live macro market (fixtures if none live), M5 writes forecasts with a stored input trace; M6 flags a synthetic incoherent negRisk fixture and stays silent on a coherent one; both models appear in the nightly eval.

### Phase 6 — Ensemble, shadow portfolio, weekly report
Tasks: M4 with rolling-window weight fit (equal weights until n≥100); shadow portfolio engine per §8; weekly report section: skill table with CIs per model, calibration grid, CLV drift, sim P&L (clearly labeled SIMULATION), LLM spend.
**Accept when:** shadow entries occur only when all filters pass (test with synthetic book data); portfolio math unit-tested (Kelly fraction, caps, slippage haircut); weekly report renders with all sections and honest "INSUFFICIENT DATA" placeholders where n is low.

### Phase 7 — Learning loop
Tasks: `lab learn` command; consolidated scheduled refits (M1/M2/M5 artifacts, M4 weights); M3 aggregator walk-forward fitter gated on ≥150 resolved M3 forecasts; challenger registration and promotion mechanics per §6; post-mortem generator (top-decile misses and wins per month, structured JSON, inside the LLM budget cap) + quarterly lessons digest wired into the report.
**Accept when:** refits produce versioned artifacts without touching live ones until promotion; on fixture data, a synthetic challenger with better out-of-sample scores gets promoted and a worse one does not; post-mortems generate on fixtures and render in the report; `test_scope.py` still green.

### Phase 7.1 — Learning-loop hardening (retrofit)
Phase 7 already exists in this repository. This phase audits it against the safeguards in §6 and retrofits what's missing — it does not rebuild Phase 7 from scratch.
Tasks: add the `model_versions` table (schema migration) as a versioning layer that **coexists** with the existing `data/models/*.json` artifact files from Phase 2 — the table owns active/promoted/retired state and points at artifacts via `artifact_path`, it does not duplicate their contents; `data/models/ACTIVE.json` becomes a generated pointer written by `registry.py`, never edited by hand; no changes to how M0–M6 read their active artifact at inference time. Add `learn/registry.py` (versioned writes, single active pointer per `model_id`, rollback); make walk-forward train/validation split a structural requirement in `refit.py` — a refit call with no validation window should raise, not silently fit on full history; add `max_step_pct` bounding to every refit before it writes; confirm promotion goes through the CI-exclusion test in `eval/scoring.py` rather than a point-estimate comparison, and fix it if it doesn't; implement the rollback circuit breaker as a check `lab learn` runs before any new refit each month; make `lab learn` dry-run by default with output-only diffs, gated behind an explicit `--apply` flag; add `lab rollback <model_id>` for manual override outside the monthly cycle; audit the `m3b_direct` / M3 prompt-challenger code path specifically and confirm every challenger carries a `registered_ts` and is never scored on forecasts predating it.
Also write `tests/test_learning_safety.py` covering: (a) a refit call missing a validation window raises; (b) a synthetic large-jump scenario gets clamped to `max_step_pct`; (c) a challenger with a better point estimate but a CI that includes zero is NOT promoted; (d) a promoted challenger whose simulated subsequent performance degrades triggers automatic rollback and `retired_reason='rollback'` is recorded.
**Accept when:** `test_learning_safety.py` passes, all four fixtures included; a real `lab learn` run against accumulated data produces a dry-run diff and writes nothing until `--apply`; `lab rollback` demonstrably restores a prior `model_versions` row as active; `test_scope.py` still green.

### Phase 8 — Optional dashboard
Streamlit app reading the same SQLite/Parquet: live universe, latest forecasts vs market, calibration, shadow book. Only after Phase 6 is stable.

### Phase 9 — Optional: cross-venue signal (M7)
Only after Phase 6 is stable; priority categories only. Tasks: thin read-only clients for Kalshi public market data and the Metaculus API (§3 — no accounts, no keys, same global rate limiter); `data/markets_map.yaml` with a propose-then-confirm matching flow (LLM proposes candidate pairs, a human confirms, the file is the source of truth); M7 per §6 wired into the ledger and the M4 weight fit; external-price snapshots stored alongside our own.
**Accept when:** on ≥5 confirmed matched pairs (fixtures acceptable), M7 writes forecasts with stored external-price traces; a proposed-but-unconfirmed pair is NOT forecast; M7 appears in the nightly eval and in M4's weight fit; `test_scope.py` still green.

---

## 11. Operations

- Run collection under tmux or a systemd user unit: `uv run lab collect`. Target host: an always-on Linux box.
- **Data health is a first-class concern.** The nightly report opens with the `lab status` health block (freshness, gaps, watcher lag, spend). The single worst operational failure is the collector dying silently — snapshot history is unrecoverable.
- **Timeline expectations.** Weather markets and the sports null-control resolve in days → first calibration stats in ~2–4 weeks. Macro releases: weeks. Long-horizon politics: months. The n ≥ 500 "standard claim" tier realistically arrives at month 3–6 depending on category mix — do not read tea leaves earlier.
- Nightly cron: `lab forecast && lab eval && lab report`. Weekly: `lab shadow report`. Monthly: `lab learn`.
- Disk: snapshots at default cadence are small (est. < 200 MB/month at 200 markets); prune policy configurable but default keep-everything.
- Backup: `data/lab.db` + `data/snapshots/` are the crown jewels — the historical order-book snapshots cannot be re-downloaded later. rsync them somewhere daily from day one.
- Success reviews: after ~200 resolved paired forecasts, read the skill table. Positive skill with CI excluding zero on some category → that category is a candidate edge worth deeper study. No skill anywhere → the lab has paid for itself by preventing a doomed trading build.

## 12. Explicitly out of scope (do not build)

Order execution or anything touching wallets/keys (belongs in downstream projects — §13); VPN/proxy logic; Betfair (requires an account); Kalshi *trading* endpoints — its read-only market data is in scope via Phase 9; ALL crypto/equity price-target markets, not just sub-24h pulses; a database server; user auth; Docker (plain `uv` is fine); notification bots. If a task seems to require any of these, stop and flag it instead.

---

## 13. Open-source model & downstream use

This project is published for anyone to analyze, improve, learn from, and build on — including commercially.

- **License: MIT** (`LICENSE` created in Phase 0). No usage restrictions of any kind: commercial use, forks, closed-source derivatives, and execution layers built on top are all permitted. The standard MIT warranty disclaimer applies; downstream users are responsible for compliance in their own jurisdictions.
- **The forecast contract is the public API.** The SQLite schema (§5) and Parquet layout are a stable interface: any breaking change bumps a schema version stored in a `meta` table and is noted in the changelog. Downstream code may read the DB and Parquet directly.
- **`lab export` is the integration point.** Latest forecast per (market, model) with market metadata, as JSONL. Any external consumer — analytics, dashboards, execution layers — plugs in here without touching lab internals.
- **Extension pattern.** An execution layer is a separate package or repo that consumes `lab export` (or the DB) and implements its own order logic, risk, and compliance. This repository defines the boundary and keeps its side of it; everything past the boundary belongs to downstream authors — their design, their responsibility.
- **Contributions.** PRs adding execution code to the core are declined and redirected to the extension pattern. Everything else — models, adapters, data sources, evaluation methods — is welcome.
