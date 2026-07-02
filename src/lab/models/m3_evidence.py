"""M3 -- LLM evidence pipeline: dossier -> retrieval -> extraction -> aggregation.

Market selection is a deterministic rule (top-K by liquidity within priority
categories, liquid tier only) -- never editorial judgment (guardrail 12).
Every run is persisted to evidence_runs with a full trace.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from lab.models.base import ForecastResult, MarketState, clamp_p
from lab.news.aggregate import aggregate
from lab.news.extract import BudgetExceeded, LlmClient, extract_evidence
from lab.news.providers import Article, NewsProvider, gather_news
from lab.util import now_utc_iso

log = logging.getLogger(__name__)


def m3_target_ids(conn, config: dict[str, Any], store=None) -> list[str]:
    """Deterministic top-K by liquidity within priority categories, liquid tier.

    When a snapshot store is provided, candidates are restricted to markets whose
    latest mid price sits inside the forecast price bounds. Without this guard the
    highest-liquidity liquid markets are usually extreme-priced longshots (e.g.
    individual 2028 candidates at ~0.01) the forecast price-bound filter drops --
    leaving M3 with zero eligible targets and never invoking the LLM.
    """
    priority = config["universe"]["priority_categories"]
    placeholders = ",".join("?" for _ in priority)
    k = int(config["forecast"]["m3_top_k"])
    rows = conn.execute(
        f"""
        SELECT condition_id FROM markets
        WHERE tier = 'liquid' AND active = 1 AND closed = 0
          AND category IN ({placeholders})
        ORDER BY liquidity_num DESC, condition_id
        """,
        (*priority,),
    ).fetchall()
    if store is None:
        return [r["condition_id"] for r in rows[:k]]

    from datetime import timedelta

    from lab.store.snapshots import utc_date_str
    from lab.util import now_utc

    now = now_utc()
    latest = store.latest_per_market([utc_date_str(now - timedelta(days=d)) for d in range(2)])
    mids = ({r["condition_id"]: r["mid"] for r in latest.to_dicts()}
            if not latest.is_empty() else {})
    lo, hi = config["universe"]["forecast_price_bounds"]
    out: list[str] = []
    for r in rows:
        mid = mids.get(r["condition_id"])
        if mid is not None and lo < mid < hi:
            out.append(r["condition_id"])
            if len(out) >= k:
                break
    return out

class M3Evidence:
    model_id = "m3_evidence"

    def __init__(self, conn, llm: LlmClient, providers: list[NewsProvider],
                 config: dict[str, Any], target_ids: list[str],
                 price_paths: dict[str, list[float]] | None = None,
                 model_id: str | None = None) -> None:
        if model_id is not None:
            self.model_id = model_id
        self.conn = conn
        self.llm = llm
        self.providers = providers
        self.config = config
        self.target_ids = set(target_ids)
        self.price_paths = price_paths or {}

    def forecast(self, market: MarketState, context: dict[str, Any]) -> ForecastResult | None:
        if market.condition_id not in self.target_ids:
            return None
        ts = now_utc_iso()
        try:
            articles = gather_news(market.question or "", self.providers)
        except Exception:
            log.exception("m3: news retrieval failed",
                          extra={"ctx": {"condition_id": market.condition_id}})
            return None
        path_7d = self.price_paths.get(market.condition_id, [])
        try:
            items, usage = extract_evidence(
                self.llm, market.question or "", market.description,
                market.end_date_iso, market.p_market, path_7d, articles,
            )
        except BudgetExceeded:
            log.warning("m3: daily cost cap reached, skipping remaining markets",
                        extra={"ctx": {"condition_id": market.condition_id}})
            raise
        m3cfg = self.config["m3"]
        trace = aggregate(market.p_market, items, ts, k=m3cfg["k"],
                          tau_days=m3cfg["tau_days"], max_shift=m3cfg["max_shift_logodds"])
        dossier = {
            "forecast_ts": ts,
            "question": market.question,
            "resolution_criteria": market.description,
            "end_date_iso": market.end_date_iso,
            "p_market": market.p_market,
            "price_path_7d": path_7d,
            "articles": [
                {"title": a.title, "url": a.url, "source": a.source,
                 "published_ts": a.published_ts} for a in articles
            ],
            "evidence_items": items,
            "aggregation": trace,
        }
        cur = self.conn.execute(
            """INSERT INTO evidence_runs (ts, condition_id, dossier_json, llm_model,
                                          tokens_in, tokens_out, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ts, market.condition_id, json.dumps(dossier, ensure_ascii=False),
             self.llm.model, usage["tokens_in"], usage["tokens_out"], usage["cost_usd"]),
        )
        return ForecastResult(
            p_yes=clamp_p(trace["p_yes"]),
            meta={"n_articles": len(articles), "n_items": len(items),
                  "clipped_shift": trace["clipped_shift"]},
            cost_usd=usage["cost_usd"],
            evidence_run_id=cur.lastrowid,
        )


DIRECT_SYSTEM = """You are a carefully calibrated forecaster. Given a prediction-market
question, its verbatim resolution criteria, current price, and recent news, state your
probability that the market resolves YES. Consider base rates, the time remaining, and
the exact resolution wording. Respond ONLY with JSON: {"p_yes": float, "rationale": str}.
p_yes must be strictly between 0 and 1. Do not anchor blindly on the market price."""


class M3bDirect:
    """Optional experiment: the LLM states the probability directly."""

    model_id = "m3b_direct"

    def __init__(self, conn, llm: LlmClient, config: dict[str, Any],
                 target_ids: list[str]) -> None:
        self.conn = conn
        self.llm = llm
        self.config = config
        self.target_ids = set(target_ids)

    def forecast(self, market: MarketState, context: dict[str, Any]) -> ForecastResult | None:
        if market.condition_id not in self.target_ids:
            return None
        # Reuse the champion's latest stored dossier so retrieval/extraction
        # spend is shared (see brief on challengers consuming shared dossiers).
        row = self.conn.execute(
            "SELECT dossier_json FROM evidence_runs WHERE condition_id = ? ORDER BY ts DESC LIMIT 1",
            (market.condition_id,),
        ).fetchone()
        articles = json.loads(row["dossier_json"]).get("articles", []) if row else []
        prompt = (
            f"QUESTION: {market.question}\n"
            f"RESOLUTION CRITERIA: {market.description or 'NOT AVAILABLE'}\n"
            f"END DATE: {market.end_date_iso}\nCURRENT PRICE: {market.p_market:.3f}\n"
            "RECENT HEADLINES:\n"
            + "\n".join(f"- ({a.get('published_ts')}) {a.get('title')}" for a in articles[:15])
        )
        try:
            text, usage = self.llm.complete(DIRECT_SYSTEM, prompt, purpose="m3b_direct",
                                            max_tokens=500)
        except BudgetExceeded:
            raise
        try:
            payload = json.loads(text.strip().strip("`").removeprefix("json"))
            p = float(payload["p_yes"])
            assert 0.0 < p < 1.0
        except (json.JSONDecodeError, KeyError, ValueError, AssertionError):
            log.warning("m3b: invalid direct JSON",
                        extra={"ctx": {"condition_id": market.condition_id}})
            return None
        return ForecastResult(p_yes=clamp_p(p), meta={"source": "direct"},
                              cost_usd=usage["cost_usd"])
