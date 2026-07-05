# The Polymarket Automated Trading Stack: Open-Source Repositories, Tooling, and Practical Assessment (July 2026)

## TL;DR
- **The building blocks exist and are mostly free**: Polymarket ships official CLOB, Gamma, and Data APIs plus open-source SDKs (now the unified `py-sdk` / `py-clob-client-v2` after the April 2026 CLOB V2 migration), and the community has produced dozens of GitHub repos for market-making, arbitrage, whale-tracking, data collection, and LLM agents. The single most production-ready piece is **NautilusTrader** (~24.3k stars, Rust-native, actively maintained — latest release v1.229.0 on 25 June 2026 — with a stable Polymarket adapter and a backtesting data loader).
- **Most community bots are reference code, not turnkey profit machines**: the most popular community market-maker (`warproxxx/poly-maker`, 1.4k stars) explicitly warns it "will lose money" in current conditions; Polymarket's own market-maker keeper hasn't shipped a release since February 2023; and many "arbitrage bot" repos are thin SEO/marketing shells. A capable engineer can wire up a running pipeline in a weekend, but making it *correct and profitable* is a much larger job.
- **The critical UK gotcha is access, not code**: Polymarket geoblocks UK IP addresses because its binary contracts fall under the FCA's retail binary-options ban and the UKGC gambling regime. There is no UK consumer prosecution risk, but there is no consumer protection, ambiguous tax treatment, and using a VPN breaches Polymarket's Terms of Service — a real operational and compliance risk for automated capital.

## Key Findings

**1. Official infrastructure is solid and well-documented, but just went through a breaking migration.** On April 28, 2026 Polymarket cut over to **CLOB V2**: new exchange contracts, a rewritten backend, and a new collateral token **pUSD** (an ERC-20 backed 1:1 by USDC). The legacy `py-clob-client` and `@polymarket/clob-client` packages *no longer work against production* and were archived. New builds must use `py-clob-client-v2` / `@polymarket/clob-client-v2` or the new unified `Polymarket/py-sdk`.

**2. The community ecosystem is unusually generous but uneven in quality.** There are genuinely useful, real repos — but GitHub is also polluted with near-identical SEO-spam "polymarket-trading-bot" repos (often TypeScript, often thin, sometimes ToS-violating and taken down by GitHub). Verify every repo against its actual commit history before trusting it.

**3. Data is a mixed picture.** Live and recent data is easy (Gamma + CLOB + Data API + WebSockets). Deep historical order-book state is NOT provided by Polymarket — only fills are on-chain — so serious backtesting requires self-collection or third-party datasets. A 1.1-billion-record trade dataset now exists on HuggingFace.

**4. Realistic zero-to-running estimate**: a competent developer can have a paper-trading bot reading live markets and simulating fills in a day or two, and a live-executing bot in under a week. Producing one that reliably makes money is a different and much harder problem — competition is fierce and the best market-making/arbitrage strategies are private.

---

## Details

### 1. Official Polymarket Infrastructure

**The three APIs.** Polymarket exposes a clean separation of concerns:
- **Gamma API** (`https://gamma-api.polymarket.com`) — market/event metadata, discovery, resolution conditions, token IDs. Fully public, no auth. REST only. Endpoints: `/markets`, `/events`, `/tags`, etc. Pagination via `limit` (max 500, reduced to 100 on `/markets/keyset` as of May 2026) and `offset`; there is NO free-text search parameter — filter by slug or tag.
- **CLOB API** (`https://clob.polymarket.com`) — the trading layer: order books, pricing, order placement/cancellation. Market-data endpoints are public; trading requires authentication. Base WebSocket: `wss://ws-subscriptions-clob.polymarket.com/ws/`.
- **Data API** (`https://data-api.polymarket.com`) — user positions, trade history, leaderboard rankings (the leaderboard moved here from a standalone `lb-api` host).

