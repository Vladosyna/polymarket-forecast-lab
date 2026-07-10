# VPS Operator Runbook (Debian 13, 167.71.201.113)

**As of 2026-07-10, this VPS is the primary host** — it runs the full pipeline
(collector + forecast/eval/report/shadow/learn + private-results publish). The
Windows laptop is being phased out: it runs in parallel for a few days as a
stand-by/verification instance (with its own push to the private results repo
disabled to avoid a git race — see "Cutover" below), and its `lab run` will be
stopped manually once the operator is satisfied this host is stable. This
document is a companion to [`docs/OPERATIONS.md`](OPERATIONS.md) (the
Windows-laptop runbook) — **not a replacement for it**; follow the same "fix
this doc if a step doesn't work" discipline as that document.

---

## What runs where

**Host.** Debian 13 ("trixie"), IP `167.71.201.113`, repo checked out at
`/root/polymarket-forecast-lab`. Access is via SSH as `root`, using a dedicated
deploy key (not the operator's personal key, so it can be rotated/revoked
independently of any other machine's access):

```bash
ssh -i ~/.ssh/id_ed25519_deploy root@167.71.201.113
```

**Why root.** This is a small, single-purpose VPS with nothing else on it; the
original `lab-collect.service` already ran as root, and every unit added since
follows the same precedent for consistency. Revisit if this host ever runs
anything beyond this lab.

**Scope.** This host runs the **full pipeline** via `lab-run.service` (`uv run
lab run` — collector + scheduled forecast/eval/report/shadow/learn, exactly like
`docs/OPERATIONS.md`'s laptop setup) plus the **Streamlit dashboard**
(`lab-dashboard.service`, read/write UI over the same data). The original
`lab-collect.service` (collector-only) is disabled, not deleted — `lab-run.service`
subsumes everything it did.

**Two long-running systemd services, plus two systemd timers, run on this host:**

| Unit | Purpose | Command |
|---|---|---|
| `lab-run.service` | **Primary orchestrator** (collector + forecast/eval/report/shadow/learn) | `uv run lab run` |
| `lab-collect.service` | Collector-only — **disabled since 2026-07-10**, kept for rollback | `uv run lab collect` |
| `lab-dashboard.service` | Streamlit dashboard, loopback-only | `/root/.local/bin/uv run streamlit run src/lab/dashboard.py --server.port 8501 --server.address 127.0.0.1 --server.headless true` |
| `pmxt-scan.timer` → `pmxt-scan.service` | pmxt Router scan, twice daily 05:00/17:00 UTC | `uv run --with pmxt python scripts/pmxt_router_scan.py` |
| `pmxt-verify.timer` → `pmxt-verify.service` | LLM-verify pmxt candidates into `markets_map.yaml`, twice daily 06:00/18:00 UTC | `uv run lab map pmxt-verify` |
| `results-pull.timer` → `results-pull.service` | **Disabled since 2026-07-10** — role reversed, see "Cutover" below | `git pull --ff-only origin main` |

Standard commands apply to the long-running services:

```bash
systemctl status lab-run.service
systemctl status lab-dashboard.service
systemctl restart lab-run.service
systemctl restart lab-dashboard.service
journalctl -u lab-run.service -f            # follow live logs
journalctl -u lab-dashboard.service -f
```

For the timers (oneshot services, triggered on schedule — nothing to "keep running"):

```bash
systemctl list-timers pmxt-scan.timer pmxt-verify.timer --no-pager   # next/last fire time
systemctl start pmxt-scan.service      # trigger a scan right now, out of schedule
systemctl start pmxt-verify.service    # trigger a verify pass right now
journalctl -u pmxt-scan.service -n 40 --no-pager
journalctl -u pmxt-verify.service -n 40 --no-pager
```

---

## Cutover to primary (2026-07-10)

This host was collector-only until 2026-07-10, when it became the primary — the
laptop's forecast/eval/wealth history was noticeably ahead of this host's own
(restored-once-then-frozen) database, so a straight "just start `lab run` here"
would have silently orphaned days of accumulated calibration history. What
actually happened, in order:

1. A consistent snapshot of the **laptop's** live `data/lab.db` (92,323
   forecasts, 12,241 resolutions, 1,714 eval_runs at cutover time — vs. this
   host's own stale 61,452/12,229/1,144, frozen since its initial restore) was
   taken via SQLite's `backup()` API (safe against a concurrently writing WAL
   connection — same method `publish.py::sync_db` already uses) and transferred
   here, replacing this host's own `data/lab.db`. The pre-cutover VPS db is kept
   at `data/pre_migration_backups/lab.db.pre_vps_primary_cutover_<timestamp>`
   for rollback.
2. `lab-collect.service` was disabled; `lab-run.service` (above) created and
   started in its place.
3. **First-boot bug, fixed on the spot:** a 0-byte `data/snapshots/date=2026-07-10/
   snapshots.parquet` (a genuinely empty, corrupt file — confirmed 0 bytes, no
   content to lose) crashed the startup report step every time
   (`ComputeError: parquet: File out of specification`), crash-looping the
   service. Deleted (with explicit confirmation, since `data/snapshots/` is the
   brief's own "crown jewels" data) and the collector recreated a valid file on
   its next successful write for that date. If a 0-byte or truncated snapshot
   parquet ever appears again for *today's* date specifically, this is almost
   certainly the same pattern (a partition file touched into existence right as
   a process was interrupted) rather than a sign of a different bug.
4. `results-pull.timer` disabled — see below, role reversed.
5. On the laptop: `config.yaml`'s `publish.enabled` set to `false` as a
   **local-only, uncommitted** override (never propagate this to git — it would
   wrongly disable the VPS's own publish too, since it's the same tracked file).
   Prevents both hosts' nightly `run_publish_job` from racing to push the same
   private repo during the parallel-verification window. Re-enable (delete that
   local override, or `git checkout -- config.yaml`) only after the laptop's
   `lab run` is fully retired — by then it won't matter, since nothing will be
   running there to push.

**Retiring the laptop (operator does this manually, when ready — not on a
schedule this doc or any process controls):** stop the laptop's `lab run`/
`lab watchdog`. Optionally set up a laptop-side pull-mirror (a Scheduled Task
running `git pull --ff-only` against the laptop's own `../Polymarket-results`
checkout, mirroring exactly what `results-pull.timer` used to do here) so the
laptop keeps an archival local copy without being an active participant.

---

## pmxt scan + verify: why this host, and how it stays safe

As of 2026-07-10, **this VPS is the sole host running the pmxt scan+verify cycle** — the
laptop's own `PolymarketForecastLabPmxtScan` Scheduled Task is disabled (not deleted; see
`docs/OPERATIONS.md`). Two things made running it on both hosts unsafe, not just
redundant:

1. **`data/markets_map.yaml` has no merge strategy.** Every write (`save_markets_map`) is
   a full read-modify-write of the whole YAML file, and nothing auto-commits it as a
   matter of course. Two hosts independently rewriting it would eventually collide on
   `git pull`/push with an ordinary line-based conflict that silently drops one side's
   proposed pairs.
2. **The `$5/day` LLM cap is enforced per host.** `llm.daily_cost_cap_usd` (config.yaml)
   is checked against each machine's own local `lab.db` — running the verify step on two
   independent checkouts doubles effective spend to `$10/day` with no code-level
   awareness of the other host.

**How this host stays the single source of truth going forward:** `run_pmxt_verify_job`
(v2.9) now commits — and, per `cross_venue.markets_map_push` (default `true`), pushes —
`data/markets_map.yaml` to the public repo whenever it actually adds new proposals. No
revert-on-failure is needed (unlike the ledger-commitment/paper-export jobs): the
proposal is already durable on disk before the git step runs, so a failed commit just
leaves the same pending change for the next scheduled run (or a human) to retry — it can
never lose or duplicate a proposal. **The laptop (or any other host) sees new proposals
only after its own `git pull`** — nothing pulls automatically in the other direction.

Reviewing and confirming proposed pairs: use this host's own dashboard
(https://167-71-201-113.sslip.io, Cross-Venue Matching (M7) mode) or SSH in and run
`uv run lab map confirm <condition_id> --venue kalshi`. Either way, confirming here means
the laptop needs its own `git pull` to see the confirmation before its next `lab
forecast` run picks it up.

**First real run, 2026-07-10:** the initial scan fired all 12 query terms back-to-back
with no delay and about half came back with an empty response body
(`Expecting value: line 1 column 1`), interleaved with queries that succeeded normally —
consistent with a rate limit on pmxt's own API, not a schema problem. Fixed by pacing
queries 1.5s apart (`scripts/pmxt_router_scan.py`); confirmed on retest — all 12 queries
returned cleanly (0 clusters matched for any of them at the time, which is a legitimate
"nothing found yet" result, not an error). If a genuine schema problem appears instead,
the script prints a line starting `pmxt schema mismatch` with the raw object dump needed
to diagnose it.

## The dashboard: nginx + Let's Encrypt + HTTP Basic Auth

The dashboard has **no in-app authentication by design** (CLAUDE.md §12 explicitly
lists "user auth" as out of scope for `src/lab`), so it is fronted by nginx doing
TLS termination and HTTP Basic Auth, with Streamlit bound to `127.0.0.1:8501` only
(never reachable directly from the internet regardless of firewall state).

**URL:** https://167-71-201-113.sslip.io (Basic Auth username `admin`, password
shared once at setup time — store it in a password manager, it is never committed
to any repo).

**Domain.** `167-71-201-113.sslip.io` — a free wildcard DNS service that resolves
`A-B-C-D.sslip.io` to `A.B.C.D` automatically, no signup, no records to manage.
**If this VPS's IP address ever changes, this domain breaks** (it resolves to the
*old* IP forever) and the TLS cert becomes invalid for the new IP. There is no
"update a DNS record" fix — mint a brand-new `<new-ip-with-dashes>.sslip.io`
hostname, rerun the nginx + certbot steps below against it, and update this
document's changelog at the bottom.

