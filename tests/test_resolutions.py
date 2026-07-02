"""Resolution finality rules: record final payout, never a first report."""

from __future__ import annotations

from lab.api.gamma import GammaMarket
from lab.collect.resolutions import extract_final_payout


def _market(**kwargs) -> GammaMarket:
    base = {
        "conditionId": "0x1",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["111", "222"]',
        "closed": True,
        "outcomePrices": '["1", "0"]',
    }
    base.update(kwargs)
    return GammaMarket.model_validate(base)


def test_final_yes_payout():
    assert extract_final_payout(_market()) == (1.0, False)
    assert extract_final_payout(_market(outcomePrices='["0", "1"]')) == (0.0, False)


def test_not_final_while_open_or_unsettled():
    assert extract_final_payout(_market(closed=False)) is None
    # Price hasn't collapsed to 0/1 -> not a final payout.
    assert extract_final_payout(_market(outcomePrices='["0.97", "0.03"]')) is None


def test_open_dispute_blocks_recording():
    disputed_open = _market(umaResolutionStatuses='["disputed"]')
    assert extract_final_payout(disputed_open) is None

    disputed_final = _market(umaResolutionStatuses='["disputed", "resolved"]')
    assert extract_final_payout(disputed_final) == (1.0, True)
