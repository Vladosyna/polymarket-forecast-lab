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
    """Deterministic seeded sample of sports markets (the null control)."""
    nc = config["universe"]["null_control"]
    rows = conn.execute(
        "SELECT condition_id FROM markets WHERE category = ? ORDER BY condition_id",
        (nc["category"],),
    ).fetchall()
    ids = [r["condition_id"] for r in rows]
    rng = random.Random(nc["random_seed"])
    return set(rng.sample(ids, min(nc["sample_size"], len(ids))))


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
        SELECT condition_id, question, category, description, end_date_iso, tier
        FROM markets WHERE tier IN ('liquid','tail') AND active = 1 AND closed = 0
        """
    ).fetchall()
    skipped_stale = 0
    for m in rows:
        if m["category"] == nc_category and m["condition_id"] not in nc_ids:
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
            )
        )
    if skipped_stale:
        log.warning("forecast: skipped markets with stale snapshots",
                    extra={"ctx": {"count": skipped_stale}})
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
    return (
        price_move_24h is not None
        and abs(price_move_24h) > config["forecast"]["price_move_trigger"]
    )


def price_moves_24h(store: SnapshotStore, config: dict[str, Any]) -> dict[str, float]:
    """|mid now - mid ~24h ago| per market, from snapshot history."""
    now = now_utc()
    dates = [utc_date_str(now - timedelta(days=d)) for d in range(3)]
    df = store.read_range(dates)
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

    from lab.models.m5_nowcast import M5Nowcast

    models.append(M5Nowcast())  # abstains on markets its adapters don't cover

    from lab.news.extract import create_llm_client, m3_model_id

    llm = create_llm_client(conn, config)
    if llm:
        from lab.models.m3_evidence import M3Evidence, m3_target_ids
        from lab.news.providers import FeedListProvider, GoogleNewsRss

        providers = [GoogleNewsRss()]
        feeds = config.get("news", {}).get("rss_feeds", [])
        if feeds:
            providers.append(FeedListProvider(feeds))
        models.append(M3Evidence(conn, llm, providers, config,
                                 m3_target_ids(conn, config, store),
                                 model_id=m3_model_id(config)))
    else:
        key_env = config.get("llm", {}).get("api_key_env", "ANTHROPIC_API_KEY")
        log.warning("forecast: no %s; M3 disabled", key_env)
    return models
