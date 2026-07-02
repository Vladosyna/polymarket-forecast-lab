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

## Quickstart

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this-repo> forecast-lab && cd forecast-lab
uv sync
cp .env.example .env        # add ANTHROPIC_API_KEY (needed for M3 only)
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

Typical operation (always-on Linux box):

```bash
uv run lab collect                      # under tmux / systemd
# nightly cron:
uv run lab forecast && uv run lab eval && uv run lab report
# weekly:  uv run lab shadow
# monthly: uv run lab learn
```

Back up `data/lab.db` and `data/snapshots/` daily from day one — historical
order-book snapshots cannot be re-downloaded later.

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
