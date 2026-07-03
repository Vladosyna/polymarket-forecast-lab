"""Metaculus API client (brief section 3) -- account/token required, verified.

CLAUDE.md v1.8 describes Metaculus as a public, no-account API. Two rounds of
live verification at Phase 9 implementation time (2026-07-03) refined that:

1. Anonymous access is genuinely gone: `/api/posts/` and the legacy
   `/api2/questions/` both return HTTP 403 "Permission Error: The API is only
   available to authenticated users. Please create an account and use your
   API token to access the API." -- Metaculus's own access-control response,
   confirmed with multiple header/UA variations (not a transient CDN block).
2. With a user-supplied API token, `GET /api/posts/{id}/` returns 200 with
   full question metadata, but `question.aggregations.recency_weighted.latest`
   (the numeric community-prediction field this adapter needs) came back
   `null` across five different heavily-forecasted real questions (up to
   4400+ forecasters) -- so a basic account token authenticates but does not
   necessarily carry data-access rights to the aggregated prediction itself;
   that looks gated behind a separate tier (see metaculus.com/api's
   "Commercial API or Data Access" page). The request shape and JSON path
   below were cross-checked against Metaculus's own open-source client
   (github.com/Metaculus/forecasting-tools, questions.py /
   metaculus_client.py) and match it exactly -- this is not a guessed shape.

Per guardrail 2 (no account creation for any data source this lab depends on
by default) this client never creates an account; it activates only when the
operator supplies their own pre-existing METACULUS_API_TOKEN. Even then it
abstains cleanly (returns None) whenever the aggregation field is null --
which, per the above, may be the common case for a non-commercial token.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import BaseModel

from lab.api.http import BaseClient, TokenBucket

log = logging.getLogger(__name__)

METACULUS_BASE_URL = "https://www.metaculus.com/api"


class MetaculusQuestion(BaseModel):
    """Subset of a Metaculus post/question actually consumed by M7."""

    id: int
    title: str | None = None
    community_prediction: float | None = None

    model_config = {"extra": "ignore"}


def _extract_probability(raw: dict[str, Any]) -> float | None:
    """`question.aggregations.{recency_weighted,unweighted}.latest.centers[0]`
    -- the exact path Metaculus's own forecasting-tools client reads. `latest`
    is frequently null (see module docstring); this returns None rather than
    raising whenever the shape isn't there."""
    q = raw.get("question")
    if not isinstance(q, dict):
        return None
    aggregations = q.get("aggregations")
    if not isinstance(aggregations, dict):
        return None
    for method in ("recency_weighted", "unweighted"):
        latest = (aggregations.get(method) or {}).get("latest")
        centers = (latest or {}).get("centers") if isinstance(latest, dict) else None
        if isinstance(centers, list) and centers:
            try:
                return float(centers[0])
            except (TypeError, ValueError):
                continue
    return None


class MetaculusClient(BaseClient):
    def __init__(self, bucket: TokenBucket, api_token: str | None = None,
                 base_url: str = METACULUS_BASE_URL) -> None:
        super().__init__(base_url, bucket)
        self._token = api_token if api_token is not None else os.environ.get("METACULUS_API_TOKEN")

    async def question(self, question_id: int) -> MetaculusQuestion | None:
        if not self._token:
            log.warning("metaculus: no METACULUS_API_TOKEN -- abstaining "
                        "(public access requires an account as of 2026-07-03, see module docstring)",
                        extra={"ctx": {"question_id": question_id}})
            return None
        try:
            self._client.headers["Authorization"] = f"Token {self._token}"
            self._client.headers["Accept-Language"] = "en"
            raw = await self.get_json(f"/posts/{question_id}/")
        except Exception:
            log.warning("metaculus: question fetch failed", extra={"ctx": {"question_id": question_id}})
            return None
        if not isinstance(raw, dict):
            return None
        p = _extract_probability(raw)
        if p is None:
            log.info("metaculus: no aggregation data for question (see module docstring)",
                     extra={"ctx": {"question_id": question_id}})
        try:
            return MetaculusQuestion(id=question_id, title=raw.get("title"), community_prediction=p)
        except Exception:
            return None
