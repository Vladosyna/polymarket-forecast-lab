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
    event_ticker: str | None = None
    title: str | None = None
    status: str | None = None
    result: str | None = None
    rules_primary: str | None = None
    close_time: str | None = None
    expiration_time: str | None = None
    settlement_ts: str | None = None
    liquidity_dollars: float | None = Field(default=None, alias="liquidity_dollars")
    volume_fp: float | None = Field(default=None, alias="volume_fp")
    yes_bid_dollars: float | None = Field(default=None, alias="yes_bid_dollars")
    yes_ask_dollars: float | None = Field(default=None, alias="yes_ask_dollars")
    last_price_dollars: float | None = Field(default=None, alias="last_price_dollars")

    model_config = {"populate_by_name": True, "extra": "ignore"}

    _norm = field_validator(
        "yes_bid_dollars", "yes_ask_dollars", "last_price_dollars", "liquidity_dollars",
        mode="before",
    )(_dollars)

    # volume_fp is a contract count (like Gamma's volumeNum), not a price -- no
    # _dollars clamp/rounding semantics apply, just a plain float coercion.
    @field_validator("volume_fp", mode="before")
    @classmethod
    def _coerce_volume(cls, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

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

    async def series_by_category(self, category: str, limit: int = 200) -> list[dict]:
        """One page of /series for a given Kalshi category (e.g. 'Economics').

        Single page only -- the universe sync bounds fan-out by
        max_series_per_sync across the whole cycle (politeness), so no
        internal pagination loop is needed here.
        """
        raw = await self.get_json("/series", params={"category": category, "limit": limit})
        return raw.get("series", []) if isinstance(raw, dict) else []

    async def markets_for_series(
        self, series_ticker: str, status: str, limit: int = 200, max_pages: int = 5
    ) -> list[KalshiMarket]:
        """All markets for one series+status, paginating via the response
        cursor until exhausted or `max_pages` is reached (politeness cap --
        a single series should never need more than a handful of pages)."""
        markets: list[KalshiMarket] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "series_ticker": series_ticker, "status": status, "limit": limit,
            }
            if cursor:
                params["cursor"] = cursor
            raw = await self.get_json("/markets", params=params)
            items = raw.get("markets", []) if isinstance(raw, dict) else []
            for item in items:
                try:
                    markets.append(KalshiMarket.model_validate(item))
                except Exception:
                    continue
            cursor = raw.get("cursor") if isinstance(raw, dict) else None
            if not cursor or not items:
                break
        return markets
