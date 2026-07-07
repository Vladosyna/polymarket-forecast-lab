"""Universe tiering and filtering rules."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from lab.api.gamma import GammaEvent, GammaMarket
from lab.collect.universe import (
    _depth_lookup,
    assign_tier,
    assign_tier_with_category,
    is_sub_24h_crypto,
    sync_universe,
)
from lab.store import db
from lab.store.snapshots import SnapshotStore
from lab.util import load_config

CONFIG = load_config()
NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)


def _market(**kwargs) -> GammaMarket:
    base = {
        "conditionId": "0x1",
        "question": "Will X happen?",
        "category": "Politics",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["111", "222"]',
        "active": True,
        "closed": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "liquidityNum": 200000.0,
        "volumeNum": 2000000.0,
        "endDate": "2026-12-31T00:00:00Z",
    }
    base.update(kwargs)
    return GammaMarket.model_validate(base)


def test_binary_detection():
    assert _market().is_binary
    assert not _market(outcomes='["A", "B", "C"]', clobTokenIds='["1","2","3"]').is_binary
    assert _market().token_id_yes == "111"


def test_tier_liquid_and_tail():
    assert assign_tier(_market(), CONFIG) == "liquid"
    assert assign_tier(_market(liquidityNum=2000.0, volumeNum=8000.0), CONFIG) == "tail"
    assert assign_tier(_market(liquidityNum=10.0, volumeNum=10.0), CONFIG) == "ignored"


def test_no_live_book_is_ignored():
    assert assign_tier(_market(enableOrderBook=False), CONFIG) == "ignored"
    assert assign_tier(_market(acceptingOrders=False), CONFIG) == "ignored"


def test_crypto_is_ignored():
    m = _market(category="Crypto", question="Will Bitcoin close above 200k?")
    assert assign_tier(m, CONFIG) == "ignored"


def test_depth_based_tiering_overrides_liquidity_and_volume():
    """Phase 17 item 2: once a snapshot exists, real order-book depth governs
    tiering -- not Gamma's self-reported liquidity_num/volume_num."""
    m = _market()  # liquidityNum/volumeNum say "liquid" via the fallback path
    # Thin real depth: same market tiers down to 'tail' or 'ignored' on depth.
    assert assign_tier_with_category(m, "politics", CONFIG, depth_usd=50.0) == "tail"
    assert assign_tier_with_category(m, "politics", CONFIG, depth_usd=1.0) == "ignored"
    assert assign_tier_with_category(m, "politics", CONFIG, depth_usd=300.0) == "liquid"

    # The reverse: thin Gamma-reported liquidity/volume, but real depth is deep.
    thin = _market(liquidityNum=10.0, volumeNum=10.0)
    assert assign_tier_with_category(thin, "politics", CONFIG, depth_usd=500.0) == "liquid"


def test_depth_none_falls_back_to_liquidity_and_volume():
    """No snapshot yet (brand-new market) -- depth_usd=None uses the
    pre-Phase-17 liquidity/volume path unchanged."""
    assert assign_tier_with_category(_market(), "politics", CONFIG, depth_usd=None) == "liquid"
    assert assign_tier_with_category(
        _market(liquidityNum=2000.0, volumeNum=8000.0), "politics", CONFIG, depth_usd=None
    ) == "tail"
    assert assign_tier_with_category(
        _market(liquidityNum=10.0, volumeNum=10.0), "politics", CONFIG, depth_usd=None
    ) == "ignored"


def test_depth_lookup_uses_latest_snapshot_bid_plus_ask(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    ts_old = NOW.replace(hour=1).isoformat(timespec="seconds")
    ts_new = NOW.replace(hour=11).isoformat(timespec="seconds")
    store.append([
        {"ts": ts_old, "condition_id": "0x1", "bid_depth_usd": 10.0, "ask_depth_usd": 10.0},
        {"ts": ts_new, "condition_id": "0x1", "bid_depth_usd": 150.0, "ask_depth_usd": 200.0},
        {"ts": ts_new, "condition_id": "0x2", "bid_depth_usd": None, "ask_depth_usd": 5.0},
    ])
    lookup = _depth_lookup(store, NOW + timedelta(hours=1))
    assert lookup["0x1"] == 350.0  # latest snapshot wins, not the older one
    assert lookup["0x2"] == 5.0    # null treated as 0, not dropped
    assert "0x3" not in lookup     # no snapshot at all -> absent, not zero


def test_sub_24h_crypto_pulse_detected():
    pulse = _market(
        category="Crypto",
        question="Bitcoin up or down today?",
        startDate="2026-07-02T00:00:00Z",
        endDate="2026-07-02T23:59:00Z",
    )
    assert is_sub_24h_crypto(pulse, NOW)
    assert not is_sub_24h_crypto(_market(), NOW)  # politics long-horizon


# --- Phase 16: negRisk event_id linking -------------------------------------

class FakeGammaClient:
    def __init__(self, events: list[GammaEvent]):
        self._events = events

    async def iter_events(self, **filters) -> list[GammaEvent]:
        return self._events


def _leg(cid, **kwargs) -> GammaMarket:
    return _market(conditionId=cid, **kwargs)


def test_sync_universe_links_negrisk_legs_into_one_event_id(tmp_path):
    legs = [_leg("0xa"), _leg("0xb"), _leg("0xc")]
    event = GammaEvent.model_validate({
        "slug": "cpi-range-event", "negRisk": True, "tags": [],
        "markets": [m.model_dump(by_alias=True) for m in legs],
    })
    gamma = FakeGammaClient([event])
    conn = db.connect(tmp_path / "lab.db")
    store = SnapshotStore(tmp_path / "snapshots")

    asyncio.run(sync_universe(gamma, conn, store, CONFIG))

    rows = conn.execute(
        "SELECT condition_id, event_id FROM markets WHERE condition_id IN ('0xa','0xb','0xc')"
    ).fetchall()
    event_ids = {r["event_id"] for r in rows}
    assert len(event_ids) == 1
    assert None not in event_ids
    conn.close()


def test_sync_universe_does_not_link_non_negrisk_event_legs(tmp_path):
    legs = [_leg("0xd"), _leg("0xe")]
    event = GammaEvent.model_validate({
        "slug": "unrelated-event", "negRisk": False, "tags": [],
        "markets": [m.model_dump(by_alias=True) for m in legs],
    })
    gamma = FakeGammaClient([event])
    conn = db.connect(tmp_path / "lab.db")
    store = SnapshotStore(tmp_path / "snapshots")

    asyncio.run(sync_universe(gamma, conn, store, CONFIG))

    rows = conn.execute(
        "SELECT condition_id, event_id FROM markets WHERE condition_id IN ('0xd','0xe')"
    ).fetchall()
    assert all(r["event_id"] is None for r in rows)
    conn.close()
