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
from lab.publish import publish_results, sync_db, sync_env
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
        # Redirected like the rest: without it every publish test copies the
        # real 50MB training set (see storage.bootstrap_dir in config.yaml).
        "bootstrap_dir": str(tmp_path / "bootstrap"),
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


def test_run_publish_job_pings_heartbeat_on_success(config, monkeypatch):
    """Phase 18: the dead-man heartbeat fires on run_publish_job's success path
    (jobs.py imports lab.heartbeat.send_heartbeat lazily by name inside the
    function, so patching the attribute on the lab.heartbeat module -- not on
    lab.jobs -- is what takes effect)."""
    import lab.heartbeat

    calls = []

    async def fake_send_heartbeat(source):
        calls.append(source)
        return True

    monkeypatch.setattr(lab.heartbeat, "send_heartbeat", fake_send_heartbeat)

    result = run_publish_job(config)

    assert result.get("committed") is True
    assert calls == ["backup"]


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


def test_sync_env_copies_dotenv_to_results_dir(tmp_path):
    results_dir = tmp_path / "results_repo"
    _init_git_repo(results_dir)
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=fake-key-123\n", encoding="utf-8")

    sync_env(results_dir, env_path)

    dst = results_dir / ".env.backup"
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "DEEPSEEK_API_KEY=fake-key-123\n"


def test_sync_env_noop_when_dotenv_missing(tmp_path):
    results_dir = tmp_path / "results_repo"
    _init_git_repo(results_dir)

    sync_env(results_dir, tmp_path / "does_not_exist.env")

    assert not (results_dir / ".env.backup").exists()


def test_run_publish_job_calls_sync_env_only_when_env_enabled(config, monkeypatch):
    """publish_results/run_publish_job wire include_env through to sync_env --
    monkeypatched rather than exercised against the real project .env (which
    holds real secrets and has no configurable path), same precedent as the
    heartbeat test above."""
    import lab.publish

    calls = []

    def fake_sync_env(results_dir, env_path):
        calls.append(env_path)
        # Write something so the second call has a real git diff to commit --
        # otherwise "no_changes" short-circuits before env_included is set.
        (Path(results_dir) / ".env.backup").write_text("fake", encoding="utf-8")

    monkeypatch.setattr(lab.publish, "sync_env", fake_sync_env)

    run_publish_job(config)  # env_enabled unset -> defaults off
    assert calls == []

    config["publish"]["raw_data"]["env_enabled"] = True
    result = run_publish_job(config)
    assert result.get("env_included") is True
    assert len(calls) == 1


# --- LFS prune after a db push (2026-07-31 disk growth) -------------------

def test_prune_lfs_verifies_the_remote_before_deleting(monkeypatch, tmp_path):
    """Not optional for this repo. Prune removes LOCAL copies and by default
    trusts its own bookkeeping about what the remote holds; this is the backup
    of data that cannot be re-collected (brief section 11), so every object is
    confirmed present on GitHub first."""
    import subprocess as sp

    from lab.publish import prune_lfs

    seen = {}

    class _Res:
        returncode = 0
        stdout = ""
        stderr = "prune: 92 local objects, 32 retained, done.\nprune: 60 files pruned, done.\n"

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return _Res()

    monkeypatch.setattr(sp, "run", fake_run)
    out = prune_lfs(tmp_path)

    assert seen["cmd"] == ["git", "lfs", "prune", "--verify-remote"]
    assert out["ok"] is True
    assert any("pruned" in line for line in out["summary"])


def test_prune_lfs_failure_never_raises(monkeypatch, tmp_path):
    """A failed prune costs disk; a raise here would cost the backup
    (guardrail 9)."""
    import subprocess as sp

    from lab.publish import prune_lfs

    class _Res:
        returncode = 1
        stdout = ""
        stderr = "fatal: could not reach remote\n"

    monkeypatch.setattr(sp, "run", lambda cmd, **k: _Res())
    out = prune_lfs(tmp_path)
    assert out["ok"] is False


