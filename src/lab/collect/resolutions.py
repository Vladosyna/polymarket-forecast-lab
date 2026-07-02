"""Resolution watcher: polls closed markets and records FINAL payouts.

Finality assumption (brief section 3: record final payout, not first report):
Gamma reports a market as finally resolved when it is `closed` and the YES
outcome price collapses to exactly 0 or 1. Markets whose UMA status mentions
a dispute are recorded with the disputed flag once they do reach a final
payout, and skipped while the dispute is open. Writes are idempotent
(at-least-once safe).
"""

from __future__ import annotations

import logging

from lab.api.gamma import GammaClient, GammaMarket
from lab.store import db
from lab.util import now_utc_iso

log = logging.getLogger(__name__)


def unresolved_closed_markets(conn, limit: int = 200) -> list[str]:
    rows = conn.execute(
        """
        SELECT m.condition_id FROM markets m
        LEFT JOIN resolutions r ON r.condition_id = m.condition_id
        WHERE r.condition_id IS NULL
          AND (m.closed = 1 OR (m.end_date_iso IS NOT NULL AND m.end_date_iso < ?))
        LIMIT ?
        """,
        (now_utc_iso(), limit),
    ).fetchall()
    return [r["condition_id"] for r in rows]


def extract_final_payout(m: GammaMarket) -> tuple[float, bool] | None:
    """Return (payout_yes, disputed) if the market has a final payout, else None."""
    if not m.closed or len(m.outcome_prices) < 2:
        return None
    try:
        p_yes = float(m.outcome_prices[0])
    except ValueError:
        return None
    if p_yes not in (0.0, 1.0):
        return None
    statuses = " ".join(m.uma_resolution_statuses).lower()
    if "disputed" in statuses and "resolved" not in statuses:
        return None  # dispute still open -- wait for the final report
    return p_yes, "disputed" in statuses


async def watch_resolutions(gamma: GammaClient, conn) -> int:
    """One poll round. Returns number of resolutions recorded."""
    recorded = 0
    for condition_id in unresolved_closed_markets(conn):
        try:
            m = await gamma.market_by_condition(condition_id)
        except Exception:
            log.warning("resolutions: gamma fetch failed",
                        extra={"ctx": {"condition_id": condition_id}})
            continue
        if m is None:
            continue
        # Keep closed flag in sync so the market leaves the snapshot loop.
        if m.closed:
            conn.execute(
                "UPDATE markets SET closed = 1, active = ?, last_synced_ts = ? WHERE condition_id = ?",
                (int(bool(m.active)), now_utc_iso(), condition_id),
            )
        final = extract_final_payout(m)
        if final is None:
            continue
        payout_yes, disputed = final
        db.record_resolution(
            conn, condition_id,
            resolved_ts=now_utc_iso(),
            payout_yes=payout_yes,
            disputed=disputed,
            source="gamma",
        )
        recorded += 1
    conn.commit()
    if recorded:
        log.info("resolutions recorded", extra={"ctx": {"count": recorded}})
    return recorded
