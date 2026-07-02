"""M5 -- structural nowcast models: thin per-category adapters.

Two adapters is the v1 scope (brief section 6): weather (open-meteo ensemble
-> threshold probability) and macro (Cleveland Fed inflation nowcast /
GDPNow -> bucket probability via a surprise distribution). Each adapter
recognizes the markets it covers by parsing the question; everything else
is abstained on. Every forecast stores its input trace in meta.
"""

from __future__ import annotations

import logging
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


MACRO_PATTERNS = {
    # question regex -> (series, default sd of release surprise)
    "inflation": (re.compile(r"\b(cpi|inflation)\b.*?(?P<threshold>\d+\.?\d*)\s*%", re.I), 0.12),
    "gdp": (re.compile(r"\bgdp\b.*?(?P<threshold>-?\d+\.?\d*)\s*%", re.I), 0.55),
}


class MacroAdapter:
    """Cleveland Fed inflation nowcast / Atlanta Fed GDPNow -> bucket probability.

    P(print > threshold) from a normal surprise distribution centered on the
    nowcast; sd defaults come from historical release surprises and are meant
    to be refit by `lab learn` once enough resolved macro markets accumulate.
    """

    CLEVELAND_URL = (
        "https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/"
        "nowcast_quarter.csv"
    )
    GDPNOW_URL = "https://www.atlantafed.org/-/media/documents/cqer/researchcq/gdpnow/GDPTrackingModelDataAndForecasts.xlsx"

    def __init__(self) -> None:
        self._nowcasts: dict[str, float | None] = {}

    def covers(self, market: MarketState) -> bool:
        q = market.question or ""
        return any(pat.search(q) for pat, _ in MACRO_PATTERNS.values())

    def _cleveland_cpi_nowcast(self) -> float | None:
        if "cpi" in self._nowcasts:
            return self._nowcasts["cpi"]
        value: float | None = None
        try:
            resp = httpx.get(self.CLEVELAND_URL, timeout=20, follow_redirects=True)
            resp.raise_for_status()
            lines = [l for l in resp.text.splitlines() if l.strip()]
            # CSV: latest row, find a 'CPI' column by header match.
            header = [h.strip().lower() for h in lines[0].split(",")]
            cpi_idx = next((i for i, h in enumerate(header) if "cpi" in h and "core" not in h), None)
            if cpi_idx is not None:
                for line in reversed(lines[1:]):
                    cells = line.split(",")
                    try:
                        value = float(cells[cpi_idx])
                        break
                    except (ValueError, IndexError):
                        continue
        except httpx.HTTPError:
            log.warning("m5 macro: cleveland fed fetch failed")
        self._nowcasts["cpi"] = value
        return value

    def probability(self, market: MarketState) -> tuple[float, dict[str, Any]] | None:
        q = market.question or ""
        m = MACRO_PATTERNS["inflation"][0].search(q)
        if m:
            nowcast = self._cleveland_cpi_nowcast()
            if nowcast is None:
                return None
            threshold = float(m.group("threshold"))
            sd = MACRO_PATTERNS["inflation"][1]
            above = not re.search(r"\b(below|under|less than)\b", q, re.I)
            p_above = 1 - norm.cdf(threshold, loc=nowcast, scale=sd)
            p = float(p_above if above else 1 - p_above)
            return p, {"adapter": "macro", "series": "cleveland_cpi_nowcast",
                       "nowcast": nowcast, "threshold": threshold, "sd": sd,
                       "direction": "above" if above else "below"}
        # GDPNow requires parsing an xlsx workbook; deferred until a live GDP
        # bucket market exists in the universe (assumption stated per guardrail 1).
        return None


class M5Nowcast:
    model_id = "m5_nowcast"

    def __init__(self, adapters: list[Any] | None = None) -> None:
        self.adapters = adapters if adapters is not None else [WeatherAdapter(), MacroAdapter()]

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
