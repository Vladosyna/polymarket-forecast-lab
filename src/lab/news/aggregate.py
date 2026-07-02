"""Deterministic log-odds aggregation of evidence items.

The LLM never writes the final number: extraction produces structured items,
and this module -- pure math, unit-tested -- turns them into a probability.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from lab.learn.refit import logit, sigmoid

DIRECTION_SIGN = {"for_yes": 1.0, "for_no": -1.0, "neutral": 0.0}


def item_delta(item: dict[str, Any], forecast_ts: str, k: float, tau_days: float) -> float:
    """Delta = k * strength * reliability * relevance * sign * exp(-age/tau).

    Items published after the forecast timestamp contribute zero
    (no look-ahead, guardrail 11).
    """
    sign = DIRECTION_SIGN.get(item.get("direction", "neutral"), 0.0)
    if sign == 0.0:
        return 0.0
    published = item.get("published_ts")
    if not published:
        return 0.0
    t_forecast = datetime.fromisoformat(forecast_ts)
    t_pub = datetime.fromisoformat(published)
    if t_forecast.tzinfo is None:
        t_forecast = t_forecast.replace(tzinfo=timezone.utc)
    if t_pub.tzinfo is None:
        t_pub = t_pub.replace(tzinfo=timezone.utc)
    age_days = (t_forecast - t_pub).total_seconds() / 86400
    if age_days < 0:
        return 0.0
    strength = min(3, max(1, int(item.get("strength", 1))))
    reliability = min(3, max(1, int(item.get("source_reliability", 1))))
    relevance = min(1.0, max(0.0, float(item.get("relevance", 0.0))))
    return k * strength * reliability * relevance * sign * math.exp(-age_days / tau_days)


def aggregate(p_market: float, items: list[dict[str, Any]], forecast_ts: str,
              k: float = 0.15, tau_days: float = 5.0,
              max_shift: float = 0.8) -> dict[str, Any]:
    """Returns p_yes plus a full trace for the dossier."""
    prior_logodds = float(logit(p_market))
    deltas = [item_delta(item, forecast_ts, k, tau_days) for item in items]
    total = sum(deltas)
    clipped = max(-max_shift, min(max_shift, total))
    p_yes = float(sigmoid(prior_logodds + clipped))
    return {
        "p_market": p_market,
        "prior_logodds": prior_logodds,
        "deltas": deltas,
        "total_shift": total,
        "clipped_shift": clipped,
        "p_yes": p_yes,
        "params": {"k": k, "tau_days": tau_days, "max_shift": max_shift},
    }
