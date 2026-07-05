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
| `m1_hier@{venue}` | statistical (hierarchical) | one global recalibration shape plus a ridge-shrunk per-venue offset (`α_g+α_v`, `β_g+β_v`), fit jointly across Polymarket/Kalshi/Metaculus — a small-n venue borrows the global curve, a large-n venue diverges where its own data demands it. Forecasts in parallel with `m1_debiased` as an observable challenger (not yet pooled into M4 — same precedent as `m3b_direct` below); its Metaculus offset also recalibrates the raw community prediction before M7 pools it |
| `m2_baserate` | statistical | historical base rate by recurring question template, blended in log-odds space |
| `m3_evidence` | LLM (structured) | news retrieval → strict-JSON evidence extraction → **deterministic** log-odds aggregator (the LLM never writes the final number) |
| `m5_nowcast` | structural | maps an external quantitative model (open-meteo/NWS ensembles, Cleveland Fed / GDPNow) straight onto the market's resolution criteria |
| `m6_consistency` | deterministic | negRisk / linked-market coherence scanner — flags legs that don't sum to ~1 |
| `m7_crossvenue` | cross-venue | log-odds pool of external venues' prices (Kalshi public market data; Metaculus community prediction where a token grants access, recalibrated through `m1_hier`'s Metaculus offset first) on a curated, human-confirmed `markets_map.yaml` — Polymarket's own price stays out. The pooled logit is extremized by a fitted, correlation-discounted exponent (Phase 13) |
| `m4_ensemble` | ensemble | log-odds weighted pool of the above, weights fit per category on resolved history; the pooled logit is extremized by a per-category exponent, discounted by how correlated the pooled sources actually are (Phase 13) so a handful of near-duplicate signals can't buy false confidence |

A `sports` null-control sample runs the cheap models only: if the lab "finds
skill" on a near-efficient market like sports, the harness is broken, not the
market — the weekly report prints null-control skill next to everything else.

## Statistical principles

A short field guide to the estimators this repo actually computes — see
[`eval/`](src/lab/eval/) and [`learn/`](src/lab/learn/) for the code.

- **Paired scoring against the market.** Every skill number is
  `mean(brier_market − brier_model)` on the *same* forecast rows, never an
  unpaired comparison — this removes the dominant variance component
  (question difficulty) for free.
  [`eval/scoring.py`](src/lab/eval/scoring.py).
- **Event-level cluster bootstrap.** The same real-world event can be priced
  on several venues; resampling by venue-market row would treat correlated
  observations as independent. The bootstrap resamples whole `event_id`
  clusters instead (falling back to `condition_id` where no cross-venue link
  exists). [`eval/cluster.py`](src/lab/eval/cluster.py).
- **Anytime-valid confidence sequence.** Nightly reports are read
  continuously, which invalidates a fixed-n p-value under repeated looks. The
  report computes a time-uniform normal-mixture confidence sequence (Howard,
  Ramdas, McAuliffe & Sekhon, *Annals of Statistics* 49(2), 2021, Prop. 3/5) —
  this interval, not the classical bootstrap CI, is what actually gates model
  promotion and rollback. [`eval/anytime.py`](src/lab/eval/anytime.py).
- **Precision-weighted stratified estimator.** Brier-difference variance is
  driven directly by price level (`p(1−p)`), so pooling naively can mask real
  heterogeneity. Resolved forecasts are stratified into price buckets, each
  stratum's mean paired difference is inverse-variance weighted, and the
  pooled estimate provably collapses to the raw mean under homogeneous
  variance — it only diverges when the heterogeneity is real.
  [`eval/stratified.py`](src/lab/eval/stratified.py).
- **Hierarchical partial pooling (`m1_hier`, Phase 12).** One recalibration
  shape is shared across venues; each venue earns its own offset only in
  proportion to how much data it has (a ridge penalty scaled ∝ 1/n_venue) —
  a new venue borrows the global curve instead of overfitting its first
  month of noise. [`learn/refit.py`](src/lab/learn/refit.py)
  (`fit_m1_hier_curves`).