**Authentication is two-layer.** L1 = EIP-712 wallet signature (proves wallet ownership, used to create/derive API credentials). L2 = HMAC-SHA256 signing with apiKey/secret/passphrase (authenticates trading requests). Even with L2, each order must still be individually signed with the private key via EIP-712. Signature types: `0` = EOA (MetaMask etc., must hold POL for gas and set token allowances), `1` = POLY_PROXY (Magic/email wallets), `2` = Gnosis Safe proxy (most common for Polymarket.com users), and new `3` = POLY_1271 deposit wallets (the new onboarding path for new API users, using ERC-1271 signature validation to prevent "ghost fills").

**Order types**: GTC (Good-Till-Cancelled, resting limit), GTD (Good-Till-Date), FOK (Fill-Or-Kill), FAK/IOC (Fill-And-Kill / Immediate-Or-Cancel). A critical gotcha carried over into NautilusTrader's docs: market BUY orders interpret quantity as quote notional (pUSD), while limit orders and market SELLs use base-unit (token) quantities — a base-denominated market buy will execute far more size than intended.

**Rate limits** (Cloudflare throttling — over-limit requests are queued/delayed, then 429 if throttling is insufficient). General REST ~15,000/10s; CLOB general ~9,000/10s; Gamma ~4,000/10s (with `/events` 500/10s, `/markets` 300/10s); Data API ~1,000/10s. Trading endpoints use dual-tier burst + sustained limits; as of June 1, 2026 `POST /order` and `DELETE /order` were raised to 200/second sustained (120,000 per 10 minutes). Use WebSockets for real-time data rather than polling.

**On-chain execution / settlement.** Hybrid model: off-chain operator matches orders (typically sub-200ms), on-chain settlement on Polygon PoS (chain ID 137) via the CTF Exchange V2 contract. Outcomes are ERC-1155 conditional tokens (Gnosis Conditional Token Framework); every YES/NO pair is backed by exactly $1 of collateral. Trade statuses: MATCHED → MINED → CONFIRMED (or RETRYING/FAILED). Trading is effectively "gasless" for standard proxy users (the relayer pays gas); EOA users must hold POL. Polygon gas is trivial (typically <$0.01/tx). Polymarket charges 0% on most markets; fees are now collected on-chain in USDC at match time in V2. Markets resolve via UMA's Optimistic Oracle (with a ~2-hour dispute window; disputes require ~$750 USDC bond); short-duration markets use Chainlink Data Streams.

**Official SDKs / repos** (all under `github.com/Polymarket`, which has ~101 repos):
- `Polymarket/py-sdk` — new unified Python SDK (Gamma + Data + CLOB + WebSockets), installed as `polymarket-client`, currently beta but the officially recommended forward path. Note: near-zero external adoption/stars yet despite being the intended future standard.
- `Polymarket/py-clob-client-v2` and `Polymarket/clob-client-v2` (TypeScript) — the V2 CLOB clients.
- `Polymarket/rs-clob-client-v2` — official Rust client (notably, currently the only official client that correctly handles POLY_1271 deposit-wallet EIP-1271 signing; the Python/TS V2 SDKs reportedly had an open deposit-wallet auth bug as of mid-2026).
- `Polymarket/ctf-exchange-v2` — the V2 exchange smart contracts (Solidity, audited by Cantina and Quantstamp).
- `Polymarket/real-time-data-client` — official TypeScript RTDS client for the real-time data socket (comments, crypto prices, equity prices).
- `Polymarket/agents` — official AI-agent framework (see below).
- Legacy `Polymarket/py-clob-client` (1.2k stars, ~381 forks) — **archived May 2026, non-functional against production. Do not use for new work**, though most third-party tutorials and repos still reference it.

**Documentation quality**: above average for crypto. `docs.polymarket.com` covers all APIs, auth, WebSockets, rate limits, the CTF, and a detailed V2 migration guide. There's an official Builders Program (`builders.polymarket.com`) with builder codes for order attribution, weekly USDC rewards, and a `#devs` Discord channel for support.

