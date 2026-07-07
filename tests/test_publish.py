"""Publish results to the private repo mirror (src/lab/publish.py,
src/lab/jobs.py::run_publish_job). Curated results always mirror; snapshots
and the db are independent raw-data knobs -- snapshots are cheap/incremental
so they can push nightly, but the db is a single ever-growing binary blob
with no LFS delta compression, so it's gated on an interval to stay inside a
free-tier LFS bandwidth budget."""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from lab.jobs import _db_push_due, run_publish_job
from lab.publish import publish_results, sync_db
from lab.store import db
from lab.store.snapshots import SnapshotStore
from lab.util import load_config, now_utc, now_utc_iso


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, capture_output=True)
    (path / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True)


@pytest.fixture()
def config(tmp_path):
    cfg = load_config()
    cfg["storage"] = {
        "db_path": str(tmp_path / "lab.db"),
        "snapshots_dir": str(tmp_path / "snapshots"),
        "models_dir": str(tmp_path / "models"),
        "logs_dir": str(tmp_path / "logs"),
        "reports_dir": str(tmp_path / "reports"),
    }
    results_dir = tmp_path / "results_repo"
    _init_git_repo(results_dir)
    cfg["publish"] = {
        "enabled": True, "results_dir": str(results_dir),
        "raw_data": {"snapshots_enabled": False, "db_enabled": False, "db_interval_days": 3},
    }
    return cfg


def test_publish_results_snapshots_and_db_are_independent_flags(config):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    store.append([{"ts": now_utc_iso(), "condition_id": "0x1", "mid": 0.5}])

    result = publish_results(config, conn, push=False, include_snapshots=True, include_db=False)
    assert result["committed"] is True
    assert result["db_included"] is False
    results_dir = Path(config["publish"]["results_dir"])
    assert (results_dir / "data" / "snapshots").exists()
    assert not (results_dir / "data" / "lab.db").exists()
    conn.close()


def test_publish_results_db_only(config):
    conn = db.connect(config["storage"]["db_path"])
    result = publish_results(config, conn, push=False, include_snapshots=False, include_db=True)
    assert result["committed"] is True
    assert result["db_included"] is True
    results_dir = Path(config["publish"]["results_dir"])
    assert (results_dir / "data" / "lab.db").exists()
    assert not (results_dir / "data" / "snapshots").exists()
    conn.close()


def test_db_push_due_first_time_then_recently_then_after_interval(config):
    conn = db.connect(config["storage"]["db_path"])
    assert _db_push_due(conn, interval_days=3) is True  # no meta yet -> due

    db.set_meta(conn, "last_raw_db_push_ts", now_utc_iso())
    assert _db_push_due(conn, interval_days=3) is False  # just pushed -> not due

    stale_ts = (now_utc() - timedelta(days=4)).isoformat(timespec="seconds")
    db.set_meta(conn, "last_raw_db_push_ts", stale_ts)
    assert _db_push_due(conn, interval_days=3) is True  # 4 days > 3-day interval -> due
    conn.close()


def test_run_publish_job_pushes_db_on_first_run_and_records_meta(config):
    config["publish"]["raw_data"]["db_enabled"] = True

    result = run_publish_job(config)
    assert result.get("committed") is True
    assert result.get("db_included") is True

    conn = db.connect(config["storage"]["db_path"])
    assert db.get_meta(conn, "last_raw_db_push_ts") is not None
    conn.close()


def test_run_publish_job_skips_db_when_interval_not_elapsed_even_with_changes(config):
    """Seed a real DB change (so a commit happens regardless) to prove the
    interval gate -- not publish_results' own no-changes short-circuit --
    is what decided db_included=False here."""
    config["publish"]["raw_data"]["db_enabled"] = True
    conn = db.connect(config["storage"]["db_path"])
    db.set_meta(conn, "last_raw_db_push_ts", now_utc_iso())  # just pushed
    conn.execute(
        "INSERT INTO markets (condition_id, question, category, tier, active, closed) "
        "VALUES ('0x1', 'q', 'politics', 'liquid', 1, 0)"
    )
    db.append_forecast(conn, {"ts": now_utc_iso(), "condition_id": "0x1", "model_id": "m0_market",
                              "p_yes": 0.5, "p_market_at_ts": 0.5})
    conn.commit()
    conn.close()

    result = run_publish_job(config)
    assert result.get("committed") is True  # export content changed -> real commit
    assert result.get("db_included") is False  # interval not elapsed


def test_run_publish_job_respects_snapshots_enabled_flag(config):
    config["publish"]["raw_data"]["snapshots_enabled"] = True
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    store.append([{"ts": now_utc_iso(), "condition_id": "0x1", "mid": 0.5}])

    result = run_publish_job(config)
    assert result.get("committed") is True
    results_dir = Path(config["publish"]["results_dir"])
    assert (results_dir / "data" / "snapshots").exists()
    assert not (results_dir / "data" / "lab.db").exists()


def test_run_publish_job_defaults_to_curated_only_when_raw_data_unset(config):
    """No publish.raw_data section at all -- pre-Phase-16 behavior preserved,
    no config change required to keep getting only the curated mirror."""
    del config["publish"]["raw_data"]
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    store.append([{"ts": now_utc_iso(), "condition_id": "0x1", "mid": 0.5}])

    result = run_publish_job(config)
    assert result.get("committed") is True
    results_dir = Path(config["publish"]["results_dir"])
    assert not (results_dir / "data" / "snapshots").exists()
    assert not (results_dir / "data" / "lab.db").exists()


def test_run_publish_job_skipped_when_disabled(config):
    config["publish"]["enabled"] = False
    assert run_publish_job(config) == {"skipped": "disabled"}


def test_sync_db_overwrites_a_stale_unsmudged_lfs_pointer_stub(config, tmp_path):
    """Real bug found live: a results-repo checkout can hold an unsmudged Git
    LFS pointer stub (a small text file) where the real lab.db binary should
    be -- sqlite3.backup() raised "file is not a database" trying to write
    into that. sync_db must overwrite it, not assume it's already a valid
    (or absent) SQLite file."""
    conn = db.connect(config["storage"]["db_path"])  # creates the real source db + schema
    conn.close()

    results_dir = Path(config["publish"]["results_dir"])
    dst_dir = results_dir / "data"
    dst_dir.mkdir(parents=True, exist_ok=True)
    (dst_dir / "lab.db").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef\nsize 12345\n"
    )

    sync_db(results_dir, Path(config["storage"]["db_path"]))

    conn = sqlite3.connect(str(dst_dir / "lab.db"))
    conn.execute("SELECT name FROM sqlite_master LIMIT 1")  # raises if still not a valid db
    conn.close()
