"""Phase 15 (v2.3): cryptographic ledger commitments -- verifiable pre-registration.

Nightly, this computes a sha256 over each closed UTC day's appended `forecasts`
rows and appends one JSON record to a git-tracked file, then commits (and
pushes) that file to THIS repo -- the public code repo, not the private
results mirror `publish.py` targets. The point: a reviewer with DB read access
can later recompute the exact same hash from the exact rows a commitment's
`first_id`/`last_id` range covers and confirm the ledger was not edited after
outcomes became known.

Design notes (see the Phase 15 plan for the full reasoning):
- No hash chain between days. Git's own commit history already covers that --
  every commit transitively includes every prior line of the file, and GitHub's
  commit timestamps are the actual pre-registration timestamps a reviewer would
  check. Adding a prev_hash field would only duplicate a guarantee git already
  gives for free.
- Verification is anchored to the committed id range, never to a live re-query
  by date -- a forecast row that arrives late for an already-committed date
  must never be able to silently change what that commitment covers.
- The very first run does not backfill historical days that predate this
  feature (see commit_pending_days) -- those hashes would carry no
  pre-registration value. That gap is documented in the pre-analysis plan
  instead of papered over with retroactive commitments.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from lab.gitutil import push_with_rebase
from lab.util import PROJECT_ROOT, now_utc, now_utc_iso

log = logging.getLogger(__name__)

_FORECAST_COLUMNS = (
    "id", "ts", "condition_id", "model_id", "p_yes", "p_market_at_ts",
    "spread_at_ts", "inputs_hash", "evidence_run_id", "cost_usd",
)


def _hash_rows(rows: list[sqlite3.Row]) -> str:
    """Canonical sha256 over rows: sorted-key compact JSON per row, newline-joined.

    `_hash_rows([])` naturally yields `sha256(b"")` -- no special case needed
    for a day with zero forecasts.
    """
    lines = [json.dumps(dict(r), sort_keys=True, separators=(",", ":")) for r in rows]
    blob = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _ledger_id(conn: sqlite3.Connection) -> str:
    """Short, stable identifier for the database a commitment was computed on.

    Derived from `meta.created_at`, which is written once with INSERT OR
    IGNORE when a database is first created and never changes afterwards --
    so two hosts running their own DB produce two different ids, while the
    same DB always produces the same one across restarts, backups and
    restores. Opaque on purpose: it exists to answer "same ledger or not,"
    not to identify a machine.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'created_at'").fetchone()
    created = row[0] if row else ""
    return hashlib.sha256(created.encode("utf-8")).hexdigest()[:8]


def _query_day(conn: sqlite3.Connection, date_str: str) -> list[sqlite3.Row]:
    cols = ", ".join(_FORECAST_COLUMNS)
    return conn.execute(
        f"SELECT {cols} FROM forecasts WHERE substr(ts, 1, 10) = ? ORDER BY id",
        (date_str,),
    ).fetchall()


