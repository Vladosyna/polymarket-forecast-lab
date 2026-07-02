"""Persisted last-run bookkeeping for scheduled analytics services.

The orchestrator's missed-run catch-up uses these helpers to decide whether a
service (forecast bundle, shadow, learn) is overdue relative to its configured
control time. Last-success timestamps live in the `meta` table so they survive
process restarts. Connections are short-lived to avoid contention with the
collector's long-lived connection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lab.store import db
from lab.util import now_utc, now_utc_iso

META_PREFIX = "last_run_"


def _meta_key(name: str) -> str:
    return f"{META_PREFIX}{name}"


def record_job_run(config: dict[str, Any], name: str) -> None:
    """Persist the current UTC time as the last successful run of `name`."""
    conn = db.connect(config["storage"]["db_path"])
    try:
        db.set_meta(conn, _meta_key(name), now_utc_iso())
    finally:
        conn.close()


def last_run_age_seconds(config: dict[str, Any], name: str) -> float | None:
    """Seconds since the last successful run of `name`, or None if never run."""
    conn = db.connect(config["storage"]["db_path"])
    try:
        value = db.get_meta(conn, _meta_key(name))
    finally:
        conn.close()
    if value is None:
        return None
    last = datetime.fromisoformat(value)
    return (now_utc() - last).total_seconds()


def is_overdue(age_seconds: float | None, max_age_hours: float) -> bool:
    """True when the service has never run or is older than its control time."""
    if age_seconds is None:
        return True
    return age_seconds > max_age_hours * 3600


def is_snapshot_stale(age_minutes: float | None, threshold_minutes: float) -> bool:
    """True when there are no snapshots yet or the newest one is too old."""
    if age_minutes is None:
        return True
    return age_minutes > threshold_minutes
