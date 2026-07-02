"""LLM evidence extraction (strict JSON) + budget-guarded Anthropic calls.

The prompt includes the verbatim resolution criteria and instructs the model
to judge relevance to those criteria, not to the topic. Invalid JSON is
rejected and retried once; a second failure skips the article batch.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from lab.news.providers import Article
from lab.store import db as dbmod
from lab.store.snapshots import utc_date_str
from lab.util import now_utc

log = logging.getLogger(__name__)

# Anthropic API pricing (USD per million tokens), Sonnet-class default.
# Config override not offered: update alongside the model id if it changes.
PRICE_IN_PER_MTOK = 3.0
PRICE_OUT_PER_MTOK = 15.0

EXTRACTION_SYSTEM = """You extract structured evidence from news articles for probability forecasting.
Respond ONLY with a JSON array, no prose. Each element:
{"claim": str, "direction": "for_yes"|"for_no"|"neutral", "strength": 1|2|3,
 "source_reliability": 1|2|3, "relevance": float 0.0-1.0, "article_index": int}
Judge relevance STRICTLY against the market's resolution criteria quoted in the
user message -- an article can be on-topic but irrelevant to the specific
resolution conditions; give such items low relevance. strength is how strongly
the claim, if true, moves the outcome. source_reliability reflects the outlet
and sourcing quality. Return [] if no evidence items qualify."""


def extraction_prompt(question: str, description: str | None, end_date_iso: str | None,
                      p_market: float, price_path_7d: list[float],
                      articles: list[Article]) -> str:
    lines = [
        f"MARKET QUESTION: {question}",
        f"RESOLUTION CRITERIA (verbatim): {description or 'NOT AVAILABLE'}",
        f"END DATE: {end_date_iso or 'unknown'}",
        f"CURRENT MARKET PRICE (YES): {p_market:.3f}",
        f"7-DAY PRICE PATH: {[round(p, 3) for p in price_path_7d]}",
        "",
        "ARTICLES:",
    ]
    for i, a in enumerate(articles):
        lines.append(f"[{i}] ({a.published_ts or 'no date'}) {a.title} -- {a.summary[:300]}")
    return "\n".join(lines)


class BudgetExceeded(RuntimeError):
    pass


class LlmClient:
    """Anthropic wrapper with a hard daily USD cap checked BEFORE each call."""

    def __init__(self, conn, config: dict[str, Any]) -> None:
        import anthropic

        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        self._conn = conn
        self._model = config["llm"]["model"]
        self._cap = float(config["llm"]["daily_cost_cap_usd"])

    @property
    def model(self) -> str:
        return self._model

    def spend_today(self) -> float:
        return dbmod.llm_spend_today(self._conn, utc_date_str(now_utc()))

    def complete(self, system: str, prompt: str, purpose: str,
                 max_tokens: int = 2000) -> tuple[str, dict[str, Any]]:
        spent = self.spend_today()
        if spent >= self._cap:
            raise BudgetExceeded(f"daily LLM cap reached: ${spent:.2f} >= ${self._cap:.2f}")
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        tokens_in = resp.usage.input_tokens
        tokens_out = resp.usage.output_tokens
        cost = (tokens_in * PRICE_IN_PER_MTOK + tokens_out * PRICE_OUT_PER_MTOK) / 1e6
        dbmod.record_llm_spend(self._conn, utc_date_str(now_utc()), purpose, cost)
        self._conn.commit()
        text = "".join(block.text for block in resp.content if block.type == "text")
        return text, {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost}


def parse_evidence_json(text: str) -> list[dict[str, Any]] | None:
    """Strict parse; returns None on any structural violation."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    items: list[dict[str, Any]] = []
    for el in data:
        if not isinstance(el, dict):
            return None
        if el.get("direction") not in ("for_yes", "for_no", "neutral"):
            return None
        try:
            item = {
                "claim": str(el["claim"]),
                "direction": el["direction"],
                "strength": int(el["strength"]),
                "source_reliability": int(el["source_reliability"]),
                "relevance": float(el["relevance"]),
                "article_index": int(el.get("article_index", -1)),
            }
        except (KeyError, TypeError, ValueError):
            return None
        if not (1 <= item["strength"] <= 3 and 1 <= item["source_reliability"] <= 3
                and 0.0 <= item["relevance"] <= 1.0):
            return None
        items.append(item)
    return items


def extract_evidence(llm: LlmClient, question: str, description: str | None,
                     end_date_iso: str | None, p_market: float,
                     price_path_7d: list[float], articles: list[Article],
                     ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One extraction round with a single strict-JSON retry."""
    prompt = extraction_prompt(question, description, end_date_iso, p_market,
                               price_path_7d, articles)
    usage_total = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    for attempt in range(2):
        text, usage = llm.complete(EXTRACTION_SYSTEM, prompt, purpose="m3_extraction")
        for key in usage_total:
            usage_total[key] += usage[key]
        items = parse_evidence_json(text)
        if items is not None:
            # Attach publish timestamps from the source articles.
            for item in items:
                idx = item.pop("article_index", -1)
                if 0 <= idx < len(articles):
                    item["published_ts"] = articles[idx].published_ts
                    item["url"] = articles[idx].url
                else:
                    item["published_ts"] = None
            return items, usage_total
        log.warning("m3: invalid extraction JSON", extra={"ctx": {"attempt": attempt}})
    return [], usage_total