**Packages** (native `apt`, never Docker — CLAUDE.md §12): `nginx`, `certbot`,
`python3-certbot-nginx`, `apache2-utils` (for `htpasswd`).

**nginx site config** at `/etc/nginx/sites-available/dashboard`, symlinked into
`sites-enabled`, with the stock default vhost removed so it can't answer on port
80 instead. Reverse-proxies to `127.0.0.1:8501` with the websocket-upgrade headers
Streamlit's live-update connection requires, and long (86400s) read/send timeouts
so nginx doesn't silently drop an idle session.

**Certificate** via `certbot --nginx`, which edits the site file in place to add
the 443/TLS block and an http→https redirect. Auto-renews via a systemd timer
certbot installs itself (`systemctl list-timers | grep certbot`).

---

## Results publishing: this host is now the pusher, not a reader

`/root/forecast-lab-results` is a checkout of the **private** `forecast-lab-results`
repo. Before 2026-07-10 this was a passive, hourly-pulled read-only mirror
(`results-pull.timer`, now **disabled** — see "Cutover" above). Now that this host
runs `lab-run.service`, its own nightly `run_publish_job` (part of the same
forecast/eval/report bundle, `config.yaml`'s `publish:` section) pushes `lab.db`,
snapshots, reports, exports, and model artifacts here directly — this host is the
one now doing what `docs/OPERATIONS.md`'s laptop-side backup section describes.

Access uses this checkout's own dedicated key (`~/.ssh/id_ed25519_results`, set up
during initial VPS provisioning, scoped via `~/.ssh/config`'s `Host github.com`
block), separate from the `id_ed25519_deploy` key the operator's own machine uses
to SSH *into* this VPS — confirmed to have real push (not just read) access via a
dry-run test before the cutover.

