# Pre-Analysis Plan

**Committed:** 2026-07-06 (UTC). **Status:** primary document — first version, no addenda yet.

This document is dated and committed once, before the confirmatory window it defines opens. It is
never edited after commitment: any later change to hypotheses, outcomes, or exclusion rules is
appended below as a dated addendum (§9), never a silent rewrite — the same append-only discipline
the forecast ledger itself follows (`CLAUDE.md` guardrail 5).

Its evidentiary weight rests on [`docs/ledger_commitments.jsonl`](ledger_commitments.jsonl): a
nightly sha256 commitment over each closed day's appended `forecasts` rows, pushed to this public
repo (see [`src/lab/ledger_commitment.py`](../src/lab/ledger_commitment.py)). Together, the two
files let a reviewer confirm both *what* was predicted and *when* the hypotheses below were fixed,
without trusting the author's word for either.

## 1. Purpose

This project (`CLAUDE.md` §1) asks one question: can probability estimates for Polymarket event
outcomes be produced that are better calibrated than the market price itself, measured after
resolution. This plan fixes, in advance, which claims from that broader research program count as
confirmatory versus exploratory.

## 2. Primary hypotheses

- **H1 — Long-horizon underconfidence recalibration edge.** Polymarket prices are systematically
  underconfident far from resolution (calibration slope > 1 at long horizons, converging toward 1
  near resolution). `m1_debiased` and its hierarchical successor `m1_hier@polymarket` (`CLAUDE.md`
  §6, Phase 2 and Phase 12) are predicted to beat the market baseline (`m0_market`) on paired Brier
  skill in the ≥30-day horizon buckets.
- **H2 — Recalibration skill net of costs, P1/P2 categories.** Restricted to the two categories the
  edge research identifies as most model-drivable (`CLAUDE.md` §3 universe policy) — P1 (economic
  data releases and central-bank decisions) and P2 (weather markets) — the recalibration and
  structural models (`m1_debiased`/`m1_hier`, `m5_nowcast`) are predicted to show positive skill.
  "Net of costs" here means net of the shadow portfolio's simulated slippage and sizing frictions
  (`CLAUDE.md` §8); the dedicated fee-schedule/net-of-cost report line described in the broader
  Phase 15 task list is a later addition and does not gate this hypothesis's current confirmatory
  test — the shadow portfolio's existing simulated fill/slippage model is the net-of-cost proxy
  until that line ships.
- **H3 — Cross-venue lead-lag.** `m7_crossvenue`'s external-venue log-odds pool (Kalshi and
  Metaculus, `CLAUDE.md` §6) is predicted to show CLV-style predictive value — i.e., Polymarket's
  own price moves toward the external pool's view more often than the reverse — ahead of, not
  merely coincident with, Polymarket's own price adjustment.

## 3. Primary outcome measure

Paired Brier skill, `skill = mean(brier_market − brier_model)` over resolved, paired forecast rows,
per venue and category (`CLAUDE.md` §7). The **sole confirmatory claim statistic** is the
event-clustered, time-uniform anytime-valid confidence sequence
(`WSR asymptotic CS — [`src/lab/eval/anytime.py`](../src/lab/eval/anytime.py)`): a hypothesis is
supported only when this interval excludes zero in the predicted direction, at the honesty tier
appropriate to `n` (`CLAUDE.md` §7: n < 200 insufficient, 200 ≤ n < 500 preliminary, n ≥ 500
standard). The precision-weighted stratified skill estimator
(`[`src/lab/eval/stratified.py`](../src/lab/eval/stratified.py)`) must agree in direction and also
exclude zero as a required secondary check — it does not on its own establish a claim, per
`CLAUDE.md` §7's own framing of it as a check against the primary CS, not an independent test.
The paired-scoring machinery both statistics run on top of lives in
[`src/lab/eval/scoring.py`](../src/lab/eval/scoring.py).

## 4. Secondary / exploratory outcomes

Explicitly non-confirmatory, reported for context but not gating any hypothesis above: log loss,
CLV-style price-drift signal ahead of resolution, reliability-diagram calibration curves, and the
wealth-ledger's sleeping-expert-normalized cumulative log-growth (`cum_log_wealth / n_forecasts`,
`CLAUDE.md` §6/Phase 14). The shadow MWU ensemble-weighting challenger (Phase 14.1) is likewise
exploratory until it clears its own promotion gate.

