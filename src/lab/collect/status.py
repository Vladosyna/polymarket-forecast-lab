"""`lab status` -- data health: freshness, gaps, watcher lag, counts, spend."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl

from lab.store import db as dbmod
from lab.store.snapshots import SnapshotStore, utc_date_str
from lab.util import now_utc


def _dates_back(now: datetime, days: int) -> list[str]:
    return [utc_date_str(now - timedelta(days=d)) for d in range(days + 1)]


def snapshot_gaps(df: pl.DataFrame, tier_markets: list[str], cadence_minutes: int,
                  window_start: datetime, window_end: datetime) -> int:
    """Count cadence buckets in the window with zero snapshots for the tier.

    A bucket counts as covered when at least one tracked market has a row --
    per-market gap accounting would flag every market that IPOs mid-window.
    """
    if not tier_markets:
        return 0
    n_buckets = int((window_end - window_start).total_seconds() // (cadence_minutes * 60))
    if n_buckets <= 0:
        return 0
    subset = df.filter(pl.col("condition_id").is_in(tier_markets))
    if subset.is_empty():
        return n_buckets
    seen = set(subset.get_column("ts").unique().to_list())
    gaps = 0
    for i in range(n_buckets):
        bucket_start = window_start + timedelta(minutes=i * cadence_minutes)
        # Bucket timestamps are floored ISO strings; match by prefix window.
        bucket_end = bucket_start + timedelta(minutes=cadence_minutes)
        covered = any(
            bucket_start.isoformat(timespec="seconds") <= ts < bucket_end.isoformat(timespec="seconds")
            for ts in seen
        )
        if not covered:
            gaps += 1
    return gaps


def gather_status(config: dict[str, Any]) -> dict[str, Any]:
    now = now_utc()
    conn = dbmod.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    out: dict[str, Any] = {"ts": now.isoformat(timespec="seconds")}

    # Ledger and universe counts.
    out["markets_by_tier"] = {
        r["tier"]: r["n"]
        for r in conn.execute("SELECT tier, COUNT(*) AS n FROM markets GROUP BY tier")
    }
    out["forecast_rows"] = conn.execute("SELECT COUNT(*) AS n FROM forecasts").fetchone()["n"]
    out["resolutions"] = conn.execute("SELECT COUNT(*) AS n FROM resolutions").fetchone()["n"]

    # Snapshot freshness + gaps per tier.
    df7 = store.read_range(_dates_back(now, 7))
    cadence = config["collect"]["snapshot_interval_minutes"]
    out["tiers"] = {}
    for tier in ("liquid", "tail"):
        markets = [
            r["condition_id"]
            for r in conn.execute(
                "SELECT condition_id FROM markets WHERE tier = ? AND active = 1 AND closed = 0",
                (tier,),
            )
        ]
        tier_df = df7.filter(pl.col("condition_id").is_in(markets)) if markets else pl.DataFrame()
        if tier_df.is_empty():
            last_age_min = None
        else:
            last_ts = datetime.fromisoformat(tier_df.get_column("ts").max()).replace(
                tzinfo=timezone.utc
            )
            last_age_min = round((now - last_ts).total_seconds() / 60, 1)
        out["tiers"][tier] = {
            "tracked_markets": len(markets),
            "last_snapshot_age_min": last_age_min,
            "gaps_24h": snapshot_gaps(df7, markets, cadence[tier], now - timedelta(hours=24), now),
            "gaps_7d": snapshot_gaps(df7, markets, cadence[tier], now - timedelta(days=7), now),
        }

    # Resolution-watcher lag: closed markets still awaiting a resolution row.
    out["resolution_watcher"] = {
        "closed_unresolved": conn.execute(
            """
            SELECT COUNT(*) AS n FROM markets m
            LEFT JOIN resolutions r ON r.condition_id = m.condition_id
            WHERE m.closed = 1 AND r.condition_id IS NULL
            """
        ).fetchone()["n"]
    }

    # Today's LLM spend vs cap.
    today = utc_date_str(now)
    out["llm_spend_today_usd"] = round(dbmod.llm_spend_today(conn, today), 4)
    out["llm_daily_cap_usd"] = config["llm"]["daily_cost_cap_usd"]

    conn.close()
    return out


def format_status(status: dict[str, Any]) -> str:
    lines = [
        f"lab status @ {status['ts']}",
        f"  markets by tier: {status['markets_by_tier'] or 'none'}",
        f"  forecast rows: {status['forecast_rows']}   resolutions: {status['resolutions']}",
    ]
    for tier, s in status["tiers"].items():
        age = s["last_snapshot_age_min"]
        lines.append(
            f"  [{tier}] tracked={s['tracked_markets']} "
            f"last_snapshot_age={age if age is not None else 'never'}min "
            f"gaps_24h={s['gaps_24h']} gaps_7d={s['gaps_7d']}"
        )
    lines.append(f"  resolution watcher: {status['resolution_watcher']['closed_unresolved']} closed markets unresolved")
    lines.append(f"  LLM spend today: ${status['llm_spend_today_usd']} / cap ${status['llm_daily_cap_usd']}")
    return "\n".join(lines)
