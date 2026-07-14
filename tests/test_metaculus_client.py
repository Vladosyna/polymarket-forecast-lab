"""_extract_probability's JSON path was cross-checked against Metaculus's own
open-source client (github.com/Metaculus/forecasting-tools) and live-verified
with a real API token (see api/metaculus.py module docstring) -- these
fixtures lock in that shape since live data for it was null on every question
tested (a basic account token's aggregations may not carry the numeric field)."""

from __future__ import annotations

from lab.api.metaculus import _extract_probability


def test_extracts_recency_weighted_center():
    raw = {"question": {"aggregations": {
        "recency_weighted": {"latest": {"centers": [0.42]}},
    }}}
    assert _extract_probability(raw) == 0.42


def test_falls_back_to_unweighted_when_recency_weighted_missing():
    raw = {"question": {"aggregations": {
        "recency_weighted": {"latest": None},
        "unweighted": {"latest": {"centers": [0.73]}},
    }}}
    assert _extract_probability(raw) == 0.73


def test_returns_none_when_latest_is_null():
    """The actual live shape observed for every question tested with a basic token."""
    raw = {"question": {"aggregations": {
        "recency_weighted": {"history": None, "latest": None, "score_data": None},
    }}}
    assert _extract_probability(raw) is None


def test_returns_none_when_aggregations_missing_entirely():
    assert _extract_probability({"question": {}}) is None
    assert _extract_probability({}) is None


def test_returns_none_for_group_of_questions_post():
    """A post with no top-level "question" key (group_of_questions/conditional
    shape, per the spec's GroupOfQuestions/Conditional schemas) abstains rather
    than guessing which sub-question's aggregations apply -- M7 pairing only
    supports plain binary questions."""
    raw = {"id": 17829, "group_of_questions": {"questions": [
        {"id": 17876, "aggregations": {"recency_weighted": {"latest": {"centers": [0.3]}}}},
    ]}}
    assert _extract_probability(raw) is None


def test_returns_none_for_conditional_post():
    raw = {"id": 1, "conditional": {
        "question_yes": {"aggregations": {"recency_weighted": {"latest": {"centers": [0.6]}}}},
    }}
    assert _extract_probability(raw) is None