- **Correlation-discounted extremization (Phase 13).** A log-odds pool of
  correlated sources is provably overconfident if extremized as though they
  were independent. The fitted exponent is scaled by the correlation-adjusted
  effective source count (`n_eff = n / (1 + (n−1)·ρ̄)`) before it's ever
  applied — a duplicated or near-duplicate source buys no extra confidence.
  [`learn/pooling.py`](src/lab/learn/pooling.py).
- **Virtual prediction economy (Phase 14).** Kelly log-optimal betting and
  log-score are formally dual (Kelly 1956; Cover 1991): staking a fixed,
  capped Kelly fraction of a model's edge against the market price on every
  resolved forecast compounds wealth at the model's own log-score advantage —
  a second, betting-theoretic readout of the same skill number the lab
  already computes rigorously, not a new signal. Every model's coverage
  differs (M5 only covers weather/macro, M7 only matched cross-venue markets),
  so models are always compared by `cum_log_wealth / n_forecasts`
  (sleeping-expert normalization), never the raw cumulative total — a
  low-coverage sharp model shouldn't lose to a high-coverage mediocre one
  just because it forecast less. [`economy/wealth.py`](src/lab/economy/wealth.py),
  [`eval/wealth_plots.py`](src/lab/eval/wealth_plots.py).

- **Shadow MWU ensemble weighting (Phase 14.1).** A Hedge/multiplicative-
  weights challenger derives M4's category weights from relative wealth
  (`w_i ∝ exp(η_t · avg_log_wealth_i)`, `η_t = √(8 ln N / t)` — the standard
  regret-bound-optimal schedule), then clamps them with the same floor/
  ceiling the incumbent monthly fit now also carries. A single pool-wide
  correlation scalar can't stop a duplicated high-wealth model's *pair* from
  jointly dominating (it dilutes toward the mean once uncorrelated models
  are also in the pool), so the ceiling is enforced per correlation-*cluster*
  (union-find on pairwise correlation) instead of per model. Registers as a
  challenger under the exact same `model_versions` key the monthly fit uses,
  so promotion is a pointer flip — no changes needed anywhere else — gated
  by a 90-day/n≥200-per-category probation before it's even eligible for the
  standard CI-gated promotion. [`economy/mwu.py`](src/lab/economy/mwu.py).

All of the above are fit or computed monthly inside `lab learn`, dry-run by
default, walk-forward validated, bounded-step, and CI-gated on promotion —
never in response to a single outcome (guardrails 14/15 in
[`CLAUDE.md`](CLAUDE.md)). Two narrow, explicitly-justified exceptions run
nightly instead, inside `lab eval`: the wealth ledger (pure arithmetic over
already-resolved forecasts, not a model parameter — it never writes a
forecast of its own) and the shadow MWU challenger above (guardrail 17 —
it touches only meta-level ensemble weights, never any model's internals,
and never affects production forecasts until it clears the same promotion
gate as any other challenger).

## Project status

All core phases, the multi-venue collection foundation, the measurement
upgrade, and the hierarchical/pooling refinements are implemented and tested
(incl. the `test_scope.py` tripwire that fails the build if execution-code
strings ever land in `src/`):