## 5. Exclusion rules

Verbatim from `CLAUDE.md` §3's universe policy:

- **Structurally unforecastable, excluded as forecast targets:** all crypto/equity price-target
  markets at any horizon (the market price *is* the forecast for a martingale underlying);
  "will X say/tweet Y"-style novelty markets and anything with ambiguous resolution wording.
- **Tail-priced markets** (≥ 0.95 or ≤ 0.05) are excluded as forecast *targets* — residual edge
  there is dominated by oracle/dispute tail risk — but are **retained** in calibration statistics.
- **Null control:** a small random sample of sports markets, forecast by the cheap models only, is
  scored identically to every other category and shown in the same report table. A statistically
  significant "skill" finding there does not support any hypothesis above — it instead invalidates
  the run pending investigation into a broken harness (`CLAUDE.md` §3/§7).
- **Venue/provenance exclusions** (`CLAUDE.md` guardrail 16): Manifold (play money) is excluded
  from all skill claims — event mapping and M2 base rates only. Historical archives (GJP,
  PredictIt, the HF bootstrap dataset) feed M2 base rates only, never a skill claim for H1–H3.

## 6. Confirmatory window

This is the part most prone to being gotten wrong, so it is stated precisely:

- M1/M2/M5's parameters (recalibration curves, base rates, error distributions) were **fit** on the
  pre-existing historical bootstrap (`CLAUDE.md` Phase 2, walk-forward split, allowed under §7 —
  "statistical models may be backtested"). That fitting is not itself under test.
- What **is** confirmatory for H1/H2 is whether those already-fit, already-frozen model versions
  beat the market on forecasts made **after this document's commitment date (2026-07-06)**.
  Forecasts made and resolved before that date, using the same model versions, are exploratory —
  useful for monitoring, not for the claim.
- H3 (`m7_crossvenue`) follows the same rule as H1/H2 above — confirmatory only for forecasts made
  after 2026-07-06 — with one simplification in its favor: M7 is deterministic at forecast time (no
  LLM call, `CLAUDE.md` §6) and was never fit on the historical bootstrap the way M1/M2/M5 were, so
  there is no separate "already-fit" caveat to track for it.
- LLM-based models (M3/M3b) carry no primary hypothesis in §2, but the same confirmatory logic
  applies to them with an extra, stricter rule: guardrail 15 forbids ever backtesting an LLM model
  on pre-cutoff history, so their skill accrues *only* from forecasts made after each specific model
  version's own `registered_ts` — never retroactively, regardless of this document's date.
- A challenger version registered after 2026-07-06 (any `model_id@vN` promoted via the champion/
  challenger machinery, `CLAUDE.md` §6/§7.1) inherits this same confirmatory-window logic relative
  to its own `registered_ts`, not this document's date — each model version's track record starts
  when it starts, per guardrail 18.

## 7. Historical gap note

`docs/ledger_commitments.jsonl`'s first entry covers 2026-07-05 (the most recent fully-elapsed UTC
day as of this feature's deployment). Forecasts and resolutions recorded in the database before
that date exist and are used for the exploratory/monitoring purposes above, but were **not**
contemporaneously hash-committed — retroactively hashing them would carry no pre-registration
value and is deliberately not attempted (see the commit history of
[`src/lab/ledger_commitment.py`](../src/lab/ledger_commitment.py) for the reasoning). This is a
documented limitation, not a gap papered over.

## 8. Deviation policy

Any change to §2–§6 after 2026-07-06 — a new primary hypothesis, a changed exclusion rule, a
different primary outcome statistic — is recorded as a new, dated, appended section below (§9+),
never as an edit to §2–§7 above. A reviewer can always reconstruct exactly what was pre-registered
at any point in time by reading this file's own git history.

## 9. Addenda

**Addendum 9.1 (2026-07-09).** The confirmatory analysis window for H1–H3 closes at 2026-12-31
23:59 UTC. Forecasts frozen on or before that timestamp, resolving at any later date, remain in
the confirmatory set; forecasts frozen after it are exploratory for this paper and may seed a
future pre-registered window. Primary analyses will be executed once, after the freeze, exactly
as specified in §2–§6; the honesty-tier label corresponding to realized n will be reported as-is,
whatever it turns out to be.

**Addendum 9.2 (2026-07-09).** Two corrections surfaced by an independent verification audit
cross-checking this plan and `CLAUDE.md` against the actual codebase:

