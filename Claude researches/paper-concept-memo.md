# Paper Concept Memo — Polymarket Forecast Lab

**Status:** working document (unlike the PAP, this file is meant to evolve). **Date:** 2026-07-08.
**Companion documents:** `docs/pre_analysis_plan.md` (committed 2026-07-06 — FROZEN, addenda only) and `CLAUDE.md` v2.6 (system spec).

This memo is where every publication decision starts: the thesis, the argument map, the referee-objection preparation, the journal strategy, and the timeline. The PAP defines what we may *claim*; this memo defines how we *argue* it.

---

## 1. Working titles

Results-contingent — pick after the analysis freeze, draft both abstracts now:

- **If skill is found (some H supported):**
  *"Beating the Crowd on Its Own Terms: A Live, Pre-Registered Test of Prediction-Market Calibration"*
- **If null (no H supported):**
  *"Efficient After All? A Live, Pre-Registered Test of Documented Prediction-Market Anomalies"*
- **Short-note variant (H1 only, Economics Letters format):**
  *"Does a Prediction-Market Anomaly Survive Its Own Publication? Out-of-Sample Evidence on Horizon-Dependent Underconfidence"*

## 2. The hook (why a journal should care)

Three contributions, in order of novelty:

**C1 — Methodological: the first (to our knowledge) cryptographically pre-registered, live market-efficiency test on prediction markets.** The entire prior calibration/efficiency literature is ex-post: researchers download resolved history and fit hypotheses to it. Our forecasts were frozen in an append-only ledger, hash-committed nightly to a public repository *before* resolution, with hypotheses fixed in a dated PAP. A referee can verify what was predicted and when without trusting the author. This methodological device is reusable regardless of what the results show — which is precisely why the null is publishable too.

**C2 — Empirical: post-publication persistence of a documented anomaly.** Le (2026, arXiv 2602.19520) documented systematic long-horizon underconfidence (calibration slope 0.99 → 1.32 by horizon) on data through 2025 and was posted publicly in February 2026. Our confirmatory window opens July 2026. H1 therefore tests whether the anomaly survived its own publication — the prediction-market analogue of McLean & Pontiff's (2016, JF) post-publication decay question for equity anomalies. Either answer is informative: persistence implies limits to arbitrage in these venues; decay implies these markets absorb academic findings fast.

**C3 — Infrastructure: an open, replicable measurement instrument.** MIT-licensed, 356+ tests, multi-venue collection, versioned models, replication export (`lab export --paper`). The instrument outlives the paper.

Framing discipline: the paper is an *efficiency test*, not a trading-strategy paper. We never claim tradable profit; the shadow portfolio and net-of-cost lines exist to bound economic significance, not to advertise returns.

## 3. Thesis and argument map

**Core thesis (skill world):** Documented calibration distortions in prediction markets are exploitable in real time by simple, pre-specified statistical corrections — implying these markets aggregate information well but weight it with systematic, persistent biases.

**Core thesis (null world):** Once tested live, pre-registered, and net of frictions, previously documented distortions provide no usable edge — prediction-market prices are efficient against public-information models, and ex-post anomaly findings in this literature likely overstate exploitability.

Argument chains (shared by both worlds):

1. **Identification of "skill":** paired Brier difference vs the same venue's contemporaneous price is the only defensible skill definition (beating chance is trivial; beating the market is the claim) → PAP §3.
2. **Honest inference:** anytime-valid confidence sequences remove peeking bias from continuous monitoring; event-level clustering prevents multi-venue pseudo-replication; the sports null control catches a broken harness → PAP §3, §5.
3. **Mechanism, not magic:** every model is a named, pre-specified correction targeting a documented bias (horizon slope, tail bias, model-drivable categories, cross-venue coherence) — we test mechanisms the literature proposed, not a black box.
4. **Economic significance bound:** shadow-portfolio net-of-cost line separates statistical from economic significance explicitly.

## 4. Referee objection table (prepare answers before writing)

