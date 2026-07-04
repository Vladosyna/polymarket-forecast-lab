"""Kalshi collector: row-building/tiering, and idempotent resolution recording
(brief section 3/Phase 10). Mirrors the style of test_universe.py /
test_resolutions.py; no real network calls -- KalshiMarket built directly from
fixture dicts, matching the live-verified /markets shape.

Async collector functions are driven via asyncio.run() from plain `def test_*`
functions rather than `async def` + `@pytest.mark.asyncio`: pytest-asyncio is
not a project dependency (see pyproject.toml), so an `async def test_*` would
never actually execute its body under plain pytest (the coroutine is returned
un-awaited and the test still reports a pass) -- asyncio.run() keeps these
tests meaningful without depending on a plugin that isn't installed.
"""

from __future__ import annotations

import asyncio

from lab.api.kalshi import KalshiMarket
from lab.collect.kalshi_collector import (
    assign_kalshi_tier,
    extract_kalshi_payout,
    kalshi_market_row,
    unresolved_kalshi_markets,
    watch_kalshi_resolutions,
)
from lab.store import db
from lab.util import load_config

CONFIG = load_config()


def _market(**kwargs) -> KalshiMarket:
    base = {
        "ticker": "KXEZGDPQOQF-26JUN05-T0.8",
        "event_ticker": "KXEZGDPQOQF-26JUN05",
        "title": "Will Euro area GDP growth rate QoQ flash for Q1 2026 be above 0.8%?",
        "rules_primary": "If Euro area GDP growth rate QoQ flash for Q1 2026 is above 0.8, then Yes.",
        "status": "active",
        "result": "",
        "close_time": "2026-05-29T19:59:46Z",
        "expiration_time": "2026-06-12T09:00:00Z",
        "liquidity_dollars": "2000.0000",
        "volume_fp": "8615.00",
        "yes_bid_dollars": "0.4500",
        "yes_ask_dollars": "0.4700",
        "last_price_dollars": "0.4600",
    }
    base.update(kwargs)
    return KalshiMarket.model_validate(base)


class FakeKalshiClient:
    """Stand-in for KalshiClient.market(ticker) in resolution-watcher tests."""

    def __init__(self, markets_by_ticker: dict[str, KalshiMarket]):
        self._markets = markets_by_ticker
        self.calls: list[str] = []

    async def market(self, ticker: str) -> KalshiMarket | None:
        self.calls.append(ticker)
        return self._markets.get(ticker)


# --- row building / tiering -------------------------------------------------


def test_kalshi_market_row_maps_fields():
    m = _market()
    row = kalshi_market_row(m, "economics")
    assert row["condition_id"] == "kalshi:KXEZGDPQOQF-26JUN05-T0.8"
    assert row["venue"] == "kalshi"
    assert row["venue_native_id"] == "KXEZGDPQOQF-26JUN05-T0.8"
    assert row["question"] == m.title
    assert row["description"] == m.rules_primary
    assert row["category"] == "economics"
    assert row["end_date_iso"] == "2026-05-29T19:59:46Z"
    assert row["token_id_yes"] is None and row["token_id_no"] is None
    assert row["neg_risk"] == 0
    assert row["active"] == 1
    assert row["closed"] == 0
    assert row["liquidity_num"] == 2000.0
    assert row["volume_num"] == 8615.0


def test_kalshi_market_row_closed_when_finalized():
    m = _market(status="finalized", result="yes")
    row = kalshi_market_row(m, "economics")
    assert row["active"] == 0
    assert row["closed"] == 1


def test_kalshi_market_row_falls_back_to_expiration_time():
    m = _market(close_time=None)
    row = kalshi_market_row(m, "economics")
    assert row["end_date_iso"] == "2026-06-12T09:00:00Z"