- [x] Phase 0 — scaffold, config, CLI skeleton
- [x] Phase 1 — collection (Gamma/CLOB clients, tiering, snapshot loop, resolution watcher)
- [x] Phase 2 — historical bootstrap & M1/M2 fitting
- [x] Phase 3 — append-only ledger, M0–M2, scoring, static report, `lab export`
- [x] Phase 4 — M3 evidence pipeline (news → LLM extraction → deterministic aggregation)
- [x] Phase 5 — M5 structural nowcasts, M6 coherence scanner
- [x] Phase 6 — M4 ensemble, shadow portfolio (simulation), weekly report
- [x] Phase 7 / 7.1 — learning loop (`lab learn`: scheduled refits, `model_versions` registry, walk-forward guard, CI-gated promotion, automatic rollback, post-mortems)
- [x] Phase 8 — Streamlit dashboard (read-only: live universe, forecasts vs market, calibration, shadow book)
- [x] Phase 9 — cross-venue signal (M7): Kalshi read-only client (verified live, public, no auth), Metaculus client (requires an operator-supplied API token — Metaculus removed anonymous access; see `src/lab/api/metaculus.py` for the verified request shape), curated propose-then-confirm matching (`lab map propose` / `lab map confirm` / `data/markets_map.yaml`), wired into the ledger and the M4 weight fit
- [x] Phase 10 — multi-venue collection foundation: Kalshi/Metaculus/Manifold collectors, `venues`/`events` schema, synthesized `{venue}:{native_id}` keys, per-venue `lab status` lines
- [x] Phase 11 — measurement upgrade: event-level cluster bootstrap, anytime-valid confidence sequence (the actual promotion/rollback gate), precision-weighted stratified estimator, venue × category report matrix
- [x] Phase 12 — hierarchical recalibration (`m1_hier`): ridge-shrunk per-venue offsets on a shared global horizon curve, fit across Polymarket/Kalshi/Metaculus
- [x] Phase 13 — extremized, correlation-aware pooling: per-category extremization exponent on M4's and M7's pools, discounted by the correlation-adjusted effective source count
- [x] Phase 14 — virtual prediction economy: `wealth_ledger`, Kelly log-wealth accounting per (model, category) wired into the nightly `lab eval` step, sleeping-expert-normalized comparison (`cum_log_wealth / n_forecasts`), equity-curve/drawdown/attribution report section and a dedicated dashboard mode
- [x] Phase 14.1 — shadow MWU ensemble weighting: a wealth-derived, regret-bounded (Hedge/MWU) challenger to M4's category weights, cluster-aware floor/ceiling clamped, 90-day/n≥200-per-category probation, CI-gated and rollback-guarded through the same registry as any other challenger

Every phase in the engineering brief ([`CLAUDE.md`](CLAUDE.md)) is now implemented and tested.

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
| `lab watchdog` | Supervises `lab run`: auto-restarts it 10 minutes after any exit/crash |
| `lab map propose` | M7: LLM proposes candidate Kalshi/Metaculus matches into `markets_map.yaml` (`proposed`, not live) |
| `lab map confirm <condition_id>` | M7: human confirms a proposed (or hand-curated) match — only confirmed pairs are ever forecast |
| `lab map list` | M7: show confirmed and pending-proposed matches |

### One button (recommended)

`lab run` keeps the collector alive and fires the analytics jobs itself on the
schedule in `config.yaml` (`schedule:` section, all UTC): forecast+eval+report
nightly, shadow weekly, learn monthly. It also runs one forecast/eval/report
pass on startup (`schedule.run_on_start`). No cron or systemd needed.

`lab watchdog` wraps `lab run` as a supervised child process: if it ever exits
for any reason (crash, hard kill, etc.), the watchdog waits 10 minutes
(`config.yaml` → `watchdog.restart_delay_seconds`) and restarts it — a
deliberate cooldown rather than a tight retry loop, so a genuinely broken
config doesn't hammer Polymarket's API in a crash loop. Any unhandled
exception in `lab run` itself is now also logged with a full traceback to
`data/logs/lab.jsonl` before the process exits (`sys.excepthook` + an asyncio
loop exception handler), so a crash always leaves a diagnosable trace.

On **Windows**, just double-click **`start.bat`** — it launches the watchdog
(which in turn supervises the orchestrator) plus the dashboard
(http://localhost:8501) in separate windows. Press `Ctrl+C` in the watchdog
window to stop everything. To halt polling without killing the process,
create the kill file `data/PAUSE` (delete it to resume).

```bash
uv run lab watchdog       # cross-platform equivalent of start.bat (no dashboard)
uv run lab run            # or run the orchestrator directly, without auto-restart
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

A read-only Streamlit dashboard over the same SQLite/Parquet, organized into modes via a
sidebar selector: **Overview** (health + universe), **Forecasts vs Market**, **Calibration &
Skill**, **Wealth Economy** (Phase 14: equity curves, drawdown, sleeping-expert rankings, M4
attribution — reusing the same plot functions the static report renders), and **Shadow
Portfolio** (SIMULATION).

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
