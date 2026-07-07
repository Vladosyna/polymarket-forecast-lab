"""M7 -- cross-venue signal on matched questions (brief section 6, Phase 9).

For a market with a confirmed external match, output the log-odds pool of the
*external* venues' probabilities only -- Polymarket's own price stays out (M0
already carries it; the ensemble learns how much to trust each source).
Deterministic at forecast time: no LLM call in this path. The LLM only
proposes candidate matches (`propose_matches`, used by `lab map propose`); a
human confirms every pair in data/markets_map.yaml before it goes live -- a
proposed-but-unconfirmed pair is never read by the forecasting path at all.

Like M6, this bypasses the per-market Forecaster.forecast() loop (it needs
async I/O against Kalshi/Metaculus) -- scan_confirmed_pairs() does the async
fetch, write_m7_forecasts() is the sync ledger writer, mirroring
m6_consistency.py's scan_universe()/write_m6_forecasts() split.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from lab.learn.refit import logit, sigmoid
from lab.models.base import ForecastResult, clamp_p
from lab.util import PROJECT_ROOT, now_utc_iso

log = logging.getLogger(__name__)

DEFAULT_MAP_PATH = PROJECT_ROOT / "data" / "markets_map.yaml"


def load_markets_map(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_MAP_PATH
    if not p.exists():
        return {"confirmed": [], "proposed": []}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("confirmed", [])
    data.setdefault("proposed", [])
    return data


def save_markets_map(data: dict[str, Any], path: Path | None = None) -> None:
    p = path or DEFAULT_MAP_PATH
    header = (
        "# Cross-venue question matching (M7, Phase 9). Propose-then-confirm:\n"
        "# `lab map propose` appends LLM candidates under `proposed`; a human\n"
        "# moves a pair into `confirmed` (via `lab map confirm`) to make it live.\n"
        "# M7 reads ONLY `confirmed`. This file is the source of truth.\n"
    )
    p.write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def confirmed_by_condition(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """condition_id -> list of confirmed {venue, external_id, ...} entries."""
    out: dict[str, list[dict[str, Any]]] = {}
    for entry in data.get("confirmed", []):
        out.setdefault(entry["condition_id"], []).append(entry)
    return out


def confirm_match(data: dict[str, Any], condition_id: str, venue: str,
                  external_id: str | None = None) -> bool:
    """Move a proposed entry into confirmed, or (external_id given) confirm a
    hand-curated pair directly -- e.g. Metaculus, which `propose` can't reach
    (see api/metaculus.py). Returns False if there's nothing to confirm.

    Idempotent: re-confirming the same (condition_id, venue) is a no-op.
    """
    already = any(e["condition_id"] == condition_id and e["venue"] == venue
                  for e in data.get("confirmed", []))
    if already:
        return True

    proposed = data.get("proposed", [])
    match_idx = next(
        (i for i, e in enumerate(proposed)
         if e["condition_id"] == condition_id and e["venue"] == venue
         and (external_id is None or e["external_id"] == external_id)),
        None,
    )
    if match_idx is not None:
        entry = proposed.pop(match_idx)
    elif external_id is not None:
        entry = {"condition_id": condition_id, "venue": venue, "external_id": external_id}
    else:
        return False

    entry.pop("rationale", None)
    entry.pop("confidence", None)
    entry.pop("proposed_ts", None)
    entry["confirmed_ts"] = now_utc_iso()
    data.setdefault("confirmed", []).append(entry)
    return True


def link_confirmed_event(conn, condition_id: str, venue: str, external_id: str) -> str:
    """Mint (or reuse) the event_id linking a Polymarket market to a confirmed
    external venue-market (brief section 5/Phase 10: "a confirmed match
    creates an event linking >=2 venue-markets"). Best-effort title from the
    Polymarket market's own question, if it's already synced."""
    from lab.store import db as dbmod

    external_cid = dbmod.venue_condition_id(venue, external_id)
    row = conn.execute(
        "SELECT question FROM markets WHERE condition_id = ?", (condition_id,)
    ).fetchone()
    title = row["question"] if row else None
    return dbmod.link_event(conn, condition_id, external_cid, title=title)


def _pair_horizon_bucket(end_date_iso: str | None, now: datetime) -> str | None:
    """Horizon bucket for the Polymarket side of a confirmed pair, used to pick
    which m1_hier_curves bucket recalibrates the Metaculus quote."""
    from lab.learn.refit import bucket_for_days

    if not end_date_iso:
        return None
    try:
        end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    days = max(0.0, (end - now).total_seconds() / 86400)
    return bucket_for_days(days)


def pool_log_odds(prices: list[float], a_eff: float = 1.0) -> float:
    """Deterministic log-odds average of external venue probabilities,
    optionally extremized by a correlation-discounted exponent (Phase 13,
    CLAUDE.md M4/M7 extremization). a_eff=1.0 (the default) is a bit-exact
    identity -- current pooling behavior, unchanged."""
    if not prices:
        raise ValueError("pool_log_odds requires at least one price")
    raw_logit = sum(logit(p) for p in prices) / len(prices)
    return float(sigmoid(a_eff * raw_logit))


async def scan_confirmed_pairs(conn, store, config: dict[str, Any],
                               markets_map_path: Path | None = None,
                               ) -> dict[str, ForecastResult]:
    """Async fetch: for every confirmed pair with a fresh own-price snapshot,
    pull each venue's current quote and pool them. Abstains per-market on a
    stale snapshot (guardrail 13) or when no venue returns a usable quote."""
    from lab.api.http import TokenBucket
    from lab.api.kalshi import KalshiClient
    from lab.api.metaculus import MetaculusClient
    from lab.learn.pooling import discount_extremization_exponent
    from lab.learn.refit import load_active_artifact
    from lab.models.m1_hier import apply_hier_curve
    from lab.store.snapshots import utc_date_str

    data = load_markets_map(markets_map_path)
    by_cid = confirmed_by_condition(data)
    if not by_cid:
        return {}

    now = datetime.now(timezone.utc)
    dates = [utc_date_str(now - timedelta(days=d)) for d in range(2)]
    latest = store.latest_per_market(dates)
    snap_by_cid = {r["condition_id"]: r for r in latest.to_dicts()} if not latest.is_empty() else {}
    market_by_cid = {
        r["condition_id"]: r
        for r in conn.execute(
            "SELECT condition_id, tier, end_date_iso FROM markets WHERE condition_id IN ({})".format(
                ",".join("?" for _ in by_cid)
            ),
            list(by_cid),
        )
    } if by_cid else {}
    max_age = config["forecast"]["max_snapshot_age_minutes"]
    hier_artifact = load_active_artifact(config, "m1_hier_curves")
    ext_artifact = load_active_artifact(config, "m7_extremization")
    ext_spec = (ext_artifact or {}).get("categories", {}).get("_all")

    bucket = TokenBucket(rate=config["collect"]["rate_limit"]["requests_per_second"],
                        burst=config["collect"]["rate_limit"]["burst"])
    kalshi = KalshiClient(bucket)
    metaculus = MetaculusClient(bucket)
    results: dict[str, ForecastResult] = {}
    try:
        for cid, pairs in by_cid.items():
            snap = snap_by_cid.get(cid)
            market = market_by_cid.get(cid)
            tier = market["tier"] if market else None
            if snap is None or snap["mid"] is None or tier is None:
                continue
            snap_ts = datetime.fromisoformat(snap["ts"])
            if snap_ts.tzinfo is None:
                snap_ts = snap_ts.replace(tzinfo=timezone.utc)
            age_min = (now - snap_ts).total_seconds() / 60
            if age_min > max_age.get(tier, max_age["tail"]):
                log.warning("m7: skipping stale-snapshot market",
                           extra={"ctx": {"condition_id": cid, "age_min": age_min}})
                continue
            horizon_bucket = _pair_horizon_bucket(market["end_date_iso"], now) if market else None

            quotes: list[dict[str, Any]] = []
            for pair in pairs:
                price = None
                recalibrated = False
                if pair["venue"] == "kalshi":
                    m = await kalshi.market(pair["external_id"])
                    price = m.yes_price if m else None
                elif pair["venue"] == "metaculus":
                    q = await metaculus.question(int(pair["external_id"]))
                    price = q.community_prediction if q else None
                    # M1.x input signal (Phase 12, CLAUDE.md M1.x): recalibrate
                    # the raw community prediction through the metaculus venue
                    # offset before pooling -- Metaculus is never a forecast
                    # target itself, only an M7 input. Falls back to the raw CP
                    # unchanged when no artifact/bucket fit exists yet.
                    if (price is not None and hier_artifact is not None and horizon_bucket is not None
                            and horizon_bucket in hier_artifact.get("buckets", {})):
                        price = apply_hier_curve(hier_artifact, "metaculus", horizon_bucket, price)
                        recalibrated = True
                if price is not None and 0 < price < 1:
                    quotes.append({
                        "venue": pair["venue"], "external_id": pair["external_id"],
                        "price": price, "fetched_ts": now_utc_iso(),
                        "recalibrated": recalibrated,
                    })
            if not quotes:
                continue
            # Phase 13: correlation-discounted extremization, using the ACTUAL
            # number of venues pooled for THIS market (n=len(quotes)), not the
            # frozen count from fit time -- n_eff wants today's real pool size.
            a_raw = ext_spec["a"] if ext_spec else 1.0
            rho_bar = ext_spec.get("rho_bar", 0.0) if ext_spec else 0.0
            a_eff = discount_extremization_exponent(a_raw, n=len(quotes), rho_bar=rho_bar)
            pooled = pool_log_odds([q["price"] for q in quotes], a_eff=a_eff)
            results[cid] = ForecastResult(
                p_yes=clamp_p(pooled),
                meta={"quotes": quotes, "n_pooled": len(quotes),
                      "extremization_a_eff": a_eff, "extremization_rho_bar": rho_bar},
            )
    finally:
        await kalshi.aclose()
        await metaculus.aclose()
    log.info("m7 scan complete", extra={"ctx": {"confirmed_pairs": len(by_cid),
                                                "forecasts": len(results)}})
    return results


def write_m7_forecasts(conn, store, results: dict[str, ForecastResult],
                       config: dict[str, Any]) -> int:
    """Append ledger rows for every market M7 produced a pooled quote for."""
    from datetime import timedelta

    from lab.store import db as dbmod
    from lab.store.snapshots import utc_date_str
    from lab.util import now_utc

    now = now_utc()
    latest = store.latest_per_market([utc_date_str(now - timedelta(days=d)) for d in range(2)])
    snap = {r["condition_id"]: r for r in latest.to_dicts()} if not latest.is_empty() else {}
    ts = now.isoformat(timespec="seconds")
    written = 0
    for cid, result in results.items():
        row = snap.get(cid)
        if row is None:
            continue
        dbmod.append_forecast(conn, {
            "ts": ts,
            "condition_id": cid,
            "model_id": "m7_crossvenue",
            "p_yes": result.p_yes,
            "p_market_at_ts": row["mid"],
            "spread_at_ts": row["spread"],
        })
        log.info("m7 forecast", extra={"ctx": {"condition_id": cid, "p_yes": result.p_yes,
                                                **result.meta}})
        written += 1
    conn.commit()
    return written


async def kalshi_propose_candidates(kalshi, config: dict[str, Any]) -> list[Any]:
    """Category-scoped Kalshi candidate pool for `lab map propose`.

    A bare `open_markets(limit=200)` (no filter) pulls whatever Kalshi
    considers "open" globally -- verified live to be dominated by garbled
    multi-leg sports/esports combo products (KXMVE... tickers), crowding out
    the handful of real Economics/Politics/Weather markets Kalshi actually
    lists. Reuses the SAME series_by_category -> markets_for_series flow
    collect/kalshi_collector.py already uses for universe sync, scoped to
    only the Kalshi category names whose categories.yaml mapping lands in
    our own priority_categories (brief section 3 P1-P3) -- everything else
    (Sports, Entertainment, World) is out of scope for matching against our
    economics/weather/politics priority markets and would just waste LLM
    calls on irrelevant candidates.
    """
    from lab.collect.categories import load_categories

    taxonomy = load_categories()
    priority = set(config["universe"]["priority_categories"])
    kalshi_categories = [k for k, v in taxonomy.get("kalshi_series", {}).items() if v in priority]
    max_series = config["venues"]["kalshi"].get("max_series_per_sync", 40)
    # Per-category share, same fix as the Polymarket-side selection below:
    # verified live that "Economics" alone has 601 series on Kalshi (vs 40
    # for the whole pass) -- a single global counter across categories meant
    # every prior live run in this session silently fetched ONLY Economics
    # series and never reached Weather/Politics/Elections/World/Entertainment
    # at all, despite the category filter itself working correctly.
    per_cat_series = max(1, max_series // len(kalshi_categories)) if kalshi_categories else 0

    candidates: list[Any] = []
    for kalshi_category in kalshi_categories:
        try:
            series_list = await kalshi.series_by_category(kalshi_category)
        except Exception:
            log.warning("m7 propose: series fetch failed",
                       extra={"ctx": {"category": kalshi_category}})
            continue
        series_seen = 0
        for s in series_list:
            if series_seen >= per_cat_series:
                break
            ticker = s.get("ticker")
            if not ticker:
                continue
            series_seen += 1
            try:
                candidates.extend(await kalshi.markets_for_series(ticker, status="open"))
            except Exception:
                log.warning("m7 propose: markets fetch failed",
                           extra={"ctx": {"series_ticker": ticker}})
    return candidates


PROPOSE_SYSTEM = """You match prediction-market questions across venues for a research pipeline.
Given ONE Polymarket question and a list of candidate Kalshi markets, identify
which candidates (if any) ask about the SAME real-world event with the SAME
resolution criteria -- not just a similar topic. Respond ONLY with a JSON
object: {"matches": [{"external_id": str, "confidence": float 0.0-1.0, "rationale": str}]}.
Return {"matches": []} if nothing qualifies. Be conservative: a wrong match is
worse than a missed one."""


def _propose_prompt(question: str, candidates: list[dict[str, str]]) -> str:
    lines = [f"POLYMARKET QUESTION: {question}", "", "CANDIDATE KALSHI MARKETS:"]
    for c in candidates:
        lines.append(f"- external_id={c['external_id']}: {c['title']}")
    return "\n".join(lines)


def propose_matches(conn, config: dict[str, Any], kalshi_candidates, llm,
                    markets_map_path: Path | None = None,
                    ) -> list[dict[str, Any]]:
    """LLM proposes candidate Kalshi matches for our top-K priority-category,
    liquid-tier markets. Metaculus is not reachable without an account (see
    api/metaculus.py) so `propose` only covers Kalshi; a human can still
    `lab map confirm` a hand-found Metaculus pair directly.

    Deterministic aggregation is not applicable here (there's no numeric
    signal to aggregate) -- the LLM's judgment on MATCH IDENTITY is the
    product itself; a human confirms every one before it's live, same as any
    other high-stakes LLM output in this codebase.
    """
    import json

    data = load_markets_map(markets_map_path)
    already = {(e["condition_id"], e["venue"]) for e in data.get("confirmed", []) + data.get("proposed", [])}

    cats = config["universe"]["priority_categories"]
    top_k = int(config["cross_venue"]["propose_top_k"])
    # Per-category share, not one global ORDER BY volume_num DESC LIMIT top_k
    # across all priority categories combined -- verified live that a single
    # voluminous negRisk event (hundreds of "will [name] win 2028" legs) can
    # swamp every slot in a global top-K, leaving weather/economics/
    # geopolitics/entertainment zero representation regardless of how the
    # legs are ranked. An even per-category share guarantees every priority
    # category gets a fair shot at being proposed.
    per_cat = max(1, top_k // len(cats))
    # Distinct EVENTS, not raw rows: verified live that even within one
    # category, one negRisk event's legs (e.g. 5 mutually exclusive "Fed
    # hikes/cuts/holds after the July meeting" buckets) can fill the whole
    # per-category share by themselves, so per_cat slots go to 5 variants of
    # one question instead of covering distinct real-world topics. Oversample
    # by volume, then dedupe by event_id (fallback condition_id) in order.
    oversample = per_cat * 5
    rows: list[Any] = []
    for cat in cats:
        raw = conn.execute(
            """
            SELECT condition_id, question, event_id FROM markets
            WHERE tier = 'liquid' AND category = ? AND active = 1 AND closed = 0
            ORDER BY volume_num DESC LIMIT ?
            """,
            (cat, oversample),
        ).fetchall()
        seen_events: set[str] = set()
        for m in raw:
            key = m["event_id"] or m["condition_id"]
            if key in seen_events:
                continue
            seen_events.add(key)
            rows.append(m)
            if len(seen_events) >= per_cat:
                break

    proposals: list[dict[str, Any]] = []
    for m in rows:
        if (m["condition_id"], "kalshi") in already or not m["question"]:
            continue
        candidates = [{"external_id": k.ticker, "title": k.title or ""} for k in kalshi_candidates]
        if not candidates:
            continue
        text, _usage = llm.complete(
            PROPOSE_SYSTEM, _propose_prompt(m["question"], candidates), purpose="m7_propose",
        )
        try:
            parsed = json.loads(text.strip().strip("`").removeprefix("json"))
        except (json.JSONDecodeError, AttributeError):
            log.warning("m7: invalid propose JSON", extra={"ctx": {"condition_id": m["condition_id"]}})
            continue
        by_ext = {c["external_id"]: c["title"] for c in candidates}
        for match in parsed.get("matches", []):
            ext_id = match.get("external_id")
            if ext_id not in by_ext:
                continue
            proposals.append({
                "condition_id": m["condition_id"], "question": m["question"],
                "venue": "kalshi", "external_id": ext_id,
                "external_question": by_ext[ext_id],
                "rationale": match.get("rationale", ""),
                "confidence": float(match.get("confidence", 0.0)),
                "proposed_ts": now_utc_iso(),
            })
    data.setdefault("proposed", []).extend(proposals)
    save_markets_map(data, markets_map_path)
    return proposals
