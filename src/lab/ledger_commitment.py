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
            })
        d += timedelta(days=1)

    if new_records:
        _append_ledger(ledger_path, new_records)
    return new_records


def verify_commitment(conn: sqlite3.Connection, record: dict[str, Any]) -> bool:
    """Recompute a commitment's hash from the DB and compare.

    Anchored to the record's own `first_id`/`last_id` range (not a live
    re-query by date), so this proves exactly what was committed at the time,
    regardless of what may have been appended for that date since.
    """
    if record["row_count"] == 0:
        return record["sha256"] == _hash_rows([])
    rows = _query_id_range(conn, record["first_id"], record["last_id"])
    if len(rows) != record["row_count"]:
        return False
    return _hash_rows(rows) == record["sha256"]


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def commit_and_push(config: dict[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute pending commitments and push them to THIS repo (`PROJECT_ROOT`).

    Distinct from `publish.py`'s `publish_results`, which targets a separate
    private results checkout -- ledger commitments belong in the public repo
    itself, since that's what makes them independently verifiable.
    """
    ledger_cfg = config.get("ledger", {})
    ledger_path = PROJECT_ROOT / ledger_cfg.get("commitments_path", "docs/ledger_commitments.jsonl")

    new_records = commit_pending_days(conn, ledger_path)
    if not new_records:
        return {"committed": False, "reason": "no_new_days"}

    rel_path = ledger_path.relative_to(PROJECT_ROOT)
    add = _run_git(["add", str(rel_path)], PROJECT_ROOT)
    if add.returncode != 0:
        return {"error": "git_add_failed", "stderr": add.stderr}

    dates = [r["date"] for r in new_records]
    commit = _run_git(["commit", "-m", f"Ledger commitment: {', '.join(dates)}"], PROJECT_ROOT)
    if commit.returncode != 0:
        return {"error": "git_commit_failed", "stderr": commit.stderr}

    result: dict[str, Any] = {"committed": True, "dates": dates}
    if ledger_cfg.get("push", True):
        pushed = _run_git(["push"], PROJECT_ROOT)
        result["pushed"] = pushed.returncode == 0
        if not result["pushed"]:
            result["push_stderr"] = pushed.stderr
    return result
