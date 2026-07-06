"""Phase 15 (v2.3): ledger commitment -- cryptographic pre-registration
(CLAUDE.md section 6/15).

Covers: hash determinism, the empty-day fixed-constant guard, first-run
no-backfill semantics, exclusion of the still-accumulating current day,
idempotency, multi-day catch-up, the prospective/backfilled flag, the core
tamper-detection value proposition, and the never-raise job boundary.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from lab import ledger_commitment as lc
from lab.store import db
from lab.util import load_config

FIXED_NOW = datetime(2026, 7, 10, 3, 0, 0, tzinfo=timezone.utc)
TODAY = FIXED_NOW.date()


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
    cfg["ledger"] = {"enabled": True, "commitments_path": "docs/ledger_commitments.jsonl", "push": False}
    return cfg


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lab.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(lc, "now_utc", lambda: FIXED_NOW)
    monkeypatch.setattr(lc, "now_utc_iso", lambda: FIXED_NOW.isoformat(timespec="seconds"))


def _seed(conn, ts, condition_id="0x1", model_id="m0_market", p_yes=0.5, p_market=0.5):
    return db.append_forecast(conn, {
        "ts": ts, "condition_id": condition_id, "model_id": model_id,
        "p_yes": p_yes, "p_market_at_ts": p_market,
    })


def _iso(d, hour=12):
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).isoformat(timespec="seconds")


def _raw_tamper(tmp_path, row_id, p_yes):
    """Mutate a forecasts row bypassing db.connect()'s append-only authorizer
    (guardrail 5) entirely -- a raw sqlite3 connection on the same file, the
    way an out-of-band edit to the DB file itself would actually happen. This
    is the real threat model a cryptographic commitment defends against; the
    in-app authorizer only guards this process's own connections."""
    raw = sqlite3.connect(tmp_path / "lab.db")
    raw.execute("UPDATE forecasts SET p_yes = ? WHERE id = ?", (p_yes, row_id))
    raw.commit()
    raw.close()


