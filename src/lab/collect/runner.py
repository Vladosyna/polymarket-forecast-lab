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
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from lab.api.clob import ClobClient
from lab.api.gamma import GammaClient
from lab.api.http import TokenBucket
from lab.api.kalshi import KalshiClient
from lab.api.manifold import ManifoldClient
from lab.api.metaculus import MetaculusClient
from lab.collect.kalshi_collector import (
    snapshot_kalshi,
    snapshot_kalshi_markets,
    sync_kalshi_universe,
    tracked_kalshi_markets_by_ids,
    watch_kalshi_resolutions,
)
from lab.collect.manifold_collector import sync_manifold_markets
from lab.collect.metaculus_collector import snapshot_metaculus, watch_metaculus_resolutions
from lab.collect.resolutions import watch_resolutions
from lab.collect.snapshots import snapshot_markets, snapshot_tier, tracked_markets_by_ids
from lab.collect.universe import sync_universe
from lab.heartbeat import send_heartbeat
from lab.store import db
from lab.store.snapshots import SnapshotStore, floor_ts_bucket
from lab.util import PROJECT_ROOT, now_utc, now_utc_iso

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


async def snapshot_matched_pairs(clob: ClobClient, kalshi: KalshiClient, conn, store: SnapshotStore,
                                config: dict[str, Any], markets_map_data: dict[str, Any] | None = None,
                                ) -> dict[str, int]:
    """Phase 17 item 3: confirmed cross-venue pairs only, at a much tighter
    cadence than the tier-wide loops -- the lead-lag hypothesis (PAP H3) is
    underpowered on a 5-min grid, and finer history can't be captured
    retroactively. Metaculus pairs get Polymarket-side HF only -- its
    community-prediction poll isn't an order-book snapshot and has no
    per-market fetch to reuse here. `markets_map_data` lets callers (tests)
    inject a fixture map directly instead of reading data/markets_map.yaml.
    """
    from lab.models.m7_crossvenue import load_markets_map

    data = markets_map_data if markets_map_data is not None else load_markets_map()
    confirmed = data.get("confirmed", [])
    counts = {"poly_written": 0, "kalshi_written": 0}
    if not confirmed:
        return counts

    poly_ids = sorted({e["condition_id"] for e in confirmed})
    kalshi_ids = sorted({
        db.venue_condition_id("kalshi", e["external_id"])
        for e in confirmed if e.get("venue") == "kalshi"
    })

    bucket_minutes = config["cross_venue"]["hf_snapshot_interval_minutes"]
    ts_bucket = floor_ts_bucket(now_utc(), bucket_minutes)
    depth_levels = config["collect"].get("book_depth_levels", 10)

    poly_markets = tracked_markets_by_ids(conn, poly_ids)
    if poly_markets:
        counts["poly_written"] = await snapshot_markets(clob, store, poly_markets, ts_bucket, depth_levels)

    if kalshi_ids:
        kalshi_markets = tracked_kalshi_markets_by_ids(conn, kalshi_ids)
        if kalshi_markets:
            counts["kalshi_written"] = await snapshot_kalshi_markets(kalshi, store, kalshi_markets, ts_bucket)

    log.info("matched-pair HF snapshot done", extra={"ctx": {
        "pairs": len(confirmed), "poly_markets": len(poly_markets) if poly_ids else 0,
        **counts,
    }})
    return counts