| # | Anticipated objection | Prepared answer | Evidence lives in |
|---|---|---|---|
| 1 | "Hypotheses chosen after seeing data" | PAP dated 2026-07-06, committed before confirmatory window; nightly sha256 ledger commitments; deviations only as dated addenda | `docs/pre_analysis_plan.md`, `docs/ledger_commitments.jsonl`, git history |
| 2 | "Selective universe / cherry-picked markets" | Whole-universe forecasting by cheap models (guardrail 12); `universe_log` records every exclusion with a reason code; exclusion rules pre-registered verbatim | PAP §5, `universe_log` table |
| 3 | "Multiple testing" | Exactly three primary hypotheses, fixed in advance, each with its own AV CS; everything else explicitly labeled exploratory (PAP §4); no garden of forking paths | PAP §2–§4 |
| 4 | "Edge is illusory once you pay spread/fees" | Net-of-cost line (simulated fills, slippage, fee schedules versioned over time); tail-priced markets excluded as targets precisely because frictions dominate there | `CLAUDE.md` §8, Phase 15; PAP H2 wording |
| 5 | "Venues aren't independent — you're triple-counting events" | Event-level cluster bootstrap; n counts resolved event clusters, not venue-market rows | `CLAUDE.md` §7; `eval/scoring.py` tests |
| 6 | "LLM training data contaminates the test" | LLM models carry no primary hypothesis; live-only rule (guardrail 15); skill accrues only after each version's `registered_ts`; boundary randomization identifies M3's marginal contribution causally | PAP §6; `CLAUDE.md` guardrails 11/15, Phase 15 |
| 7 | "Volume/liquidity metrics are contaminated by wash trading" | Tiering and liquidity covariates are order-book-depth-based, not volume-based; volume retained only as a flagged covariate; cite Sirolly et al. (2025) | `CLAUDE.md` Phase 17 |
| 8 | "One venue, one regime, one year" | Multi-venue (Polymarket, Kalshi confirmatory; Metaculus signal); period-specificity acknowledged; AV inference is honest about n via tier labels and MDE | PAP §3; report tiers |
| 9 | "Model parameters drift — what exactly was pre-registered?" | The pre-registered object is the *system including its update protocol*: versioned models, walk-forward-only refits, champion/challenger promotion with its own registered_ts logic — all specified before the window | PAP §6; `model_versions` registry |
| 10 | "Disputed/ambiguous resolutions bias outcomes" | Dispute flag recorded; robustness section re-runs primary analyses excluding disputed markets; ambiguous-wording markets excluded ex ante | PAP §5; `resolutions.disputed` |
| 11 | "Why is a null interesting?" | Because the prior literature is ex-post and reports anomalies; a pre-registered live null bounds their exploitability and speaks to post-publication decay (C2) | §2 of this memo |

## 5. Literature spine (positioning)

Core: Wolfers & Zitzewitz (2004, JEP) — prediction markets as information aggregators; Snowberg & Wolfers (2010, JPE) — favorite-longshot bias mechanisms; Le (2026, arXiv 2602.19520) — horizon-dependent calibration on 292M trades (the anomaly H1 replicates out-of-sample); Qin & Yang (2026, arXiv 2606.04217) — reversed FLB on the 1.2B-trade Polymarket archive; Reichenbach & Walther (SSRN 5910522) — trader skill concentration; McLean & Pontiff (2016, JF) — post-publication anomaly decay (the C2 frame); Satopää et al. (2014) / Baron et al. (2014) — extremizing; Atanasov et al. (2017, Mgmt Sci) — markets vs polls; Choe & Ramdas (2024, OR) — anytime-valid forecaster comparison (our inference backbone); Halawi et al. (2024, NeurIPS) — LLM forecasting vs crowds; Sirolly et al. (2025) — wash trading on Polymarket.

Gap we fill: none of the empirical papers above are pre-registered or live; none provide a verifiable commitment device; the LLM-forecasting papers are backtests subject to training-data leakage, which our live-only rule eliminates by construction.

## 6. Paper skeleton

1. **Introduction** — the efficiency question; the ex-post problem in this literature; our commitment device; preview of results. (Write LAST.)
2. **Related work** — per §5 spine; end on the McLean–Pontiff framing.
3. **Institutional setting & data** — venues, resolution mechanics (UMA, dispute windows), universe policy, multi-venue collection, wash-trading caveat and depth-based liquidity.
4. **Methodology: the measurement instrument** — ledger + hash commitments; paired Brier vs market; event clustering; anytime-valid CS; stratified secondary estimator; null control; PAP summary. This section carries C1 — write it as the paper's centerpiece.
5. **Models** — M0 baseline; recalibration family (M1/M1.x, hierarchical partial pooling); structural nowcasts (M5); coherence (M6); cross-venue pool (M7); ensemble with correlation-discounted extremization. LLM pipeline (M3) described but flagged as exploratory + randomized-boundary design.
6. **Results** — H1, H2, H3 in PAP order, each with CS plot, tier label, MDE; attribution waterfall for the ensemble; wealth-ledger equity curves as interpretation (log scale, bootstrap bands, null-control band).
7. **Robustness** — exclude disputed; stratified estimator agreement; per-venue splits; gap-aware CLV; exploratory outcomes clearly fenced.
8. **Economic significance** — net-of-cost shadow results; why statistical ≠ tradable.
9. **Discussion & limitations** — one period, category coverage, hierarchical small-group caveat, what decay/persistence means.
10. **Reproducibility statement** — repo, `lab export --paper`, commitment verification instructions.

