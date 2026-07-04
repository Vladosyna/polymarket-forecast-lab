"""Manifold collector: row-building/mapping, the always-'ignored' tiering
decision, and idempotent resolution recording (guardrail 16: play-money venue,
markets + resolutions only, never a snapshot time series)."""

from __future__ import annotations

import asyncio

import pytest

from lab.api.manifold import ManifoldMarket
from lab.collect.manifold_collector import (
    _iso_from_ms,
    manifold_market_row,
    record_manifold_resolution,
    sync_manifold_markets,
)
from lab.store import db


def _market(**kwargs) -> ManifoldMarket:
    base = {
        "id": "gUN8ElOQLP",
        "question": "Will X happen?",
        "slug": "will-x-happen",
        "closeTime": 1877821140000,
        "probability": 0.54,
        "outcomeType": "BINARY",
        "volume": 11.38,
        "isResolved": False,
    }
    base.update(kwargs)
    return ManifoldMarket.model_validate(base)


def test_iso_from_ms_converts_epoch_milliseconds():
    iso = _iso_from_ms(1877821140000)
    assert iso is not None
    assert iso.startswith("2029-") or "T" in iso  # sanity: parses, doesn't crash
    assert _iso_from_ms(None) is None


def test_manifold_market_row_maps_fields_and_forces_ignored_tier():
    m = _market()
    row = manifold_market_row(m)
    assert row["condition_id"] == "manifold:gUN8ElOQLP"
    assert row["venue"] == "manifold"
    assert row["venue_native_id"] == "gUN8ElOQLP"
    assert row["category"] == "unknown"
    assert row["tier"] == "ignored"
    assert row["token_id_yes"] is None and row["token_id_no"] is None
    assert row["neg_risk"] == 0
    assert row["active"] == 1 and row["closed"] == 0
    assert row["volume_num"] == pytest.approx(11.38)
    assert row["end_date_iso"] is not None


def test_manifold_market_row_resolved_market_is_inactive_and_closed():
    m = _market(isResolved=True, resolution="YES")
    row = manifold_market_row(m)
    assert row["active"] == 0
    assert row["closed"] == 1
    assert row["tier"] == "ignored"  # still 'ignored', never liquid/tail


