"""Event-vs-condition cluster-id resolution (brief section 7, Phase 11).

Forecasts on the same market are correlated; the same underlying event listed
on several venues is still ONE observation of the world. The cluster bootstrap
(scoring.py) already resamples whole clusters -- this module is the single
documented source of truth for *which* id a cluster is keyed by: the event_id
where a cross-venue match has been confirmed, falling back to condition_id
otherwise.
"""

from __future__ import annotations

import numpy as np


def resolve_cluster_ids(
    condition_ids: np.ndarray, event_id_by_condition: dict[str, str | None]
) -> np.ndarray:
    """event_id_by_condition.get(cid) if truthy, else cid itself."""
    return np.array(
        [event_id_by_condition.get(cid) or cid for cid in condition_ids]
    )
