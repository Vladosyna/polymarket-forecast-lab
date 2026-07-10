# VPS Operator Runbook (Debian 13, 167.71.201.113)

This is the runbook for the second observation host: a small Debian VPS running an
independent, parallel Polymarket collector. It is a companion to
[`docs/OPERATIONS.md`](OPERATIONS.md) (the Windows-laptop runbook, which remains the
primary host running the full pipeline) — **not a replacement for it.** Follow the
same "fix this doc if a step doesn't work" discipline as that document.

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
existing `lab-collect.service` already runs as root, and the new dashboard unit
below follows the same precedent for consistency. Revisit if this host ever runs
anything beyond this lab.

**Scope — read this before assuming the VPS does what the laptop does.** The VPS
currently runs **only the collector** (`lab-collect.service`, i.e. `uv run lab
collect` — snapshots + resolution watcher, no forecasting) plus, as of this setup,
the **Streamlit dashboard** (`lab-dashboard.service`, read/write UI over the same
data). It does **not** run `lab forecast` / `lab eval` / `lab report` / `lab shadow`
/ `lab learn` — the full analytics pipeline stays on the Windows laptop for now (a
deliberate scope decision, not a gap). The VPS exists as a second, independent
data-collection vantage point for cross-checking continuity against the laptop's
own collection — nothing here forecasts, scores, or learns.

**Two long-running systemd services, plus two systemd timers, run on this host:**

| Unit | Purpose | Command |
|---|---|---|
| `lab-collect.service` | Long-running collector (pre-existing) | `uv run lab collect` |
| `lab-dashboard.service` | Streamlit dashboard, loopback-only | `/root/.local/bin/uv run streamlit run src/lab/dashboard.py --server.port 8501 --server.address 127.0.0.1 --server.headless true` |
| `pmxt-scan.timer` → `pmxt-scan.service` | pmxt Router scan, twice daily 05:00/17:00 UTC | `uv run --with pmxt python scripts/pmxt_router_scan.py` |
| `pmxt-verify.timer` → `pmxt-verify.service` | LLM-verify pmxt candidates into `markets_map.yaml`, twice daily 06:00/18:00 UTC | `uv run lab map pmxt-verify` |
| `results-pull.timer` → `results-pull.service` | Hourly pull of the private results mirror (see below) | `git pull --ff-only origin main` |

Standard commands apply to the two long-running services:

```bash
systemctl status lab-collect.service
systemctl status lab-dashboard.service
systemctl restart lab-dashboard.service
systemctl stop lab-dashboard.service
journalctl -u lab-dashboard.service -f      # follow live logs
journalctl -u lab-collect.service -n 200    # last 200 lines
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

## Results mirror: hourly pull, a second local copy of the experiment results

`/root/forecast-lab-results` is a checkout of the **private** `forecast-lab-results`
repo — the same one the laptop's nightly `run_publish_job` pushes `lab.db`, snapshots,
reports, exports, and model artifacts to (see `docs/OPERATIONS.md`'s backup section).
It was restored here once during initial VPS setup; `results-pull.timer` now keeps it
current automatically (hourly, `git pull --ff-only`), so **this host holds its own
independent, recent local copy of the actual experiment results** — not just code — that
survives the laptop's own orchestrator stopping. Read-only: this checkout is never
written to or pushed from the VPS.

Access uses its own dedicated key (`~/.ssh/id_ed25519_results`, set up during initial VPS
provisioning, scoped via `~/.ssh/config`'s `Host github.com` block), separate from the
`id_ed25519_deploy` key the operator's own machine uses to SSH *into* this VPS — already
confirmed working (git-lfs installed and configured, `data/lab.db` pulls as real content,
not an LFS pointer stub).

```bash
systemctl list-timers results-pull.timer --no-pager   # next/last fire time
systemctl start results-pull.service                  # pull right now, out of schedule
journalctl -u results-pull.service -n 40 --no-pager
```

A failed pull (network blip, etc.) just logs to the journal and retries next hour —
`--ff-only` means it can never silently create a merge commit or lose history; if the
mirror's local history and origin's ever genuinely diverge (shouldn't happen, since
nothing ever commits into this checkout from the VPS side), the timer will fail loudly
every hour until a human resolves it by hand.

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
5. `systemctl restart lab-collect.service lab-dashboard.service`
6. Confirm: `systemctl status lab-collect.service lab-dashboard.service`, then
   `curl -I https://167-71-201-113.sslip.io` (expect `401` without credentials,
   `200` with `-u admin:<password>`).

---

## Pointer back to the primary runbook

The full forecast/eval/report/shadow/learn pipeline, Windows Scheduled Tasks, the
PAUSE file, the private-results backup/restore drill, and the key-rotation table
all live in [`docs/OPERATIONS.md`](OPERATIONS.md) — this document only covers what
is specific to this second, collector+dashboard-only VPS host.

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-10 | Initial VPS dashboard exposure: nginx + Let's Encrypt + HTTP Basic Auth on `167-71-201-113.sslip.io`, `lab-dashboard.service` added alongside the pre-existing `lab-collect.service`. |
| 2026-07-10 | pmxt scan+verify cycle moved here (sole owner): `pmxt-scan.timer`/`pmxt-verify.timer` added; laptop's `PolymarketForecastLabPmxtScan` task disabled. |
| 2026-07-10 | `results-pull.timer` added: hourly `git pull --ff-only` of `/root/forecast-lab-results`, so this host holds an independent, recent local copy of the actual experiment results if the laptop's orchestrator ever stops. |
