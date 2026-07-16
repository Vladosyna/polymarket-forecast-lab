# Reading List — Polymarket Forecast Lab / Paper Prep

**Purpose:** get you fluent in both the domain and the specific choices `CLAUDE.md` and the Concept Memo already made, so drafting Sections 2–5 of the paper (which don't depend on results) is writing-from-understanding, not writing-from-citation-list. Organized by role in the argument, not alphabetically — matches the Concept Memo's skeleton (§6) so you can cross-reference while drafting.

Citations marked ✓ were verified via search just now (exact journal/volume/pages) specifically because they're headed for a real bibliography and memory-recalled details are the easiest way to embarrass yourself in a References section. Unmarked ones are extremely well-established works I'm highly confident in from training, but double-check page numbers when you actually build the bibliography — standard practice regardless of source.

---

## Tier 0 — Field scan for competing/adjacent work (added 2026-07-10)

Checked whether anyone has already done our specific study (live, pre-registered, cryptographically-committed calibration test). Nobody has — the C1+C2 combination is unclaimed. But the field moved fast since the Concept Memo's original literature spine was drafted, and one paper below requires a real methodological response, not just a citation.

- **Gebele, J. & Matthes, F. (2026).** *When Certainty Is Not Worth It: Capital Lock-Up and Settlement Discounting in Prediction Markets.* arXiv:2605.31431. — **Read this one carefully, before drafting H1's Methodology paragraph.** Argues persuasively that horizon-dependent underpricing of near-certain contracts partly reflects a rational cost-of-locked-capital discount, not belief miscalibration — their adjustment removes 48–88% of the raw horizon gradient. Notably does not cite Le (2026) — the two papers analyze the same underlying phenomenon independently. We pre-register a robustness response (PAP Addendum 9.3, drafted in the Concept Memo §4.1) rather than discover this after seeing results.
- **Grant, A., Johnstone, D. & Kwon, O.K. (2019).** *The Cost of Capital in a Prediction Market.* International Journal of Forecasting, 35(1), 313–320. — The theoretical ancestor of the settlement-discounting mechanism above, and already published in our target journal — a strong, on-point citation for the Introduction.
- **Page, L. & Clemen, R.T. (2013).** *Do Prediction Markets Produce Well-Calibrated Probability Forecasts?* Economic Journal, 123(568), 491–513. — Gebele & Matthes's own "closest direct predecessor" for horizon-conditioned calibration distortion (on Intrade). Read alongside Le (2026) — together they're the pre-2026 and 2026 anchors for the exact pattern H1 tests the persistence of.
- **Maresca, C. (2026).** *Can Interest-Bearing Positions Solve the Long-Horizon Problem in Prediction Markets?* arXiv:2602.21091. — Agent-based simulation (LLM traders), complementary direction: the induced bias itself is small (0.72pp), and interest-bearing positions eliminate ~83% of it — but per the paper's own reading, mostly by tripling participation (17%→62% of wealth at stake), not by directly correcting the bias. Keep that channel distinct if you cite the 83% figure. Gebele & Matthes cite this themselves as directionally consistent with their empirical result, while explicitly cautioning it isn't a replication — the two papers' estimands differ.
- **Gomez-Cram, R., Guo, Y., Jensen, T.I. & Kung, H. (2026).** *Prediction Market Accuracy: Crowd Wisdom or Informed Minority?* SSRN 6617059. — High-visibility LBS/Yale paper: ~3% of Polymarket traders drive most price discovery. Orthogonal to our question (who moves the price vs. whether the resulting price is well-calibrated) but expect referees to know it; worth a Related Work sentence.
- **Della Vedova, J. (2026).** *Who Profits from Prediction Markets? Execution, not Information.* SSRN 6191618. — Trader-level profit decomposition (directional vs. execution skill) on 222M trades; finds no trader type beats the price-implied accuracy benchmark on pure direction. Adjacent, citable, not competing.
- **Diercks, A.M., Katz, J.D. & Wright, J.H. (2026).** *Kalshi and the Rise of Macro Markets.* NBER Working Paper 34702 (also circulated as a Federal Reserve FEDS paper). — **Read this before drafting anything about P1.** Fed Reserve authors, not competitors: Kalshi's CPI/FOMC/GDP/unemployment markets already match or beat Bloomberg consensus and Fed funds futures. This is your citation for "P1 is a serious category," and your honest baseline for how good the raw price already is before M5/M1 try to improve on it.
- Briefly noted: no critique or replication of Le (2026) exists yet (it's five months old). Le's own paper explicitly invites a persistence check — worth quoting directly in your Introduction. The anytime-valid/e-value literature is active in 2026 (mostly clinical trials — Howard/Ramdas-adjacent authors are still publishing on it) but nobody has applied it to prediction-market efficiency testing yet.
- Lower priority, cite only if Related Work needs more breadth: Becker (2026, jbecker.dev) — maker/taker wealth transfer on Kalshi; Akey, Grégoire, Harvie & Martineau (2026, SSRN 6443103) — who wins/loses on Polymarket; Bartlett & O'Hara (2026, SSRN 6615739) — adverse selection on Kalshi. All trader-P&L microstructure, different unit of analysis from ours.



These three ARE the paper's spine (Concept Memo §2–§3). Everything else is support.

1. **Le, N.A. (2026).** *Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics in Prediction Markets.* arXiv:2602.19520. — The anomaly H1 tests for persistence of. 292M trades, 327K contracts, Kalshi+Polymarket. Read for the exact horizon-slope numbers (0.99→1.32) and the politics/weather domain breakdown — you'll be citing these figures directly.
2. **Choe, Y.J. & Ramdas, A. (2024).** *Comparing Sequential Forecasters.* Operations Research, 72(4), 1368–1387. — Your evaluation backbone. This is literally the confidence-sequence machinery `eval/anytime.py` implements. Understand it well enough to defend it under review (objection #1 in the Concept Memo's table).
3. **McLean, R.D. & Pontiff, J. (2016).** ✓ *Does Academic Research Destroy Stock Return Predictability?* The Journal of Finance, 71(1), 5–32. — Not about prediction markets at all, but it's the paper that makes your null result interesting (C2 in the memo). Read it for the *framing move*, not the equity-market mechanics: publication as a treatment, out-of-sample decay as the outcome. That's your H1 in different clothes.

---

## Tier 2 — The theory underneath each design choice

Organized by which piece of the system they justify. You've already argued through the substance of most of these in our conversation — this tier is about being able to cite them correctly and defend them from first principles, not re-deriving anything.

**Anytime-valid inference (why we can read the report every night without inflating error):**
- **Shafer, G. (2021).** ✓ *Testing by Betting: A Strategy for Statistical and Scientific Communication.* Journal of the Royal Statistical Society: Series A, 184(2), 407–431. — The accessible entry point to the whole "capital process = statistical test" idea. Short, was read before the RSS with discussion — the discussion papers (same issue) are worth skimming for objections you'll face.
- **Howard, S.R., Ramdas, A., McAuliffe, J. & Sekhon, J. (2021).** ✓ *Time-Uniform, Nonparametric, Nonasymptotic Confidence Sequences.* The Annals of Statistics, 49(2), 1055–1080 (arXiv:1810.08240). — The actual construction our CS uses (normal-mixture uniform boundary, plug-in running variance) — corrected here after a v2.7 audit found the brief had cited Waudby-Smith–Ramdas instead, which is a *different*, nonasymptotic betting-based construction we deliberately don't implement. Read this one, not the WSR paper below, if you're verifying the Methodology section's CS paragraph against `eval/anytime.py`.
- **Waudby-Smith, I. & Ramdas, A. (2024).** ✓ *Estimating Means of Bounded Random Variables by Betting.* Journal of the Royal Statistical Society: Series B, 86(1), 1–27 (with discussion). — Related work, not what we implement (see above) — still worth citing in the paper as the betting-based alternative construction, since referees who know this literature will expect it acknowledged.
- **Shafer, G. & Vovk, V. (2019).** *Game-Theoretic Foundations for Probability and Finance.* Wiley. — The full book, if you want the deep version. Not required for the paper; useful if you want to actually verify the martingale math rather than trust the citation.

**Extremization (why the ensemble pushes toward 0/1, and by how much):**
- **Satopää, V.A., Baron, J., Foster, D.P., Mellers, B.A., Tetlock, P.E. & Ungar, L.H. (2014).** *Combining Multiple Probability Predictions Using a Simple Logit Model.* International Journal of Forecasting, 30(2), 344–356. — The empirical case for the ~2× extremization factor.
- **Neyman, E. & Roughgarden, T. (2022).** *Are You Smarter Than a Random Expert? The Robust Aggregation of Substitutable Signals.* Proceedings of the 23rd ACM Conference on Economics and Computation, 990–1012. — The worst-case-optimal √3 factor, and the correlation-discount logic Phase 13 implements.

**The Kelly/wealth-ledger duality (and its limit — you already caught the overclaim here):**
- **Kelly, J.L. (1956).** *A New Interpretation of Information Rate.* Bell System Technical Journal, 35(4), 917–926.
- **Cover, T.M. (1991).** *Universal Portfolios.* Mathematical Finance, 1(1), 1–29.
- Read these for the log-wealth = log-score duality itself — but remember the v2.2 correction we made: the wealth ledger and the anytime-valid CS are *related by this theory*, not identical computations. If you cite Kelly/Cover to justify the wealth ledger's interpretability, don't let the paper imply it's a third independent confidence interval — it isn't, and a sharp referee will notice if it's oversold.

**Proper scoring (why Brier, why not just "did the higher-probability side win"):**
- **Gneiting, T. & Raftery, A.E. (2007).** *Strictly Proper Scoring Rules, Prediction, and Estimation.* Journal of the American Statistical Association, 102(477), 359–378. — The canonical citation for the whole choice-of-metric section.

**Online learning / MWU (the theory behind the Phase 14.1 shadow challenger):**
- **Cesa-Bianchi, N. & Lugosi, G. (2006).** *Prediction, Learning, and Games.* Cambridge University Press. — A textbook, not a paper. Read Chapter 2 (Hedge / exponentially weighted forecaster) and the log-loss mixability result (Vovk's aggregating algorithm, ch. 3 or 9 depending on edition) — that's the η=1-reduces-to-Bayesian-model-averaging result the design leans on.

**Hierarchical shrinkage (the theory behind M1.x's ridge/empirical-Bayes partial pooling):**
- **Efron, B. & Morris, C. (1977).** *Stein's Paradox in Statistics.* Scientific American, 236(5), 119–127. — Short, non-technical, the best possible entry point to why shrinkage beats per-group MLE, and directly explains the "small-group caveat" already flagged in the M1.x paragraph.

---

## Tier 3 — The prediction-market empirical literature (situates the paper)

- **Wolfers, J. & Zitzewitz, E. (2004).** *Prediction Markets.* Journal of Economic Perspectives, 18(2), 107–126. — The foundational "markets as aggregators" framing. Almost certainly your first citation in the Introduction.
- **Arrow, K.J. et al. (2008).** *The Promise of Prediction Markets.* Science, 320(5878), 877–878. — Short, institutional, good for a second early citation alongside Wolfers & Zitzewitz.
- **Snowberg, E. & Wolfers, J. (2010).** *Explaining the Favorite-Longshot Bias: Is it Risk-Love or Misperceptions?* Journal of Political Economy, 118(4), 723–746. — The classical FLB mechanism story; needed to set up why Qin & Yang's *reversed* FLB on Polymarket is notable.
- **Qin, B. & Yang, R. (2026).** *Polymarket-v1 Database.* arXiv:2606.04217. — **Correction (2026-07-12): this is NOT the bootstrap dataset our system trains on.** Qin & Yang's own archive is a different, independently-hosted one (1.2B trades, 1.3M markets, ground-truth on-chain trade direction, at `TimeSeventeen/Polymarket-v1`); our bootstrap uses `SII-WANGZJ/Polymarket_data` (1.1B/268,706), which carries no paper attribution in our own specification. An earlier version of this entry said otherwise and that error propagated into the paper draft — worth remembering exactly because it happened once already. Qin & Yang remains a legitimate, on-point citation for two separate things: the reversed-FLB finding it documents directly (termed a "favorite-longshot reversal" in their own §4.2, which is what justifies M1 fitting the tail-bias direction rather than hardcoding it), and — a bonus find — a sourced, category-dated account of Polymarket's 2026 fee reform (crypto Jan, sports Feb, other categories Mar) via their own difference-in-differences design, useful if `data/fee_schedule.yaml`'s currently-sentinel Polymarket date is ever replaced with a researched one.
- **Reichenbach, F. & Walther, M. (2025).** *Exploring Decentralized Prediction Markets: Accuracy, Skill, and Bias on Polymarket.* SSRN 5910522. — Trader-skill concentration; relevant to your Discussion section on who's on the other side of a "beat the market" claim. Note the year: SSRN itself dates this December 12, 2025, not 2026 — fixed here after catching the wrong year in an earlier draft of this list.
- **Sirolly, N., Ma, S., Kanoria, Y. & Sethi, R. (2025).** *Network-Based Detection of Wash Trading.* Columbia Business School Research Paper No. 5714122. — Your citation for the depth-vs-volume tiering decision (Phase 17); needed in the Data section to preempt an obvious objection.

## Tier 4 — Forecasting/aggregation, adjacent literature

- **Tetlock, P.E. & Gardner, D. (2015).** *Superforecasting: The Art and Science of Prediction.* Crown. — Popular-press, but the right on-ramp before the academic GJP papers; explains base rates, Fermi-ization, and the "actively open-minded thinking" that motivated M2's design.
- **Mellers, B., Ungar, L., Baron, J., et al. (2014).** *Psychological Strategies for Winning a Geopolitical Forecasting Tournament.* Psychological Science, 25(5), 1106–1115. — The actual academic GJP result behind the superforecasting narrative.
- **Atanasov, P., Rescober, P., Stone, E., et al. (2017).** *Distilling the Wisdom of Crowds: Prediction Markets vs. Prediction Polls.* Management Science, 63(3), 691–706. — Markets vs. structured human aggregation; relevant to how you position M3/M2 relative to M0.

## Tier 5 — LLM forecasting (for the M3 limitations/discussion section)

- **Halawi, D., Zhang, F., Yueh-Han, C. & Steinhardt, J. (2024).** *Approaching Human-Level Forecasting with Language Models.* NeurIPS 2024. — The closest existing academic treatment of LLM-as-forecaster; cite for the "crowd aggregate, not standalone" framing that matches why M3 never sets the final probability.
- **ForecastBench** (Karger et al., ongoing benchmark reports) — for current LLM-vs-superforecaster gap numbers if you want a recent contextual figure in the Discussion.

---

## Suggested reading order given your timeline

Given the Concept Memo's schedule (drafting Sections 3–5 now, results in Jan 2027): read Tier 1 this month — it directly shapes how you frame the Methodology section, which you're writing now regardless of what the data eventually show. Tier 2 items you can read piecemeal as you draft the section that needs them (e.g., read Choe & Ramdas properly right before writing the inference paragraph, Satopää/Neyman-Roughgarden right before the aggregation paragraph). Tier 3–5 are Introduction/Related Work/Discussion material — fine to leave for the autumn drafting pass, closer to when those sections actually get written.
