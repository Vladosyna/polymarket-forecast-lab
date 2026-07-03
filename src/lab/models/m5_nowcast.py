"""M5 -- structural nowcast models: thin per-category adapters.

Two adapters is the v1 scope (brief section 6): weather (Open-Meteo Ensemble
API -> threshold probability from ensemble spread) and macro (FRED nowcast
series -> bucket probability via a surprise distribution). Each adapter
recognizes the markets it covers by parsing the question; everything else is
abstained on. Every forecast stores its input trace in meta.

Macro sourcing (brief v1.6/v1.7 section 3): FRED is primary. FRED mirrors the
Atlanta Fed nowcasts as clean documented series -- GDPNOW (real GDP growth)
and PCENOW (real PCE growth). Cleveland Fed's inflation nowcast has no
confirmed stable machine endpoint, so it is not wired as a guessed URL; it can
be added later as an optional sub-adapter once a real endpoint is verified.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from scipy.stats import norm

from lab.models.base import ForecastResult, MarketState, clamp_p

log = logging.getLogger(__name__)

# Minimal city gazetteer for weather markets; extend as markets appear.
CITY_COORDS = {
    "nyc": (40.78, -73.97), "new york": (40.78, -73.97),
    "london": (51.51, -0.13), "chicago": (41.88, -87.63),
    "miami": (25.76, -80.19), "los angeles": (34.05, -118.24),
    "philadelphia": (39.95, -75.17), "washington": (38.90, -77.04),
    "atlanta": (33.75, -84.39), "dallas": (32.78, -96.80),
    "seattle": (47.61, -122.33), "denver": (39.74, -104.99),
}

WEATHER_RE = re.compile(
    r"(?:high|highest|max(?:imum)?)\s+temp(?:erature)?\s+in\s+(?P<city>[a-z .]+?)\s+"
    r"(?:be\s+)?(?P<dir>above|below|exceed|over|under)?\s*(?P<threshold>-?\d+)\s*"
    r"(?:°|degrees)?\s*(?P<unit>f|c|fahrenheit|celsius)?",
    re.IGNORECASE,
)


def parse_weather_question(question: str) -> dict[str, Any] | None:
    m = WEATHER_RE.search(question or "")
    if not m:
        return None
    city = m.group("city").strip().lower()
    if city not in CITY_COORDS:
        return None
    unit = (m.group("unit") or "f").lower()
    direction = (m.group("dir") or "above").lower()
    return {
        "city": city,
        "threshold": float(m.group("threshold")),
        "unit": "fahrenheit" if unit.startswith("f") else "celsius",
        "above": direction in ("above", "exceed", "over"),
    }


class WeatherAdapter:
    """Ensemble members from open-meteo -> P(threshold exceeded)."""

    ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

    def covers(self, market: MarketState) -> bool:
        return parse_weather_question(market.question or "") is not None

    def probability(self, market: MarketState) -> tuple[float, dict[str, Any]] | None:
        spec = parse_weather_question(market.question or "")
        if spec is None or not market.end_date_iso:
            return None
        target_date = market.end_date_iso[:10]
        lat, lon = CITY_COORDS[spec["city"]]
        try:
            resp = httpx.get(self.ENSEMBLE_URL, params={
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max",
                "temperature_unit": spec["unit"],
                "start_date": target_date, "end_date": target_date,
                "models": "gfs_seamless",
            }, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError:
            log.warning("m5 weather: ensemble fetch failed",
                        extra={"ctx": {"condition_id": market.condition_id}})
            return None
        members = [
            v[0] for k, v in data.get("daily", {}).items()
            if k.startswith("temperature_2m_max") and isinstance(v, list) and v and v[0] is not None
        ]
        if len(members) < 5:
            return None
        exceed = sum(1 for t in members if t > spec["threshold"]) / len(members)
        p = exceed if spec["above"] else 1 - exceed
        # Laplace smoothing keeps 0/1 off the books; ensembles are overconfident.
        p = (p * len(members) + 1) / (len(members) + 2)
        return p, {"adapter": "weather", "spec": spec, "n_members": len(members),
                   "members": members, "target_date": target_date}


# FRED nowcast series -> (series_id, default sd of release surprise, question regex).
# sd defaults come from historical release surprises and are refit by `lab learn`
# once enough resolved macro markets accumulate.
FRED_SERIES = {
    "gdp": {
        "series_id": "GDPNOW", "sd": 0.55,
        "pattern": re.compile(r"\bgdp\b.*?(?P<threshold>-?\d+\.?\d*)\s*%", re.I),
    },
    "pce": {
        "series_id": "PCENOW", "sd": 0.50,
        "pattern": re.compile(
            r"\b(pce|personal consumption)\b.*?(?P<threshold>-?\d+\.?\d*)\s*%", re.I),
    },
}


class MacroAdapter:
    """FRED nowcast series -> bucket probability (brief section 3, FRED primary).

    P(print > threshold) from a normal surprise distribution centered on the
    latest FRED nowcast (GDPNOW / PCENOW). Requires FRED_API_KEY; without it the
    adapter abstains (returns None) so M5 falls through cleanly rather than
    guessing. sd defaults are hand-set; `sd_overrides` (from the active
    m5_macro_sd artifact, refit by `lab learn` on real FRED history -- see
    learn/refit.py) takes precedence once one exists.
    """

    FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(self, api_key: str | None = None,
                 sd_overrides: dict[str, float] | None = None) -> None:
        # Explicit "" disables; None falls back to the environment.
        self._api_key = api_key if api_key is not None else os.environ.get("FRED_API_KEY")
        self._nowcasts: dict[str, float | None] = {}
        self._sd_overrides = sd_overrides or {}

    def covers(self, market: MarketState) -> bool:
        q = market.question or ""
        return any(spec["pattern"].search(q) for spec in FRED_SERIES.values())

    def _latest_nowcast(self, series_id: str) -> float | None:
        if series_id in self._nowcasts:
            return self._nowcasts[series_id]
        value: float | None = None
        if not self._api_key:
            log.warning("m5 macro: FRED_API_KEY not set -- abstaining",
                        extra={"ctx": {"series": series_id}})
            self._nowcasts[series_id] = None
            return None
        try:
            resp = httpx.get(self.FRED_URL, params={
                "series_id": series_id, "api_key": self._api_key, "file_type": "json",
                "sort_order": "desc", "limit": 5,
            }, timeout=20)
            resp.raise_for_status()
            for obs in resp.json().get("observations", []):
                v = obs.get("value")
                if v in (None, ".", ""):
                    continue
                try:
                    value = float(v)
                    break
                except (TypeError, ValueError):
                    continue
        except httpx.HTTPError:
            log.warning("m5 macro: FRED fetch failed", extra={"ctx": {"series": series_id}})
        self._nowcasts[series_id] = value
        return value

    def probability(self, market: MarketState) -> tuple[float, dict[str, Any]] | None:
        q = market.question or ""
        for spec in FRED_SERIES.values():
            m = spec["pattern"].search(q)
            if not m:
                continue
            nowcast = self._latest_nowcast(spec["series_id"])
            if nowcast is None:
                return None
            threshold = float(m.group("threshold"))
            sd = self._sd_overrides.get(spec["series_id"], spec["sd"])
            above = not re.search(r"\b(below|under|less than)\b", q, re.I)
            p_above = 1 - norm.cdf(threshold, loc=nowcast, scale=sd)
            p = float(p_above if above else 1 - p_above)
            return p, {"adapter": "macro", "series": spec["series_id"],
                       "nowcast": nowcast, "threshold": threshold, "sd": sd,
                       "direction": "above" if above else "below"}
        return None


def _sd_overrides_from_artifact(artifact: dict[str, Any] | None) -> dict[str, float]:
    if not artifact:
        return {}
    return {series_id: fit["sd"] for series_id, fit in artifact.get("series", {}).items()
            if "sd" in fit}


class M5Nowcast:
    model_id = "m5_nowcast"

    def __init__(self, adapters: list[Any] | None = None,
                 macro_artifact: dict[str, Any] | None = None) -> None:
        self.adapters = adapters if adapters is not None else [
            WeatherAdapter(),
            MacroAdapter(sd_overrides=_sd_overrides_from_artifact(macro_artifact)),
        ]

    def forecast(self, market: MarketState, context: dict[str, Any]) -> ForecastResult | None:
        for adapter in self.adapters:
            try:
                if not adapter.covers(market):
                    continue
                out = adapter.probability(market)
            except Exception:
                log.exception("m5: adapter failed",
                              extra={"ctx": {"condition_id": market.condition_id}})
                continue
            if out is None:
                continue
            p, trace = out
            return ForecastResult(p_yes=clamp_p(p), meta=trace)
        return None
