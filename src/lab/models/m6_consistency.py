"""M6 -- cross-market coherence scanner. Deterministic, no LLM.

Within a negRisk event the mutually exclusive YES prices must sum to ~1.
The scanner records every event's deviation and emits standalone forecasts
only for incoherent legs, pulling them toward the coherent joint solution
(price / sum). Pairwise logical links come from a curated links.yaml
(starts empty).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from lab.util import PROJECT_ROOT

log = logging.getLogger(__name__)

DEVIATION_THRESHOLD = 0.03   # |sum - 1| beyond this flags the event
LEG_MIN_SHIFT = 0.01         # emit a forecast only when the leg moves this much


def scan_negrisk_event(legs: list[dict[str, float | str]]) -> dict[str, Any]:
    """legs: [{condition_id, p_yes}, ...] for one negRisk event.

    Returns the deviation record and per-leg coherent prices when incoherent.
    """
    total = sum(float(l["p_yes"]) for l in legs)
    deviation = total - 1.0
    result: dict[str, Any] = {
        "n_legs": len(legs),
        "sum_p_yes": total,
        "deviation": deviation,
        "incoherent": abs(deviation) > DEVIATION_THRESHOLD and len(legs) >= 2,
        "corrections": [],
    }
    if result["incoherent"] and total > 0:
        for l in legs:
            p = float(l["p_yes"])
            coherent = p / total
            if abs(coherent - p) >= LEG_MIN_SHIFT:
                result["corrections"].append({
                    "condition_id": l["condition_id"],
                    "p_market": p,
                    "p_coherent": coherent,
                })
    return result


def load_links(path: Path | None = None) -> list[dict[str, Any]]:
    """Curated pairwise logical constraints; empty file at start."""
    p = path or PROJECT_ROOT / "links.yaml"
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("links", [])


def check_link(link: dict[str, Any], prices: dict[str, float]) -> dict[str, Any] | None:
    """Conditional-probability bound: P(narrow) <= P(broad) for implies-links.

    link: {kind: 'implies', narrow: condition_id, broad: condition_id}
    (if the narrow event happens, the broad one must too).
    """
    if link.get("kind") != "implies":
        return None
    p_narrow = prices.get(link["narrow"])
    p_broad = prices.get(link["broad"])
    if p_narrow is None or p_broad is None:
        return None
    violation = p_narrow - p_broad
    if violation <= DEVIATION_THRESHOLD:
        return None
    midpoint = (p_narrow + p_broad) / 2
    return {
        "link": link,
        "violation": violation,
        "corrections": [
            {"condition_id": link["narrow"], "p_market": p_narrow, "p_coherent": midpoint},
            {"condition_id": link["broad"], "p_market": p_broad, "p_coherent": midpoint},
        ],
    }


async def scan_universe(conn, store, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Group active negRisk markets into events via Gamma and scan each.

    Event grouping is fetched live (the markets table intentionally follows
    the brief's schema, which has no event column).
    """
    from lab.api.gamma import GammaClient
    from lab.api.http import TokenBucket
    from lab.store.snapshots import utc_date_str
    from datetime import timedelta
    from lab.util import now_utc

    now = now_utc()
    latest = store.latest_per_market([utc_date_str(now - timedelta(days=d)) for d in range(2)])
    if latest.is_empty():
        return []
    mid_by_cid = {r["condition_id"]: r["mid"] for r in latest.to_dicts()}

    bucket = TokenBucket(rate=config["collect"]["rate_limit"]["requests_per_second"],
                        burst=config["collect"]["rate_limit"]["burst"])
    gamma = GammaClient(bucket)
    findings: list[dict[str, Any]] = []
    try:
        raw = await gamma.get_json("/events", params={
            "limit": 100, "offset": 0, "active": "true", "closed": "false",
            "order": "volume", "ascending": "false",
        })
        events = raw if isinstance(raw, list) else raw.get("data", [])
        for ev in events:
            if not ev.get("negRisk"):
                continue
            legs = []
            for m in ev.get("markets", []):
                cid = m.get("conditionId")
                if cid in mid_by_cid and mid_by_cid[cid] is not None:
                    legs.append({"condition_id": cid, "p_yes": mid_by_cid[cid]})
            if len(legs) >= 2:
                scan = scan_negrisk_event(legs)
                scan["event_slug"] = ev.get("slug")
                findings.append(scan)
    finally:
        await gamma.aclose()

    prices = {cid: p for cid, p in mid_by_cid.items() if p is not None}
    for link in load_links():
        v = check_link(link, prices)
        if v:
            findings.append({"kind": "link_violation", **v})

    incoherent = [f for f in findings if f.get("incoherent") or f.get("violation")]
    log.info("m6 scan complete",
             extra={"ctx": {"events": len(findings), "incoherent": len(incoherent)}})
    return findings


def write_m6_forecasts(conn, store, findings: list[dict[str, Any]],
                       config: dict[str, Any]) -> int:
    """Append ledger rows for incoherent legs, pulled toward coherence."""
    from datetime import timedelta

    from lab.store import db as dbmod
    from lab.store.snapshots import utc_date_str
    from lab.util import now_utc

    now = now_utc()
    latest = store.latest_per_market([utc_date_str(now - timedelta(days=d)) for d in range(2)])
    snap = {r["condition_id"]: r for r in latest.to_dicts()} if not latest.is_empty() else {}
    ts = now.isoformat(timespec="seconds")
    lo, hi = config["forecast"]["p_clamp"]

    # §3's null-control restriction is a UNIVERSE policy, so it has to hold on
    # every write path, not just the one inside eligible_market_states. Until
    # 2026-08-22 it held only there, and this loop wrote on 62 sports markets
    # against a configured sample of 30.
    from lab.forecast import drop_null_control_outsiders

    allowed = drop_null_control_outsiders(
        conn, config,
        [c["condition_id"] for f in findings for c in f.get("corrections", [])])

    written = skipped_null_control = 0
    for finding in findings:
        for corr in finding.get("corrections", []):
            if corr["condition_id"] not in allowed:
                skipped_null_control += 1
                continue
            row = snap.get(corr["condition_id"])
            if row is None:
                continue
            dbmod.append_forecast(conn, {
                "ts": ts,
                "condition_id": corr["condition_id"],
                "model_id": "m6_consistency",
                "p_yes": min(hi, max(lo, corr["p_coherent"])),
                "p_market_at_ts": row["mid"],
                "spread_at_ts": row["spread"],
            })
            written += 1
    if skipped_null_control:
        log.info("m6: skipped sports markets outside the null-control sample",
                 extra={"ctx": {"count": skipped_null_control}})
    conn.commit()
    return written
