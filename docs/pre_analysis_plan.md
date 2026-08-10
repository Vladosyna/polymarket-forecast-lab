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

**Addendum 9.7 (2026-08-09).** The shadow portfolio's entry evaluation is restored from weekly to
**daily**, the cadence `CLAUDE.md` §8 has specified since inception ("Entry rule (evaluated daily on
liquid tier, using M4)"). This is recorded here because H2 names the shadow portfolio's simulated
slippage and sizing frictions as its net-of-cost proxy, and that proxy is computed from *realized*
trades — so how often entries are evaluated is load-bearing for a pre-registered hypothesis, not an
interpretability detail.

**What was found.** On this date the portfolio held 14 positions total — 3 resolved, 11 open —
after a month of operation. Three compounding causes, all operational:
(i) `schedule.shadow_cron` was weekly, not daily, from the start: a 7× reduction in entry
opportunities against the specified rule;
(ii) most of even those weekly firings never happened — every analytics cron job carried
APScheduler's 1-second default `misfire_grace_time`, so a firing landing on a busy event loop was
discarded silently (fixed the same day). Positions were opened on three dates only — 2026-07-09,
07-20 and 07-27 — and the latter two are Mondays, i.e. catch-up firings after the 168-hour control
window expired rather than scheduled runs;
(iii) exits are hold-to-resolution and the book is long-horizon by construction: of the 11 open
positions one resolves 2026-08-13 and the rest run to 2026-10-31, 11-30, 12-31 (four), 2027-01-03
and 2027-12-31. The entry rule requires |p_M4 − p_market| ≥ 0.05, and that disagreement concentrates
in exactly the long-horizon markets where the recalibration edge is hypothesised to live.

Capital was never the constraint: 25.5% of the simulated bankroll was deployed, the largest category
at 9.2% of its 20% cap.

**What changes and what does not.** Only the evaluation cadence, back to what §8 already said; the
entry filter, sizing, slippage model, fee schedule and hold-to-resolution exit are all untouched.
No hypothesis, outcome, statistic or honesty tier changes.

**Stated plainly, because it is the honest reading:** this does not retroactively create the trades
that were not opened between 2026-07-09 and 2026-08-09, and it cannot undo (iii). H2's net-of-cost
proxy will therefore rest on a trade population that is thin in absolute terms and thinner still in
*resolved* trades before the 2026-12-31 freeze, since most positions opened from here will not have
resolved by then. H2 will be reported at whatever honesty tier its realized resolved-trade count
earns, and if that count cannot support the net-of-cost comparison, the paper will say so rather
than report a P&L figure that reads as evidence. Filed under 9.1's discipline: a dated operational
correction with its limits stated before its results are seen.

**Addendum 9.8 (2026-08-09).** Kalshi's tier assignment is rekeyed, and Kalshi order-book depth is
collected for the first time. Both change the composition of the tradeable universe, so both are
recorded here before any confirmatory analysis runs.

**What was found.** Every one of the 4,822 Kalshi markets this lab tracks was assigned to the
`tail` tier, and none had ever been `liquid`. The cause is that `assign_kalshi_tier` gated the
liquid tier on Kalshi's own `liquidity_dollars` field, which reads **0.0 for every market Kalshi
publishes** — verified on this date across all 4,822 collected and against a live API sample. The
threshold could therefore never be met. Two consequences followed silently:
(i) the shadow portfolio scans the liquid tier only, so the entire Kalshi venue — including 2,092
markets resolving within seven days — was structurally excluded from it; and
(ii) `bid_depth_usd`/`ask_depth_usd` were written NULL for every Kalshi snapshot (167,564 rows on
this date, none with depth), so even had the tier been right, §8's own entry filter (top-of-book
depth ≥ $500) would have rejected every Kalshi market on a null.

This matters because H2's net-of-cost proxy is computed from *realized* shadow trades, and the
short-horizon stream that could produce resolved trades before the freeze is overwhelmingly
Kalshi's: Polymarket's liquid tier carried six candidates under 30 days to resolution on this date,
out of 481.

**What changes.** Kalshi tiers on traded volume and open interest, which are populated
(`min_volume: 5000`, `min_open_interest: 100`, chosen from the live distribution: volume > 5,000
selects 788 of 4,822 markets). Top-of-book depth is now recorded from `yes_bid_size_fp`/
`yes_ask_size_fp`, which arrive with the market object the collector already fetches — no extra
request — as price × size, the same USD notional the Polymarket depth columns hold. A missing quote
stays NULL and never becomes 0.0.

