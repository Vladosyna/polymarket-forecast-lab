"""Missed-run catch-up: overdue decision logic and last-run persistence."""

from __future__ import annotations

import asyncio

import pytest

from lab import schedule_state
from lab.collect.runner import (
    _register_analytics_jobs,
    _register_health_check,
    _snapshot_stale_threshold_minutes,
    register_collect_jobs,
)
from lab.schedule_state import is_overdue, is_snapshot_stale
from lab.store import db
from lab.util import load_config


def test_is_overdue_never_run():
    assert is_overdue(None, 24) is True


def test_is_overdue_fresh():
    assert is_overdue(60.0, 24) is False


def test_is_overdue_stale():
    assert is_overdue(25 * 3600, 24) is True


def test_is_overdue_boundary():
    assert is_overdue(24 * 3600, 24) is False


def test_is_snapshot_stale_never():
    assert is_snapshot_stale(None, 10) is True


def test_is_snapshot_stale_fresh():
    assert is_snapshot_stale(5.0, 10) is False


def test_is_snapshot_stale_stalled():
    assert is_snapshot_stale(11.0, 10) is True


def test_is_snapshot_stale_boundary():
    assert is_snapshot_stale(10.0, 10) is False


def test_snapshot_stale_threshold_minutes():
    config = {
        "collect": {"snapshot_interval_minutes": {"liquid": 5}},
        "forecast": {"max_snapshot_age_minutes": {"liquid": 15}},
    }
    assert _snapshot_stale_threshold_minutes(config) == 15


def test_meta_round_trip(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    try:
        assert db.get_meta(conn, "missing") is None
        db.set_meta(conn, "last_run_forecast", "2026-07-01T00:00:00+00:00")
        assert db.get_meta(conn, "last_run_forecast") == "2026-07-01T00:00:00+00:00"
        db.set_meta(conn, "last_run_forecast", "2026-07-02T00:00:00+00:00")
        assert db.get_meta(conn, "last_run_forecast") == "2026-07-02T00:00:00+00:00"
    finally:
        conn.close()


def test_record_and_read_age(tmp_path):
    config = {"storage": {"db_path": str(tmp_path / "lab.db")}}
    assert schedule_state.last_run_age_seconds(config, "forecast") is None

    schedule_state.record_job_run(config, "forecast")
    age = schedule_state.last_run_age_seconds(config, "forecast")
    assert age is not None
    assert age == pytest.approx(0, abs=5)
    assert is_overdue(age, 24) is False


def test_orchestrator_job_registration(tmp_path):
    """Non-destructive wiring check: all expected scheduler jobs are registered."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    config = load_config()
    config = {
        **config,
        "storage": {
            **config["storage"],
            "db_path": str(tmp_path / "lab.db"),
            "snapshots_dir": str(tmp_path / "snapshots"),
        },
    }

    async def _check() -> None:
        scheduler = AsyncIOScheduler(timezone="UTC")
        ctx = register_collect_jobs(scheduler, config)
        actx = _register_analytics_jobs(scheduler, config)
        _register_health_check(scheduler, config, ctx, actx)

        job_ids = {job.id for job in scheduler.get_jobs()}
        assert job_ids >= {"nightly", "weekly", "monthly", "health_check"}
        assert len(job_ids) >= 8  # 4 collector + 3 analytics + health_check

        health = scheduler.get_job("health_check")
        assert health is not None
        assert health.trigger.interval.total_seconds() == 60 * 60

        await ctx.aclose()

    asyncio.run(_check())