@dataclass
class CollectContext:
    """Owns the shared clients/DB connection and the collection job callables."""

    gamma: GammaClient
    clob: ClobClient
    conn: Any
    store: SnapshotStore
    jobs: dict[str, Callable[[], Awaitable[None]]]
    # Phase 10: one client per external venue, each on its own TokenBucket
    # (per-host rate limiters, not shared with Polymarket's bucket object).
    kalshi: KalshiClient | None = None
    metaculus: MetaculusClient | None = None
    manifold: ManifoldClient | None = None

    async def aclose(self) -> None:
        await self.gamma.aclose()
        await self.clob.aclose()
        for client in (self.kalshi, self.metaculus, self.manifold):
            if client is not None:
                await client.aclose()
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
            await sync_universe(gamma, conn, store, config)

    async def job_snap_liquid() -> None:
        if not is_paused(config):
            await snapshot_tier(clob, conn, store, "liquid", config)

    async def job_snap_tail() -> None:
        if not is_paused(config):
            await snapshot_tier(clob, conn, store, "tail", config)

    async def job_resolutions() -> None:
        if not is_paused(config):
            await watch_resolutions(
                gamma, conn, limit=config["collect"].get("resolution_backlog_limit", 200)
            )

    cadence = config["collect"]["snapshot_interval_minutes"]
    scheduler.add_job(job_sync, "interval",
                      minutes=config["universe"]["sync_interval_minutes"])
    scheduler.add_job(job_snap_liquid, "interval", minutes=cadence["liquid"])
    scheduler.add_job(job_snap_tail, "interval", minutes=cadence["tail"])
    scheduler.add_job(job_resolutions, "interval",
                      minutes=config["collect"]["resolution_poll_minutes"])

    # Phase 18: unconditional, unlike the 4 jobs above -- deliberately does NOT
    # check is_paused(). The heartbeat's whole point is proving the process/
    # event-loop itself is alive; if it went silent only because the operator
    # deliberately dropped data/PAUSE for maintenance, the operator should NOT
    # get a false "collector is dead" alert. So it pings every tick, PAUSE or not.
    async def job_heartbeat_ping() -> None:
        await send_heartbeat("collector")

    scheduler.add_job(job_heartbeat_ping, "interval",
                      minutes=config.get("ops", {}).get("heartbeat_interval_minutes", 5),
                      id="heartbeat_ping", max_instances=1, coalesce=True)

    # --- Phase 10: external-venue collectors, each on its own TokenBucket ---
    venues_cfg = config.get("venues", {})

    kalshi_bucket = TokenBucket(rate=venues_cfg["kalshi"]["rate_limit"]["requests_per_second"],
                               burst=venues_cfg["kalshi"]["rate_limit"]["burst"])
    kalshi = KalshiClient(kalshi_bucket)

    metaculus_bucket = TokenBucket(rate=venues_cfg["metaculus"]["rate_limit"]["requests_per_second"],
                                   burst=venues_cfg["metaculus"]["rate_limit"]["burst"])
    metaculus = MetaculusClient(metaculus_bucket)

    manifold_bucket = TokenBucket(rate=venues_cfg["manifold"]["rate_limit"]["requests_per_second"],
                                  burst=venues_cfg["manifold"]["rate_limit"]["burst"])
    manifold = ManifoldClient(manifold_bucket)

    async def job_kalshi_sync() -> None:
        if not is_paused(config):
            await sync_kalshi_universe(kalshi, conn, config)

    async def job_kalshi_snapshot() -> None:
        if not is_paused(config):
            await snapshot_kalshi(kalshi, conn, store, config)

    async def job_kalshi_resolutions() -> None:
        if not is_paused(config):
            await watch_kalshi_resolutions(kalshi, conn)

    async def job_metaculus_snapshot() -> None:
        if not is_paused(config):
            await snapshot_metaculus(metaculus, conn, store, config)

    async def job_metaculus_resolutions() -> None:
        if not is_paused(config):
            await watch_metaculus_resolutions(metaculus, conn)

    async def job_manifold_sync() -> None:
        if not is_paused(config):
            await sync_manifold_markets(manifold, conn, config)

    async def job_snap_matched() -> None:
        if not is_paused(config):
            await snapshot_matched_pairs(clob, kalshi, conn, store, config)

    scheduler.add_job(job_kalshi_sync, "interval",
                      minutes=venues_cfg["kalshi"]["sync_interval_minutes"])
    scheduler.add_job(job_kalshi_snapshot, "interval",
                      minutes=venues_cfg["kalshi"]["snapshot_interval_minutes"])
    scheduler.add_job(job_kalshi_resolutions, "interval",
                      minutes=venues_cfg["kalshi"]["resolution_poll_minutes"])
    scheduler.add_job(job_metaculus_snapshot, "interval",
                      minutes=venues_cfg["metaculus"]["snapshot_interval_minutes"])
    scheduler.add_job(job_metaculus_resolutions, "interval",
                      minutes=venues_cfg["metaculus"]["resolution_poll_minutes"])
    scheduler.add_job(job_manifold_sync, "interval",
                      minutes=venues_cfg["manifold"]["sync_interval_minutes"])
    scheduler.add_job(job_snap_matched, "interval",
                      minutes=config["cross_venue"]["hf_snapshot_interval_minutes"],
                      max_instances=1, coalesce=True)

    return CollectContext(
        gamma=gamma, clob=clob, conn=conn, store=store,
        kalshi=kalshi, metaculus=metaculus, manifold=manifold,
        jobs={
            "sync": job_sync,
            "snap_liquid": job_snap_liquid,
            "snap_tail": job_snap_tail,
            "resolutions": job_resolutions,
            "kalshi_sync": job_kalshi_sync,
            "kalshi_snapshot": job_kalshi_snapshot,
            "kalshi_resolutions": job_kalshi_resolutions,
            "metaculus_snapshot": job_metaculus_snapshot,
            "metaculus_resolutions": job_metaculus_resolutions,
            "manifold_sync": job_manifold_sync,
            "snap_matched": job_snap_matched,
        },
    )


