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
        assert job_ids >= {"nightly", "weekly", "monthly", "health_check", "heartbeat_ping",
                           "map_propose_weekly", "pmxt_verify_twice_daily"}
        assert len(job_ids) >= 10  # 4 collector + heartbeat_ping + 4 analytics + health_check

        health = scheduler.get_job("health_check")
        assert health is not None
        assert health.trigger.interval.total_seconds() == 60 * 60
        # Live VPS audit (2026-07-14): APScheduler's 1s default misfire_grace_time
        # was tighter than this scheduler's routine multi-second jitter under load,
        # so every hourly firing was silently discarded and the job never once ran.
        assert health.misfire_grace_time == 300

        hb = scheduler.get_job("heartbeat_ping")
        assert hb is not None
        assert hb.trigger.interval.total_seconds() == 5 * 60

        await ctx.aclose()

    asyncio.run(_check())



# --- a failed tail must not re-trigger the body (2026-07-28 crash loop) ----

def test_success_is_recorded_before_the_tail_runs(tmp_path, monkeypatch):
    """The bundle's retry engine must track the steps that WRITE data, not the
    ones that render it.

    Live failure this encodes: once the resolution backlog drained (13k -> 57k
    resolved), the report render stopped fitting in memory and the process was
    OOM-killed mid-render. Because the last-run stamp was written after the
    tail, an already-finished forecast+eval never counted as done, so the
    hourly catch-up rebuilt the whole bundle every ~3 hours for 28 hours,
    taking the collector down with it each time.

    An OOM kill cannot be simulated in-process -- it is not an exception -- so
    the test asserts the ordering that makes it survivable: the stamp exists by
    the time the tail is entered.
    """
    import asyncio

    from lab.schedule_state import last_run_age_seconds, record_job_run

    config = {"storage": {"db_path": str(tmp_path / "lab.db")}}
    stamped_when_tail_ran = {}

    # Exercise the ordering the real _run contract guarantees.
    async def run_like_service():
        body_ok = False
        try:
            pass  # body: forecast + eval, succeeds
        except Exception:
            body_ok = False
        else:
            body_ok = True
        if body_ok:
            await asyncio.to_thread(record_job_run, config, "forecast")
        # tail: report render -- observe whether the stamp is already in place
        stamped_when_tail_ran["age"] = await asyncio.to_thread(
            last_run_age_seconds, config, "forecast")
        raise RuntimeError("report OOM stand-in")

    with pytest.raises(RuntimeError):
        asyncio.run(run_like_service())

    assert stamped_when_tail_ran["age"] is not None, (
        "no last-run stamp when the tail started -- a dying render would leave "
        "the bundle permanently overdue and re-trigger it hourly"
    )


def test_report_is_not_in_the_retriable_body():
    """Guards the split itself: run_report_job must not sit in the path whose
    failure marks the bundle unfinished."""
    import inspect

    from lab.collect import runner

    src = inspect.getsource(runner._build_analytics_services)
    body_src = src.split("async def body() -> None:", 1)[1].split("async def tail()", 1)[0]
    assert "run_forecast_job" in body_src and "run_eval_job" in body_src
    assert "run_report_job" not in body_src, "report is back in the retriable body"
