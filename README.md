# Polymarket Forecast Lab

A read-only research system that answers one question with statistical rigor:

> Can we produce probability estimates for Polymarket event outcomes that are
> better calibrated than the market price itself, measured after resolution?

The lab continuously collects public market data, generates timestamped
probability forecasts from several models, freezes them in an append-only
ledger, waits for markets to resolve, and scores every model against the
market baseline (Brier score, log loss, calibration curves). A simulated
"shadow portfolio" translates any measured edge into hypothetical P&L.
**No real money is ever involved and this repository contains no execution
code** — order placement, wallets, keys, and CLOB authentication are out of
scope by design (see "Downstream use & license" below).

## Why this exists

Prediction-market prices are often treated as ground-truth probabilities.
They're a good prior, but they are not automatically well-calibrated —
academic work on Polymarket/PredictIt-style markets documents systematic
long-horizon underconfidence, thin-book noise, and category-specific biases.
The only honest way to know whether *any* model — statistical, LLM-based, or
market-derived — beats the market is to freeze a probability before the
outcome is known and score it after resolution, on a large enough sample to
say so with a confidence interval instead of a hunch. That's the entire
project: no trading, no execution, just a measurement instrument run
continuously against public data.

## How it works

```
 Gamma API  ──┐
 CLOB API   ──┼──▶ lab sync / collect ──▶ SQLite (markets, resolutions)
 Data API   ──┘                       └─▶ Parquet (order-book snapshots)
                                              │
                                              ▼
                          lab forecast  (M0…M6 → M4 ensemble)
                          ├─ M0 market mid (null baseline)
                          ├─ M1 horizon-recalibration (logistic, per-bucket)
                          ├─ M2 category base rates
                          ├─ M3 LLM evidence pipeline (news → strict-JSON → deterministic aggregator)
                          ├─ M5 structural nowcasts (weather / macro)
                          ├─ M6 negRisk coherence scanner
                          ├─ M7 cross-venue signal (Kalshi / Metaculus, confirmed matches only)
                          └─ M4 ensemble (log-odds weighted pool, fit per category)
                                              │
                                              ▼
                     forecasts ledger (SQLite, append-only, never UPDATE/DELETE)
                                              │
                     ┌────────────────────────┼─────────────────────────┐
                     ▼                        ▼                         ▼
              lab eval (paired          lab shadow (simulated       lab learn (monthly:
              Brier / log-loss vs       portfolio, P&L, no          refits, champion/
              market, cluster           real money — labeled        challenger promotion,
              bootstrap CIs)            SIMULATION everywhere)       post-mortems)
                     │                        │                         │
                     └────────────────────────┴─────────────────────────┘
                                              ▼
                                lab report → static HTML (reports/)
                                lab export → JSONL (the integration point)
```

Every forecast row is frozen at write time against the market price captured
in the *same* snapshot (`p_market_at_ts`) — that pairing is what makes the
skill measurement honest; nothing is ever retroactively edited. See
[`CLAUDE.md`](CLAUDE.md) for the full engineering brief (schema, guardrails,
phase-by-phase acceptance criteria) this repo was built against.

## The model roster

| Model | Type | Edge thesis |
|---|---|---|
| `m0_market` | null baseline | market mid — the number every other model must beat |
| `m1_debiased` | statistical | logistic recalibration of the market price, fit per time-to-resolution bucket; the direction of the bias is *fit*, never hardcoded |
| `m2_baserate` | statistical | historical base rate by recurring question template, blended in log-odds space |
| `m3_evidence` | LLM (structured) | news retrieval → strict-JSON evidence extraction → **deterministic** log-odds aggregator (the LLM never writes the final number) |
| `m5_nowcast` | structural | maps an external quantitative model (open-meteo/NWS ensembles, Cleveland Fed / GDPNow) straight onto the market's resolution criteria |
| `m6_consistency` | deterministic | negRisk / linked-market coherence scanner — flags legs that don't sum to ~1 |
| `m7_crossvenue` | cross-venue | log-odds pool of external venues' prices (Kalshi public market data; Metaculus community prediction where a token grants access) on a curated, human-confirmed `markets_map.yaml` — Polymarket's own price stays out |
| `m4_ensemble` | ensemble | log-odds weighted pool of the above, weights fit per category on resolved history |

A `sports` null-control sample runs the cheap models only: if the lab "finds
skill" on a near-efficient market like sports, the harness is broken, not the
market — the weekly report prints null-control skill next to everything else.

## Project status

All core phases plus the optional cross-venue signal are implemented and
tested (incl. the `test_scope.py` tripwire that fails the build if
execution-code strings ever land in `src/`):

- [x] Phase 0 — scaffold, config, CLI skeleton
- [x] Phase 1 — collection (Gamma/CLOB clients, tiering, snapshot loop, resolution watcher)
- [x] Phase 2 — historical bootstrap & M1/M2 fitting
- [x] Phase 3 — append-only ledger, M0–M2, scoring, static report, `lab export`
- [x] Phase 4 — M3 evidence pipeline (news → LLM extraction → deterministic aggregation)
- [x] Phase 5 — M5 structural nowcasts, M6 coherence scanner
- [x] Phase 6 — M4 ensemble, shadow portfolio (simulation), weekly report
- [x] Phase 7 / 7.1 — learning loop (`lab learn`: scheduled refits, `model_versions` registry, walk-forward guard, CI-gated promotion, automatic rollback, post-mortems)
- [ ] Phase 8 — optional Streamlit dashboard
- [x] Phase 9 — cross-venue signal (M7): Kalshi read-only client (verified live, public, no auth), Metaculus client (requires an operator-supplied API token — Metaculus removed anonymous access; see `src/lab/api/metaculus.py` for the verified request shape), curated propose-then-confirm matching (`lab map propose` / `lab map confirm` / `data/markets_map.yaml`), wired into the ledger and the M4 weight fit

