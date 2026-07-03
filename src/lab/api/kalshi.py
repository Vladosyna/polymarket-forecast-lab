"""Kalshi public market-data client (read-only, no account/auth -- verified
live against https://external-api.kalshi.com/trade-api/v2 at implementation
time; brief section 3). Only the fields the lab consumes are modeled.

Field names carry a `_dollars` suffix and arrive as strings (e.g. "0.0460"),
not the cents-integer shape of Kalshi's older API versions -- confirmed by a
live GET against /markets during implementation, per guardrail 1 (verify
rather than guess).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

from lab.api.http import BaseClient, TokenBucket

log = logging.getLogger(__name__)

KALSHI_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


def _dollars(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class KalshiMarket(BaseModel):
    """Subset of a Kalshi /markets item used by the lab."""

    ticker: str
    title: str | None = None
    status: str | None = None
    close_time: str | None = None
    yes_bid_dollars: float | None = Field(default=None, alias="yes_bid_dollars")
    yes_ask_dollars: float | None = Field(default=None, alias="yes_ask_dollars")
    last_price_dollars: float | None = Field(default=None, alias="last_price_dollars")

    model_config = {"populate_by_name": True, "extra": "ignore"}

    _norm = field_validator(
        "yes_bid_dollars", "yes_ask_dollars", "last_price_dollars", mode="before"
    )(_dollars)

    @property
    def yes_price(self) -> float | None:
        """Best available YES probability estimate: bid/ask mid, falling back
        to last trade price when there's no live two-sided quote."""
        if self.yes_bid_dollars is not None and self.yes_ask_dollars is not None:
            if 0 < self.yes_bid_dollars <= 1 and 0 < self.yes_ask_dollars <= 1:
                return (self.yes_bid_dollars + self.yes_ask_dollars) / 2
        if self.last_price_dollars is not None and 0 < self.last_price_dollars < 1:
            return self.last_price_dollars
        return None


class KalshiClient(BaseClient):
    def __init__(self, bucket: TokenBucket, base_url: str = KALSHI_BASE_URL) -> None:
        super().__init__(base_url, bucket)

    async def market(self, ticker: str) -> KalshiMarket | None:
        try:
            raw = await self.get_json(f"/markets/{ticker}")
        except Exception:
            log.warning("kalshi: market fetch failed", extra={"ctx": {"ticker": ticker}})
            return None
        item = raw.get("market") if isinstance(raw, dict) else None
        if not item:
            return None
        try:
            return KalshiMarket.model_validate(item)
        except Exception:
            log.warning("kalshi: unparseable market", extra={"ctx": {"ticker": ticker}})
            return None

    async def open_markets(self, limit: int = 200, **filters: Any) -> list[KalshiMarket]:
        """One page of open markets, for the propose flow's candidate list."""
        params: dict[str, Any] = {"limit": limit, "status": "open", **filters}
        raw = await self.get_json("/markets", params=params)
        items = raw.get("markets", []) if isinstance(raw, dict) else []
        markets: list[KalshiMarket] = []
        for item in items:
            try:
                markets.append(KalshiMarket.model_validate(item))
            except Exception:
                continue
        return markets
