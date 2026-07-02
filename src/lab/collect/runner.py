"""Long-running collection process: APScheduler driving sync/snapshots/resolutions.

Every job checks the PAUSE kill file first, so polling halts within one cycle
of the file appearing (guardrail 8).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

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


def is_paused(config: dict[str, Any]) -> bool:
    if pause_file_path(config).exists():
        log.warning("PAUSE file present -- skipping cycle")
        return True
    return False


async def run_collect(config: dict[str, Any]) -> None:
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

    scheduler = AsyncIOScheduler(timezone="UTC")
    cadence = config["collect"]["snapshot_interval_minutes"]
    scheduler.add_job(job_sync, "interval",
                      minutes=config["universe"]["sync_interval_minutes"])
    scheduler.add_job(job_snap_liquid, "interval", minutes=cadence["liquid"])
    scheduler.add_job(job_snap_tail, "interval", minutes=cadence["tail"])
    scheduler.add_job(job_resolutions, "interval",
                      minutes=config["collect"]["resolution_poll_minutes"])
    scheduler.start()

    log.info("collector started; running startup cycle")
    await job_sync()
    await job_snap_liquid()
    await job_snap_tail()
    await job_resolutions()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.shutdown(wait=False)
        await gamma.aclose()
        await clob.aclose()
        conn.close()
        log.info("collector stopped")