def _install_failing_precommit_hook(repo):
    """A pre-commit hook that always rejects -- reliably forces `git commit`
    to fail regardless of the environment's own global git config (unsetting
    user.name/email would silently fall back to a real global identity on a
    dev machine and not actually reproduce the failure)."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)


def _init_git_repo(path):
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "test@test.local"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(args, cwd=path, capture_output=True, text=True, check=True)
    (path / "README.md").write_text("test repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, text=True, check=True)


# --- hashing ------------------------------------------------------------

def test_hash_is_deterministic(conn):
    _seed(conn, _iso(TODAY - timedelta(days=1)))
    _seed(conn, _iso(TODAY - timedelta(days=1)), condition_id="0x2")
    conn.commit()
    rows = lc._query_day(conn, (TODAY - timedelta(days=1)).isoformat())
    assert lc._hash_rows(rows) == lc._hash_rows(rows)


def test_hash_changes_if_row_content_changes(conn, tmp_path):
    _seed(conn, _iso(TODAY - timedelta(days=1)), p_yes=0.5)
    conn.commit()
    date_str = (TODAY - timedelta(days=1)).isoformat()
    hash_before = lc._hash_rows(lc._query_day(conn, date_str))
    row_id = lc._query_day(conn, date_str)[0]["id"]
    _raw_tamper(tmp_path, row_id, 0.51)
    hash_after = lc._hash_rows(lc._query_day(conn, date_str))
    assert hash_after != hash_before


def test_empty_day_hash_is_fixed_sha256_empty_constant():
    assert lc._hash_rows([]) == hashlib.sha256(b"").hexdigest()


# --- commit_pending_days --------------------------------------------------

def test_commit_pending_days_first_run_only_commits_yesterday(conn, tmp_path):
    # Pre-existing history from well before this feature existed -- must NOT
    # be backfilled (decision 2 in the plan).
    _seed(conn, _iso(TODAY - timedelta(days=30)))
    _seed(conn, _iso(TODAY - timedelta(days=10)))
    _seed(conn, _iso(TODAY - timedelta(days=1)))
    conn.commit()
    ledger_path = tmp_path / "ledger_commitments.jsonl"
    records = lc.commit_pending_days(conn, ledger_path)
    assert [r["date"] for r in records] == [(TODAY - timedelta(days=1)).isoformat()]


def test_commit_pending_days_excludes_today(conn, tmp_path):
    _seed(conn, _iso(TODAY))
    conn.commit()
    ledger_path = tmp_path / "ledger_commitments.jsonl"
    records = lc.commit_pending_days(conn, ledger_path)
    assert all(r["date"] != TODAY.isoformat() for r in records)
    # Yesterday is still fully past and gets a (zero-row) record of its own.
    assert len(records) == 1
    assert records[0]["date"] == (TODAY - timedelta(days=1)).isoformat()
    assert records[0]["row_count"] == 0


def test_commit_pending_days_is_idempotent(conn, tmp_path):
    _seed(conn, _iso(TODAY - timedelta(days=1)))
    conn.commit()
    ledger_path = tmp_path / "ledger_commitments.jsonl"
    first = lc.commit_pending_days(conn, ledger_path)
    assert len(first) == 1
    second = lc.commit_pending_days(conn, ledger_path)
    assert second == []


def test_commit_pending_days_catches_up_multiple_missing_days(conn, tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger_commitments.jsonl"
    first = lc.commit_pending_days(conn, ledger_path)
    assert [r["date"] for r in first] == [(TODAY - timedelta(days=1)).isoformat()]

    # Simulate the job not running for 3 days.
    later = FIXED_NOW + timedelta(days=3)
    monkeypatch.setattr(lc, "now_utc", lambda: later)
    monkeypatch.setattr(lc, "now_utc_iso", lambda: later.isoformat(timespec="seconds"))
    _seed(conn, _iso(TODAY))
    _seed(conn, _iso(TODAY + timedelta(days=1)))
    _seed(conn, _iso(TODAY + timedelta(days=2)))
    conn.commit()

    second = lc.commit_pending_days(conn, ledger_path)
    expected_dates = [(TODAY + timedelta(days=d)).isoformat() for d in (0, 1, 2)]
    assert [r["date"] for r in second] == expected_dates


def test_zero_row_day_still_produces_record(conn, tmp_path):
    ledger_path = tmp_path / "ledger_commitments.jsonl"
    records = lc.commit_pending_days(conn, ledger_path)
    assert len(records) == 1
    r = records[0]
    assert r["row_count"] == 0
    assert r["first_id"] is None and r["last_id"] is None
    assert r["sha256"] == hashlib.sha256(b"").hexdigest()


def test_prospective_flag_true_for_next_day_commit(conn, tmp_path):
    _seed(conn, _iso(TODAY - timedelta(days=1)))
    conn.commit()
    ledger_path = tmp_path / "ledger_commitments.jsonl"
    records = lc.commit_pending_days(conn, ledger_path)
    assert records[0]["prospective"] is True


def test_prospective_flag_false_for_backfilled_commit(conn, tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger_commitments.jsonl"
    lc.commit_pending_days(conn, ledger_path)  # commits TODAY-1 first

    later = FIXED_NOW + timedelta(days=5)
    monkeypatch.setattr(lc, "now_utc", lambda: later)
    monkeypatch.setattr(lc, "now_utc_iso", lambda: later.isoformat(timespec="seconds"))
    records = lc.commit_pending_days(conn, ledger_path)
    # The oldest caught-up day was committed 5 days late -- not prospective.
    assert records[0]["date"] == TODAY.isoformat()
    assert records[0]["prospective"] is False


# --- verify_commitment: the core value proposition ------------------------

def test_verify_commitment_succeeds_on_untampered_data(conn, tmp_path):
    _seed(conn, _iso(TODAY - timedelta(days=1)))
    _seed(conn, _iso(TODAY - timedelta(days=1)), condition_id="0x2")
    conn.commit()
    ledger_path = tmp_path / "ledger_commitments.jsonl"
    records = lc.commit_pending_days(conn, ledger_path)
    assert lc.verify_commitment(conn, records[0]) is True


def test_verify_commitment_fails_after_row_tampered(conn, tmp_path):
    _seed(conn, _iso(TODAY - timedelta(days=1)), p_yes=0.5)
    conn.commit()
    ledger_path = tmp_path / "ledger_commitments.jsonl"
    records = lc.commit_pending_days(conn, ledger_path)
    assert lc.verify_commitment(conn, records[0]) is True

    _raw_tamper(tmp_path, records[0]["first_id"], 0.9123)
    assert lc.verify_commitment(conn, records[0]) is False


def test_verify_commitment_detects_added_row_on_previously_zero_day(conn, tmp_path):
    ledger_path = tmp_path / "ledger_commitments.jsonl"
    records = lc.commit_pending_days(conn, ledger_path)  # commits TODAY-1 with 0 rows
    assert records[0]["row_count"] == 0
    assert lc.verify_commitment(conn, records[0]) is True

    # A forecast later appears for that already-committed (zero-row) date.
    # Under normal operation this never happens -- ts is always "now" at
    # write time -- so its mere presence is itself the tamper signal; a
    # verifier anchored only to a (nonexistent) id range would miss this.
    _seed(conn, _iso(TODAY - timedelta(days=1)))
    conn.commit()
    assert lc.verify_commitment(conn, records[0]) is False


def test_verify_commitment_detects_inconsistent_zero_metadata(conn):
    # A hand-edited or corrupted ledger line claiming zero rows but carrying
    # a stale id range must not pass verification.
    bad_record = {
        "date": "2026-01-01", "row_count": 0, "first_id": 5, "last_id": 5,
        "sha256": hashlib.sha256(b"").hexdigest(), "committed_ts": "x", "prospective": True,
    }
    assert lc.verify_commitment(conn, bad_record) is False


# --- git orchestration -----------------------------------------------------

def test_job_creates_real_git_commit(conn, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.setattr(lc, "PROJECT_ROOT", repo)

    _seed(conn, _iso(TODAY - timedelta(days=1)))
    conn.commit()

    cfg = {"ledger": {"enabled": True, "commitments_path": "docs/ledger_commitments.jsonl", "push": False}}
    result = lc.commit_and_push(cfg, conn)
    assert result["committed"] is True

    log = subprocess.run(["git", "log", "--name-only", "-1"], cwd=repo, capture_output=True, text=True)
    assert "docs/ledger_commitments.jsonl" in log.stdout


def test_commit_and_push_returns_error_on_git_failure(conn, tmp_path, monkeypatch):
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    monkeypatch.setattr(lc, "PROJECT_ROOT", not_a_repo)

    _seed(conn, _iso(TODAY - timedelta(days=1)))
    conn.commit()

    cfg = {"ledger": {"enabled": True, "commitments_path": "docs/ledger_commitments.jsonl", "push": False}}
    result = lc.commit_and_push(cfg, conn)
    assert "error" in result


def test_commit_and_push_reverts_append_on_commit_failure(conn, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    _install_failing_precommit_hook(repo)
    monkeypatch.setattr(lc, "PROJECT_ROOT", repo)

    _seed(conn, _iso(TODAY - timedelta(days=1)))
    conn.commit()

    cfg = {"ledger": {"enabled": True, "commitments_path": "docs/ledger_commitments.jsonl", "push": False}}
    ledger_path = repo / "docs" / "ledger_commitments.jsonl"

    result = lc.commit_and_push(cfg, conn)
    assert "error" in result
    # The append must be rolled back -- otherwise a retry would see this date
    # as already "committed" in the file with no matching git commit to show
    # for it, permanently orphaning it.
    assert lc._read_ledger(ledger_path) == []

    # Remove the hook and retry: the SAME date must be retried, not skipped.
    (repo / ".git" / "hooks" / "pre-commit").unlink()
    retry = lc.commit_and_push(cfg, conn)
    assert retry["committed"] is True
    assert retry["dates"] == [(TODAY - timedelta(days=1)).isoformat()]


def test_job_never_raises_when_commit_and_push_raises(config, monkeypatch):
    from lab import jobs

    def boom(_config, _conn):
        raise RuntimeError("boom")

    monkeypatch.setattr("lab.ledger_commitment.commit_and_push", boom)
    result = jobs.run_ledger_commitment_job(config)
    assert result == {"error": "ledger_commitment_failed"}


def test_job_skipped_when_disabled(config):
    from lab import jobs

    config["ledger"]["enabled"] = False
    result = jobs.run_ledger_commitment_job(config)
    assert result == {"skipped": "disabled"}