def _query_id_range(conn: sqlite3.Connection, first_id: int, last_id: int) -> list[sqlite3.Row]:
    cols = ", ".join(_FORECAST_COLUMNS)
    return conn.execute(
        f"SELECT {cols} FROM forecasts WHERE id BETWEEN ? AND ? ORDER BY id",
        (first_id, last_id),
    ).fetchall()


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _append_ledger(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")


def commit_pending_days(conn: sqlite3.Connection, ledger_path: Path) -> list[dict[str, Any]]:
    """Append one commitment record per not-yet-committed, fully-past UTC day.

    On an empty/missing ledger file, bootstraps from `today_utc - 1` only --
    it deliberately does not backfill the (potentially large) history of
    forecasts that predates this feature; see module docstring. From then on,
    any gap (e.g. the job missed a few nights) is caught up in date order,
    same as the rest of this codebase's overdue-service pattern.
    """
    existing = _read_ledger(ledger_path)
    committed_dates = {r["date"] for r in existing}
    today = now_utc().date()

    if existing:
        start = max(date.fromisoformat(r["date"]) for r in existing) + timedelta(days=1)
    else:
        start = today - timedelta(days=1)

    new_records: list[dict[str, Any]] = []
    d = start
    while d < today:
        date_str = d.isoformat()
        if date_str not in committed_dates:
            rows = _query_day(conn, date_str)
            committed_ts = now_utc_iso()
            new_records.append({
                "date": date_str,
                "row_count": len(rows),
                "first_id": rows[0]["id"] if rows else None,
                "last_id": rows[-1]["id"] if rows else None,
                "sha256": _hash_rows(rows),
                "committed_ts": committed_ts,
                "prospective": (today - d).days <= 1,
                # Which database produced this record (2026-07-28). During the
                # 2026-07-10 laptop-to-VPS cutover two hosts ran this job
                # against their own separate DBs and pushed to this same file;
                # the resulting duplicate-date records were indistinguishable
                # from tampering by inspection alone, and took a deliberate
                # investigation to explain. A ledger identifier makes a
                # dual-host write self-evident from the record itself.
                "ledger_id": _ledger_id(conn),
            })
        d += timedelta(days=1)

    if new_records:
        _append_ledger(ledger_path, new_records)
    return new_records


def verify_commitment(conn: sqlite3.Connection, record: dict[str, Any]) -> bool:
    """Recompute a commitment's hash from the DB and compare.

    Non-zero commitments are anchored to the record's own `first_id`/`last_id`
    range (not a live re-query by date), so this proves exactly what was
    committed at the time, regardless of what may have been appended for that
    date since. A zero-row commitment has no id range to anchor to -- it
    asserts "this date had no forecasts as of commit time," so verifying it
    means re-checking that the date is STILL empty; under normal operation no
    code path ever backdates a forecast's `ts` into an already-closed past
    date, so any row appearing there now is itself the tamper signal.
    """
    if record["row_count"] == 0:
        if record["first_id"] is not None or record["last_id"] is not None:
            return False
        return len(_query_day(conn, record["date"])) == 0 and record["sha256"] == _hash_rows([])
    if record["first_id"] is None or record["last_id"] is None:
        return False
    rows = _query_id_range(conn, record["first_id"], record["last_id"])
    if len(rows) != record["row_count"]:
        return False
    return _hash_rows(rows) == record["sha256"]


def verify_ledger(conn: sqlite3.Connection, ledger_path: Path) -> dict[str, Any]:
    """Verify every commitment in the file, grouped by date.

    Exists because the paper's verifiability claim is only as good as someone
    actually running it: the procedure was prose-only until 2026-07-28, and a
    real defect (below) sat unnoticed in the file for two weeks as a result.

    The per-date grouping is the point. A date can legitimately carry more
    than one record -- during the 2026-07-10 laptop-to-VPS cutover BOTH hosts
    ran the nightly job for a few days against their own separate databases
    and pushed to this same file, so 2026-07-10, -11 and -14 each got two
    records with different id ranges. Since verification anchors to a
    record's own id range, the record written against the other host's DB
    cannot verify here, and never will. That is an artifact of dual-host
    operation, not evidence of an edited ledger, and the distinction is
    exactly what a reviewer needs: `dates_unverified` (a date with NO valid
    commitment) is the fatal condition; `superseded` records are explainable
    and enumerated so the explanation can be checked rather than trusted.
    """
    records = _read_ledger(ledger_path)
    by_date: dict[str, list[tuple[dict[str, Any], bool]]] = {}
    for r in records:
        by_date.setdefault(r["date"], []).append((r, verify_commitment(conn, r)))

    dates_ok = sorted(d for d, rs in by_date.items() if any(ok for _, ok in rs))
    dates_unverified = sorted(d for d, rs in by_date.items() if not any(ok for _, ok in rs))
    superseded = [
        {"date": d, "first_id": r.get("first_id"), "last_id": r.get("last_id"),
         "row_count": r.get("row_count"), "committed_ts": r.get("committed_ts")}
        for d, rs in sorted(by_date.items())
        for r, ok in rs
        if not ok and any(o for _, o in rs)
    ]
    return {
        "records": len(records),
        "dates": len(by_date),
        "dates_verified": len(dates_ok),
        "dates_unverified": dates_unverified,
        "superseded": superseded,
        "ok": not dates_unverified,
    }


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _revert_ledger_append(path: Path, n: int) -> None:
    """Undo the last `n` JSONL lines appended to `path`.

    Used when the git add/commit step fails after the file was already
    appended to: without this, the next run's idempotency check
    (`commit_pending_days` reading dates already present in the file) would
    treat those dates as committed forever, even though no git commit for
    them actually exists -- silently orphaning them past retry.
    """
    if n <= 0:
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = lines[: len(lines) - n]
    text = "\n".join(remaining)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def commit_and_push(config: dict[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute pending commitments and push them to THIS repo (`PROJECT_ROOT`).

    Distinct from `publish.py`'s `publish_results`, which targets a separate
    private results checkout -- ledger commitments belong in the public repo
    itself, since that's what makes them independently verifiable. Reverts its
    own file append if the git add/commit step fails, so a retry recomputes
    and retries the same dates rather than silently treating them as done.
    """
    ledger_cfg = config.get("ledger", {})
    ledger_path = PROJECT_ROOT / ledger_cfg.get("commitments_path", "docs/ledger_commitments.jsonl")

    new_records = commit_pending_days(conn, ledger_path)
    if not new_records:
        return {"committed": False, "reason": "no_new_days"}

    dates = [r["date"] for r in new_records]
    try:
        rel_path = ledger_path.relative_to(PROJECT_ROOT)
        add = _run_git(["add", str(rel_path)], PROJECT_ROOT)
        if add.returncode != 0:
            _revert_ledger_append(ledger_path, len(new_records))
            return {"error": "git_add_failed", "stderr": add.stderr}

        commit = _run_git(["commit", "-m", f"Ledger commitment: {', '.join(dates)}"], PROJECT_ROOT)
        if commit.returncode != 0:
            _revert_ledger_append(ledger_path, len(new_records))
            return {"error": "git_commit_failed", "stderr": commit.stderr}
    except Exception as exc:
        _revert_ledger_append(ledger_path, len(new_records))
        log.exception("ledger commitment git step failed")
        return {"error": "git_step_exception", "detail": str(exc)}

    result: dict[str, Any] = {"committed": True, "dates": dates}
    if ledger_cfg.get("push", True):
        try:
            pushed = push_with_rebase(PROJECT_ROOT)
            result["pushed"] = pushed.returncode == 0
            if not result["pushed"]:
                result["push_stderr"] = pushed.stderr
        except Exception as exc:
            # The local commit already succeeded and is safely idempotent --
            # only the push failed, so no revert; the next run (or a manual
            # `git push`) picks up what's already committed locally.
            result["pushed"] = False
            result["push_error"] = str(exc)
    return result
