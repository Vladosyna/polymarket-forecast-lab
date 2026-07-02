"""Long-running collection process: APScheduler driving sync/snapshots/resolutions.

Every job checks the PAUSE kill file first, so polling halts within one cycle
of the file appearing (guardrail 8).

`run_collect` runs collection only. `run_orchestrator` (the one-button entry
point) additionally schedules the analytics jobs -- forecast/eval/report,
shadow, and learn -- on the same event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from lab.api.clob import ClobClient
from lab.api.gamma import GammaClient
from lab.api.http import TokenBucket
from lab.collect.resolutions import watch_resolutions
from lab.collect.snapshots import snapshot_tier
from lab.collect.universe import sync_universe
from lab.store import db
from lab.store.snapshots import SnapshotStore
from lab.util import PROJECT_ROOT

log = logging.getLogger(__name__)


def pause_file_path(config: dict[str, Any]) -> Path:
    p = Path(config["collect"]["pause_file"])
    return p if p.is_absolute() else PROJECT_ROOT / p


def _runtime_dir(config: dict[str, Any]) -> Path:
    d = Path(config["storage"]["db_path"])
    d = (d if d.is_absolute() else PROJECT_ROOT / d).parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def heartbeat_path(config: dict[str, Any]) -> Path:
    """File the orchestrator touches every loop; the watchdog reads its mtime."""
    return _runtime_dir(config) / "orchestrator.heartbeat"


def pid_path(config: dict[str, Any]) -> Path:
    return _runtime_dir(config) / "orchestrator.pid"


def _write_heartbeat(config: dict[str, Any]) -> None:
    from lab.util import now_utc_iso

    try:
        heartbeat_path(config).write_text(now_utc_iso(), encoding="utf-8")
    except OSError:
        log.warning("could not write heartbeat file")


def is_paused(config: dict[str, Any]) -> bool:
    if pause_file_path(config).exists():
        log.warning("PAUSE file present -- skipping cycle")
        return True
    return False


@dataclass
class CollectContext:
    """Owns the shared clients/DB connection and the collection job callables."""

    gamma: GammaClient
    clob: ClobClient
    conn: Any
    store: SnapshotStore
    jobs: dict[str, Callable[[], Awaitable[None]]]

    async def aclose(self) -> None:
        await self.gamma.aclose()
        await self.clob.aclose()
        self.conn.close()


def register_collect_jobs(scheduler: AsyncIOScheduler, config: dict[str, Any]) -> CollectContext:
    """Build shared resources and register the collection interval jobs."""
    bucket = TokenBucket(
        rate=config["collect"]["rate_limit"]["requests_per_second"],
        burst=config["collect"]["rate_limit"]["burst"],
    )
    gamma = GammaClient(bucket)
    clob = ClobClient(bucket)
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])

    async def job_sync() -> None:
        if not is_paused(config):
            await sync_universe(gamma, conn, config)

    async def job_snap_liquid() -> None:
        if not is_paused(config):
            await snapshot_tier(clob, conn, store, "liquid", config)

    async def job_snap_tail() -> None:
        if not is_paused(config):
            await snapshot_tier(clob, conn, store, "tail", config)

    async def job_resolutions() -> None:
        if not is_paused(config):
            await watch_resolutions(gamma, conn)

    cadence = config["collect"]["snapshot_interval_minutes"]
    scheduler.add_job(job_sync, "interval",
                      minutes=config["universe"]["sync_interval_minutes"])
    scheduler.add_job(job_snap_liquid, "interval", minutes=cadence["liquid"])
    scheduler.add_job(job_snap_tail, "interval", minutes=cadence["tail"])
    scheduler.add_job(job_resolutions, "interval",
                      minutes=config["collect"]["resolution_poll_minutes"])

    return CollectContext(
        gamma=gamma, clob=clob, conn=conn, store=store,
        jobs={
            "sync": job_sync,
            "snap_liquid": job_snap_liquid,
            "snap_tail": job_snap_tail,
            "resolutions": job_resolutions,
        },
    )


async def _startup_collection_cycle(ctx: CollectContext) -> None:
    log.info("collector startup cycle")
    await ctx.jobs["sync"]()
    await ctx.jobs["snap_liquid"]()
    await ctx.jobs["snap_tail"]()
    await ctx.jobs["resolutions"]()


async def run_collect(config: dict[str, Any]) -> None:
    from lab import process_guard

    try:
        process_guard.enforce(config, "collector")
    except Exception:
        log.exception("instance guard failed at collector startup")

    scheduler = AsyncIOScheduler(timezone="UTC")
    ctx = register_collect_jobs(scheduler, config)
    scheduler.start()

    log.info("collector started")
    await _startup_collection_cycle(ctx)

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.shutdown(wait=False)
        await ctx.aclose()
        log.info("collector stopped")


SERVICE_NAMES = ("forecast", "shadow", "learn")


@dataclass
class AnalyticsContext:
    """The per-service runner coroutines plus their control-time thresholds."""

    services: dict[str, Callable[[], Awaitable[None]]]
    max_age_hours: dict[str, float]


def _control_max_ages(config: dict[str, Any]) -> dict[str, float]:
    control = config.get("schedule", {}).get("control", {})
    return {
        "forecast": control.get("forecast_max_age_hours", 24),
        "shadow": control.get("shadow_max_age_hours", 168),
        "learn": control.get("learn_max_age_hours", 720),
    }


def _build_analytics_services(config: dict[str, Any]) -> dict[str, Callable[[], Awaitable[None]]]:
    """One guarded coroutine per service; cron, startup, and health-check share these.

    A per-service asyncio.Lock ensures a service never runs concurrently with
    itself; on success each records its run time so overdue checks stay honest.
    """
    from lab import jobs as analytics
    from lab.schedule_state import record_job_run

    locks = {name: asyncio.Lock() for name in SERVICE_NAMES}

    async def _run(name: str, body: Callable[[], Awaitable[None]]) -> None:
        if is_paused(config):
            return
        if locks[name].locked():
            log.info("analytics service already running -- skipping",
                     extra={"ctx": {"service": name}})
            return
        async with locks[name]:
            try:
                await body()
            except Exception:
                log.exception("analytics service failed", extra={"ctx": {"service": name}})
                return
            await asyncio.to_thread(record_job_run, config, name)

    async def run_forecast_service() -> None:
        async def body() -> None:
            await asyncio.to_thread(analytics.run_forecast_job, config)
            await asyncio.to_thread(analytics.run_eval_job, config)
            await asyncio.to_thread(analytics.run_report_job, config)

        await _run("forecast", body)

    async def run_shadow_service() -> None:
        await _run("shadow", lambda: asyncio.to_thread(analytics.run_shadow_job, config))

    async def run_learn_service() -> None:
        await _run("learn", lambda: asyncio.to_thread(analytics.run_learn_job, config))

    return {
        "forecast": run_forecast_service,
        "shadow": run_shadow_service,
        "learn": run_learn_service,
    }


async def _run_overdue_services(
    config: dict[str, Any],
    actx: AnalyticsContext,
    skip: set[str] | None = None,
) -> list[str]:
    """Run each service immediately if its last success is past its control time.

    Returns the names of services that were started.
    """
    from lab.schedule_state import is_overdue, last_run_age_seconds

    skip = skip or set()
    started: list[str] = []
    for name, service in actx.services.items():
        if name in skip:
            continue
        age = await asyncio.to_thread(last_run_age_seconds, config, name)
        if is_overdue(age, actx.max_age_hours[name]):
            log.info("catch-up: running overdue service",
                     extra={"ctx": {"service": name, "age_seconds": age,
                                    "max_age_hours": actx.max_age_hours[name]}})
            await service()
            started.append(name)
    return started


def _register_analytics_jobs(scheduler: AsyncIOScheduler, config: dict[str, Any]) -> AnalyticsContext:
    """Schedule forecast/eval/report, shadow, and learn on cron triggers (UTC)."""
    sched = config.get("schedule", {})
    forecast_cron = sched.get("forecast_cron", "0 2 * * *")   # nightly 02:00
    shadow_cron = sched.get("shadow_cron", "0 3 * * 0")       # weekly Sun 03:00
    learn_cron = sched.get("learn_cron", "0 4 1 * *")         # monthly 1st 04:00

    services = _build_analytics_services(config)
    actx = AnalyticsContext(services=services, max_age_hours=_control_max_ages(config))

    scheduler.add_job(services["forecast"], CronTrigger.from_crontab(forecast_cron, timezone="UTC"),
                      id="nightly", max_instances=1, coalesce=True)
    scheduler.add_job(services["shadow"], CronTrigger.from_crontab(shadow_cron, timezone="UTC"),
                      id="weekly", max_instances=1, coalesce=True)
    scheduler.add_job(services["learn"], CronTrigger.from_crontab(learn_cron, timezone="UTC"),
                      id="monthly", max_instances=1, coalesce=True)
    log.info("analytics scheduled",
             extra={"ctx": {"nightly": forecast_cron, "weekly": shadow_cron,
                            "monthly": learn_cron, "control": actx.max_age_hours}})
    return actx


def _snapshot_stale_threshold_minutes(config: dict[str, Any]) -> float:
    """Safe margin before the liquid tier counts as stalled: the larger of 2x
    the snapshot cadence and the forecast freshness guard."""
    cadence = config["collect"]["snapshot_interval_minutes"]["liquid"]
    forecast_max = config.get("forecast", {}).get(
        "max_snapshot_age_minutes", {}).get("liquid", cadence * 2)
    return max(2 * cadence, forecast_max)


async def _check_collector_liveness(config: dict[str, Any], ctx: CollectContext) -> bool:
    """Verify the liquid tier is fresh; force a collection cycle if it has stalled.

    Returns True when a recovery cycle was triggered.
    """
    from lab.collect.status import gather_status
    from lab.schedule_state import is_snapshot_stale

    status = await asyncio.to_thread(gather_status, config)
    age_min = status.get("tiers", {}).get("liquid", {}).get("last_snapshot_age_min")
    threshold = _snapshot_stale_threshold_minutes(config)
    if is_snapshot_stale(age_min, threshold):
        log.warning("health-check: collector stalled -- forcing collection cycle",
                    extra={"ctx": {"liquid_snapshot_age_min": age_min,
                                   "threshold_min": threshold}})
        await ctx.jobs["sync"]()
        await ctx.jobs["snap_liquid"]()
        return True
    log.info("health-check: collector healthy",
             extra={"ctx": {"liquid_snapshot_age_min": age_min,
                            "threshold_min": threshold}})
    return False


def _enforce_instance_guard(config: dict[str, Any], role: str) -> dict[str, Any]:
    """Stand down outdated/redundant instances; never raise into the caller."""
    from lab import process_guard

    try:
        return process_guard.enforce(config, role)
    except Exception:
        log.exception("instance guard failed")
        return {}


def _register_health_check(
    scheduler: AsyncIOScheduler,
    config: dict[str, Any],
    ctx: CollectContext,
    actx: AnalyticsContext,
) -> None:
    """Hourly liveness check: restart overdue analytics services and a stalled
    collector immediately upon detection."""
    minutes = config.get("schedule", {}).get("health_check_interval_minutes", 60)

    async def health_check() -> None:
        if is_paused(config):
            return
        guard = await asyncio.to_thread(_enforce_instance_guard, config, "orchestrator")
        restarted = await _run_overdue_services(config, actx)
        collector_recovered = await _check_collector_liveness(config, ctx)
        log.info("health-check complete",
                 extra={"ctx": {"restarted_services": restarted,
                                "collector_recovered": collector_recovered,
                                "instances_stopped": guard.get("stopped", [])}})

    scheduler.add_job(health_check, "interval", minutes=minutes,
                      id="health_check", max_instances=1, coalesce=True)
    log.info("health-check scheduled", extra={"ctx": {"interval_minutes": minutes}})


async def run_orchestrator(config: dict[str, Any]) -> None:
    """One-button entry point: collector + scheduled analytics in one process."""
    guard = _enforce_instance_guard(config, "orchestrator")
    if guard.get("stopped"):
        log.info("orchestrator took over from prior instances",
                 extra={"ctx": {"stopped": guard["stopped"]}})

    scheduler = AsyncIOScheduler(timezone="UTC")
    ctx = register_collect_jobs(scheduler, config)
    actx = _register_analytics_jobs(scheduler, config)
    _register_health_check(scheduler, config, ctx, actx)
    scheduler.start()

    pid_path(config).write_text(str(os.getpid()), encoding="utf-8")
    _write_heartbeat(config)
    log.info("orchestrator started", extra={"ctx": {"pid": os.getpid()}})
    await _startup_collection_cycle(ctx)

    skip: set[str] = set()
    if config.get("schedule", {}).get("run_on_start", True):
        log.info("orchestrator startup analytics pass")
        await actx.services["forecast"]()
        skip.add("forecast")
    await _run_overdue_services(config, actx, skip=skip)

    try:
        while True:
            _write_heartbeat(config)
            await asyncio.sleep(60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.shutdown(wait=False)
        await ctx.aclose()
        try:
            pid_path(config).unlink(missing_ok=True)
        except OSError:
            pass
        log.info("orchestrator stopped")