- (a) §3's reference to "WSR asymptotic CS" was a citation error. The confirmatory statistic
  (`src/lab/eval/anytime.py`) implements the normal-mixture uniform boundary of Howard, Ramdas,
  McAuliffe & Sekhon (2021, *Annals of Statistics* 49(2):1055-1080, arXiv:1810.08240), not
  Waudby-Smith & Ramdas (2020)'s distinct betting-based construction. This is a citation
  correction only — the statistic itself, its time-uniform coverage guarantee, and its role as
  the sole confirmatory claim statistic for H1–H3 are unchanged.
- (b) A pre-specified robustness check, implicit in the exclusion rules (§5) but not previously
  stated explicitly: primary analyses for H1–H3 will be re-run excluding forecasts on markets
  where `resolutions.disputed = 1`, reported as a named robustness check alongside the primary
  result — not a new primary outcome, and not a gate on any hypothesis in §2.

**Addendum 9.3 (2026-07-10).** Motivated by Gebele & Matthes (2026, arXiv 2605.31431), which shows
that a substantial share of apparent long-horizon underconfidence in near-certain prediction-market
contracts reflects settlement-induced discounting (delayed, collateral-locked redemption) rather
than belief miscalibration: as a pre-specified robustness check on H1 (not a change to its primary
specification), the confirmatory analysis will additionally report M1/M1.x skill separately for
(a) negRisk vs. non-negRisk markets, and (b) venues/periods with active collateral-yield programs
(e.g., Kalshi's APY, Polymarket's holding-rewards-eligible markets) vs. without — both mitigate the
settlement wedge per the cited mechanism (Gebele & Matthes §5.3: negRisk conversion compresses it,
yield-bearing collateral flattens its term structure). This stratification is exploratory relative
to the frozen primary hypotheses but is committed now, before any confirmatory data exists,
specifically to prevent this becoming a post hoc excuse in either direction if H1 resolves cleanly
or resolves to null.

**Addendum 9.4 (2026-07-30).** H3's pre-registered external pool (§2) names Kalshi *and*
Metaculus. Metaculus access will not be obtained: on 2026-07-29 Metaculus declined this project's
researcher data request for recent data, offering access only to a 2023-and-earlier archive. That
archive cannot serve a design that scores *live* community predictions against contemporaneous
market prices as questions resolve, so the offer was not a partial fit but a non-fit. H3's realized
external pool is therefore **Kalshi-only for the entire confirmatory window**, and the paper will
report it as such rather than describing a two-venue pool it never had.

Nothing else changes: not the claim statistic, not the honesty tiers, not the exclusion rules, not
any hypothesis in §2. This addendum records an external constraint on realized data scope — a fact
about what could be collected — and is deliberately *not* a revision of the analysis plan. It is
filed under the same discipline as 9.1: a dated fact, not a specification changed after seeing how
the data behaved.

Two consequences worth stating now rather than at write-up. First, `m1_hier@metaculus` (the bare
recalibration call that would have fed a Metaculus quote into M7) will never be exercised on live
data; the M1.x family's realized scope is Polymarket and Kalshi. Second, M1.x's documented
limitation — that it trains on a Polymarket-only historical bootstrap and that between-venue
variance components are weakly identified with so few groups — stops being a caveat about a future
state and becomes a permanent property of this study, to be stated in those terms.

**Addendum 9.5 (2026-08-06).** A five-day operational incident materially changed the *shape* of
the forecast ledger, and this addendum records it before any confirmatory analysis is run.

**What happened.** From 2026-08-02T16:18 to 2026-08-06T16:15 the orchestrator was OOM-killed on a
~62-minute cycle (19 restarts; full technical account in `docs/OPERATIONS.md`). The nightly bundle
completed its forecast step on each attempt and died later, before recording success, so the hourly
missed-run catch-up re-ran it every hour. §6's forecast cadence is "once per market per day per
model, plus an extra forecast when |24h price move| > 0.10" — and that second clause carried no
minimum spacing, because under a once-daily bundle it cannot fire more than once a day. Running
hourly, it fired hourly.