## 7. Journal strategy

- **Primary: International Journal of Forecasting.** Exact fit (prediction markets, forecast evaluation, aggregation); values replication packages; realistic timeline 6–12 months to first decision. Aim: full paper per skeleton above.
- **Fast-track option: Economics Letters** — 2,000-word note on H1 only (post-publication persistence). Decision rule: choose this ONLY if H1 resolves cleanly and early (tier ≥ preliminary by October 2026) AND we accept that the full IJF paper must then lead with H2/H3 to avoid salami-slicing objections. Otherwise skip.
- **Fallbacks:** Journal of Forecasting; Journal of Behavioral Finance (if the bias-persistence angle dominates); Journal of Prediction Markets (floor).
- **Explicitly not a fit:** China & World Economy — different scope entirely; this paper is a second, independent track of the publication portfolio, parallel to (not replacing) the Uzbekistan solar-adoption paper.

## 8. Timeline

| When | What |
|---|---|
| Now (July 2026) | Commit Addendum 9.1 to the PAP fixing the analysis freeze (§9 below). Start drafting Sections 3–5 (setting, methodology, models) — they don't depend on results. |
| Oct 2026 | Mid-window checkpoint: tier status per hypothesis; EL fast-track go/no-go on H1. |
| 2026-12-31 | Confirmatory data freeze (per Addendum 9.1). |
| Jan 2027 | Run pre-registered analyses exactly as PAP specifies; write Results/Robustness. |
| Feb–Mar 2027 | Full draft; internal red-team pass using §4's objection table; replication package check. |
| Apr 2027 | Submit IJF. |
| Through 2027 | Review cycle; an R&R in hand by PhD-application season is itself a credible signal even before acceptance. |

## 9. Proposed PAP Addendum 9.1 (ready to commit — the author's deliberate act, verbatim text below)

> **Addendum 9.1 (2026-07-XX).** The confirmatory analysis window for H1–H3 closes at 2026-12-31 23:59 UTC. Forecasts frozen on or before that timestamp, resolving at any later date, remain in the confirmatory set; forecasts frozen after it are exploratory for this paper and may seed a future pre-registered window. Primary analyses will be executed once, after the freeze, exactly as specified in §2–§6; the honesty-tier label corresponding to realized n will be reported as-is, whatever it turns out to be.

Commit it early: fixing the endpoint two days into a six-month window, before any confirmatory result is visible, strengthens the pre-registration; fixing it later invites the "you stopped when the numbers looked good" objection.

## 10. Authorship, disclosure, ethics

- **Sole author:** Yurchyna Vladyslav. AI systems cannot hold authorship under Elsevier/IJF policy.
- **AI disclosure (two distinct roles, keep them separated in the paper):** (a) generative AI as *object of study* — the M3 evidence pipeline — described fully in Methods; (b) generative AI as *tool* — system engineering and manuscript drafting assistance — disclosed in the declaration section per journal policy. Conflating these invites confusion; the paper states both plainly.
- **Ethics/data:** public market data only; no human subjects; no trading, no capital at risk; read-only architecture documented in the repo. One line in the paper suffices.
- **Funding/conflicts:** none; open-source MIT; author holds no positions on any studied venue.

## 11. Pre-submission checklist

- [ ] Addendum 9.1 committed (dated) — do this first.
- [ ] PAP §9 contains zero silent edits (verify via git history).
- [ ] Every claim in Results cites its tier and MDE; nothing below "preliminary" is claimed.
- [ ] Null-control row shown in every skill table.
- [ ] Ledger-commitment verification instructions tested by a person who is not the author.
- [ ] `lab export --paper` round-trips on a clean machine.
- [ ] Each §4 objection has a written answer somewhere in the manuscript.
- [ ] AI-disclosure declaration drafted per current Elsevier wording.
- [ ] Cover letter: leads with C1 (pre-registration device), not with any performance number.
