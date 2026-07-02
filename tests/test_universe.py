"""Universe tiering and filtering rules."""

from __future__ import annotations

from datetime import datetime, timezone

from lab.api.gamma import GammaMarket
from lab.collect.universe import assign_tier, is_sub_24h_crypto
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


def test_sub_24h_crypto_pulse_detected():
    pulse = _market(
        category="Crypto",
        question="Bitcoin up or down today?",
        startDate="2026-07-02T00:00:00Z",
        endDate="2026-07-02T23:59:00Z",
    )
    assert is_sub_24h_crypto(pulse, NOW)
    assert not is_sub_24h_crypto(_market(), NOW)  # politics long-horizon