**Measured effect.** In 2026-08-02..06 the ledger received 144,282 rows, of which **54,634 are
beyond one per (market, model, day)** — 38% of that window and **10.0% of the all-time ledger** at
the time of writing. Up to 25 forecasts landed on a single market-model-day (maximum 1 on every day
through 2026-08-01). The excess is concentrated on **1,030 of 5,562 markets (19%)** — and not at
random: the trigger selects markets whose price moved more than 0.10 in 24 hours, so the
over-represented rows are precisely the high-information, high-volatility market-days.

**Why nothing is retracted.** Every one of those rows is individually valid: written at its own
timestamp, paired with its own contemporaneous `p_market_at_ts` (guardrail 13's freshness check
applied unchanged), with no look-ahead. The ledger is append-only (§5) and its daily hashes are
already committed in `docs/ledger_commitments.jsonl`; deleting rows to tidy the record is exactly
the act that discipline exists to make detectable. They stay.

**What this plan commits to instead.** The primary outcome is unchanged: paired Brier skill with
event-clustered anytime-valid confidence sequences, over all resolved forecasts, exactly as
pre-registered. Clustering already absorbs the *dependence* these rows introduce (they are the same
markets), but it does not absorb the *weighting* — a row-weighted mean gives a 25×-duplicated
market-day 25× the influence. Therefore, as a pre-specified robustness check committed here before
the analysis window closes: the confirmatory analysis will additionally report the identical
model × venue × category × window matrix computed on a **deduplicated ledger — the first forecast
per (market, model, UTC day), which is the pre-registered cadence** — reported alongside, never
replacing, the primary result. A material disagreement in sign or in CS exclusion between the two
is itself the finding and will be reported as such.

This is the same construction as 9.2(b)'s disputed-market check and uses the same mechanism (a
parallel `window_label` suffix, never overwriting primary rows). It is filed under 9.1's discipline:
a dated operational fact and a robustness check specified before seeing its result — not a primary
specification changed after seeing how the data behaved.

**Forward fix, for completeness.** `forecast.price_move_min_hours` (default 6h) now gates the
price-move trigger. It is inert under the intended daily bundle — by the time that runs, the last
forecast is ~24h old — and exists solely so a catch-up storm cannot re-fire the same 24-hour move.
The incident window is bounded and closed; no data after 2026-08-06T16:15 is affected.

**Addendum 9.6 (2026-08-06).** M3's coverage parameter `forecast.m3_top_k` is raised from 20 to
120, effective this date. This is a deliberate, dated change to a **collection** parameter, made
before the confirmatory window closes and recorded here rather than discovered in the data.

**Why.** Measured on this date: M3 had accumulated **4 resolved event clusters** — 32 resolved rows
from 694 forecasts, a 4.6% resolution yield, because the priority-category liquid pool it draws
from is 92% longer than 30 days to resolution (921 of 1,001 candidates). At that rate two things
specified in this project reach the 2026-12-31 freeze with nothing to report: the Phase 7 M3
**aggregator** walk-forward refit, gated at `learn.m3_min_resolved: 150` resolved M3 forecasts and
never once triggered; and the Phase 15 **boundary-randomization experiment**, which has assigned
311 randomized and 383 non-randomized forecasts but has almost no resolved outcomes to identify a
marginal effect from. Cost was never the binding constraint: at $0.00081 per evidence run, K=120
costs ~$0.097/day against a $5.00/day cap.

**What this does not do.** It does not make an M3 skill claim reachable. 200 resolved event
clusters — this plan's own INSUFFICIENT boundary (§7) — is out of range for M3 under any K, and
**M3 and M3b will be reported at whatever honesty tier their realized n earns, which on present
evidence is INSUFFICIENT.** This addendum is filed to improve two *secondary* instruments, not to
rescue a primary claim, and it must not be read at write-up as having done the latter.

**Comparability, stated up front.** M3's covered population changes composition on this date: from
2026-08-06 it includes markets ranked 21..120 by the same deterministic liquidity ordering, which
are systematically less liquid than the first 20. The confirmatory analysis will therefore report
M3 results **split at 2026-08-06** as well as pooled, and will not present a pooled M3 figure
without that split alongside it. The ordering rule itself is unchanged — still liquidity-DESC
within priority categories, still no editorial judgment (guardrail 12) — and the randomization band
continues to sit at K±10, now 110..130.

Nothing else changes: not the primary outcome, not the claim statistic, not the honesty tiers, not
any hypothesis in §2. Filed under 9.1's discipline: a dated operational decision with its
consequences stated before its results are seen.