**What this is expected to yield, measured rather than hoped.** Kalshi books are far thinner at the
top than Polymarket's: across the 60 highest-volume Kalshi markets, median non-zero top-of-book
depth is **$16** against **$415** for Polymarket's liquid tier, and only 6.7% (bid) / 13.3% (ask)
clear §8's $500 threshold, against 48% on Polymarket. The realistic effect is therefore on the order
of dozens of newly eligible Kalshi markets, not thousands — but dozens of *short-horizon* ones,
against the current zero. No filter, threshold or sizing rule in §8 is relaxed to achieve this; if
Kalshi markets do not clear the same $500 bar Polymarket markets face, they are not traded.

**Watch item, recorded now.** `tier` also selects the price-freshness bound (guardrail 13: 15 min
for liquid, 90 for tail), while the tier-wide Kalshi snapshot round runs every 15 minutes. Kalshi
markets promoted to `liquid` therefore sit near that bound, and if the round lengthens, forecasts on
them would be skipped as stale rather than paired against an old price — the safe direction, but a
coverage loss. Kalshi forecast counts will be checked against their pre-change level, and this
addendum amended if the bound has to move.

**Addendum 9.9 (2026-08-10).** Kalshi's tier assignment moves onto the lab's own measured
order-book depth, using the same thresholds already applied to Polymarket. This supersedes the
volume/open-interest keys introduced one day earlier in 9.8, which are retained only as a fallback
for markets not yet snapshotted.

**Why now, and why this rather than tuning the proxies.** `CLAUDE.md` Phase 17 item 2 already
requires tiering on collected depth rather than venue-reported fields, and Polymarket was moved to
it on 2026-07-07. Kalshi could not follow because no depth was collected for it; that changed on
2026-08-09 (addendum 9.8). With depth in hand, keeping Kalshi on volume and open interest would
have left the two venues on different definitions of the same word — and the proxies are the weaker
signal in both directions: lifetime volume says nothing about whether anyone is quoting now, and a
market can be deeply quoted with no volume recorded at all.

**One bar, both venues.** The measured distributions are close enough to share the existing
`universe.tiers.*.min_depth_usd` thresholds rather than inventing venue-specific ones: on
2026-08-10, per-market top-of-book depth was p25 $10 / p50 $45 on Kalshi against p25 $9 / p50 $66 on
Polymarket. At the shared liquid bar ($250) this selects 484 of 4,803 Kalshi markets, against 26
under 9.8's open-interest rule. Polymarket's assignment is unchanged — verified by differential
comparison against the previous implementation across every depth × liquidity × volume combination
tested, with zero mismatches.

**A contract defect found and fixed in the same pass.** `_depth_lookup` documents that a market with
no depth data is *absent* from its result, so tiering falls back rather than treating it as $0. The
implementation summed `fill_null(0)` over both columns, so a row whose quote had no size on either
side became a measured $0 and tiered `ignored`. This was unobservable while Polymarket was the only
venue with depth (every row has it); on Kalshi 1,715 of 4,803 markets were in exactly that state,
and shipping the change without this fix would have excluded all of them from the universe. The
implementation now matches its documented contract for both venues.

**Also closed here:** Kalshi universe exclusions were never written to `universe_log`, though this
venue carries roughly 80% of the lab's daily forecast rows. Phase 15's commitment — that "why isn't
X in the ledger" is answerable for every considered market — now holds for Kalshi too.

**What this does not change.** No hypothesis, outcome, statistic or honesty tier. The shadow
portfolio's entry filters (§8) are untouched: a Kalshi market still has to clear the same $500
top-of-book depth, the same 0.03 spread and the same 0.05 edge as a Polymarket one. Widening the
liquid tier changes which markets are *considered*, never the bar they must clear.

**Watch items.** The liquid tier drives one order-book-ladder request per market per 15-minute
round, so 484 markets adds ~0.5 req/s against Kalshi's ~10 (guardrail 8) -- the round's duration
will be checked and this addendum amended if it crowds. And 9.8's freshness watch item stands:
`tier` also selects the price-freshness bound, and a much larger liquid tier is a much larger
exposure to it. Kalshi forecast counts have not dropped so far (11,164 → 13,507 across 08-07..08-10)
and will keep being compared against that level.