def test_publish_prunes_only_after_a_successful_db_push(config, monkeypatch):
    """Only a db push leaves a new multi-hundred-MB object behind, and only a
    successful push means the remote actually has it. A snapshots-only or
    failed push must not trigger a prune."""
    from lab import publish as pub

    calls = []
    monkeypatch.setattr(pub, "prune_lfs", lambda d: calls.append(d) or {"ok": True})

    results_dir = Path(config["publish"]["results_dir"])

    conn = db.connect(config["storage"]["db_path"])
    try:
        # Curated-only, and push=False: nothing large added and nothing sent,
        # so there is nothing on the remote to safely retire against.
        pub.publish_results(config, conn, results_dir=results_dir, push=False,
                            include_db=True)
        assert calls == [], "pruned without a successful push"
    finally:
        conn.close()


def test_publish_does_not_prune_when_the_push_fails(config, monkeypatch):
    """The dangerous case: a db push that ERRORS must not be followed by a
    prune. Prune only deletes local copies it can confirm on the remote, but
    a failed push means the newest object may not be there at all -- retiring
    anything against that state is exactly the wrong moment."""
    from pathlib import Path

    from lab import publish as pub

    calls = []
    monkeypatch.setattr(pub, "prune_lfs", lambda d: calls.append(d) or {"ok": True})

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "fatal: unable to access remote"

    real_run_git = pub._run_git

    def fake_run_git(args, cwd):
        if args and args[0] == "push":
            return _Failed()
        return real_run_git(args, cwd)

    monkeypatch.setattr(pub, "_run_git", fake_run_git)

    results_dir = Path(config["publish"]["results_dir"])
    conn = db.connect(config["storage"]["db_path"])
    try:
        result = pub.publish_results(config, conn, results_dir=results_dir,
                                     push=True, include_db=True)
    finally:
        conn.close()

    assert result.get("pushed") is False
    assert calls == [], "pruned after a failed push -- the local copy may be all there is"


def test_sync_bootstrap_mirrors_the_training_set(tmp_path):
    """Found missing 2026-08-22: publish.py had never mentioned `bootstrap`, so
    the mirror held a 594KB predecessor committed by hand in July while every
    refit since 2026-08-02 fitted on a 50MB file with no off-host copy. Without
    it the recalibration curves cannot be reproduced -- it is derivable from the
    HuggingFace dataset, but a 21-27GB download is not a backup."""
    from lab.publish import sync_bootstrap

    src = tmp_path / "bootstrap"
    src.mkdir()
    (src / "observations.parquet").write_bytes(b"PAR1" + b"x" * 500)
    results = tmp_path / "results"
    results.mkdir()

    assert sync_bootstrap(results, src) == 1
    mirrored = results / "bootstrap" / "observations.parquet"
    assert mirrored.read_bytes() == (src / "observations.parquet").read_bytes()

    # unchanged on a second pass -- it is one static file, not a nightly cost
    assert sync_bootstrap(results, src) == 0

    # ...and a rebuild does propagate
    (src / "observations.parquet").write_bytes(b"PAR1" + b"y" * 900)
    assert sync_bootstrap(results, src) == 1
    assert mirrored.read_bytes() == (src / "observations.parquet").read_bytes()


def test_sync_bootstrap_is_a_no_op_without_a_training_set(tmp_path):
    """A host that has never fetched the bootstrap archive (see OPERATIONS.md,
    'The M1 training set is a host dependency') must publish normally."""
    from lab.publish import sync_bootstrap

    results = tmp_path / "results"
    results.mkdir()
    assert sync_bootstrap(results, tmp_path / "absent") == 0


def _ledger_fixture(conn, days=("2026-08-20", "2026-08-21")):
    from lab.store import db
    for i, day in enumerate(days):
        for j in range(3):
            db.append_forecast(conn, {
                "ts": f"{day}T02:00:0{j}+00:00", "condition_id": f"0x{i}{j}",
                "model_id": "m0_market", "p_yes": 0.5, "p_market_at_ts": 0.5,
            })
    conn.commit()