### 2. Open-Source Trading Bots & Frameworks

**Backtesting / production engine — the standout:**
- **NautilusTrader** (`github.com/nautechsystems/nautilus_trader`) — ~24.3k stars, Rust-native with Python control plane, LGPL-3.0, ~19,500 commits, very actively maintained (latest release v1.229.0 on 25 June 2026). Has a **`stable`-rated Polymarket adapter** (`nautilus_trader.adapters.polymarket`) using the official py-clob-client-v2, supports live data + execution + backtesting with identical strategy code, includes a `PolymarketDataLoader` and a `PolymarketFeeModel` (models the 20% crypto / 25% other maker rebates). This is the single most credible foundation for a serious system. Caveat: still pre-2.0, breaking API changes possible; the data loader does not expose CLOB order-book history snapshots, and the Data API caps pagination on high-activity markets.
- **`evan-kolberg/prediction-market-backtesting`** — a NautilusTrader extension purpose-built for prediction-market backtesting (Polymarket + Kalshi), with a PMXT quote-tick data layer.

**Market-making:**
- **`Polymarket/poly-market-maker`** (official) — 303 stars, Python, "Bands" and "AMM" strategies, Docker support. But its last release (v0.0.3) was February 2023; effectively unmaintained. Good as a reference for the keeper pattern.
- **`warproxxx/poly-maker`** — 1.4k stars, Python+JS, the most popular community market-maker. WebSocket order-book monitoring, Google Sheets config, position merging. **The author explicitly warns it "is not profitable and will lose money" in current conditions — use as a reference implementation only.**
- **`ent0n29/polybot`** — 609 stars, Java 21 microservices HFT infrastructure (ClickHouse + Redpanda pipeline, Grafana/Prometheus, paper-trading default, includes a complete-set arbitrage strategy). Actively developed but single-maintainer. The most "infrastructure-grade" of the community projects.
- Others: `elielieli909/polymarket-marketmaking` (bands-based), `miladhist/polymarket-market-maker` and `gamma-trade-lab/polymarket-market-maker` (both inspired by poly-maker).

