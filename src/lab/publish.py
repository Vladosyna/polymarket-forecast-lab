"""Mirror lab results -- curated (reports/exports/model artifacts) and raw
(data/lab.db, data/snapshots/) -- into a private git checkout and push.

This is the offsite backup CLAUDE.md calls for (Sec. 11: "the historical
order-book snapshots cannot be re-downloaded later") plus a visible feed of
model output, in one place. Runs as the last step of the nightly forecast
service (see collect/runner.py); never raises -- a failed publish must never
block or re-trigger the forecast/eval/report bundle it follows.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from lab.util import PROJECT_ROOT, now_utc_iso

import logging

log = logging.getLogger(__name__)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def sync_env(results_dir: Path, env_path: Path) -> None:
    """Back up .env (every API key/secret this lab uses) into the private
    results repo so a dead laptop doesn't mean re-requesting every key from
    scratch. Named .env.backup, not .env -- nothing in the results repo's own
    tooling could accidentally load it as active config.

    Security tradeoff, not free: the results repo is confirmed private, but
    committing here means every key value ever set lives in that repo's git
    history permanently, including after rotation (rotating the key in the
    provider's dashboard does not erase old commits). This is a deliberate,
    explicit choice to prioritize "don't lose the keys" over "minimize where
    secrets ever touched disk" -- acceptable for a solo-operator private repo,
    worth reconsidering before ever adding a second collaborator to it.
    """
    if not env_path.exists():
        return
    dst = results_dir / ".env.backup"
    shutil.copy2(env_path, dst)


def sync_reports(results_dir: Path, reports_dir: Path) -> None:
    if not reports_dir.exists():
        return
    dst = results_dir / "reports"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(reports_dir, dst)


def sync_model_artifacts(results_dir: Path, models_dir: Path) -> None:
    if not models_dir.exists():
        return
    dst = results_dir / "models"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(models_dir, dst)


def sync_export(results_dir: Path, conn: sqlite3.Connection) -> None:
    from lab.export import export_jsonl

    out_path = results_dir / "exports" / "latest_forecasts.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = list(export_jsonl(conn))
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def sync_db(results_dir: Path, db_path: Path) -> None:
    """Consistent copy via SQLite's backup API -- safe against a concurrently
    writing WAL connection, unlike a raw file copy.

    Always starts from a fresh destination file: a stale results-repo
    checkout can leave an unsmudged Git LFS pointer stub in place of the real
    binary (e.g. `git lfs pull` was never run there) -- sqlite3.backup()
    raises "file is not a database" trying to write into that, since it
    isn't a valid SQLite header. Removing any existing destination first
    means the backup always starts from nothing, regardless of what was
    there before."""
    if not db_path.exists():
        return
    dst_dir = results_dir / "data"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_path = dst_dir / "lab.db"
    dst_path.unlink(missing_ok=True)
    src_conn = sqlite3.connect(str(db_path))
    try:
        dst_conn = sqlite3.connect(str(dst_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def sync_snapshots(results_dir: Path, snapshots_dir: Path) -> int:
    """Mirror new/changed parquet partitions. Older date partitions are
    immutable once written, so this is normally a cheap incremental copy;
    only today's in-progress partition is re-copied on size/mtime change."""
    if not snapshots_dir.exists():
        return 0
    dst_root = results_dir / "data" / "snapshots"
    copied = 0
    for src_file in snapshots_dir.rglob("*.parquet"):
        rel = src_file.relative_to(snapshots_dir)
        dst_file = dst_root / rel
        if dst_file.exists():
            src_stat, dst_stat = src_file.stat(), dst_file.stat()
            if src_stat.st_size == dst_stat.st_size and src_stat.st_mtime <= dst_stat.st_mtime:
                continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        copied += 1
    return copied


def publish_results(
    config: dict[str, Any],
    conn: sqlite3.Connection,
    results_dir: Path | None = None,
    push: bool = True,
    include_snapshots: bool = False,
    include_db: bool = False,
    include_env: bool = False,
) -> dict[str, Any]:
    """Snapshots and the db are independent knobs, not one combined
    "raw data" flag: snapshots are cheap and incremental (only new/changed
    partitions copy, §Sec 11's "cannot be re-downloaded later" data), so they
    can run every night for free-tier LFS bandwidth. lab.db is a single ever-
    growing binary blob -- LFS has no delta compression for it, so every push
    transfers the FULL current size. Pushing it as often as snapshots would
    burn a GitHub LFS free-tier month's bandwidth (1GB) in days once the db
    passes a few hundred MB. run_publish_job gates `include_db` on an
    interval (publish.raw_data.db_interval_days) using a last-push timestamp
    in `meta` -- this function itself just does what it's told for either
    flag, independently. `include_env` is a third, separate knob: .env is
    tiny (no LFS bandwidth concern) so it needs no interval gating, but see
    sync_env's own docstring for the security tradeoff of backing up secrets
    into git history at all, even a private repo's."""
    pub_cfg = config.get("publish", {})
    results_dir = results_dir or (PROJECT_ROOT.parent / pub_cfg.get("results_dir", "../Polymarket-results"))
    results_dir = Path(results_dir).resolve()

    if not (results_dir / ".git").exists():
        return {"skipped": "results_dir_not_a_git_checkout", "results_dir": str(results_dir)}

    storage = config["storage"]
    sync_reports(results_dir, PROJECT_ROOT / storage["reports_dir"])
    sync_model_artifacts(results_dir, PROJECT_ROOT / storage["models_dir"])
    sync_export(results_dir, conn)
    n_snapshots = 0
    if include_snapshots:
        n_snapshots = sync_snapshots(results_dir, PROJECT_ROOT / storage["snapshots_dir"])
    if include_db:
        sync_db(results_dir, PROJECT_ROOT / storage["db_path"])
    if include_env:
        sync_env(results_dir, PROJECT_ROOT / ".env")

    _run_git(["add", "-A"], results_dir)
    diff = _run_git(["diff", "--cached", "--quiet"], results_dir)
    if diff.returncode == 0:
        return {"committed": False, "reason": "no_changes"}

    ts = now_utc_iso()
    commit = _run_git(["commit", "-m", f"Results update {ts}"], results_dir)
    if commit.returncode != 0:
        return {"committed": False, "reason": "commit_failed", "stderr": commit.stderr}

    result = {"committed": True, "ts": ts, "snapshot_files_copied": n_snapshots,
             "db_included": include_db, "env_included": include_env}
    if push:
        pushed = _run_git(["push"], results_dir)
        result["pushed"] = pushed.returncode == 0
        if not result["pushed"]:
            result["push_stderr"] = pushed.stderr
    return result
