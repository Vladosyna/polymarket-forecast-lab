"""Phase 15: real, sourced venue trading-fee schedule for the shadow
portfolio's net-of-cost accounting (brief section 8/15).

Both Polymarket and Kalshi publish PRICE-DEPENDENT taker-fee formulas (peak
at the 50c price point, ~0 at the 1c/99c extremes) -- not flat bps of stake:
    Polymarket: fee_per_share    = category_rate * p * (1-p)
                (docs.polymarket.com/trading/fees, fetched 2026-07-09)
    Kalshi:     fee_per_contract = 0.07 * C * (1-C), taker only
                (kalshi.com/fee-schedule, effective 2026-07-07)

Buying N shares/contracts at price p costs stake = N*p, so:
    total_fee = N * rate * p * (1-p)
              = (stake / p) * rate * p * (1-p)
              = stake * rate * (1-p)
`fee_usd_for` below implements exactly this derived formula, using whichever
price the simulated fill actually happened at.

Our shadow portfolio always fills as a TAKER -- it crosses the best bid/ask
plus a slippage haircut (brief section 8) and never rests a limit order --
so maker fees/rebates (both venues charge/pay less to makers) are never
modeled here; only the taker rate applies.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from lab.util import PROJECT_ROOT

log = logging.getLogger(__name__)

DEFAULT_FEE_SCHEDULE_PATH = PROJECT_ROOT / "data" / "fee_schedule.yaml"

_logged_unknown_venues: set[str] = set()


def load_fee_schedule(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_FEE_SCHEDULE_PATH
    if not p.exists():
        return {"version": 0, "schedule": []}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("schedule", [])
    return data


def taker_rate_for(schedule: dict[str, Any], venue: str, category: str | None,
                   as_of_ts: str) -> float:
    """Latest entry with effective_from <= as_of_ts for (venue, category),
    falling back to (venue, 'default'). An entirely unknown venue defaults
    to 0.0, logged once per venue -- not raised, fail-soft per guardrail 9."""
    entries = [e for e in schedule.get("schedule", [])
              if e.get("venue") == venue and e.get("effective_from", "") <= as_of_ts]
    if not entries:
        if venue not in _logged_unknown_venues:
            log.warning("fee schedule: no entry for venue, defaulting to 0",
                       extra={"ctx": {"venue": venue}})
            _logged_unknown_venues.add(venue)
        return 0.0
    for cat in (category, "default"):
        matching = [e for e in entries if e.get("category") == cat]
        if matching:
            latest = max(matching, key=lambda e: e["effective_from"])
            return float(latest["taker_rate"])
    return 0.0


def fee_usd_for(schedule: dict[str, Any], venue: str, category: str | None,
                entry_price: float, stake_usd: float, as_of_ts: str) -> float:
    """stake * rate * (1 - entry_price) -- see module docstring for the
    per-share-formula derivation. entry_price is the price of the side
    actually bought (already includes the slippage haircut)."""
    rate = taker_rate_for(schedule, venue, category, as_of_ts)
    return stake_usd * rate * (1.0 - entry_price)
