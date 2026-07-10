# Operator Runbook

This is the operator runbook required by CLAUDE.md Phase 18 ("Operations hardening
(final)"). It is written for a tired 3am operator: follow the numbered steps literally.
If a step below does not work exactly as written when you actually run it, **fix this
doc** — don't route around it and don't rely on tribal knowledge instead.

**As of 2026-07-10, this laptop is the secondary host — the VPS
(`docs/VPS_OPERATIONS.md`) is primary.** The laptop's `lab run` is running in
parallel for a few days as a stand-by/verification instance only, with
`config.yaml`'s `publish.enabled` overridden to `false` **locally, uncommitted**
(never push that change — it would wrongly disable the VPS's own publish too,
since it's the same tracked file) so both hosts don't race pushing to the
private results repo on the same nightly cron. Everything below still describes
this host accurately for as long as it keeps running — just read "primary" as
"was primary, now stand-by" until this laptop's `lab run` is retired for good.

---

## What runs where

**Host.** This lab runs on a Windows 11 Pro laptop, hostname `VLADOSYNAPC`, under
`D:\Polymarket`. This is a **deliberate deviation** from CLAUDE.md §11, which assumes
"an always-on Linux box" — there is no Linux box for this deployment. Everything below
(Scheduled Tasks instead of systemd/cron, PowerShell instead of bash) exists because of
that choice, not by oversight.

**Known past incident — sleep froze the collector.** Earlier in this project's life,
Windows Modern Standby (sleep) silently froze the entire asyncio event loop for about 8
hours overnight. This was confirmed by correlating `Get-WinEvent` System log event IDs
506/507 (sleep/wake) exactly against the timestamp of the last collector log line before
the gap. **Fix applied:** AC-power sleep was set to Never (Balanced power scheme,
"Sleep after" = 0) via `powercfg`. Because this is a laptop and Windows updates can
silently reset power plans, **periodically spot-check this hasn't regressed**:

```powershell
powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE
```

Confirm the AC line reads:

```
Current AC Power Setting Index: 0x00000000
```

If it reads anything else, the laptop can silently sleep again and freeze the collector
exactly like the original incident. Reset it:

```powershell
powercfg /change standby-timeout-ac 0
```

**Services (Windows Scheduled Tasks, not systemd units).** Confirmed live via
`schtasks /Query`, both currently status "Ready":

| Task name | Trigger |
|---|---|
| `PolymarketForecastLabWatchdog` | At user logon |
| `PolymarketForecastLabWatchdogHourly` | Every 1 hour |

Both tasks run `scripts\watchdog-task.bat`, which invokes `scripts\watchdog.ps1`. Every
cycle, the watchdog:

1. Runs `lab guard` (cleans up stale locks / dead processes).
2. Checks whether the orchestrator (`.venv\Scripts\python.exe -m lab run`) is alive, by
   process list and by `data\orchestrator.pid`. Starts it detached if not.
3. Does the same check-and-start for the Streamlit dashboard (port 8501).
4. Logs every action to `data\logs\watchdog.log`.
5. Treats `data\orchestrator.heartbeat` — a local file the orchestrator's main loop
   touches every 60 seconds — as "possibly hung" (logged only, **not** auto-killed) if
   it is older than 45 minutes.

**Do not confuse the two heartbeats.** `data\orchestrator.heartbeat` above is a local
liveness file the watchdog reads on this machine only. The Phase 18 dead-man heartbeat
(`HEARTBEAT_URL`, see below) is a separate, outbound HTTPS ping to an external
monitoring service, so someone gets alerted even if this laptop itself is fully down and
no watchdog is running at all. They serve different failure modes — don't treat one as
a substitute for the other.

Both scheduled tasks, the venv, and all scripts live under `D:\Polymarket`.

**Third scheduled task — pmxt Router scan (M7, out-of-band, separate from everything
above) — DISABLED on this machine since 2026-07-10, ownership moved to the VPS.**
`PolymarketForecastLabPmxtScan` (`Disable-ScheduledTask`, not deleted — re-enable with
`Enable-ScheduledTask -TaskName PolymarketForecastLabPmxtScan` if the VPS is ever
decommissioned) used to run `uv run --with pmxt python scripts\pmxt_router_scan.py`
twice daily (05:00/17:00 local). This is deliberately its own task, not part of the
orchestrator or the watchdog: pmxt is a third-party unified prediction-market
**trading** SDK (Claude.md tech-stack row / §12) — its hosted API key (`PMXT_API_KEY`
in `.env`) can also authorize live trading, so per Claude.md's own rule it must never
be imported into `src/lab` or run by any automated process this repo owns. `uv run
--with pmxt` installs it into an ephemeral environment for just that one invocation —
`pyproject.toml` is never touched, so pmxt never becomes this project's own dependency.

**Why disabled, not left running alongside the VPS's copy (v2.9):** the pmxt scan +
LLM-verify cycle writes to `data\markets_map.yaml`, a single git-tracked file with no
merge strategy — two hosts independently rewriting it would silently drop whichever
side lost the next merge, and the $5/day LLM cap (`llm.daily_cost_cap_usd`) is enforced
per-host against each machine's own `lab.db`, so running it twice would silently double
effective spend to $10/day. See `docs/VPS_OPERATIONS.md`'s pmxt section for the VPS
side, which is now the sole owner of this cycle — `run_pmxt_verify_job` there commits
and pushes `markets_map.yaml` whenever it finds new proposals, so `git pull` on this
laptop is what surfaces them here.

The scan writes `data\pmxt_candidates.json` (raw pairs pmxt's Router thinks might be the
same event across Polymarket and Kalshi). A **second, independent** step —
`verify_pmxt_candidates` (also wired into `lab run`'s own scheduler at
`cross_venue.pmxt_verify_cron`, twice daily, default 06:00/18:00 UTC, though it's a
harmless no-op here now with the scan disabled and no fresh candidates file ever
appearing) — reads that file, runs our own LLM check on each pair (pmxt's own
confidence score is treated as context, not ground truth), and only THEN appends
anything it agrees with into `data\markets_map.yaml`'s `proposed` list. A human still
runs `lab map confirm` (CLI or the dashboard's Cross-Venue Matching mode) before any
pair is ever live — nothing pmxt surfaces is ever auto-confirmed.

Install: `powershell -ExecutionPolicy Bypass -File scripts\install-pmxt-scan-task.ps1`.
Remove: `scripts\uninstall-pmxt-scan-task.ps1`. **First-run caveat (resolved, kept for
history):** the very first live run fired every query term back-to-back with no delay
and about half came back with an empty response body (`Expecting value: line 1 column
1`), interleaved with queries that succeeded normally — consistent with a rate limit on
pmxt's own API. Fixed by pacing queries 1.5s apart (`scripts\pmxt_router_scan.py`); if a
genuine schema problem ever appears instead, it prints a line starting `pmxt schema
mismatch` with the raw object dump needed to fix it.

---

## Cold-start restart procedure

From a clean machine or checkout, with the Python venv already set up (`uv sync` has
been run):

1. Open an elevated or normal PowerShell in the repo root (`D:\Polymarket` or your
   clone path).
2. Run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install-watchdog.ps1
   ```
   This registers both scheduled tasks above **and** immediately runs the watchdog once,
   which starts the orchestrator (and dashboard) if they are not already running.
3. Confirm it worked:
   ```powershell
   schtasks /Query /TN PolymarketForecastLabWatchdog
   schtasks /Query /TN PolymarketForecastLabWatchdogHourly
   uv run lab status
   ```

**Manual start, without the scheduler:**

- Foreground orchestrator (blocks the terminal, good for debugging):
  ```powershell
  uv run lab run
  ```
- Supervised orchestrator (auto-restarts on crash after a delay — default 10 minutes,
  `watchdog.restart_delay_seconds` in `config.yaml`):
  ```powershell
  uv run lab watchdog
  ```

**To remove the scheduled tasks:**
```powershell
scripts\uninstall-watchdog.ps1
```

If any of the three numbered steps above does not behave exactly as described the next
time someone runs them fresh, that is a bug in this document — fix the doc, do not
improvise a workaround and leave the doc wrong for the next person.

---

## The PAUSE file

Path: `data\PAUSE` (configured at `collect.pause_file` in `config.yaml`).

- **To pause safely for maintenance:** create an empty file at that path.
- **To resume:** delete the file.
- Every collector job, across every venue (Polymarket, Kalshi, Metaculus, Manifold),
  checks for this file first and skips its cycle if present (guardrail 8). It therefore
  takes at most one collection cycle — a few minutes — to fully halt all polling.
- **The Phase 18 outbound heartbeat still pings while PAUSE is set.** This is
  deliberate: the heartbeat only proves the process is alive, not that it is actively
  collecting. If it stopped pinging during PAUSE, every deliberate maintenance window
  would fire a false "collector is dead" alert.

---

## Backup location and restore procedure

**Location.** The private mirror lives at a sibling checkout: `..\Polymarket-results`
(i.e. next to, not inside, this repo). This is **not** the public GitHub repo — raw
market data and the sqlite db must stay private, so they never go to the MIT-licensed
public repo.

**How it stays in sync (automatic, nightly):**

- Every nightly run of the analytics bundle also runs the publish job, which **always**
  mirrors curated output (`reports/`, `exports/`, model artifacts) to the private repo.
- It **additionally** pushes `data\snapshots\` every night, when
  `publish.raw_data.snapshots_enabled` is `true` in `config.yaml` (currently `true`).
- It **additionally** pushes `data\lab.db` every `publish.raw_data.db_interval_days`
  days (currently `3`), when `publish.raw_data.db_enabled` is `true` (currently
  `true`) — gated by a `last_raw_db_push_ts` timestamp stored in the db's own `meta`
  table. This spacing exists because `lab.db` is a single, ever-growing binary with no
  Git LFS delta compression: pushing it as often as the small/incremental snapshot
  partitions would burn a GitHub LFS free-tier month's 1GB bandwidth quota in days.

**Currently paused on this host (2026-07-10 onward, local-only override — see the
banner at the top of this doc):** `publish.enabled` is `false` in this laptop's own
`config.yaml`, uncommitted, so none of the above runs from here while the VPS is
the primary pusher to the same private repo. Nothing to fix — this is intentional
for the parallel-verification window, not a broken backup.

**Manual, on-demand push** (does not wait for the nightly schedule):
```powershell
uv run python scripts\publish_results.py [--raw-data | --snapshots-only | --db-only] [--no-push]
```

**Known quirk — a stale legacy path exists in the results repo.** The private results
repo currently also contains a **legacy root-level `lab.db` / `lab.db-shm` pair**, left
over from before the Phase 15 raw-data-path reorg. That pair is **not** the current
backup. The live, current path the sync code actually writes to is:

```
..\Polymarket-results\data\lab.db
```

Always restore from that path, never from the root-level pair. This doc does not delete
the stale pair automatically — that is a manual cleanup call for the operator to make
next time they are in that repo (verify it's truly unused, then delete it by hand).

### Restore procedure

1. Clone the public code repo into a clean directory:
   ```powershell
   git clone https://github.com/Vladosyna/polymarket-forecast-lab.git
   ```
   This confirms the published repo alone (MIT-licensed, per CLAUDE.md §13) is
   sufficient to rebuild the lab's code.
2. Inside the clone, install dependencies:
   ```powershell
   uv sync
   ```
3. Copy your separately-held `.env` file into the clone's root. `.env` is gitignored
   and excluded from **both** git remotes (public code repo and private results repo)
   by design — it is never part of any automatic backup. Keep your own secure copy
   (password manager or equivalent) from day one; this runbook cannot restore what was
   never backed up in the first place.
4. Copy `data\lab.db` and `data\snapshots\` from `..\Polymarket-results\data\` (the
   current path — see the quirk note above, not the legacy root-level pair) into the
   clone's `data\` directory. Copying `models\` from the results repo into the clone's
   `data\models\` is optional but recommended for full forecasting capability
   immediately after restore.
5. Run:
   ```powershell
   uv run lab status
   ```
   Confirm it reports the restored row counts/freshness sensibly. Freshness will show
   as stale until the collector starts running again in the new location — that is
   expected, not a failure.
6. Run:
   ```powershell
   uv run pytest -q
   ```
   Confirm the full suite passes. This proves the published repo alone (no
   machine-specific state beyond `.env` plus the `data\` directory) is sufficient to
   stand the lab back up.
7. Resume unattended operation:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install-watchdog.ps1
   ```

See the drill log at the very end of this document for the record of when this
procedure was last actually executed, by whom, and with what result.

---

## Key inventory and rotation

All keys live in `.env`, which is gitignored in **this** repo and never touches the
public remote. As of `publish.raw_data.env_enabled: true` (default on), `.env` is
additionally backed up as `.env.backup` into the private results repo
(`forecast-lab-results`, confirmed a genuinely private GitHub repo) every night via
`run_publish_job` — so losing this laptop doesn't mean re-requesting every key from
scratch. This is a deliberate tradeoff, not a free lunch: every key value ever set now
lives permanently in that private repo's git history, including after rotation
(rotating a key at the provider only replaces the value in the live `.env`; old commits
still hold the old value). Acceptable for a solo-operator private repo; reconsider before
ever adding a second collaborator to `forecast-lab-results`, or before making it public.
Set `env_enabled: false` in `config.yaml` to opt back out.

| Key | Used for | Rotate at | Notes |
|---|---|---|---|
| `DEEPSEEK_API_KEY` (or `ANTHROPIC_API_KEY` if `config.yaml`'s `llm.provider` is set to `"anthropic"`) | M3 evidence pipeline, M7 cross-venue propose LLM calls | `platform.deepseek.com` / `console.anthropic.com` dashboard | See restart nuance below |
| `FRED_API_KEY` | M5 macro nowcast inputs (GDPNow/PCENow series) | Free key at `fredaccount.stlouisfed.org` | |
| `METACULUS_API_KEY` | M7 cross-venue signal input, M1.x recalibration | Requires a real Metaculus account (anonymous access removed as of 2026-07-03) | |
| `NEWSAPI_KEY` | Optional M3 retrieval augmentation | `newsapi.org` | Google News RSS works without it — this key is optional |
| `HEARTBEAT_URL` | Phase 18 dead-man heartbeat | Any healthchecks.io-class free monitoring service, no card required | Absent = feature silently off: no error, no forecast-quality impact, just no external monitoring |
| `PMXT_API_KEY` | M7 out-of-band pmxt Router scan (`scripts\pmxt_router_scan.py`, its own separate scheduled task — see above) | `pmxt.dev` dashboard | **Trading-capable key** — the same hosted key also authorizes live order placement/escrow custody on pmxt's other endpoints, even though this repo only ever calls its read-only Router search. Never used by `src/lab` or any process this repo schedules directly — only by the standalone scan script, on its own task. Rotate immediately if this key is ever suspected leaked, same urgency as a trading credential, not a read-only data key. |

**`HEARTBEAT_URL` setup notes:** prefer a "Cron" check type over a "Simple" check type
if the monitoring service offers one. Both the collector loop (every
`ops.heartbeat_interval_minutes`, default 5 minutes) and the once-nightly backup job
ping the same URL at very different cadences, which can confuse a strict fixed-period
"Simple" check's expected-interval estimate.

**Restart nuance — applies to every key above.** `.env` is only loaded once, at process
start (`load_dotenv()`). An already-running orchestrator process will **not** pick up a
rotated key until it is restarted. The watchdog's own next scheduled restart is **not**
triggered automatically by a key rotation — after rotating a key, manually restart the
orchestrator (e.g. via Task Scheduler "Run", or `uv run lab run`) right away.

---

## `lab status` red-flag glossary

| Flag | What it means | What to do |
|---|---|---|
| `last_snapshot_age` far above the tier's configured cadence (liquid ~5 min, tail ~60 min) | The collector process is stalled, crashed, or the machine itself was asleep/off (see the past sleep incident above) | Check `data\logs\watchdog.log` first — is the watchdog even seeing the process as alive? Then check Windows Event Viewer's System log for sleep/wake events: `Get-WinEvent -FilterHashtable @{LogName='System';Id=42,506,507}`, and correlate against the last line in `data\logs\lab.jsonl` before the gap. |
| `gaps_24h` / `gaps_7d` elevated | Intermittent connectivity or upstream rate-limiting, not necessarily a dead process | Watch for repeated 429/5xx entries in `data\logs\lab.jsonl` around the gap window. Check `last_snapshot_age` too, to tell "intermittent" apart from "dead." |
| "resolution watcher: N closed markets unresolved" growing over time | Either a genuine UMA dispute-window backlog (expected, transient) or the resolution watcher itself falling behind its poll cadence | Check whether N is roughly stable (healthy — disputes resolve on their own schedule) or monotonically climbing (watcher problem — investigate). |
| Per-venue `closed_unresolved` lines (Kalshi/Manifold) | Same read as above, scoped to that venue | Same action as above, per venue. |
| `metaculus last_snapshot_age=never` | Expected and benign by design, until at least one Metaculus pair exists in `data\markets_map.yaml`'s confirmed list | No action — there is no broad Metaculus universe sync, only confirmed-pair snapshots. |
| "LLM spend today: $X / cap $Y" near the cap | By design (guardrail 10): M3 and the weekly M7 propose job will start skipping remaining markets for the rest of the UTC day once the cap is hit | Not an error — just fewer forecasts/proposals until the cap resets at UTC midnight. No action needed. |

---

## Backup-restore drill log

One-time now, then quarterly per this runbook: restore `data\` from the private backup
repo onto a clean checkout, run `lab status` and the full test suite against the
restored state, and record the result below.

| Date | Performed by | Result | Notes |
|---|---|---|---|
| 2026-07-08 | Claude Code | **Pass** | Fresh `git clone` of the public repo into a clean directory, `uv sync`, `.env` copied in by hand (never backed up automatically, by design), `data/lab.db` + `data/snapshots/` + `models/` restored from `../Polymarket-results/data/` (the current path — see the stale root-level `lab.db` note above). `uv run lab status` reported correct row counts (61452 forecast rows, 11980 resolutions) with expected stale snapshot ages (collector wasn't running in the clone — not a failure). `uv run pytest -q` → 356 passed (the clone predates this same day's Phase 18 push, so it doesn't yet include `heartbeat.py`'s own tests; a same-day re-run after the Phase 18 push would show 361). Confirms the published repo + a restored `data/` directory alone are sufficient to stand the lab back up. |