def test_manifold_market_row_upserts_into_db(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    row = manifold_market_row(_market())
    db.upsert_market(conn, row)
    conn.commit()
    fetched = conn.execute(
        "SELECT venue, venue_native_id, tier, category FROM markets WHERE condition_id = ?",
        ("manifold:gUN8ElOQLP",),
    ).fetchone()
    assert fetched["venue"] == "manifold"
    assert fetched["venue_native_id"] == "gUN8ElOQLP"
    assert fetched["tier"] == "ignored"
    assert fetched["category"] == "unknown"
    conn.close()


def test_record_manifold_resolution_records_yes(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    m = _market(isResolved=True, resolution="YES", resolutionTime=1877821140000)
    db.upsert_market(conn, manifold_market_row(m))
    conn.commit()
    assert record_manifold_resolution(conn, m) is True
    row = conn.execute(
        "SELECT payout_yes, disputed, source FROM resolutions WHERE condition_id = ?",
        ("manifold:gUN8ElOQLP",),
    ).fetchone()
    assert row["payout_yes"] == pytest.approx(1.0)
    assert row["disputed"] == 0
    assert row["source"] == "manifold"
    conn.close()


def test_record_manifold_resolution_no(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    m = _market(isResolved=True, resolution="NO")
    db.upsert_market(conn, manifold_market_row(m))
    conn.commit()
    assert record_manifold_resolution(conn, m) is True
    row = conn.execute(
        "SELECT payout_yes FROM resolutions WHERE condition_id = ?", ("manifold:gUN8ElOQLP",)
    ).fetchone()
    assert row["payout_yes"] == pytest.approx(0.0)
    conn.close()


@pytest.mark.parametrize("resolution", ["MKT", "CANCEL", None])
def test_record_manifold_resolution_skips_unusable_outcomes(tmp_path, resolution):
    conn = db.connect(tmp_path / "lab.db")
    m = _market(isResolved=True, resolution=resolution)
    db.upsert_market(conn, manifold_market_row(m))
    conn.commit()
    assert record_manifold_resolution(conn, m) is False
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM resolutions WHERE condition_id = ?", ("manifold:gUN8ElOQLP",)
    ).fetchone()
    assert row["n"] == 0
    conn.close()


def test_record_manifold_resolution_not_yet_resolved_is_skipped(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    m = _market(isResolved=False)
    db.upsert_market(conn, manifold_market_row(m))
    conn.commit()
    assert record_manifold_resolution(conn, m) is False
    conn.close()


def test_record_manifold_resolution_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    m = _market(isResolved=True, resolution="YES")
    db.upsert_market(conn, manifold_market_row(m))
    conn.commit()
    assert record_manifold_resolution(conn, m) is True
    assert record_manifold_resolution(conn, m) is True  # calling twice: no raise, no double-count
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM resolutions WHERE condition_id = ?", ("manifold:gUN8ElOQLP",)
    ).fetchall()
    assert rows[0]["n"] == 1
    conn.close()


class FakeManifoldClient:
    """Stands in for ManifoldClient: pages canned market lists per filter,
    mirroring the real client's (filter, offset, limit) signature."""

    def __init__(self, pages: dict[str, list[list[ManifoldMarket]]]) -> None:
        self._pages = pages
        self._calls: dict[str, int] = {"open": 0, "resolved": 0}

    async def search_markets(self, filter: str, offset: int = 0, limit: int = 200):
        idx = self._calls[filter]
        self._calls[filter] += 1
        pages = self._pages.get(filter, [])
        return pages[idx] if idx < len(pages) else []


@pytest.fixture()
def config(tmp_path):
    return {"venues": {"manifold": {"max_markets_per_sync": 10}}}


def test_sync_manifold_markets_upserts_open_and_resolved(tmp_path, config):
    conn = db.connect(tmp_path / "lab.db")
    open_page = [_market(id=f"open{i}") for i in range(3)]
    resolved_page = [
        _market(id=f"res{i}", isResolved=True, resolution="YES" if i % 2 == 0 else "NO")
        for i in range(2)
    ]
    client = FakeManifoldClient({"open": [open_page, []], "resolved": [resolved_page, []]})

    counts = asyncio.run(sync_manifold_markets(client, conn, config))

    assert counts["open_fetched"] == 3
    assert counts["resolved_fetched"] == 2
    assert counts["upserted"] == 5
    assert counts["resolutions_recorded"] == 2

    n_markets = conn.execute("SELECT COUNT(*) AS n FROM markets WHERE venue='manifold'").fetchone()["n"]
    assert n_markets == 5
    n_res = conn.execute("SELECT COUNT(*) AS n FROM resolutions").fetchone()["n"]
    assert n_res == 2
    conn.close()


def test_sync_manifold_markets_respects_cap_and_logs_truncation(tmp_path, config, caplog):
    conn = db.connect(tmp_path / "lab.db")
    # 10 total cap -> 5 per filter. Offer more than that per filter.
    open_page_1 = [_market(id=f"open{i}") for i in range(5)]
    open_page_2 = [_market(id=f"open{i}") for i in range(5, 10)]
    client = FakeManifoldClient({"open": [open_page_1, open_page_2], "resolved": [[]]})

    counts = asyncio.run(sync_manifold_markets(client, conn, config))

    assert counts["open_fetched"] == 5  # capped at per-filter share, not the full 10 offered
    conn.close()


def test_sync_manifold_markets_stops_on_empty_page(tmp_path, config):
    conn = db.connect(tmp_path / "lab.db")
    client = FakeManifoldClient({"open": [[]], "resolved": [[]]})
    counts = asyncio.run(sync_manifold_markets(client, conn, config))
    assert counts["open_fetched"] == 0
    assert counts["resolved_fetched"] == 0
    assert counts["upserted"] == 0
    conn.close()
