"""Forecaster protocol and the shared forecast context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class MarketState:
    """Everything a model may look at for one market at freeze time."""

    condition_id: str
    question: str | None
    category: str
    description: str | None
    end_date_iso: str | None
    tier: str
    p_market: float          # YES mid from the freshest snapshot
    spread: float | None
    snapshot_ts: str         # ts of the snapshot backing p_market
    days_to_resolution: float | None
    venue: str = "polymarket"  # Phase 10 venue tag; default preserves pre-Phase-12 behavior


@dataclass
class ForecastResult:
    p_yes: float
    meta: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    evidence_run_id: int | None = None


class Forecaster(Protocol):
    model_id: str

    def forecast(self, market: MarketState, context: dict[str, Any]) -> ForecastResult | None:
        """Return a forecast, or None to abstain (e.g. no artifact coverage)."""
        ...


def clamp_p(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return min(hi, max(lo, p))
