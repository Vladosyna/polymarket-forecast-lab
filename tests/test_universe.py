"""Universe tiering and filtering rules."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from lab.api.gamma import GammaEvent, GammaMarket
from lab.collect.universe import (
    _depth_lookup,
    _mid_lookup,
    assign_tier,
    assign_tier_with_category,
    is_sub_24h_crypto,
    log_universe_exclusion,
    sync_universe,
)
from lab.store import db
from lab.store.snapshots import SnapshotStore
from lab.util import load_config, now_utc

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


def test_equity_price_target_is_ignored():
    """A real leak found live: category="unknown" price-target markets
    (market cap / valuation / FDV thresholds) are equity-side martingale
    underlyings, same as crypto price-target markets (brief section 3:
    "ALL crypto/equity price-target markets") -- the current keyword filter
    only caught the crypto half."""
    cases = [
        _market(category="unknown", question="Extended FDV above $800M one day after launch?"),
        _market(category="unknown",
                question="Will Anthropic's valuation hit (HIGH) $2.0T by December 31?"),
        _market(category="unknown", question="OpenAI IPO closing market cap above $1T?"),
        _market(category="unknown",
                question="Will NVIDIA be the largest company in the world by market cap on December 31?"),
        _market(category="unknown", question="Some token's fully diluted valuation exceeds $5B?"),
    ]
    for m in cases:
        assert assign_tier(m, CONFIG) == "ignored", m.question


def test_equity_event_markets_are_not_excluded():
    """Corporate-event questions (acquisition speculation, IPO timing) are
    NOT price-target markets -- the resolution isn't a price/valuation
    threshold, so they stay forecastable and must not be caught by the new
    equity-price-target heuristic."""
    cases = [
        _market(category="unknown", question="Will Nebius Group be acquired before 2027?"),
        _market(category="unknown", question="Will OpenAI IPO by December 31 2026?"),
        _market(category="unknown", question="Will GameStop acquire eBay?"),
    ]
    for m in cases:
        assert assign_tier(m, CONFIG) != "ignored", m.question


def test_depth_based_tiering_overrides_liquidity_and_volume():
    """Phase 17 item 2: once a snapshot exists, real order-book depth governs
    tiering -- not Gamma's self-reported liquidity_num/volume_num."""
    m = _market()  # liquidityNum/volumeNum say "liquid" via the fallback path
    # Thin real depth: same market tiers down to 'tail' or 'ignored' on depth.
    assert assign_tier_with_category(m, "politics", CONFIG, depth_usd=50.0)[0] == "tail"
    assert assign_tier_with_category(m, "politics", CONFIG, depth_usd=1.0)[0] == "ignored"
    assert assign_tier_with_category(m, "politics", CONFIG, depth_usd=300.0)[0] == "liquid"

    # The reverse: thin Gamma-reported liquidity/volume, but real depth is deep.
    thin = _market(liquidityNum=10.0, volumeNum=10.0)
    assert assign_tier_with_category(thin, "politics", CONFIG, depth_usd=500.0)[0] == "liquid"


def test_depth_none_falls_back_to_liquidity_and_volume():
    """No snapshot yet (brand-new market) -- depth_usd=None uses the
    pre-Phase-17 liquidity/volume path unchanged."""
    assert assign_tier_with_category(_market(), "politics", CONFIG, depth_usd=None)[0] == "liquid"
    assert assign_tier_with_category(
        _market(liquidityNum=2000.0, volumeNum=8000.0), "politics", CONFIG, depth_usd=None
    )[0] == "tail"
    assert assign_tier_with_category(
        _market(liquidityNum=10.0, volumeNum=10.0), "politics", CONFIG, depth_usd=None
    )[0] == "ignored"


# --- Phase 15: universe_log reason codes ------------------------------------

def test_reason_code_no_orderbook():
    _, reason = assign_tier_with_category(_market(enableOrderBook=False), "politics", CONFIG)
    assert reason == "no_orderbook"
    _, reason = assign_tier_with_category(_market(acceptingOrders=False), "politics", CONFIG)
    assert reason == "no_orderbook"


def test_reason_code_crypto_price_target_via_category():
    m = _market(category="Crypto", question="Will Bitcoin close above 200k?")
    tier, reason = assign_tier_with_category(m, "crypto", CONFIG)
    assert tier == "ignored"
    assert reason == "crypto_price_target"


def test_reason_code_crypto_price_target_via_keyword():
    m = _market(category="unknown", question="OpenAI IPO closing market cap above $1T?")
    tier, reason = assign_tier_with_category(m, "unknown", CONFIG)
    assert tier == "ignored"
    assert reason == "crypto_price_target"


def test_reason_code_low_liquidity():
    tier, reason = assign_tier_with_category(
        _market(liquidityNum=10.0, volumeNum=10.0), "politics", CONFIG
    )
    assert tier == "ignored"
    assert reason == "low_liquidity"
    tier, reason = assign_tier_with_category(_market(), "politics", CONFIG, depth_usd=1.0)
    assert tier == "ignored"
    assert reason == "low_liquidity"


def test_reason_code_none_when_liquid_or_tail():
    _, reason = assign_tier_with_category(_market(), "politics", CONFIG)
    assert reason is None
    _, reason = assign_tier_with_category(
        _market(liquidityNum=2000.0, volumeNum=8000.0), "politics", CONFIG
    )
    assert reason is None


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