**Addendum 9.10 (2026-08-10).** Measured consequences of 9.9, recorded before the reassignment
takes effect rather than after, and one decision stated explicitly so it cannot be re-read later as
convenient.

Applying the shared depth bar to the 4,808 live Kalshi markets moves them as follows: **444 to
`liquid`** (from 26), 1,865 to `tail`, **649 to `ignored`**, and 1,876 fall through to the
volume/open-interest fallback because they have no measured depth yet. Twenty-two of the 26 markets
9.8's open-interest rule had made liquid stay liquid; eleven drop to `tail` and eleven to `ignored`
on their actual books, which is the point of preferring measurement to a proxy.

**The 649 exclusions are the part that costs something.** They are all `economics`, and 154 of them
(24%) resolve within seven days — a *higher* short-horizon share than the 444 being promoted (5%).
Short-horizon resolved observations are this study's scarcest resource, so this cuts against the
direction 9.7–9.9 were working in. Two facts about them: their measured top-of-book depth is below
$10, and none of the 649 has ever produced a resolved forecast to date.

**The exclusion stands, and the reason is not n.** A market with under $10 of top-of-book depth has
no price anyone is meaningfully making, and this study's entire claim is skill measured *against the
market price*. Pairing a forecast with a quote that thin does not produce a weak observation; it
produces a comparison whose baseline is noise, in both directions. The same bar applies to
Polymarket and lands at the same place in each venue's own distribution (roughly p25: $9 on
Polymarket, $10 on Kalshi), so this is not a venue-specific harshness either. Choosing a laxer bar
for Kalshi after seeing that it would retain more short-horizon markets would be fitting the
specification to the sample — the precise move the pre-analysis discipline exists to prevent — and
is therefore not made.

Realized Kalshi forecast volume will be reported before and after this change, so the coverage cost
appears in the paper as a number rather than as an absence.

**Addendum 9.11 (2026-08-10).** A defect in Kalshi's universe sync put forecasts into the ledger on
markets that had already ended. This addendum records it, its measured effect, and a pre-specified
robustness check — before the confirmatory analysis is run.

**What happened.** `sync_kalshi_universe` bounded each cycle at `max_series_per_sync` (40) but
walked a fixed category and API order and simply stopped at the cap. The same head of the list was
re-synced every hour and the tail was never reached at all. Because only a sync refreshes a market's
`active`/`closed` flags, markets in the starved tail stayed flagged open indefinitely. Measured on
2026-08-10 across 5,009 Kalshi markets in ~285 series: **82% had not been re-synced in over three
days**, and **1,772 were still flagged active with an end date in the past**.

Those markets stayed in the forecast-eligible set, so **39,583 forecasts were written on Kalshi
markets already past their end date at the moment of writing** — across 549 distinct markets, and
all 39,583 have since resolved, so all are in the scoring population. That is **38% of Kalshi's
resolved rows** (8,028 of 21,356 for `m1_debiased`).

**Why it matters, and in which direction.** A market past its end date has stopped trading and its
outcome is determined; the price we pair against is a frozen last quote. Both the model and the
market baseline therefore sit on the known answer and the paired Brier difference collapses toward
zero. This is dilution, not inflation — measured on the live data, excluding these rows moves Kalshi
skill *away* from zero in every case:

| model | all rows | past-dated only | live-only |
|---|---|---|---|
| `m1_debiased` | −0.001852 | −0.000759 | **−0.002510** |
| `m1_hier@kalshi` | −0.001380 | −0.000773 | **−0.001747** |
| `m4_ensemble` | +0.000935 | +0.001201 | **+0.000779** |

The bias is conservative for a positive skill claim, but it is still a specification defect, and it
inflates n — this study's binding constraint — by 38% on its largest venue with rows that carry
almost no information. The honesty tiers (§7) are computed on that n.

**What is committed.** The rows stay: the ledger is append-only and their hashes are already in
`docs/ledger_commitments.jsonl`. As a pre-specified robustness check, the confirmatory analysis will
report the identical model × venue × category × window matrix **excluding forecasts written on or
after their market's end date**, alongside — never replacing — the primary result. This is the same
construction as 9.5's deduplicated-ledger check and 9.2(b)'s disputed-market check, and uses the
same parallel `window_label` mechanism.

