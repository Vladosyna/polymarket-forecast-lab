"""Model-version registry (Phase 7.1) -- the versioning/rollback layer.

`model_versions` (SQLite) owns which artifact is active for each model, the
promotion/retirement audit trail, and rollback state. It COEXISTS with the
Phase 2 `data/models/*.json` artifact files: a row points at its artifact via
`artifact_path` + `params_hash` (sha256 of the file) rather than duplicating
the contents. `data/models/ACTIVE.json` is a generated pointer -- this module
is its only writer, rewriting it on every `is_active` change so inference-time
code (`load_active_artifact`) needs no changes.

Invariant: at most one active row per `model_id`. Enforced here (clear before
set, in one transaction) and backstopped by a partial unique index in db.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from lab.util import PROJECT_ROOT, now_utc_iso

log = logging.getLogger(__name__)

ACTIVE_JSON = "ACTIVE.json"


# --- paths & hashing ------------------------------------------------------

def models_dir(config: dict[str, Any]) -> Path:
    d = Path(config["storage"]["models_dir"])
    d = d if d.is_absolute() else PROJECT_ROOT / d
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _rel_path(path: str | Path) -> str:
    """Store paths relative to the project root when possible (portable)."""
    p = Path(path)
    try:
        return p.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    h.update(_resolve(path).read_bytes())
    return h.hexdigest()


def _version_tag_from_path(artifact_path: str | Path) -> str:
    """'.../m1_curves_v3.json' -> 'v3'; falls back to the stem."""
    stem = Path(artifact_path).stem
    if "_v" in stem:
        tail = stem.rsplit("_v", 1)[1]
        if tail.isdigit():
            return f"v{tail}"
    return stem


# --- reads ----------------------------------------------------------------

def active_version(conn: sqlite3.Connection, model_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM model_versions WHERE model_id = ? AND is_active = 1", (model_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def history(conn: sqlite3.Connection, model_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM model_versions WHERE model_id = ? ORDER BY id", (model_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_version(conn: sqlite3.Connection, version_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM model_versions WHERE id = ?", (version_id,)).fetchone()
    return dict(row) if row is not None else None


def find_by_tag(conn: sqlite3.Connection, model_id: str, version_tag: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM model_versions WHERE model_id = ? AND version_tag = ? ORDER BY id DESC",
        (model_id, version_tag),
    ).fetchone()
    return dict(row) if row is not None else None


# --- writes ---------------------------------------------------------------

def register_version(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    model_id: str,
    artifact_path: str | Path,
    *,
    version_tag: str | None = None,
    fit_window: tuple[str | None, str | None] = (None, None),
    registered_ts: str | None = None,
) -> int:
    """Append-only insert of a challenger version (is_active=0). Returns row id."""
    tag = version_tag or _version_tag_from_path(artifact_path)
    rel = _rel_path(artifact_path)
    cur = conn.execute(
        """
        INSERT INTO model_versions (model_id, version_tag, artifact_path, params_hash,
                                    fit_window_start, fit_window_end, registered_ts, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (model_id, tag, rel, sha256_file(artifact_path),
         fit_window[0], fit_window[1], registered_ts or now_utc_iso()),
    )
    conn.commit()
    log.info("model version registered",
             extra={"ctx": {"model_id": model_id, "version_tag": tag, "id": cur.lastrowid}})
    return int(cur.lastrowid)


def set_active(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    model_id: str,
    version_id: int,
    *,
    reason: str = "replaced",
) -> None:
    """Promote `version_id` to active; retire the incumbent. Regenerates ACTIVE.json.

    Clears any current active row for this model_id first so the single-active
    invariant (and the partial unique index) holds at every statement boundary.
    """
    ts = now_utc_iso()
    incumbent = active_version(conn, model_id)
    if incumbent is not None and incumbent["id"] != version_id:
        conn.execute(
            "UPDATE model_versions SET is_active = 0, retired_ts = ?, retired_reason = ? "
            "WHERE id = ?",
            (ts, reason, incumbent["id"]),
        )
    target = get_version(conn, version_id)
    promoted_ts = ts if (target is None or target.get("promoted_ts") is None) else target["promoted_ts"]
    conn.execute(
        "UPDATE model_versions SET is_active = 1, promoted_ts = ?, retired_ts = NULL, "
        "retired_reason = NULL WHERE id = ?",
        (promoted_ts, version_id),
    )
    conn.commit()
    regenerate_active_json(conn, config)
    log.info("model version activated",
             extra={"ctx": {"model_id": model_id, "version_id": version_id,
                            "retired": None if incumbent is None else incumbent["id"]}})


def previous_promotable(
    conn: sqlite3.Connection, model_id: str, exclude_id: int
) -> dict[str, Any] | None:
    """The most recent prior version eligible to be restored on rollback.

    Prefers a previously-promoted version; never one already retired by rollback
    (that path was tried and reverted). Excludes `exclude_id` (the current active).
    """
    row = conn.execute(
        """
        SELECT * FROM model_versions
        WHERE model_id = ? AND id != ?
          AND (retired_reason IS NULL OR retired_reason = 'replaced')
          AND promoted_ts IS NOT NULL
        ORDER BY id DESC LIMIT 1
        """,
        (model_id, exclude_id),
    ).fetchone()
    return dict(row) if row is not None else None


def rollback(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    model_id: str,
    *,
    reason: str = "rollback",
    to_version_tag: str | None = None,
) -> dict[str, Any] | None:
    """Retire the current active version and reactivate a prior one.

    Returns the restored version row, or None if there was nothing to roll back
    to. Records `retired_reason` on the demoted version.
    """
    current = active_version(conn, model_id)
    if current is None:
        log.warning("rollback: no active version", extra={"ctx": {"model_id": model_id}})
        return None

    if to_version_tag is not None:
        target = find_by_tag(conn, model_id, to_version_tag)
        if target is None or target["id"] == current["id"]:
            log.warning("rollback: target version not found",
                        extra={"ctx": {"model_id": model_id, "to": to_version_tag}})
            return None
    else:
        target = previous_promotable(conn, model_id, exclude_id=current["id"])
        if target is None:
            log.warning("rollback: no prior promotable version",
                        extra={"ctx": {"model_id": model_id}})
            return None

    ts = now_utc_iso()
    conn.execute(
        "UPDATE model_versions SET is_active = 0, retired_ts = ?, retired_reason = ? WHERE id = ?",
        (ts, reason, current["id"]),
    )
    conn.execute(
        "UPDATE model_versions SET is_active = 1, retired_ts = NULL, retired_reason = NULL "
        "WHERE id = ?",
        (target["id"],),
    )
    conn.commit()
    regenerate_active_json(conn, config)
    log.warning("model version rolled back",
                extra={"ctx": {"model_id": model_id, "from": current["id"],
                               "to": target["id"], "reason": reason}})
    return get_version(conn, target["id"])


def regenerate_active_json(conn: sqlite3.Connection, config: dict[str, Any]) -> Path:
    """Rewrite data/models/ACTIVE.json from the is_active rows (the only writer).

    Maps each model_id to the basename of its active artifact -- the format
    `load_active_artifact` already expects, so inference is unchanged.
    """
    rows = conn.execute(
        "SELECT model_id, artifact_path FROM model_versions WHERE is_active = 1"
    ).fetchall()
    active = {r["model_id"]: Path(r["artifact_path"]).name for r in rows}
    path = models_dir(config) / ACTIVE_JSON
    path.write_text(json.dumps(active, indent=2), encoding="utf-8")
    return path
