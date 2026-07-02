"""M1 -- horizon-aware logistic recalibration of the market price.

Applies the active m1_curves artifact: p = sigmoid(alpha_h + beta_h * logit(p_market))
for the market's time-to-resolution bucket. Abstains when the bucket has no
fitted curve or the horizon is unknown.
"""

from __future__ import annotations

from typing import Any

from lab.learn.refit import bucket_for_days, logit, sigmoid
from lab.models.base import ForecastResult, MarketState, clamp_p


class M1Debiased:
    model_id = "m1_debiased"

    def __init__(self, artifact: dict[str, Any]) -> None:
        self.artifact = artifact

    def forecast(self, market: MarketState, context: dict[str, Any]) -> ForecastResult | None:
        if market.days_to_resolution is None:
            return None
        bucket = bucket_for_days(market.days_to_resolution)
        fit = self.artifact.get("buckets", {}).get(bucket)
        if fit is None:
            return None
        p = float(sigmoid(fit["alpha"] + fit["beta"] * logit(market.p_market)))
        return ForecastResult(
            p_yes=clamp_p(p),
            meta={
                "horizon_bucket": bucket,
                "alpha": fit["alpha"],
                "beta": fit["beta"],
                "artifact_version": self.artifact.get("version"),
            },
        )
