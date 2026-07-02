"""M2 -- category base rates blended with the market prior in log-odds space.

Sanity-check model: small fixed weight toward the category's historical YES
rate. Abstains for categories without a fitted base rate.
"""

from __future__ import annotations

from typing import Any

from lab.learn.refit import logit, sigmoid
from lab.models.base import ForecastResult, MarketState, clamp_p

BLEND_WEIGHT = 0.15  # small fixed weight per the brief; not tuned


class M2BaseRate:
    model_id = "m2_baserate"

    def __init__(self, artifact: dict[str, Any]) -> None:
        self.artifact = artifact

    def forecast(self, market: MarketState, context: dict[str, Any]) -> ForecastResult | None:
        entry = self.artifact.get("categories", {}).get(market.category)
        if entry is None:
            return None
        blended = (1 - BLEND_WEIGHT) * logit(market.p_market) + BLEND_WEIGHT * logit(
            entry["base_rate"]
        )
        return ForecastResult(
            p_yes=clamp_p(float(sigmoid(blended))),
            meta={
                "category": market.category,
                "base_rate": entry["base_rate"],
                "base_rate_n": entry["n"],
                "blend_weight": BLEND_WEIGHT,
                "artifact_version": self.artifact.get("version"),
            },
        )
