"""M1.x -- hierarchical multi-venue recalibration (Phase 12, CLAUDE.md M1.x).

One recalibration family: a global logistic curve per horizon bucket plus a
ridge-shrunk per-venue offset (fitted in `learn.refit.fit_m1_hier_curves`).
`M1Hier` forecasts only its own declared venue and abstains everywhere else --
one instance per venue, all reading the SAME shared artifact.

`m1_hier@polymarket` and `m1_hier@kalshi` forecast in parallel with the
existing `m1_debiased`/`m0_market` (wired in `forecast.build_default_models`),
earning their own skill track record through the ordinary Phase 11 eval
pipeline. `m1_hier@metaculus` is never forecast through this class at all --
Metaculus markets are never tiered liquid/tail, so they never reach
`eligible_market_states`; its venue offset is instead applied directly inside
`m7_crossvenue.scan_confirmed_pairs()` to recalibrate the raw community
prediction before pooling (CLAUDE.md: "recalibrates the community prediction
as an input signal for M7").
"""

from __future__ import annotations

from typing import Any

from lab.learn.refit import bucket_for_days, logit, sigmoid
from lab.models.base import ForecastResult, MarketState, clamp_p


def apply_hier_curve(artifact: dict[str, Any], venue: str, bucket: str, p_market: float) -> float:
    """sigmoid((alpha_g+alpha_v) + (beta_g+beta_v) * logit(p_market)) for one
    bucket/venue. A venue with no offset entry in the artifact (brand-new
    venue, or below the fit's min_venue_n threshold) falls back to the
    global-only curve -- this IS the partial-pooling behavior, not a special
    case."""
    fit = artifact["buckets"][bucket]
    g = fit["global"]
    v = fit.get("venues", {}).get(venue, {"alpha_offset": 0.0, "beta_offset": 0.0})
    alpha = g["alpha"] + v["alpha_offset"]
    beta = g["beta"] + v["beta_offset"]
    return float(sigmoid(alpha + beta * logit(p_market)))


class M1Hier:
    def __init__(self, artifact: dict[str, Any], venue: str) -> None:
        self.artifact = artifact
        self.venue = venue
        self.model_id = f"m1_hier@{venue}"

    def forecast(self, market: MarketState, context: dict[str, Any]) -> ForecastResult | None:
        if market.venue != self.venue or market.days_to_resolution is None:
            return None  # abstain outside its own declared venue
        bucket = bucket_for_days(market.days_to_resolution)
        if bucket is None or bucket not in self.artifact.get("buckets", {}):
            return None
        p = apply_hier_curve(self.artifact, self.venue, bucket, market.p_market)
        return ForecastResult(
            p_yes=clamp_p(p),
            meta={
                "horizon_bucket": bucket,
                "venue": self.venue,
                "artifact_version": self.artifact.get("version"),
            },
        )
