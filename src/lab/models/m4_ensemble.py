"""M4 -- log-odds weighted pool of the other models' forecasts.

Weights are fit per category on resolved forecasts (inverse-Brier softmax
over a rolling window) and stored as a versioned artifact; equal weights
until a category has >= 100 resolved samples. M4 pools each market's latest
same-day forecasts from the ledger, so it runs after the other models.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from lab.learn.refit import logit, sigmoid
from lab.models.base import ForecastResult, MarketState, clamp_p

log = logging.getLogger(__name__)

POOLABLE = ("m0_market", "m1_debiased", "m2_baserate", "m3_evidence",
            "m5_nowcast", "m6_consistency")
MIN_RESOLVED_PER_CATEGORY = 100


def fit_m4_weights(conn, config: dict[str, Any]) -> dict[str, Any]:
    """Per-category softmax(-brier) weights over resolved forecasts."""
    from lab.util import now_utc_iso

    artifact: dict[str, Any] = {"kind": "m4_weights", "fitted_at": now_utc_iso(),
                                "categories": {}}
    rows = conn.execute(
        """
        SELECT m.category, f.model_id, AVG((f.p_yes - r.payout_yes)*(f.p_yes - r.payout_yes)) AS brier,
               COUNT(*) AS n
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id AND r.disputed = 0
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id IN ({})
        GROUP BY m.category, f.model_id
        """.format(",".join("?" for _ in POOLABLE)),
        POOLABLE,
    ).fetchall()
    by_cat: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], {})[r["model_id"]] = {"brier": r["brier"], "n": r["n"]}
    for cat, models in by_cat.items():
        total_n = sum(v["n"] for v in models.values())
        if total_n < MIN_RESOLVED_PER_CATEGORY:
            continue
        briers = np.array([models[m]["brier"] for m in sorted(models)])
        # Softmax over negative Brier: better models earn weight through
        # realized Brier and nothing else (temperature 0.05 ~ Brier scale).
        w = np.exp(-briers / 0.05)
        w = w / w.sum()
        artifact["categories"][cat] = {
            "weights": dict(zip(sorted(models), w.tolist())),
            "n_resolved": total_n,
        }
    return artifact


class M4Ensemble:
    model_id = "m4_ensemble"

    def __init__(self, conn, weights_artifact: dict[str, Any] | None) -> None:
        self.conn = conn
        self.artifact = weights_artifact or {"categories": {}}

    def _todays_pool(self, condition_id: str) -> dict[str, float]:
        rows = self.conn.execute(
            """
            SELECT f.model_id, f.p_yes FROM forecasts f
            JOIN (SELECT model_id, MAX(ts) AS ts FROM forecasts
                  WHERE condition_id = ? AND date(ts) = date('now')
                  GROUP BY model_id) latest
            ON latest.model_id = f.model_id AND latest.ts = f.ts
            WHERE f.condition_id = ? AND f.model_id IN ({})
            """.format(",".join("?" for _ in POOLABLE)),
            (condition_id, condition_id, *POOLABLE),
        ).fetchall()
        return {r["model_id"]: r["p_yes"] for r in rows}

    def forecast(self, market: MarketState, context: dict[str, Any]) -> ForecastResult | None:
        pool = self._todays_pool(market.condition_id)
        if len(pool) < 2:  # pooling one model is just that model
            return None
        cat_weights = self.artifact.get("categories", {}).get(market.category, {}).get("weights")
        members = sorted(pool)
        if cat_weights:
            w = np.array([cat_weights.get(m, 0.0) for m in members])
            if w.sum() <= 0:
                w = np.ones(len(members))
        else:
            w = np.ones(len(members))  # equal weights until n >= 100
        w = w / w.sum()
        pooled = float(sigmoid(np.dot(w, [logit(pool[m]) for m in members])))
        return ForecastResult(
            p_yes=clamp_p(pooled),
            meta={"members": members, "weights": w.tolist(),
                  "weighted": bool(cat_weights),
                  "artifact_version": self.artifact.get("version")},
        )
