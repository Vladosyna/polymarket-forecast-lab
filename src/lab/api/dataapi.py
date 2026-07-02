"""Data API client -- public trades and holder aggregates (no auth).

Minimal in Phase 1; extended only when a model needs these inputs.
"""

from __future__ import annotations

from lab.api.http import BaseClient, TokenBucket

DATA_API_BASE_URL = "https://data-api.polymarket.com"


class DataApiClient(BaseClient):
    def __init__(self, bucket: TokenBucket, base_url: str = DATA_API_BASE_URL) -> None:
        super().__init__(base_url, bucket)

    async def trades(self, condition_id: str, limit: int = 100) -> list[dict]:
        raw = await self.get_json("/trades", params={"market": condition_id, "limit": limit})
        return raw if isinstance(raw, list) else []
