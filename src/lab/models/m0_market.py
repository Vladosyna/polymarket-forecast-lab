"""M0 -- the null model: p_yes = market mid. The baseline everyone must beat."""

from __future__ import annotations

from typing import Any

from lab.models.base import ForecastResult, MarketState, clamp_p


class M0Market:
    model_id = "m0_market"

    def forecast(self, market: MarketState, context: dict[str, Any]) -> ForecastResult:
        return ForecastResult(p_yes=clamp_p(market.p_market))