**Arbitrage:**
- **`ImMike/polymarket-arbitrage`** — Python, watches 10,000+ markets, does cross-platform (Polymarket vs Kalshi) + bundle arbitrage (YES+NO ≠ $1) + market-making, with a FastAPI dashboard, risk manager/kill switch, and simulation mode. One of the more complete arb repos. Its own README cautions that "real prediction markets are highly efficient; arbitrage opportunities are rare and fleeting."
- **`taetaehoho/poly-kalshi-arb`** — Rust, WebSocket-driven, with circuit breaker and position tracking.
- **`CrewSX/Polymarket-Sports-Arbitrage-Bot`** — Python, compares Polymarket vs sportsbook odds (via The Odds API) for *directional* edges (correctly notes there's no risk-free arb on Polymarket alone).
- Numerous BTC/crypto 15-minute-market arb repos (`CarlosIbCu/...`, `Sectionnaenumerate/...`, `cutupdev/...`) — these are heavily SEO-driven, quality varies; treat with skepticism.
- `realfishsam/prediction-market-arbitrage-bot` — built on the `pmxt` unified prediction-market API.
- Caution: some arb repos (e.g. one under `dev-protocol`) have been disabled by GitHub for ToS violations.

**Whale-watching / wallet tracking / copy-trading:**
- **`al1enjesus/polymarket-whales`** — 52 stars, Python CLI, terminal + Telegram/Discord alerts on trades above a threshold. Light, some embedded marketing.
- **`pselamy/polymarket-insider-tracker`** — 95 stars, Python, detects suspicious patterns (fresh wallets, unusual sizing, niche markets, funding-chain analysis via Polygon RPC), scores risk, dispatches alerts. Docker + Postgres + Redis, moderately built out.
- **`enviodev/poly-whale-tracker`** — TypeScript TUI of large buys, powered by Envio HyperSync (fast on-chain data). New/minimal but backed by a company.
- **`NYTEMODEONLY/polyterm`** — Polymarket terminal with wallet-level whale tracking, insider scoring, arbitrage scanning, wash-trade detection, UMA-dispute risk analysis, and an MCP/agent server; view-only (never touches keys).
- **`Drakkar-Software/OctoBot-Prediction-Market`** — 93 stars, GPL-3.0, built on the established OctoBot project, self-custody, visual UI, paper trading; copy-trading and arbitrage features still marked work-in-progress. Kalshi support planned.
- Numerous SEO/marketing "copy-trading bot" repos (e.g. `unitmargaretaustin/...` by Bitbash) — often demos/lead-gen for paid custom builds.

**LLM / AI-agent projects:**
- **`Polymarket/agents`** (official) — 2.8k stars, Python, MIT. Framework for autonomous LLM agents: Gamma/CLOB connectors, ChromaDB for news vectorization, a CLI to query markets/news, prompt LLMs, and execute trades. Popular but **thin (only ~7 commits, no releases, many unmerged PRs) — early-stage scaffolding, not a maintained product.**
- **`guberm/polymarket-bot`** — C#/.NET, Claude ensemble probability estimation, fractional-Kelly sizing, layered risk management (stop-loss, take-profit, re-estimation, cooldowns). Well-specified.
- **`skharchikov/polymarket-bot`** — Rust workspace: ML trading bot (XGBoost ensemble + LLM consensus + Bayesian anchoring, 29 engineered features) plus a copy-trading bot mirroring leaderboard traders; PostgreSQL, Telegram.
- **`arkyu2077/polyclaw`** — AI news-edge scanner: ingests 10+ news sources every 90s, matches to markets, estimates probability shifts, sizes with half-Kelly, auto-trades via CLOB.
- **`llSourcell/Poly-Trader`** — Siraj Raval's ChatGPT-edge-detection agent with Kelly sizing (educational).
- **`aulekator/Polymarket-BTC-15-Minute-Trading-Bot`** — 7-phase BTC 15-min bot built on NautilusTrader (multi-source signals, Grafana monitoring, risk engine).
- MCP servers: `berlinbra/polymarket-mcp` (Claude tool server for market info/prices/history) and the paper-trading `agent-next/polymarket-paper-trader` (~350 stars).

### 3. Data Availability

- **Live/recent**: trivial via Gamma (metadata), CLOB (`/price`, `/book`, `/prices-history`, `/midpoint`), Data API (trades, positions), and WebSockets (~100ms latency vs ~1s REST). The CLOB has a `/prices-history` timeseries endpoint (configurable interval/fidelity).
- **Historical order-book depth**: NOT provided by Polymarket — only fills are stored on-chain. Deep backtesting requires self-collection or third-party sources. This is the single biggest data gap.
- **Self-collection tools**: `warproxxx/poly_data` (fetches markets via Gamma keyset API + reads OrderFilled events from the CTF Exchange V2 contract directly via Polygon JSON-RPC — notably rebuilt after Goldsky's free subgraph tier was removed and Polymarket dropped its old subgraph indexer on April 28, 2026); `TenghanZhong/polymarket-data-scraper` (Polymarket + Deribit + Kalshi into PostgreSQL); `academy17/polymarket-histories` (timeseries fetch scripts); `leolopez007/polymarket-trade-tracker` (PnL/maker-taker analysis tool).
- **Public datasets**: `SII-WANGZJ/Polymarket_data` — **1.1 billion trade records (107GB, 268K+ markets) in analysis-ready Parquet on HuggingFace**, plus a reproducible collection toolkit (academic, MIT). `manja316/polymarket-historical-data` offers a free sample (metadata + 100K price snapshots) with a larger paid dataset.
- **On-chain analytics**: Dune Analytics has many community Polymarket dashboards (e.g. `dune.com/rchen8/polymarket`, `dune.com/datadashboards/polymarket-overview`, `dune.com/filarm/polymarket-activity`, and a multi-platform Prediction Market dashboard by @dunedata). Also Allium, Goldsky (subgraphs/mirror pipelines, now largely paid), Bitquery (Polymarket CTF Exchange API), and Envio HyperSync. PolygonScan for raw contract inspection.
- **Insider/whale via on-chain analysis**: `pselamy/polymarket-insider-tracker` and `enviodev/poly-whale-tracker` above; plus many hosted (non-open-source) trackers (PolyInsider, PolyTrack, Polywhaler, etc.) cataloged in the awesome lists.

### 4. Supporting / Glue Tooling

- **Backtesting**: NautilusTrader (best), its prediction-market extension, and the paper-trader MCP tool. Most bots also ship a simulation/dry-run mode.
- **Kelly / bankroll management**: implemented directly in many of the AI bots above (`guberm/polymarket-bot`, `skharchikov/polymarket-bot`, `polyclaw`, `Poly-Trader`). No single canonical Polymarket-specific Kelly library — it's a few lines of code (binary-settlement Kelly is straightforward given an estimated true probability). The GitHub `kelly-criterion` topic has generic calculators.
- **Risk management**: mostly bespoke inside each bot (position limits, stop-loss/take-profit, exposure caps, kill switches). NautilusTrader provides a proper risk engine.
- **News → signals**: `polyclaw` (10+ news sources), `Polymarket/agents` (ChromaDB news vectorization), and the sentiment layers in `aulekator/Polymarket-BTC-15-Minute-Trading-Bot`.
- **Unified SDKs / wrappers**: `HuakunShen/polymarket-kit` (typed TS SDK + proxy + OpenAPI), `polymarket-apis` on PyPI (unified Pydantic-validated CLOB/Gamma/Data/Web3/WebSocket/GraphQL), `ivanzzeth/polymarket-go-gamma-client` (Go).
- **Curated indexes**: `harish-garg/Awesome-Polymarket-Tools` and `aarora4/Awesome-Prediction-Market-Tools` — useful starting points but contain broken/placeholder links; verify entries.

### 5. Accessibility & Practical Setup

**What's technically required:**
1. A Polygon-compatible wallet (MetaMask/EOA, or a Polymarket proxy/Safe, or a new POLY_1271 deposit wallet).
2. Funding: pUSD (converted 1:1 from USDC) on Polygon for collateral, plus a small amount of POL for gas if using an EOA.
3. For EOA wallets, set token allowances once (there's a standard `set_allowances.py` script, e.g. in NautilusTrader, adapted from a @poly-rodr gist) approving pUSD and the CTF contract for the exchange contracts.
4. Derive API credentials (L1 → create/derive keys → L2 for trading).

**KYC**: Polymarket's core DEX is non-custodial and generally permissionless to *access* by wallet; there's no traditional broker KYC to place trades via API. (The new CFTC-regulated Polymarket US, live since November 2025, is a separate, invite-gated, KYC'd product.)

**The UK problem (this is the big one for this user):** Despite the user believing they have "full access," Polymarket **actively geoblocks UK IP addresses**. The reasons are structural, not incidental:
- The FCA's permanent retail ban on binary options — in force since 2 April 2019 per FCA Policy Statement PS19/11. FCA Executive Director Christopher Woolard stated: "Binary options are gambling products dressed up as financial instruments. By confirming our ban today we are ensuring that investors don't lose money from an inherently flawed product." Polymarket's $1/$0 binary settlement fits this description precisely.
- The UKGC gambling regime — in a 4 February 2026 blog post, UKGC Director of Strategy Brad Enright wrote that prediction markets "would appear... [to] fall within the definition of a 'Betting Intermediary' under UK legislation. Whilst the presentation of prediction markets may differ, their core aspects are akin to what in the UK would be described as a 'Betting Exchange.' The betting intermediary gambling licence exists to cover such business models" — a licence Polymarket doesn't hold.
- Criminal liability under FSMA 2000 attaches to the *operator*, not the retail user — so a UK resident is not personally prosecutable for using it, but has zero consumer protection (no FSCS, no ombudsman), ambiguous HMRC tax treatment (gambling winnings are normally tax-free, but HMRC could treat systematic trading as taxable), and no UK-regulated payment/exchange rail will legally facilitate access.
- Practical access requires a VPN + non-custodial wallet, which **breaches Polymarket's Terms of Service** (risking account restriction) and puts automated capital in a legal grey zone. Europe has hardened, not softened: the Dutch KSA (Kansspelautoriteit) ordered Polymarket operator Adventure One QSS to cease Dutch operations by 17 February 2026 or face fines of €420,000 per week (capped at €840,000), and collected a forfeited €420,000 penalty via a 19 May 2026 decree; KSA director of licensing Ella Seijsener said the platform "constitutes illegal gambling. Anyone without a Ksa license has no business in our market." Germany, Belgium, France, Italy, and (per some reporting) Portugal and Hungary also block or have taken exception to it.

**Cost considerations**: software is free; trading fees are 0% on most markets (maker rebates of 20–25% on many categories); gas is negligible on Polygon (<$0.01/tx, and gasless for proxy users). Realistic minimum capital to test meaningfully is small ($50–500 as several repos suggest for dry-runs then small live). Ongoing infra (RPC, VPS, paid data) runs from a few dollars to a couple hundred per month depending on strategy latency needs — public Polygon RPC is unreliable under load, so a paid Alchemy/QuickNode/Chainstack endpoint is effectively required for anything serious.

**Zero-to-running difficulty**: Low-to-moderate for a capable engineer to get a *paper-trading* bot reading live markets in 1–2 days using py-clob-client-v2 or NautilusTrader. Getting *live execution* correct (V2 signing, allowances, quote-vs-base quantity, tick sizes, minimum order sizes, ghost fills) is a few days more. Getting it *profitable* is the hard part and is not solved by any off-the-shelf repo.

### 6. Realistic Assessment (production-ready vs experimental)

**Production-grade / actively maintained:**
- NautilusTrader (~24.3k stars, stable Polymarket adapter) — genuinely production-capable.
- Official V2 SDKs (`py-sdk`, `py-clob-client-v2`, `rs-clob-client-v2`, `clob-client-v2`) — actively developed, though the unified `py-sdk` is beta with near-zero external adoption yet, and the Python/TS clients had a known deposit-wallet signing bug.
- The official APIs and docs themselves.

**Useful references but NOT turnkey / not currently profitable:**
- `warproxxx/poly-maker` (author says it loses money), `Polymarket/poly-market-maker` (unmaintained since February 2023), `Polymarket/agents` (thin scaffolding), most arbitrage bots.

**Experimental / single-maintainer / caveat emptor:**
- `ent0n29/polybot`, `skharchikov/polymarket-bot`, `guberm/polymarket-bot`, `polyclaw`, the whale trackers, and essentially all the "15-minute BTC" and copy-trading repos.

**Avoid / verify carefully:**
- The large cluster of SEO-spam repos with keyword-stuffed names and near-identical READMEs; some have been removed by GitHub for ToS violations. Marketing-driven "contact me on Telegram to buy the bot" repos.

**Known pitfalls & gotchas** repeatedly surfaced in docs/issues:
- The April 2026 V2 cutover wiped all open orders and broke all V1 clients — anything not updated is silently dead.
- No historical order-book data from Polymarket.
- Market BUY quantity semantics (quote vs base) — easy to over-fill.
- Tick-size and minimum-order-size validation; markets can be closed/resolved with valid metadata but no live book (`enableOrderBook`/`accepting_orders`).
- Fee rate must match what the API expects or EIP-712 signatures are rejected.
- Public RPC rate limits/timeouts under sustained load.
- Prediction markets are highly efficient — arbitrage is rare and fleeting; thin markets are vulnerable to manipulation.
- UMA resolution disputes can be contentious and slow.

**Community resources**: official Polymarket Discord (`#devs` channel) and Builders Program; NautilusTrader's Discord/community; numerous trader Discords (PolyZone, PolyToolz, PolyOdds). Notable developers/handles in the space include @warproxxx (poly-maker/poly_data), @poly-rodr (allowances gist), and the NautilusTrader team (Nautech Systems).

---

## Recommendations

**Stage 1 — Resolve the UK access/compliance question first (before writing any code).** This is the gating issue. Get explicit legal/tax advice on (a) using a VPN to bypass geoblocking given the ToS breach and legal grey zone, and (b) HMRC treatment of systematic automated trading profits. Consider whether a UKGC-regulated alternative (Betfair Exchange, Smarkets, Matchbook Predictions, Spreadex) or the CFTC-regulated Polymarket US (if eligible) better fits your risk appetite. **Threshold to proceed with Polymarket proper**: you're comfortable with no consumer protection, ToS-breach account risk, and uncertain tax treatment.

**Stage 2 — Stand up read-only infrastructure (no capital at risk).** Clone NautilusTrader and/or `polymarket-apis`, pull live data via Gamma + CLOB WebSockets, and start collecting your own order-book/trade data immediately (using `warproxxx/poly_data` as a pattern) since historical depth isn't available retroactively. Pull the `SII-WANGZJ/Polymarket_data` HuggingFace dataset for historical trade-level backtesting. Build a Dune dashboard or use `polyterm` for market/whale monitoring.

**Stage 3 — Backtest and paper-trade.** Use NautilusTrader's Polymarket adapter + `PolymarketDataLoader` + `PolymarketFeeModel` (or `evan-kolberg/prediction-market-backtesting`) to validate a strategy against your collected data. Run any bot in dry-run/simulation mode for at least a couple of weeks. **Threshold to go live**: a strategy that survives realistic fee/slippage/latency modeling in backtest AND paper trading, with a hard kill-switch and position limits coded in.

**Stage 4 — Live, small, and instrumented.** Use the official `py-clob-client-v2`/`py-sdk` (or the Rust client if using POLY_1271 deposit wallets, given the Python/TS signing bug). Fund with minimal capital ($50–200). Use a paid Polygon RPC and, if latency-sensitive, a VPS in a non-geoblocked region near Polymarket's infra. Reference the official `poly-market-maker` keeper pattern and `ent0n29/polybot`'s architecture, but write your own strategy and risk logic — do not deploy `poly-maker` as-is (its author says it loses money). **Threshold to scale**: consistent positive risk-adjusted return net of all costs over a statistically meaningful number of trades, with no unexplained losses.

**What would change this plan**: If the UKGC/FCA issue guidance carving out event contracts (expected late 2026), or if Polymarket US opens to UK users, the compliance calculus improves materially. If you only want *signals/research* rather than execution, you can skip most of the execution stack and rely on data APIs + Dune + tracking tools with far lower legal exposure.

## Caveats
- **Regulatory/compliance is the dominant risk for a UK user** and the task's premise ("no geographic access restrictions") is factually incorrect as of mid-2026 — Polymarket geoblocks the UK. I've flagged this prominently rather than assuming access.
- **Star counts and maintenance status** were verified as of July 1, 2026; these change quickly. GitHub did not expose exact last-commit dates in the fetched pages, so recency for some repos is inferred from release history and commit counts.
- **Many "arbitrage" and "trading bot" repos are marketing/SEO artifacts** with inflated or fabricated performance claims (e.g. "$500-700/day from $200") — treat all profitability claims as unverified.
- **Third-party guides and blog posts** (VPS vendors, "how to use Polymarket in the UK" affiliate sites, SEO tutorials) were used for corroboration of technical facts but carry commercial bias, especially around VPNs and hosting.
- **This is not legal, tax, or investment advice.** Prediction-market trading carries substantial risk of total loss, and automating it amplifies operational risk (runaway orders, stale prices, key compromise).