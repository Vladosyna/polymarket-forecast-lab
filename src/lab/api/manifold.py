"""Manifold Markets public API client (read-only, no account/auth -- verified
live against https://api.manifold.markets/v0 on 2026-07-04; brief section 3 /
Phase 10 orchestrator instructions).

Guardrail 16 (venue trust & provenance): Manifold is PLAY MONEY. It is
collected for event mapping and M2 base rates ONLY -- never for snapshot price
tracking, M7 pooling, or any skill/forecast claim. This client therefore has
no order-book/price-history methods at all; there is nothing here to build a
snapshot loop on top of.

Live-verified shapes used here (brief's orchestrator note, cross-checked
2026-07-04):
- `GET /v0/markets?limit=N` returns a bare JSON array, newest first, with no
  resolved/binary filter available -- not used for bulk sync.
- `GET /v0/search-markets?term=&filter=<open|resolved>&contractType=BINARY&
  offset=&limit=` returns a similarly bare array, filterable and paginatable
  via `offset`, which is what `sync_manifold_markets` needs.
- `closeTime` / `resolutionTime` arrive as epoch MILLISECONDS, not seconds.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from lab.api.http import BaseClient, TokenBucket

log = logging.getLogger(__name__)

MANIFOLD_BASE_URL = "https://api.manifold.markets/v0"


class ManifoldMarket(BaseModel):
    """Subset of a Manifold market object actually consumed by the lab."""

    id: str
    question: str | None = None
    slug: str | None = None
    close_time_ms: int | None = Field(default=None, alias="closeTime")
    probability: float | None = None
    outcome_type: str | None = Field(default=None, alias="outcomeType")
    volume: float | None = None
    is_resolved: bool | None = Field(default=None, alias="isResolved")
    resolution: str | None = None
    resolution_time_ms: int | None = Field(default=None, alias="resolutionTime")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class ManifoldClient(BaseClient):
    def __init__(self, bucket: TokenBucket, base_url: str = MANIFOLD_BASE_URL) -> None:
        super().__init__(base_url, bucket)

    async def search_markets(
        self, filter: str, offset: int = 0, limit: int = 200
    ) -> list[ManifoldMarket]:
        """One page of `/search-markets`, filtered to BINARY contracts.

        `filter` is Manifold's own query param: 'open' or 'resolved' are the
        two values the lab uses. Parses defensively: one unparseable item is
        logged and skipped rather than failing the whole page (guardrail 9).
        """
        params: dict[str, Any] = {
            "term": "",
            "filter": filter,
            "contractType": "BINARY",
            "offset": offset,
            "limit": limit,
        }
        raw = await self.get_json("/search-markets", params=params)
        items = raw if isinstance(raw, list) else []
        markets: list[ManifoldMarket] = []
        for item in items:
            try:
                markets.append(ManifoldMarket.model_validate(item))
            except Exception:
                log.warning("manifold: unparseable market item", extra={"ctx": {"item_id": (item or {}).get("id")}})
                continue
        return markets