The collector runs continuously against live Polymarket data. Calibration
statistics need resolved markets to accumulate before any skill claim clears
the honesty thresholds in the brief (n ≥ 200 = "preliminary", n ≥ 500 =
"standard claim") — weather and the sports null-control resolve in days,
long-horizon politics in months.

## Quickstart

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this-repo> forecast-lab && cd forecast-lab
uv sync
cp .env.example .env        # add DEEPSEEK_API_KEY (or ANTHROPIC_API_KEY) for M3
uv run lab --help
uv run pytest               # includes the scope-guard tripwire test
```

Configuration lives in `config.yaml` (universe filters, cadences, thresholds,
cost caps) — every default is documented inline.

## Commands

| Command | Purpose |
|---|---|
| `lab sync` | Discover markets from Gamma, tier the universe (liquid / tail / ignored) |
| `lab bootstrap` | One-time historical bootstrap: download resolved markets, fit M1/M2 artifacts |
| `lab collect` | Long-running collector: order-book snapshots + resolution watcher |
| `lab forecast` | Generate forecasts for the eligible universe, freeze in the ledger |
| `lab eval` | Score resolved forecasts: paired Brier / log loss, skill with bootstrap CIs |
| `lab report` | Render the static HTML report |
| `lab shadow` | Simulated shadow portfolio (SIMULATION only) |
| `lab export` | Latest forecast per (market, model) as JSONL — the integration point |
| `lab status` | Data health: snapshot freshness, gaps, watcher lag, spend |
| `lab learn` | Monthly learning loop: batch refits, champion/challenger, post-mortems |
| `lab run` | **One-button orchestrator**: collector + scheduled forecast/eval/report/shadow/learn in a single process |
| `lab map propose` | M7: LLM proposes candidate Kalshi/Metaculus matches into `markets_map.yaml` (`proposed`, not live) |
| `lab map confirm <condition_id>` | M7: human confirms a proposed (or hand-curated) match — only confirmed pairs are ever forecast |
| `lab map list` | M7: show confirmed and pending-proposed matches |

### One button (recommended)

`lab run` keeps the collector alive and fires the analytics jobs itself on the
schedule in `config.yaml` (`schedule:` section, all UTC): forecast+eval+report
nightly, shadow weekly, learn monthly. It also runs one forecast/eval/report
pass on startup (`schedule.run_on_start`). No cron or systemd needed.

On **Windows**, just double-click **`start.bat`** — it launches the
orchestrator plus the dashboard (http://localhost:8501) in separate windows.
Press `Ctrl+C` in the orchestrator window to stop. To halt polling without
killing the process, create the kill file `data/PAUSE` (delete it to resume).

```bash
uv run lab run            # cross-platform equivalent of start.bat (no dashboard)
```

### Manual operation (advanced)

If you prefer external scheduling (cron / systemd) instead of `lab run`:

```bash
uv run lab collect                      # under tmux / systemd
# nightly cron:
uv run lab forecast && uv run lab eval && uv run lab report
# weekly:  uv run lab shadow
# monthly: uv run lab learn
```

Back up `data/lab.db` and `data/snapshots/` daily from day one — historical
order-book snapshots cannot be re-downloaded later.

## Dashboard (optional)

A read-only Streamlit dashboard over the same SQLite/Parquet: live universe,
latest forecasts vs market, calibration, shadow book.

```bash
uv sync --group dashboard
uv run streamlit run src/lab/dashboard.py
```

## Scope invariants

1. **No execution code.** Measurement instrument only; "buy"/"sell" appear
   only in clearly-labeled simulation code. Enforced by `tests/test_scope.py`.
2. **Public endpoints only.** No Polymarket account, no Polymarket API keys.
3. **No geoblock circumvention.** Unreachable endpoints are logged and the
   lab falls back to historical/offline sources.
4. **Polite API citizenship.** Global rate limiter, exponential backoff,
   conservative polling, and a `data/PAUSE` kill file.
5. **Immutable forecast ledger.** Forecast rows are never updated or deleted;
   scoring lives in separate tables. This is what makes the calibration
   measurement honest.

## Downstream use & license

Published under the **MIT license** — no usage restrictions of any kind:
commercial use, forks, closed-source derivatives, and execution layers built
on top are all permitted. The standard MIT warranty disclaimer applies;
downstream users are responsible for compliance in their own jurisdictions.

- **The forecast contract is the public API.** The SQLite schema and Parquet
  layout are a stable interface; any breaking change bumps the schema version
  in the `meta` table and is noted in the changelog.
- **`lab export` is the integration point.** Latest forecast per
  (market, model) with market metadata, as JSONL. External consumers —
  analytics, dashboards, execution layers — plug in here without touching
  lab internals.
- **Extension pattern.** An execution layer is a separate package or repo
  that consumes `lab export` (or reads the DB directly) and implements its
  own order logic, risk, and compliance. This repository defines the boundary
  and keeps its side of it.
- **Contributions.** PRs adding execution code to the core are declined and
  redirected to the extension pattern. Everything else — models, adapters,
  data sources, evaluation methods — is welcome.
