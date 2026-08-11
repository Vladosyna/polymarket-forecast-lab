"""Forecast runner: builds MarketState per eligible market, runs every model,
freezes results in the append-only ledger.

Eligibility (universe policy + guardrails 12/13):
- tier liquid/tail, active, not closed, with a fresh snapshot (15/90 min);
- price inside forecast bounds (0.05, 0.95) -- extreme-priced markets stay in
  calibration stats via already-written rows but get no new forecasts;
- sports markets only if in the seeded null-control sample (cheap models);
- once per market per day per model, plus an extra pass when the 24h price
  move exceeds the trigger.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from lab.models.base import Forecaster, MarketState
from lab.store import db as dbmod
from lab.store.snapshots import SnapshotStore, utc_date_str
from lab.util import now_utc

log = logging.getLogger(__name__)


def null_control_ids(conn, config: dict[str, Any]) -> set[str]:
    """Seeded sample of *currently forecastable* sports markets (the null control).

    The eligibility filter here is load-bearing, and its absence was a silent
    failure of the whole control (found 2026-07-28). This sampled from every
    sports market ever seen -- 41k rows, ~80% of them already resolved and
    ~93% in the `ignored` tier -- while the forecast loop only ever considers
    `tier IN ('liquid','tail') AND active = 1 AND closed = 0`. Of the 30 ids
    drawn, 27 were already closed and 0 survived the loop's filter, so across
    the entire collection period the sports null control produced exactly zero
    Polymarket forecasts. The placebo that §4.7 of the write-up leans on to
    detect a broken harness was itself never running.

    Sampling the eligible pool means the cohort turns over as markets resolve
    and new ones list, which is why scoring must NOT try to reproduce it by
    re-drawing -- see null_control_ids_by_venue.
    """
    nc = config["universe"]["null_control"]
    rows = conn.execute(
        "SELECT condition_id FROM markets WHERE category = ? "
        "AND tier IN ('liquid','tail') AND active = 1 AND closed = 0 "
        "ORDER BY condition_id",
        (nc["category"],),
    ).fetchall()
    ids = [r["condition_id"] for r in rows]
    rng = random.Random(nc["random_seed"])
    return set(rng.sample(ids, min(nc["sample_size"], len(ids))))


def null_control_ids_by_venue(conn, config: dict[str, Any]) -> dict[str, set[str]]:
    """Per-venue null-control membership for scoring: the sports markets that
    were actually forecast.

    Deliberately not a re-draw of `null_control_ids`'s sample. That sample is
    over *currently eligible* markets, so it necessarily turns over -- a market
    forecast as a null control in July has closed by September and can no
    longer be drawn, yet its resolved forecasts are exactly the observations
    the control exists to score. Re-sampling at eval time would therefore both
    miss real null-control rows and admit markets never forecast at all.

    Reading membership off the ledger instead is exact by construction: a
    sports market carries forecasts only if it was in the eligible sample when
    they were written (or via M6's negRisk sweep, the documented §3.5
    exception, which is equally a placebo observation and belongs here too).
    It is also stable under re-runs, which re-sampling was not.

    Used for both directions in `run_eval`: excluding these from the primary
    per-category analysis, and selecting them for the null_control window.
    """
    nc = config["universe"]["null_control"]
    venues = [r["venue"] for r in conn.execute(
        "SELECT venue FROM venues WHERE forecastable = 1"
    )]
    out: dict[str, set[str]] = {}
    for venue in venues:
        rows = conn.execute(
            "SELECT DISTINCT m.condition_id FROM markets m "
            "JOIN forecasts f ON f.condition_id = m.condition_id "
            "WHERE m.category = ? AND m.venue = ?",
            (nc["category"], venue),
        ).fetchall()
        out[venue] = {r["condition_id"] for r in rows}
    return out


def _days_to_resolution(end_date_iso: str | None, now: datetime) -> float | None:
    if not end_date_iso:
        return None
    try:
        end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - now).total_seconds() / 86400)


def _depth_usd(snap: dict) -> float | None:
    """Top-of-book depth from the snapshot backing this forecast's price.

    None, not 0.0, when neither side was measured: a venue can quote with no
    size, and downstream a 0.0 asserts "measured, and there is none" -- a
    different claim from "not measured". Mirrors `_depth_lookup`'s contract.
    """
    bid, ask = snap.get("bid_depth_usd"), snap.get("ask_depth_usd")
    if bid is None and ask is None:
        return None
    return float(bid or 0.0) + float(ask or 0.0)


def _inputs_hash(model_id: str, meta: dict, config: dict[str, Any], snapshot_ts: str) -> str:
    payload = json.dumps(
        {
            "model_id": model_id,
            "artifact_version": meta.get("artifact_version"),
            "config": {k: config[k] for k in ("forecast", "m3") if k in config},
            "snapshot_ts": snapshot_ts,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def eligible_market_states(conn, store: SnapshotStore, config: dict[str, Any]) -> list[MarketState]:
    now = now_utc()
    dates = [utc_date_str(now - timedelta(days=d)) for d in range(2)]
    latest = store.latest_per_market(dates)
    if latest.is_empty():
        log.warning("forecast: no snapshots available")
        return []
    snap_by_cid = {r["condition_id"]: r for r in latest.to_dicts()}

    lo, hi = config["universe"]["forecast_price_bounds"]
    max_age = config["forecast"]["max_snapshot_age_minutes"]
    nc_ids = null_control_ids(conn, config)
    nc_category = config["universe"]["null_control"]["category"]

    states: list[MarketState] = []
    rows = conn.execute(
        """
        SELECT condition_id, question, category, description, end_date_iso, tier, venue,
               volume_24h_num
        FROM markets WHERE tier IN ('liquid','tail') AND active = 1 AND closed = 0
        """
    ).fetchall()
    skipped_stale = 0
    skipped_ended = 0
    for m in rows:
        if m["category"] == nc_category and m["condition_id"] not in nc_ids:
            continue
        # A market past its own end date is not forecastable: trading has
        # stopped, the outcome is determined, and the "market price" we would
        # pair against is a frozen last quote. Such rows are not weak
        # observations, they are near-degenerate ones -- both the model and the
        # baseline sit on the known answer, so the paired difference collapses
        # toward zero and dilutes the measurement while inflating n.
        #
        # `active`/`closed` should already have caught this, but they are only
        # as fresh as the last universe sync, and Kalshi's sync was starving its
        # own tail (see docs/OPERATIONS.md, 2026-08-10): 39,583 forecasts had
        # been written on already-ended Kalshi markets, 38% of that venue's
        # scoring population. This guard does not depend on sync timeliness.
        days_left = _days_to_resolution(m["end_date_iso"], now)
        if days_left is not None and days_left <= 0:
            skipped_ended += 1
            continue
        snap = snap_by_cid.get(m["condition_id"])
        if snap is None or snap["mid"] is None:
            continue
        snap_ts = datetime.fromisoformat(snap["ts"])
        if snap_ts.tzinfo is None:
            snap_ts = snap_ts.replace(tzinfo=timezone.utc)
        age_min = (now - snap_ts).total_seconds() / 60
        if age_min > max_age[m["tier"]]:
            skipped_stale += 1  # guardrail 13: never pair against a stale price
            continue
        if not (lo < snap["mid"] < hi):
            continue
        states.append(
            MarketState(
                condition_id=m["condition_id"],
                question=m["question"],
                category=m["category"],
                description=m["description"],
                end_date_iso=m["end_date_iso"],
                tier=m["tier"],
                p_market=snap["mid"],
                spread=snap["spread"],
                snapshot_ts=snap["ts"],
                days_to_resolution=_days_to_resolution(m["end_date_iso"], now),
                venue=m["venue"] or "polymarket",
                depth_usd=_depth_usd(snap),
                volume_24h=m["volume_24h_num"],
            )
        )
    if skipped_stale:
        log.warning("forecast: skipped markets with stale snapshots",
                    extra={"ctx": {"count": skipped_stale}})
    if skipped_ended:
        log.info("forecast: skipped markets already past their end date",
                 extra={"ctx": {"count": skipped_ended}})
    return states


def _due(conn, condition_id: str, model_id: str, config: dict[str, Any],
         price_move_24h: float | None) -> bool:
    row = conn.execute(
        "SELECT MAX(ts) AS last_ts FROM forecasts WHERE condition_id = ? AND model_id = ?",
        (condition_id, model_id),
    ).fetchone()
    if row["last_ts"] is None:
        return True
    last = datetime.fromisoformat(row["last_ts"])
    age_h = (now_utc() - last).total_seconds() / 3600
    if age_h >= config["forecast"]["cadence_hours"]:
        return True
    # The price-move trigger needs its own minimum spacing. It reads a 24-HOUR
    # move, so re-firing on the same move writes the same event repeatedly --
    # inert while the bundle runs once a day (age is ~24h by then), but during
    # the 2026-08-02..06 hourly crash loop it fired every hour and produced up
    # to 25 forecasts per market-day, 54,634 rows in five days. Those rows are
    # each individually valid (own ts, own paired price) and stay in the
    # append-only ledger, but they re-weight the scoring population toward
    # exactly the markets the trigger selects for -- volatile ones. See
    # docs/pre_analysis_plan.md addendum 9.5.
    min_gap_h = float(config["forecast"].get("price_move_min_hours", 6))
    return (
        price_move_24h is not None
        and age_h >= min_gap_h
        and abs(price_move_24h) > config["forecast"]["price_move_trigger"]
    )


def price_moves_24h(store: SnapshotStore, config: dict[str, Any]) -> dict[str, float]:
    """|mid now - mid ~24h ago| per market, from snapshot history."""
    now = now_utc()
    dates = [utc_date_str(now - timedelta(days=d)) for d in range(3)]
    # Same projection point as estimate_rho_bar_m7: only these three columns are
    # read below, and an unprojected read drags the order-book JSON blobs in
    # with them -- here inside the collector's own cgroup, every night.
    df = store.read_range(dates, columns=["ts", "condition_id", "mid"])
    if df.is_empty():
        return {}
    moves: dict[str, float] = {}
    cutoff = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    for cid, group in df.sort("ts").group_by("condition_id"):
        past = group.filter(group["ts"] <= cutoff)
        if past.is_empty():
            continue
        moves[cid[0]] = group["mid"][-1] - past["mid"][-1]
    return moves


def run_forecasts(conn, store: SnapshotStore, models: list[Forecaster],
                  config: dict[str, Any]) -> dict[str, int]:
    from lab.news.extract import BudgetExceeded

    states = eligible_market_states(conn, store, config)
    moves = price_moves_24h(store, config)
    counts = {"eligible_markets": len(states), "written": 0, "abstained": 0, "not_due": 0}
    ts = now_utc().isoformat(timespec="seconds")
    exhausted: set[str] = set()  # models past their daily budget
    for state in states:
        for model in models:
            if model.model_id in exhausted:
                continue
            if not _due(conn, state.condition_id, model.model_id, config,
                        moves.get(state.condition_id)):
                counts["not_due"] += 1
                continue
            try:
                result = model.forecast(state, {})
            except BudgetExceeded:
                log.warning("forecast: cost cap hit, disabling model for this run",
                            extra={"ctx": {"model": model.model_id}})
                exhausted.add(model.model_id)
                continue
            except Exception:
                log.exception("forecast: model failed",
                              extra={"ctx": {"model": model.model_id,
                                             "condition_id": state.condition_id}})
                continue
            if result is None:
                counts["abstained"] += 1
                continue
            dbmod.append_forecast(conn, {
                "ts": ts,
                "condition_id": state.condition_id,
                "model_id": model.model_id,
                "p_yes": result.p_yes,
                "p_market_at_ts": state.p_market,
                "spread_at_ts": state.spread,
                "inputs_hash": _inputs_hash(model.model_id, result.meta, config, state.snapshot_ts),
                "evidence_run_id": result.evidence_run_id,
                "cost_usd": result.cost_usd,
                "m3_randomized": result.m3_randomized,
                "m3_random_seed": result.m3_random_seed,
                # Phase 15 covariates, frozen with the forecast rather than
                # reconstructed later. hour_utc is derivable from ts and stored
                # anyway because the brief's schema names it; trades_24h stays
                # NULL -- neither venue returns a 24h trade count on the objects
                # the collector already fetches.
                "depth_covariate": state.depth_usd,
                "volume_24h": state.volume_24h,
                "hour_utc": datetime.fromisoformat(ts).hour,
                "trades_24h": None,
            })
            counts["written"] += 1
    conn.commit()
    log.info("forecast run complete", extra={"ctx": counts})
    return counts


def build_default_models(conn, config: dict[str, Any], store=None) -> list[Forecaster]:
    """M0 always; M1/M2 when their active artifacts exist; M3 when a key is set."""
    import os

    from lab.learn.refit import load_active_artifact
    from lab.models.m0_market import M0Market
    from lab.models.m1_debiased import M1Debiased
    from lab.models.m2_baserate import M2BaseRate

    models: list[Forecaster] = [M0Market()]
    m1_art = load_active_artifact(config, "m1_curves")
    if m1_art:
        models.append(M1Debiased(m1_art))
    else:
        log.warning("forecast: no m1_curves artifact; M1 disabled")
    m2_art = load_active_artifact(config, "m2_baserates")
    if m2_art:
        models.append(M2BaseRate(m2_art))
    else:
        log.warning("forecast: no m2_baserates artifact; M2 disabled")

    from lab.models.m1_hier import M1Hier

    m1h_art = load_active_artifact(config, "m1_hier_curves")
    if m1h_art:
        # Forecast in parallel with m1_debiased on both venues that reach the
        # eligible universe (Metaculus never does -- it's never tiered
        # liquid/tail; its offset is applied inside m7_crossvenue instead).
        # Phase 12 keeps these observable challengers, not auto-pooled into
        # M4 (see plan: the m3b_direct precedent -- POOLABLE never includes it).
        models.append(M1Hier(m1h_art, venue="polymarket"))
        models.append(M1Hier(m1h_art, venue="kalshi"))
    else:
        log.warning("forecast: no m1_hier_curves artifact; m1_hier disabled")

    from lab.models.m5_nowcast import M5Nowcast

    m5_art = load_active_artifact(config, "m5_macro_sd")
    models.append(M5Nowcast(macro_artifact=m5_art))  # abstains on markets its adapters don't cover

    from lab.news.extract import create_llm_client, m3_model_id

    llm = create_llm_client(conn, config)
    if llm:
        from lab.models.m3_evidence import M3Evidence, m3_boundary_randomized_ids, m3_target_ids
        from lab.news.providers import FeedListProvider, GoogleNewsRss

        providers = [GoogleNewsRss()]
        feeds = config.get("news", {}).get("rss_feeds", [])
        if feeds:
            providers.append(FeedListProvider(feeds))
        if config["forecast"].get("m3_boundary_randomization_enabled", False):
            target_ids, randomized_ids, seed = m3_boundary_randomized_ids(conn, config, store)
            models.append(M3Evidence(conn, llm, providers, config, target_ids,
                                     model_id=m3_model_id(config),
                                     randomized_ids=randomized_ids, random_seed=seed))
        else:
            target_ids = m3_target_ids(conn, config, store)
            randomized_ids = None
            models.append(M3Evidence(conn, llm, providers, config, target_ids,
                                     model_id=m3_model_id(config)))

        # M3b: the same LLM states a probability directly, on the SAME markets,
        # from the SAME stored dossier -- that pairing is the experiment
        # (brief section 6: "measure whether deterministic aggregation beats
        # direct LLM estimates -- a genuinely useful result either way").
        # Appended AFTER M3 so it reads the dossier M3 just wrote rather than
        # yesterday's. Never in POOLABLE: it is a comparison arm, not an
        # ensemble member.
        if config["forecast"].get("m3b_direct_enabled", False):
            from lab.models.m3_evidence import M3bDirect

            models.append(M3bDirect(conn, llm, config, target_ids))
    else:
        key_env = config.get("llm", {}).get("api_key_env", "ANTHROPIC_API_KEY")
        log.warning("forecast: no %s; M3 disabled", key_env)
    return models