async def _startup_collection_cycle(ctx: CollectContext) -> None:
    log.info("collector startup cycle")
    await ctx.jobs["sync"]()
    await ctx.jobs["snap_liquid"]()
    await ctx.jobs["snap_tail"]()
    await ctx.jobs["resolutions"]()
    # Phase 10: external venues. Each is independently fail-soft (guardrail 9)
    # inside its own collector module -- one venue's outage never blocks
    # another's startup pass.
    for name in ("kalshi_sync", "kalshi_snapshot", "kalshi_resolutions",
                 "metaculus_snapshot", "metaculus_resolutions", "manifold_sync",
                 "snap_matched"):
        try:
            await ctx.jobs[name]()
        except Exception:
            log.exception("startup cycle: venue job failed", extra={"ctx": {"job": name}})


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


SERVICE_NAMES = ("forecast", "shadow", "learn", "map_propose")


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
        "map_propose": control.get("map_propose_max_age_hours", 168),
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
        # Outside _run: run_publish_job never raises, and its own success/failure
        # must not gate the forecast/eval/report age bookkeeping above -- a stalled
        # git push should not re-trigger (and re-bill) the whole bundle hourly.
        await asyncio.to_thread(analytics.run_publish_job, config)
        # Same reasoning: a stalled ledger-commitment push targets a different
        # repo (this one, not the results mirror) but must equally never gate
        # or re-trigger the bundle above (Phase 15).
        await asyncio.to_thread(analytics.run_ledger_commitment_job, config)

    async def run_shadow_service() -> None:
        await _run("shadow", lambda: asyncio.to_thread(analytics.run_shadow_job, config))

    async def run_learn_service() -> None:
        await _run("learn", lambda: asyncio.to_thread(analytics.run_learn_job, config))

    async def run_map_propose_service() -> None:
        await _run("map_propose", lambda: asyncio.to_thread(analytics.run_map_propose_job, config))

    return {
        "forecast": run_forecast_service,
        "shadow": run_shadow_service,
        "learn": run_learn_service,
        "map_propose": run_map_propose_service,
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
    map_propose_cron = config.get("cross_venue", {}).get(
        "propose_cron", "0 5 * * 1")                          # weekly Mon 05:00

    services = _build_analytics_services(config)
    actx = AnalyticsContext(services=services, max_age_hours=_control_max_ages(config))

    scheduler.add_job(services["forecast"], CronTrigger.from_crontab(forecast_cron, timezone="UTC"),
                      id="nightly", max_instances=1, coalesce=True)
    scheduler.add_job(services["shadow"], CronTrigger.from_crontab(shadow_cron, timezone="UTC"),
                      id="weekly", max_instances=1, coalesce=True)
    scheduler.add_job(services["learn"], CronTrigger.from_crontab(learn_cron, timezone="UTC"),
                      id="monthly", max_instances=1, coalesce=True)
    # M7: proposes candidate matches only -- never auto-confirms. A human still
    # has to run `lab map confirm` before a pair goes live (brief section 6/9).
    scheduler.add_job(services["map_propose"], CronTrigger.from_crontab(map_propose_cron, timezone="UTC"),
                      id="map_propose_weekly", max_instances=1, coalesce=True)
    log.info("analytics scheduled",
             extra={"ctx": {"nightly": forecast_cron, "weekly": shadow_cron,
                            "monthly": learn_cron, "map_propose": map_propose_cron,
                            "control": actx.max_age_hours}})
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


def _checkpoint(config: dict[str, Any], marker: str) -> None:
    """Unbuffered startup progress marker (survives a hard crash for diagnosis)."""
    try:
        p = pid_path(config).with_name("orchestrator.startup")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{now_utc_iso()} {marker}\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def _loop_exception_handler(config: dict[str, Any], loop: asyncio.AbstractEventLoop,
                             context: dict[str, Any]) -> None:
    """Guaranteed last stop for exceptions asyncio would otherwise only print to
    stderr (e.g. raised inside a callback or a fire-and-forget task with no one
    awaiting it) -- these previously left orchestrator crashes with zero trace
    in data/logs/. Falls back to asyncio's own default handler after logging."""
    exc = context.get("exception")
    log.critical("unhandled asyncio exception -- orchestrator may be about to die",
                 extra={"ctx": {"message": context.get("message")}}, exc_info=exc)
    loop.default_exception_handler(context)


def _install_signal_handlers(config: dict[str, Any]) -> None:
    """Log *why* the process is stopping before it stops.

    A future crash with no corresponding "received stop signal" line here,
    but a heartbeat that still went stale, means something killed the process
    outright (SIGKILL / TerminateProcess) rather than asking it to shut down
    -- that distinction is the whole point, since a hard kill can never be
    intercepted by any handler, Python or otherwise.
    """
    def _handler(signum, _frame):
        log.warning("received stop signal", extra={"ctx": {"signal": signum}})

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):  # SIGBREAK is Windows-only (Ctrl+Break)
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # e.g. not the main thread, or unsupported on this platform