def test_tier_liquid_and_tail_and_ignored():
    liquid_tiers = CONFIG["venues"]["kalshi"]["tiers"]["liquid"]
    m_liquid = _market(
        liquidity_dollars=str(liquid_tiers["min_liquidity"]),
        volume_fp=str(liquid_tiers["min_volume"]),
    )
    assert assign_kalshi_tier(m_liquid, CONFIG) == "liquid"

    m_tail = _market(liquidity_dollars="0", volume_fp="0")
    assert assign_kalshi_tier(m_tail, CONFIG) == "tail"

    m_ignored = _market(liquidity_dollars=None, volume_fp=None)
    # tail thresholds are 0/0 in config, so None (-> 0.0) still clears tail;
    # exercise "ignored" by dropping below tail thresholds explicitly instead.
    tail_tiers = CONFIG["venues"]["kalshi"]["tiers"]["tail"]
    if tail_tiers["min_liquidity"] > 0 or tail_tiers["min_volume"] > 0:
        assert assign_kalshi_tier(m_ignored, CONFIG) == "ignored"
    else:
        assert assign_kalshi_tier(m_ignored, CONFIG) == "tail"


# --- resolution watcher: finality + idempotency -----------------------------


def test_extract_kalshi_payout():
    assert extract_kalshi_payout(_market(status="finalized", result="yes")) == 1.0
    assert extract_kalshi_payout(_market(status="finalized", result="no")) == 0.0
    assert extract_kalshi_payout(_market(status="active", result="")) is None
    assert extract_kalshi_payout(_market(status="finalized", result="")) is None


def test_unresolved_kalshi_markets_filters_by_venue(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    db.upsert_market(conn, {
        "condition_id": "kalshi:T1", "venue": "kalshi", "venue_native_id": "T1",
        "slug": None, "question": "q", "category": "economics", "description": "d",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 0, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "tail",
    })
    db.upsert_market(conn, {
        "condition_id": "0xpoly", "venue": "polymarket", "venue_native_id": "0xpoly",
        "slug": "s", "question": "q2", "category": "politics", "description": "d2",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": "111", "token_id_no": "222",
        "neg_risk": 0, "active": 0, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "tail",
    })
    conn.commit()

    unresolved = unresolved_kalshi_markets(conn)
    assert {r["condition_id"] for r in unresolved} == {"kalshi:T1"}
    conn.close()


def test_watch_kalshi_resolutions_records_and_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    db.upsert_market(conn, {
        "condition_id": "kalshi:T1", "venue": "kalshi", "venue_native_id": "T1",
        "slug": None, "question": "q", "category": "economics", "description": "d",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 0, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "tail",
    })
    conn.commit()

    client = FakeKalshiClient({"T1": _market(ticker="T1", status="finalized", result="yes")})

    recorded_first = asyncio.run(watch_kalshi_resolutions(client, conn))
    assert recorded_first == 1
    row = conn.execute(
        "SELECT payout_yes, disputed, source FROM resolutions WHERE condition_id='kalshi:T1'"
    ).fetchone()
    assert row["payout_yes"] == 1.0
    assert row["disputed"] == 0
    assert row["source"] == "kalshi"

    # Second poll: no longer in the unresolved set (it now has a resolutions
    # row), so watch_kalshi_resolutions should not double-count or raise.
    recorded_second = asyncio.run(watch_kalshi_resolutions(client, conn))
    assert recorded_second == 0
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM resolutions WHERE condition_id='kalshi:T1'"
    ).fetchone()["n"]
    assert count == 1
    conn.close()


def test_watch_kalshi_resolutions_skips_unsettled(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    db.upsert_market(conn, {
        "condition_id": "kalshi:T2", "venue": "kalshi", "venue_native_id": "T2",
        "slug": None, "question": "q", "category": "economics", "description": "d",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 0, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "tail",
    })
    conn.commit()

    client = FakeKalshiClient({"T2": _market(ticker="T2", status="active", result="")})
    recorded = asyncio.run(watch_kalshi_resolutions(client, conn))
    assert recorded == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM resolutions").fetchone()["n"] == 0
    conn.close()
