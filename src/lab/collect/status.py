"""`lab status` -- data health: freshness, gaps, watcher lag, counts, spend."""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl

from lab.store import db as dbmod
from lab.store.snapshots import SnapshotStore, utc_date_str
from lab.util import now_utc


def _dates_back(now: datetime, days: int) -> list[str]:
    return [utc_date_str(now - timedelta(days=d)) for d in range(days + 1)]


def gap_windows(df: pl.DataFrame, tier_markets: list[str], cadence_minutes: int,
                window_start: datetime, window_end: datetime) -> list[tuple[datetime, datetime]]:
    """Actual [bucket_start, bucket_end) intervals with zero snapshots for the
    tier (Phase 17 item 5). A bucket counts as covered when at least one
    tracked market has a row -- per-market gap accounting would flag every
    market that IPOs mid-window. Returning the intervals themselves (not just
    a count) lets callers -- e.g. eval/clv.py's gap-aware drift -- check
    whether a SPECIFIC window overlaps a recorded gap.
    """
    if not tier_markets:
        return []
    n_buckets = int((window_end - window_start).total_seconds() // (cadence_minutes * 60))
    if n_buckets <= 0:
        return []
    all_buckets = [
        (window_start + timedelta(minutes=i * cadence_minutes),
         window_start + timedelta(minutes=(i + 1) * cadence_minutes))
        for i in range(n_buckets)
    ]
    subset = df.filter(pl.col("condition_id").is_in(tier_markets))
    if subset.is_empty():
        return all_buckets
    # Sorted-list + bisect instead of a per-bucket linear scan over `seen`:
    # provably the same predicate (`exists ts: start <= ts < end` on identical
    # fixed-width ISO strings, where lexicographic order is chronological
    # order for this format) but O(n_buckets log n) instead of O(n_buckets *
    # n) -- measured 324-494x on real data, verified list-identical output to
    # the prior implementation (see tests/test_status.py). This was ~75% of a
    # report render's wall clock: the old version also recomputed both
    # bucket_start.isoformat() and bucket_end.isoformat() once per (bucket,
    # ts) pair rather than once per bucket.
    seen_sorted = sorted(subset.get_column("ts").unique().to_list())
    gaps: list[tuple[datetime, datetime]] = []
    for bucket_start, bucket_end in all_buckets:
        start_iso = bucket_start.isoformat(timespec="seconds")
        end_iso = bucket_end.isoformat(timespec="seconds")
        i = bisect_left(seen_sorted, start_iso)
        covered = i < len(seen_sorted) and seen_sorted[i] < end_iso
        if not covered:
            gaps.append((bucket_start, bucket_end))
    return gaps


def snapshot_gaps(df: pl.DataFrame, tier_markets: list[str], cadence_minutes: int,
                  window_start: datetime, window_end: datetime) -> int:
    """Count cadence buckets in the window with zero snapshots for the tier."""
    return len(gap_windows(df, tier_markets, cadence_minutes, window_start, window_end))


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

    # Snapshot freshness + gaps per tier. Only ts/condition_id/venue are ever
    # read off df7 here -- project to those so a week of history doesn't drag
    # the order-book JSON blobs into memory (see SnapshotStore.read_range).
    df7 = store.read_range(_dates_back(now, 7), columns=["ts", "condition_id", "venue"])
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

    # Resolution-watcher lag. `backlog` is the watcher's OWN working set, not
    # just the closed=1 subset: reporting only the latter (17k of a real 42k)
    # is part of why the 2026-07-25 scan stall hid for weeks -- the number on
    # the dashboard was not the number the watcher was working through.
    # `oldest_check_age_h` is the direct stall signal: with the round-robin
    # cursor it should stay near one full sweep, and grow without bound if the
    # watcher ever wedges again.
    from lab.collect.resolutions import resolution_backlog_size

    oldest = conn.execute(
        """
        SELECT MIN(m.resolution_checked_ts) AS t FROM markets m
        LEFT JOIN resolutions r ON r.condition_id = m.condition_id
        WHERE r.condition_id IS NULL AND m.resolution_checked_ts IS NOT NULL
          AND (m.closed = 1 OR (m.end_date_iso IS NOT NULL AND m.end_date_iso < ?))
        """,
        (now.isoformat(timespec="seconds"),),
    ).fetchone()["t"]
    unchecked = conn.execute(
        """
        SELECT COUNT(*) AS n FROM markets m
        LEFT JOIN resolutions r ON r.condition_id = m.condition_id
        WHERE r.condition_id IS NULL AND m.resolution_checked_ts IS NULL
          AND (m.closed = 1 OR (m.end_date_iso IS NOT NULL AND m.end_date_iso < ?))
        """,
        (now.isoformat(timespec="seconds"),),
    ).fetchone()["n"]
    out["resolution_watcher"] = {
        "closed_unresolved": conn.execute(
            """
            SELECT COUNT(*) AS n FROM markets m
            LEFT JOIN resolutions r ON r.condition_id = m.condition_id
            WHERE m.closed = 1 AND r.condition_id IS NULL
            """
        ).fetchone()["n"],
        "backlog": resolution_backlog_size(conn),
        "never_checked": unchecked,
        "oldest_check_age_h": (
            round((now - datetime.fromisoformat(oldest).replace(tzinfo=timezone.utc))
                  .total_seconds() / 3600, 1)
            if oldest else None
        ),
    }

    # Per-venue collector health (Phase 10). Kalshi/Metaculus run a snapshot
    # loop; Manifold deliberately does not (guardrail 16: markets+resolutions
    # only, never a price time series) -- no last_snapshot_age for it.
    out["venues"] = {}
    for venue in ("kalshi", "metaculus", "manifold"):
        markets_n = conn.execute(
            "SELECT COUNT(*) AS n FROM markets WHERE venue = ?", (venue,)
        ).fetchone()["n"]
        resolutions_n = conn.execute(
            """
            SELECT COUNT(*) AS n FROM resolutions r
            JOIN markets m ON m.condition_id = r.condition_id
            WHERE m.venue = ?
            """,
            (venue,),
        ).fetchone()["n"]
        closed_unresolved = conn.execute(
            """
            SELECT COUNT(*) AS n FROM markets m
            LEFT JOIN resolutions r ON r.condition_id = m.condition_id
            WHERE m.venue = ? AND m.closed = 1 AND r.condition_id IS NULL
            """,
            (venue,),
        ).fetchone()["n"]
        entry: dict[str, Any] = {
            "markets": markets_n, "resolutions": resolutions_n,
            "closed_unresolved": closed_unresolved,
        }
        if venue in ("kalshi", "metaculus"):
            vdf = df7.filter(pl.col("venue") == venue) if "venue" in df7.columns else pl.DataFrame()
            if vdf.is_empty():
                entry["last_snapshot_age_min"] = None
            else:
                last_ts = datetime.fromisoformat(vdf.get_column("ts").max()).replace(
                    tzinfo=timezone.utc
                )
                entry["last_snapshot_age_min"] = round((now - last_ts).total_seconds() / 60, 1)
        out["venues"][venue] = entry

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
        age_str = f"{age}min" if age is not None else "never"
        lines.append(
            f"  [{tier}] tracked={s['tracked_markets']} "
            f"last_snapshot_age={age_str} "
            f"gaps_24h={s['gaps_24h']} gaps_7d={s['gaps_7d']}"
        )
    rw = status["resolution_watcher"]
    lines.append(
        f"  resolution watcher: backlog={rw['backlog']} "
        f"(closed={rw['closed_unresolved']}, never_checked={rw['never_checked']}) "
        f"oldest_check={rw['oldest_check_age_h']}h"
    )
    for venue, v in status.get("venues", {}).items():
        if "last_snapshot_age_min" in v:
            age = v["last_snapshot_age_min"]
            age_str = f"{age}min" if age is not None else "never"
            lines.append(
                f"  [{venue}] markets={v['markets']} last_snapshot_age={age_str} "
                f"resolutions={v['resolutions']} closed_unresolved={v['closed_unresolved']}"
            )
        else:
            lines.append(
                f"  [{venue}] markets={v['markets']} resolutions={v['resolutions']} "
                f"closed_unresolved={v['closed_unresolved']} (no snapshot loop -- guardrail 16)"
            )
    lines.append(f"  LLM spend today: ${status['llm_spend_today_usd']} / cap ${status['llm_daily_cap_usd']}")
    return "\n".join(lines)