def test_mid_lookup_uses_latest_snapshot_mid(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    ts_old = NOW.replace(hour=1).isoformat(timespec="seconds")
    ts_new = NOW.replace(hour=11).isoformat(timespec="seconds")
    store.append([
        {"ts": ts_old, "condition_id": "0x1", "mid": 0.50},
        {"ts": ts_new, "condition_id": "0x1", "mid": 0.97},
    ])
    lookup = _mid_lookup(store, NOW + timedelta(hours=1))
    assert lookup["0x1"] == 0.97  # latest snapshot wins
    assert "0x2" not in lookup    # no snapshot at all -> absent, not a verdict


def test_log_universe_exclusion_writes_row(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    log_universe_exclusion(conn, "polymarket", "0x1", "low_liquidity")
    conn.commit()
    row = conn.execute(
        "SELECT venue, venue_native_id, reason_code FROM universe_log"
    ).fetchone()
    assert (row["venue"], row["venue_native_id"], row["reason_code"]) == (
        "polymarket", "0x1", "low_liquidity",
    )
    conn.close()


def test_sync_universe_logs_exclusions_with_reason_codes(tmp_path):
    """End-to-end: a non-binary market, a sub-24h crypto pulse, and a
    low-liquidity market each land in universe_log with the right reason,
    via the real sync_universe() code path (not just the unit-level
    assign_tier_with_category checks above)."""
    non_binary = _leg("0xnb", outcomes='["A", "B", "C"]', clobTokenIds='["1","2","3"]')
    pulse = _leg("0xpulse", category="Crypto", question="Bitcoin up or down today?",
                 startDate="2026-07-02T00:00:00Z", endDate="2026-07-02T23:59:00Z")
    thin = _leg("0xthin", liquidityNum=10.0, volumeNum=10.0)
    event = GammaEvent.model_validate({
        "slug": "mixed-event", "negRisk": False, "tags": [],
        "markets": [m.model_dump(by_alias=True) for m in [non_binary, pulse, thin]],
    })
    gamma = FakeGammaClient([event])
    conn = db.connect(tmp_path / "lab.db")
    store = SnapshotStore(tmp_path / "snapshots")

    asyncio.run(sync_universe(gamma, conn, store, CONFIG))

    rows = conn.execute(
        "SELECT venue_native_id, reason_code FROM universe_log ORDER BY venue_native_id"
    ).fetchall()
    by_id = {r["venue_native_id"]: r["reason_code"] for r in rows}
    assert by_id["0xnb"] == "non_binary"
    assert by_id["0xpulse"] == "crypto_price_target"
    assert by_id["0xthin"] == "low_liquidity"
    conn.close()


def test_sync_universe_logs_tail_price_for_out_of_bounds_mid(tmp_path):
    """A liquid market priced outside forecast_price_bounds is excluded as a
    forecast TARGET only -- it keeps its tier, but still gets a tail_price
    universe_log row (Phase 15)."""
    m = _leg("0xtail")
    event = GammaEvent.model_validate({
        "slug": "tail-event", "negRisk": False, "tags": [],
        "markets": [m.model_dump(by_alias=True)],
    })
    gamma = FakeGammaClient([event])
    conn = db.connect(tmp_path / "lab.db")
    store = SnapshotStore(tmp_path / "snapshots")
    # sync_universe() calls now_utc() internally (real current time), not the
    # fixture's fixed NOW -- the snapshot must be timestamped near real "now"
    # so _mid_lookup's days_back window actually picks it up. Depth fields
    # must clear the liquid threshold too, since _depth_lookup treats a
    # present-but-fieldless row as depth=0 (null-filled), not absent.
    store.append([{
        "ts": now_utc().isoformat(timespec="seconds"), "condition_id": "0xtail",
        "mid": 0.97, "bid_depth_usd": 200.0, "ask_depth_usd": 200.0,
    }])

    asyncio.run(sync_universe(gamma, conn, store, CONFIG))

    row = conn.execute(
        "SELECT tier FROM markets WHERE condition_id = '0xtail'"
    ).fetchone()
    assert row["tier"] == "liquid"  # stays liquid -- not excluded from the universe
    reasons = {r["reason_code"] for r in conn.execute(
        "SELECT reason_code FROM universe_log WHERE venue_native_id = '0xtail'"
    ).fetchall()}
    assert "tail_price" in reasons
    conn.close()


def test_sync_universe_no_tail_price_verdict_without_a_snapshot(tmp_path):
    """No snapshot yet for a market -> no tail_price verdict this cycle, not
    a false 'in bounds' assumption (mirrors _mid_lookup's absent contract)."""
    m = _leg("0xnosnap")
    event = GammaEvent.model_validate({
        "slug": "no-snap-event", "negRisk": False, "tags": [],
        "markets": [m.model_dump(by_alias=True)],
    })
    gamma = FakeGammaClient([event])
    conn = db.connect(tmp_path / "lab.db")
    store = SnapshotStore(tmp_path / "snapshots")

    asyncio.run(sync_universe(gamma, conn, store, CONFIG))

    reasons = {r["reason_code"] for r in conn.execute(
        "SELECT reason_code FROM universe_log WHERE venue_native_id = '0xnosnap'"
    ).fetchall()}
    assert "tail_price" not in reasons
    conn.close()


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
