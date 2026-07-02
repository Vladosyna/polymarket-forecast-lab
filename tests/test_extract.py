"""Strict-JSON evidence parsing and the M3 cost-cap behavior."""

from __future__ import annotations

import json

import pytest

from lab.news.extract import parse_evidence_json
from lab.news.providers import dedup_articles, market_query, Article

VALID = json.dumps([
    {"claim": "X announced Y", "direction": "for_yes", "strength": 2,
     "source_reliability": 3, "relevance": 0.9, "article_index": 0},
])


def test_parse_valid():
    items = parse_evidence_json(VALID)
    assert len(items) == 1
    assert items[0]["direction"] == "for_yes"


def test_parse_valid_with_code_fence():
    assert parse_evidence_json(f"```json\n{VALID}\n```") is not None


def test_parse_rejects_bad_direction():
    bad = json.dumps([{"claim": "c", "direction": "maybe", "strength": 2,
                       "source_reliability": 2, "relevance": 0.5}])
    assert parse_evidence_json(bad) is None


def test_parse_rejects_out_of_range():
    bad = json.dumps([{"claim": "c", "direction": "for_yes", "strength": 9,
                       "source_reliability": 2, "relevance": 0.5}])
    assert parse_evidence_json(bad) is None
    bad2 = json.dumps([{"claim": "c", "direction": "for_yes", "strength": 2,
                        "source_reliability": 2, "relevance": 1.5}])
    assert parse_evidence_json(bad2) is None


def test_parse_rejects_prose():
    assert parse_evidence_json("Here is the evidence: none found.") is None
    assert parse_evidence_json('{"not": "a list"}') is None


def test_market_query_drops_stopwords():
    q = market_query("Will the Fed cut rates by December 2026?")
    assert "Will" not in q.split()
    assert "Fed" in q


def test_dedup_by_url():
    a = Article(title="t1", url="http://x/1", source="s", published_ts=None)
    b = Article(title="t2", url="http://x/1", source="s", published_ts=None)
    c = Article(title="t3", url="http://x/2", source="s", published_ts=None)
    assert len(dedup_articles([a, b, c])) == 2


def test_parse_accepts_wrapped_object():
    wrapped = '{"items": ' + VALID + '}'
    assert parse_evidence_json(wrapped) is not None


def test_m3_model_id_deepseek(monkeypatch):
    from lab.news.extract import m3_model_id, resolve_llm_provider

    cfg = {"llm": {"provider": "deepseek", "api_key_env": "DEEPSEEK_API_KEY"}}
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert resolve_llm_provider(cfg) is None
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    assert resolve_llm_provider(cfg) == "deepseek"
    assert m3_model_id(cfg) == "m3_evidence@deepseek"

