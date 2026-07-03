"""M5 adapters (fixture-driven) and M6 coherence scanner."""

from __future__ import annotations

import pytest

from lab.models.base import MarketState
from lab.models.m5_nowcast import M5Nowcast, MacroAdapter, parse_weather_question
from lab.models.m6_consistency import check_link, scan_negrisk_event


def _state(question, category="weather", p=0.5) -> MarketState:
    return MarketState(
        condition_id="0x1", question=question, category=category, description="d",
        end_date_iso="2026-07-05T00:00:00+00:00", tier="liquid", p_market=p,
        spread=0.02, snapshot_ts="2026-07-02T12:00:00+00:00", days_to_resolution=3.0,
    )


def test_parse_weather_question():
    spec = parse_weather_question("Will the highest temperature in NYC be above 90 degrees F?")
    assert spec == {"city": "nyc", "threshold": 90.0, "unit": "fahrenheit", "above": True}
    assert parse_weather_question("Will the Fed cut rates?") is None
    assert parse_weather_question("Will the highest temperature in Gotham be above 90?") is None


class StubWeatherAdapter:
    """Fixture adapter: 20 ensemble members, 15 exceed the threshold."""

    def covers(self, market):
        return "temperature" in (market.question or "")

    def probability(self, market):
        members = [88.0] * 5 + [92.0] * 15
        p = 15 / 20
        p = (p * 20 + 1) / 22
        return p, {"adapter": "weather", "n_members": 20, "members": members}


def test_m5_uses_adapter_and_stores_trace():
    m5 = M5Nowcast(adapters=[StubWeatherAdapter()])
    res = m5.forecast(_state("highest temperature in NYC above 90"), {})
    assert res is not None
    assert res.p_yes == pytest.approx((0.75 * 20 + 1) / 22)
    assert res.meta["n_members"] == 20        # the stored input trace
    assert "members" in res.meta


def test_m5_abstains_without_coverage():
    m5 = M5Nowcast(adapters=[StubWeatherAdapter()])
    assert m5.forecast(_state("Will X win the election?", category="politics"), {}) is None


def test_macro_adapter_uses_fred_series(monkeypatch):
    adapter = MacroAdapter(api_key="test-key")
    monkeypatch.setattr(adapter, "_latest_nowcast", lambda sid: 2.5)
    out = adapter.probability(
        _state("Will Q3 GDP growth be above 2.0%?", category="economics"))
    assert out is not None
    p, trace = out
    assert trace["series"] == "GDPNOW"
    assert trace["nowcast"] == 2.5
    assert p > 0.5   # nowcast 2.5 sits above the 2.0 threshold


def test_macro_adapter_pce_and_direction(monkeypatch):
    adapter = MacroAdapter(api_key="test-key")
    monkeypatch.setattr(adapter, "_latest_nowcast", lambda sid: 1.0)
    out = adapter.probability(
        _state("Will PCE growth be below 2.0% this quarter?", category="economics"))
    assert out is not None
    p, trace = out
    assert trace["series"] == "PCENOW"
    assert trace["direction"] == "below"
    assert p > 0.5   # nowcast 1.0 is below the 2.0 threshold


def test_macro_adapter_abstains_without_key():
    adapter = MacroAdapter(api_key="")   # explicit empty key disables FRED
    assert adapter.covers(_state("Will GDP growth be above 2.0%?", category="economics"))
    assert adapter.probability(
        _state("Will GDP growth be above 2.0%?", category="economics")) is None


def test_m6_flags_incoherent_negrisk():
    legs = [
        {"condition_id": "a", "p_yes": 0.50},
        {"condition_id": "b", "p_yes": 0.40},
        {"condition_id": "c", "p_yes": 0.25},
    ]  # sums to 1.15
    scan = scan_negrisk_event(legs)
    assert scan["incoherent"]
    assert scan["deviation"] == pytest.approx(0.15)
    assert len(scan["corrections"]) >= 1
    for c in scan["corrections"]:
        assert c["p_coherent"] == pytest.approx(c["p_market"] / 1.15)


def test_m6_silent_on_coherent_negrisk():
    legs = [
        {"condition_id": "a", "p_yes": 0.55},
        {"condition_id": "b", "p_yes": 0.30},
        {"condition_id": "c", "p_yes": 0.16},
    ]  # sums to 1.01 -- inside tolerance
    scan = scan_negrisk_event(legs)
    assert not scan["incoherent"]
    assert scan["corrections"] == []


def test_m6_link_violation():
    link = {"kind": "implies", "narrow": "n", "broad": "b"}
    # Narrow event priced ABOVE broad -> logically impossible.
    v = check_link(link, {"n": 0.6, "b": 0.4})
    assert v is not None and v["violation"] == pytest.approx(0.2)
    assert check_link(link, {"n": 0.3, "b": 0.5}) is None
