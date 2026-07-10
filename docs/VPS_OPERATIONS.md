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

**Two systemd units run on this host:**

| Unit | Purpose | Command |
|---|---|---|
| `lab-collect.service` | Long-running collector (pre-existing) | `uv run lab collect` |
| `lab-dashboard.service` | Streamlit dashboard, loopback-only (new) | `/root/.local/bin/uv run streamlit run src/lab/dashboard.py --server.port 8501 --server.address 127.0.0.1 --server.headless true` |

Standard commands apply to both:

```bash
systemctl status lab-collect.service
systemctl status lab-dashboard.service
systemctl restart lab-dashboard.service
systemctl stop lab-dashboard.service
journalctl -u lab-dashboard.service -f      # follow live logs
journalctl -u lab-collect.service -n 200    # last 200 lines
```

---

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
