"""Event-vs-condition cluster-id fallback (brief section 7, Phase 11)."""

from __future__ import annotations

import numpy as np

from lab.eval.cluster import resolve_cluster_ids
from lab.eval.scoring import cluster_bootstrap_ci


def test_resolve_cluster_ids_prefers_event_id_falls_back_to_condition_id():
    condition_ids = np.array(["0x1", "kalshi:T1", "0x2"])
    event_id_by_condition = {"0x1": "evt_a", "kalshi:T1": "evt_a", "0x2": None}
    resolved = resolve_cluster_ids(condition_ids, event_id_by_condition)
    assert resolved.tolist() == ["evt_a", "evt_a", "0x2"]


def test_resolve_cluster_ids_missing_key_falls_back_to_condition_id():
    condition_ids = np.array(["0x1", "0x2"])
    resolved = resolve_cluster_ids(condition_ids, {})
    assert resolved.tolist() == ["0x1", "0x2"]


def test_event_clustered_ci_wider_than_condition_clustered():
    """Phase 11 acceptance criterion: a fixture with cross-venue-correlated
    outcomes must yield a WIDER event-clustered CI than a condition-clustered
    one -- clustering by raw condition_id treats the same real-world event
    listed on two venues as two independent observations, understating
    correlation and overstating effective n."""
    rng = np.random.default_rng(7)
    n_events = 40
    event_effect = rng.normal(0, 0.1, n_events)
    diffs, condition_ids, event_id_by_condition = [], [], {}
    for i in range(n_events):
        evt = f"evt_{i}"
        for venue in ("polymarket", "kalshi"):
            cid = f"{venue}:{i}"
            event_id_by_condition[cid] = evt
            for _ in range(5):  # several forecasts per venue-market
                diffs.append(event_effect[i] + rng.normal(0, 0.001))
                condition_ids.append(cid)
    diffs = np.array(diffs)
    condition_ids = np.array(condition_ids)

    event_clusters = resolve_cluster_ids(condition_ids, event_id_by_condition)

    lo_evt, hi_evt = cluster_bootstrap_ci(diffs, event_clusters, iterations=500)
    lo_cid, hi_cid = cluster_bootstrap_ci(diffs, condition_ids, iterations=500)
    assert (hi_evt - lo_evt) > (hi_cid - lo_cid)