def test_ledger_increment_writes_one_file_per_closed_day(tmp_path, config, monkeypatch):
    """The db goes weekly, so the one irreplaceable artifact in this project had
    a seven-day recovery point while the snapshots describing it had a one-day
    one: 90,767 forecast rows existed only on the collecting host when this was
    measured. A day of appended rows gzips to a few MB."""
    import gzip
    import json

    from lab.publish import sync_ledger_increment
    from lab.store import db

    conn = db.connect(config["storage"]["db_path"])
    _ledger_fixture(conn)
    results = tmp_path / "ledger_out"
    results.mkdir()

    written = sync_ledger_increment(results, conn)
    assert written["forecasts"] == 2

    f = results / "ledger" / "forecasts" / "2026-08-21.jsonl.gz"
    rows = [json.loads(l) for l in gzip.open(f, "rt", encoding="utf-8")]
    assert len(rows) == 3
    # full rows, not a projection -- a restore has to be faithful
    assert {"id", "ts", "condition_id", "model_id", "p_yes", "p_market_at_ts"} <= set(rows[0])
    conn.close()


def test_ledger_increment_is_idempotent_and_never_rewrites_a_day(tmp_path, config):
    """Stateless: 'already mirrored' is read from the files, so a second run is
    a no-op and a finished day is never rewritten -- the same property the
    append-only ledger itself has."""
    from lab.publish import sync_ledger_increment
    from lab.store import db

    conn = db.connect(config["storage"]["db_path"])
    _ledger_fixture(conn)
    results = tmp_path / "ledger_out"
    results.mkdir()

    assert sync_ledger_increment(results, conn)["forecasts"] == 2
    f = results / "ledger" / "forecasts" / "2026-08-21.jsonl.gz"
    stamp = f.stat().st_mtime_ns

    assert sync_ledger_increment(results, conn) == {}
    assert f.stat().st_mtime_ns == stamp
    conn.close()


def test_ledger_increment_skips_today_and_goes_newest_first(tmp_path, config, monkeypatch):
    """Today is still being appended to, so a file for it would be a partial
    day. And the backlog is bounded newest-first, because the point is to
    shrink the recovery point now, not to drain history first."""
    from lab.publish import sync_ledger_increment
    from lab.store import db
    from lab.util import now_utc

    conn = db.connect(config["storage"]["db_path"])
    today = now_utc().date().isoformat()
    _ledger_fixture(conn, days=("2026-08-18", "2026-08-19", "2026-08-20", today))
    results = tmp_path / "ledger_out"
    results.mkdir()

    written = sync_ledger_increment(results, conn, max_days=2)
    assert written["forecasts"] == 2

    got = sorted(p.name for p in (results / "ledger" / "forecasts").glob("*.jsonl.gz"))
    assert got == ["2026-08-19.jsonl.gz", "2026-08-20.jsonl.gz"]   # newest closed days
    assert not (results / "ledger" / "forecasts" / f"{today}.jsonl.gz").exists()
    conn.close()


def test_ledger_increment_writes_nothing_to_the_database(tmp_path, config):
    """It must stay a pure reader. The forecasts table is guarded by a SQLite
    authorizer that hard-denies UPDATE/DELETE (guardrail: immutable ledger),
    and a backup path is the last place that should be negotiating with it."""
    from lab.publish import sync_ledger_increment
    from lab.store import db

    conn = db.connect(config["storage"]["db_path"])
    _ledger_fixture(conn)
    before = conn.execute("SELECT COUNT(*), COALESCE(SUM(id), 0) FROM forecasts").fetchone()
    results = tmp_path / "ledger_out"
    results.mkdir()

    sync_ledger_increment(results, conn)

    assert conn.execute("SELECT COUNT(*), COALESCE(SUM(id), 0) FROM forecasts").fetchone() == before
    assert conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'last_ledger%'").fetchone()[0] == 0
    conn.close()