async def run_orchestrator(config: dict[str, Any]) -> None:
    """One-button entry point: collector + scheduled analytics in one process."""
    _checkpoint(config, "A: entered run_orchestrator")
    _install_signal_handlers(config)
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: _loop_exception_handler(config, loop, context))
    guard = _enforce_instance_guard(config, "orchestrator")
    _checkpoint(config, f"B: enforce done, stopped={guard.get('stopped')}")
    if guard.get("stopped"):
        log.info("orchestrator took over from prior instances",
                 extra={"ctx": {"stopped": guard["stopped"]}})

    scheduler = AsyncIOScheduler(timezone="UTC")
    ctx = register_collect_jobs(scheduler, config)
    actx = _register_analytics_jobs(scheduler, config)
    _register_health_check(scheduler, config, ctx, actx)
    scheduler.start()
    _checkpoint(config, "C: scheduler started")

    pid_path(config).write_text(str(os.getpid()), encoding="utf-8")
    _write_heartbeat(config)
    log.info("orchestrator started", extra={"ctx": {"pid": os.getpid()}})
    _checkpoint(config, "D: pid+heartbeat written, entering startup cycle")
    await _startup_collection_cycle(ctx)
    _checkpoint(config, "E: startup collection cycle done")

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


def run_watchdog(config: dict[str, Any]) -> None:
    """Supervise `lab run` as a child process: on any exit, wait
    `watchdog.restart_delay_seconds` (default 10 min) and relaunch it.

    Runs until the watchdog itself is interrupted (Ctrl+C / SIGTERM), at which
    point its child is terminated too rather than left orphaned. The delay is
    deliberate (see config.yaml) -- this is a supervisor, not a tight retry loop.

    Stands down any other live `lab watchdog` instance on startup (same guard
    the orchestrator uses on itself): two supervisors each restarting their own
    orchestrator child fight over which child survives via process_guard,
    producing a permanent crash-restart loop with no forward progress.
    """
    guard = _enforce_instance_guard(config, "watchdog")
    if guard.get("stopped"):
        log.info("watchdog took over from prior watchdog instances",
                 extra={"ctx": {"stopped": guard["stopped"]}})
    delay = config.get("watchdog", {}).get("restart_delay_seconds", 600)
    cmd = [sys.executable, "-m", "lab", "run"]
    attempt = 0
    while True:
        attempt += 1
        log.info("watchdog: starting orchestrator", extra={"ctx": {"attempt": attempt}})
        proc = subprocess.Popen(cmd)
        try:
            code = proc.wait()
        except (KeyboardInterrupt, SystemExit):
            log.info("watchdog: stopping -- terminating orchestrator child")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise
        log.warning("watchdog: orchestrator exited -- will restart after delay",
                   extra={"ctx": {"exit_code": code, "attempt": attempt, "delay_seconds": delay}})
        try:
            time.sleep(delay)
        except (KeyboardInterrupt, SystemExit):
            log.info("watchdog: stopping during restart delay")
            raise
