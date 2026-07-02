"""Category classification from Gamma event tags."""

from __future__ import annotations

from lab.collect.universe import category_from_tags


def test_tag_mapping():
    assert category_from_tags(["soccer", "sports", "fifa-world-cup"]) == "sports"
    assert category_from_tags(["economy", "fed-rates"]) == "economics"
    assert category_from_tags(["politics", "us-election"]) == "politics"
    assert category_from_tags(["geopolitics", "ukraine"]) == "geopolitics"
    assert category_from_tags(["movies", "oscars"]) == "entertainment"
    assert category_from_tags(["weather"]) == "weather"
    assert category_from_tags(["bitcoin"]) == "crypto"
    assert category_from_tags([]) == "unknown"
    assert category_from_tags(["some-new-tag"]) == "unknown"


def test_exclusions_win_over_broader_tags():
    # A crypto event also tagged 'economy' must classify as crypto (excluded).
    assert category_from_tags(["crypto", "economy"]) == "crypto"