```bash
journalctl -u lab-run.service --no-pager | grep -i "publish job complete"
cd /root/forecast-lab-results && git log --oneline -5   # confirm recent pushes landed
```

**If the laptop's own `lab run` is still running in parallel** (the few-day
verification window), its `publish.enabled` is set `false` locally (see "Cutover")
specifically so it does *not* also try to push here — two hosts pushing the same
repo on their own nightly cron would race on non-fast-forward errors. Don't
re-enable it on the laptop until that instance is being fully retired.

**If you ever want a *read-only* mirror again** (e.g. on a third host, or back on
the laptop after it's retired), `results-pull.timer`'s unit files are still present
(just disabled) — `systemctl enable --now results-pull.timer` brings the old
hourly-pull behavior back on whichever host that mechanism belongs on next.

---

## Rotating the Basic Auth password

```bash
GEN_PASSWORD=$(openssl rand -base64 24)
htpasswd -b /etc/nginx/.htpasswd admin "$GEN_PASSWORD"   # no -c: update the entry, don't recreate the file
echo "New Basic Auth password: $GEN_PASSWORD"
systemctl reload nginx
```
No dashboard restart is needed — nginx re-reads `.htpasswd` per request.

---

## Cold-start / redeploy procedure

1. `ssh -i ~/.ssh/id_ed25519_deploy root@167.71.201.113`
2. `cd /root/polymarket-forecast-lab && git pull`
3. `uv sync --group dashboard`
4. `systemctl daemon-reload` (only if a unit file changed)
5. `systemctl restart lab-run.service lab-dashboard.service`
6. Confirm: `systemctl status lab-run.service lab-dashboard.service`, then
   `curl -I https://167-71-201-113.sslip.io` (expect `401` without credentials,
   `200` with `-u admin:<password>`).

---

## Pointer back to the Windows-side runbook

Windows Scheduled Tasks, the PAUSE file, the key-rotation table, and everything
specific to running this same pipeline on the laptop (now the secondary/
stand-by host) live in [`docs/OPERATIONS.md`](OPERATIONS.md). This document
covers what is specific to this VPS host.

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-10 | Initial VPS dashboard exposure: nginx + Let's Encrypt + HTTP Basic Auth on `167-71-201-113.sslip.io`, `lab-dashboard.service` added alongside the pre-existing `lab-collect.service`. |
| 2026-07-10 | pmxt scan+verify cycle moved here (sole owner): `pmxt-scan.timer`/`pmxt-verify.timer` added; laptop's `PolymarketForecastLabPmxtScan` task disabled. |
| 2026-07-10 | `results-pull.timer` added: hourly `git pull --ff-only` of `/root/forecast-lab-results`, so this host holds an independent, recent local copy of the actual experiment results if the laptop's orchestrator ever stops. |
| 2026-07-10 | **Cutover to primary**: laptop's `lab.db` (fuller forecast/eval history) copied here, replacing this host's stale one; `lab-collect.service` disabled and replaced by `lab-run.service` (full orchestrator); `results-pull.timer` disabled (role reversed — this host now pushes to `forecast-lab-results` via its own nightly `run_publish_job`); laptop's own push disabled locally to avoid a git race during the parallel-verification window. |