**Forward fixes, both landed today.** The forecast loop now refuses any market past its own end date,
independent of how fresh the sync is. And the sync's cap became a rotation: series are ordered
least-recently-synced first (never-synced ahead of all), so the bound stays a politeness limit
rather than a permanent cutoff. The incident window is closed as of 2026-08-10.

**Addendum 9.12 (2026-08-10).** Phase 15's microstructure covariates on the forecast ledger begin
today. They were specified in `CLAUDE.md` §5 and in Phase 15's own acceptance criteria ("covariate
columns populate on live forecasts") when the phase was written, and were never implemented: an
audit on this date found only `spread_at_ts`, which predates Phase 15, and a code comment recording
the rest as "a separate sub-task" that was then never picked up.

From 2026-08-10, every forecast row carries `depth_covariate` (top-of-book depth in USD, from the
same snapshot that supplies `p_market_at_ts`), `volume_24h` (the venue's own 24-hour volume, from the
market object the universe sync already fetches), and `hour_utc`, alongside the `spread_at_ts` that
was already there. `trades_24h` remains NULL: neither venue returns a 24-hour trade count on the
objects the collector already fetches, and adding a per-market Data API call is not free at the
collector's current load. The paper will report it as not collected rather than as missing data.

**Consequence to state plainly.** The brief requires these to be "populated going forward, never
backfilled by reconstruction", and that rule is kept. Forecast rows written before today therefore
have NULL covariates, and the heterogeneity analyses that use them — the pre-registered exclusion
and stratification work is unaffected, but any depth-, volume- or hour-conditioned split is not —
run on the window from 2026-08-10 to the 2026-12-31 freeze, not on the full collection period. That
window is stated with each such result rather than left implicit, and no covariate is reconstructed
for earlier rows even where the archive would technically permit it: a reconstructed covariate is a
different measurement from a frozen one, and mixing them silently is the failure this rule exists to
prevent.

Nothing else changes: no hypothesis, outcome, statistic, honesty tier or exclusion rule.

**Addendum 9.13 (2026-08-10).** H1's primary statistic was never being computed, and its realized
sample size is far smaller than this plan assumed. Both facts are recorded here, before the
confirmatory analysis, because the second is the one that matters for what this paper can claim.

**The measurement gap.** §2 states H1 over horizon buckets — `m1_debiased` and `m1_hier@polymarket`
beating `m0_market` "on paired Brier skill in the ≥30-day horizon buckets". `run_eval`'s dimensions
were model × venue × category × window, with no horizon dimension at all, so no row in `eval_runs`
has ever corresponded to H1's stated stratification. Everything downstream that keys off an
`eval_runs` row — the anytime-valid confidence sequence, event-cluster counts, honesty tiers, the
report — therefore never covered the primary hypothesis either. Fixed on this date: horizon buckets
are scored through the same machinery as every other cell, under `window_label` suffixes
(`all_time_h_30to90d` and so on) that sit alongside primary rows and never overwrite them.

**What computing it revealed.** Resolved paired forecasts on Polymarket, by M1's own horizon
buckets, in **event clusters** (the unit §7's honesty tiers count):

| model | <7d | 7–30d | **30–90d** | **>90d** |
|---|---|---|---|---|
| `m1_debiased` | 993 | 316 | **33** | **22** |
| `m1_hier@polymarket` | 771 | 267 | **13** | **18** |

H1's pre-registered stratum holds **13–33 clusters against this plan's own 200-cluster INSUFFICIENT
floor** — roughly an order of magnitude short, while the short-horizon buckets it is *not* stated
over are well populated. The cause is structural rather than operational: a forecast enters the
≥30-day stratum only once its market resolves, which for that stratum is by construction ≥30 days
later, and the confirmatory window closes 2026-12-31.

**What this plan commits to.** No change to H1, its statistic, or the honesty tiers: the discipline
that matters here is reporting the tier the realized n earns. On present trajectory **H1 will be
reported as INSUFFICIENT DATA in its own pre-registered stratum**, and the paper will say so plainly
rather than substituting the well-populated short-horizon buckets, which test a different claim —
substituting them after seeing which strata filled would be precisely the specification-fitting this
plan exists to prevent. The horizon-bucket table will be reported in full, including the
short-horizon cells, so the reader sees both the result and why the stratum H1 names is thin.

Recorded now rather than at write-up so that the shortfall is a pre-registered expectation, not a
post-hoc discovery. Had the statistic been computed when the pipeline was built, this would have
been visible months earlier.
