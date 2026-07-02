"""CLOB API client -- public market data only (order books, prices).

No authentication, no order endpoints; this client can only read.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from lab.api.http import BaseClient, TokenBucket

log = logging.getLogger(__name__)

CLOB_BASE_URL = "https://clob.polymarket.com"


class BookLevel(BaseModel):
    price: float
    size: float

    model_config = {"extra": "ignore", "coerce_numbers_to_str": False}


class OrderBook(BaseModel):
    market: str | None = None          # condition_id
    asset_id: str | None = None        # token_id
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)

    model_config = {"extra": "ignore"}

    @property
    def best_bid(self) -> float | None:
        return max((l.price for l in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((l.price for l in self.asks), default=None)

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        return (bb + ba) / 2 if bb is not None and ba is not None else None

    @property
    def spread(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        return ba - bb if bb is not None and ba is not None else None

    def depth_usd(self, side: str) -> float:
        """Top-of-book depth in USD (price * size of the best level)."""
        levels = self.bids if side == "bid" else self.asks
        best = self.best_bid if side == "bid" else self.best_ask
        return sum(l.price * l.size for l in levels if l.price == best)


class ClobClient(BaseClient):
    def __init__(self, bucket: TokenBucket, base_url: str = CLOB_BASE_URL) -> None:
        super().__init__(base_url, bucket)

    async def book(self, token_id: str) -> OrderBook:
        raw = await self.get_json("/book", params={"token_id": token_id})
        return OrderBook.model_validate(raw)

    async def books(self, token_ids: list[str]) -> list[OrderBook]:
        """Batch order books; falls back to per-token GETs if batch fails."""
        if not token_ids:
            return []
        try:
            raw = await self.post_json("/books", [{"token_id": t} for t in token_ids])
            if isinstance(raw, list):
                return [OrderBook.model_validate(item) for item in raw]
        except Exception:
            log.warning("clob: batch /books failed, falling back to per-token requests")
        out: list[OrderBook] = []
        for token_id in token_ids:
            try:
                out.append(await self.book(token_id))
            except Exception:
                log.warning("clob: /book failed", extra={"ctx": {"token_id": token_id}})
        return out

    async def midpoint(self, token_id: str) -> float | None:
        raw = await self.get_json("/midpoint", params={"token_id": token_id})
        try:
            return float(raw["mid"])
        except (KeyError, TypeError, ValueError):
            return None

    async def last_trade_price(self, token_id: str) -> float | None:
        raw = await self.get_json("/last-trade-price", params={"token_id": token_id})
        try:
            return float(raw["price"])
        except (KeyError, TypeError, ValueError):
            return None

    async def prices_history(
        self, token_id: str, interval: str = "1w", fidelity: int = 60
    ) -> list[dict]:
        raw = await self.get_json(
            "/prices-history",
            params={"market": token_id, "interval": interval, "fidelity": fidelity},
        )
        return raw.get("history", []) if isinstance(raw, dict) else []
