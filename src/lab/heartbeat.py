"""Phase 18: dead-man heartbeat -- an outbound ping to an external monitoring
endpoint (healthchecks.io-class), so the collector or nightly backup job dying
silently while the operator is away for weeks still gets caught (brief S11's
named worst failure). Our code only emits a ping; the external service does
the alerting -- not a notification bot (S12 carve-out).

HEARTBEAT_URL unset in .env => send_heartbeat() is a silent no-op, the same
convention every other optional external key in this project follows (FRED,
Metaculus, NewsAPI).
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


async def send_heartbeat(source: str) -> bool:
    """Ping HEARTBEAT_URL (env var) to signal `source` is alive.

    Returns True on a successful ping, False on a no-op (URL unset) or a
    failed ping. Never raises -- a dead/unreachable monitoring endpoint must
    not take down the collector or the backup job it's meant to be watching
    over (guardrail 9).
    """
    url = os.environ.get("HEARTBEAT_URL", "").strip()
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.get(url)
        return True
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # httpx.InvalidURL (e.g. a malformed HEARTBEAT_URL typo -- unbalanced
        # brackets, bad IDNA host) is NOT a subclass of httpx.HTTPError, so it
        # must be caught explicitly here too -- otherwise a bad env value
        # would violate this function's "never raises" contract and, via
        # jobs.run_publish_job's shared try block, could make a successful
        # backup get reported as a publish failure.
        log.warning("heartbeat ping failed", extra={"ctx": {"source": source, "error": str(exc)}})
        return False
