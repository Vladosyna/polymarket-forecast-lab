"""SIMULATION-ONLY shadow portfolio. No real money exists anywhere here.

Entry (daily, liquid tier, M4): |edge| >= 0.05 AND spread <= 0.03 AND
top-of-book depth >= $500 on the entry side AND 0.05 < p_market < 0.95.
"Buy" and "sell" below refer exclusively to simulated positions.
"""

from __future__ import annotations

import logging
from typing import Any

from lab.store import db as dbmod
from lab.util import now_utc_iso

log = logging.getLogger(__name__)


def kelly_stake(bankroll: float, p: float, price: float, kelly_mult: float,
                per_market_cap: float) -> float:
    """Fractional Kelly for a binary contract bought at `price` paying 1.

    Full Kelly fraction: f* = (p - price) / (1 - price); scaled by kelly_mult
    and capped at per_market_cap of bankroll. Returns 0 when edge <= 0.
    """
    if not (0 < price < 1):
        return 0.0
    f_star = (p - price) / (1 - price)
    if f_star <= 0:
        return 0.0
    return bankroll * min(f_star * kelly_mult, per_market_cap)


def slippage_haircut(stake: float, depth_usd: float, coefficient: float,
                     cap: float) -> float:
    """Price penalty proportional to stake / visible depth, capped."""
    if depth_usd <= 0:
        return cap
    return min(cap, coefficient * stake / depth_usd)


def entry_check(p_model: float, p_market: float, spread: float | None,
                depth_entry_usd: float | None, config: dict[str, Any]) -> str | None:
    """Return 'YES'/'NO' side when all filters pass, else None."""
    scfg = config["shadow"]
    lo, hi = scfg["entry_price_bounds"]
    if not (lo < p_market < hi):
        return None
    if abs(p_model - p_market) < scfg["min_edge"]:
        return None
    if spread is None or spread > scfg["max_spread"]:
        return None
    if depth_entry_usd is None or depth_entry_usd < scfg["min_depth_usd"]:
        return None
    return "YES" if p_model > p_market else "NO"


def open_exposure(conn) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(stake_sim), 0) AS s FROM shadow_trades WHERE status='open'"
    ).fetchone()
    return float(row["s"])


def category_exposure(conn, category: str) -> float:
    row = conn.execute(
        """SELECT COALESCE(SUM(t.stake_sim), 0) AS s FROM shadow_trades t
           JOIN markets m ON m.condition_id = t.condition_id
           WHERE t.status='open' AND m.category = ?""",
        (category,),
    ).fetchone()
    return float(row["s"])


def run_shadow_entries(conn, store, config: dict[str, Any],
                       model_id: str = "m4_ensemble") -> int:
    """One daily entry pass over today's model forecasts on the liquid tier."""
    scfg = config["shadow"]
    bankroll = scfg["bankroll_sim_usd"]
    rows = conn.execute(
        """
        SELECT f.condition_id, f.p_yes, f.p_market_at_ts, f.spread_at_ts, m.category
        FROM forecasts f
        JOIN markets m ON m.condition_id = f.condition_id
        JOIN (SELECT condition_id, MAX(ts) AS ts FROM forecasts
              WHERE model_id = ? AND date(ts) = date('now') GROUP BY condition_id) latest
          ON latest.condition_id = f.condition_id AND latest.ts = f.ts
        WHERE f.model_id = ? AND m.tier = 'liquid'
          AND f.condition_id NOT IN (SELECT condition_id FROM shadow_trades WHERE status='open')
        """,
        (model_id, model_id),
    ).fetchall()

    from datetime import timedelta

    from lab.store.snapshots import utc_date_str
    from lab.util import now_utc

    latest = store.latest_per_market(
        [utc_date_str(now_utc() - timedelta(days=d)) for d in range(2)]
    )
    snap = {r["condition_id"]: r for r in latest.to_dicts()} if not latest.is_empty() else {}

    opened = 0
    for r in rows:
        book = snap.get(r["condition_id"])
        if book is None:
            continue
        side = entry_check(
            r["p_yes"], r["p_market_at_ts"], r["spread_at_ts"],
            book["ask_depth_usd"] if r["p_yes"] > r["p_market_at_ts"] else book["bid_depth_usd"],
            config,
        )
        if side is None:
            continue
        if side == "YES":
            raw_price = book["best_ask"] if book["best_ask"] is not None else r["p_market_at_ts"]
            p_win = r["p_yes"]
        else:
            best_bid = book["best_bid"] if book["best_bid"] is not None else r["p_market_at_ts"]
            raw_price = 1 - best_bid  # simulated NO fill at 1 - bid
            p_win = 1 - r["p_yes"]
        stake = kelly_stake(bankroll, p_win, raw_price, scfg["kelly_fraction"],
                            scfg["per_market_cap"])
        if stake <= 0:
            continue
        cat_cap = scfg["per_category_cap"] * bankroll
        if category_exposure(conn, r["category"]) + stake > cat_cap:
            continue
        depth = book["ask_depth_usd"] if side == "YES" else book["bid_depth_usd"]
        entry_price = min(
            0.999,
            raw_price + slippage_haircut(stake, depth or 0.0,
                                         scfg["slippage_coefficient"], scfg["slippage_cap"]),
        )
        edge = abs(r["p_yes"] - r["p_market_at_ts"])
        conn.execute(
            """INSERT INTO shadow_trades (opened_ts, condition_id, token_side, entry_price,
                                          p_model, p_market, edge, stake_sim, kelly_frac, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (now_utc_iso(), r["condition_id"], side, entry_price, r["p_yes"],
             r["p_market_at_ts"], edge, stake, scfg["kelly_fraction"]),
        )
        opened += 1
    conn.commit()
    log.info("shadow entries (SIMULATION)", extra={"ctx": {"opened": opened}})
    return opened


def settle_resolved(conn) -> int:
    """Close open sim positions whose markets resolved. Hold-to-resolution v1."""
    rows = conn.execute(
        """SELECT t.id, t.token_side, t.entry_price, t.stake_sim, r.payout_yes, r.resolved_ts
           FROM shadow_trades t JOIN resolutions r ON r.condition_id = t.condition_id
           WHERE t.status = 'open'"""
    ).fetchall()
    settled = 0
    for r in rows:
        won = (r["payout_yes"] == 1.0) == (r["token_side"] == "YES")
        exit_price = 1.0 if won else 0.0
        shares = r["stake_sim"] / r["entry_price"]
        pnl = shares * exit_price - r["stake_sim"]
        conn.execute(
            "UPDATE shadow_trades SET status='resolved', exit_ts=?, exit_price=?, pnl_sim=? WHERE id=?",
            (r["resolved_ts"], exit_price, pnl, r["id"]),
        )
        settled += 1
    conn.commit()
    return settled


def portfolio_summary(conn, store, config: dict[str, Any]) -> dict[str, Any]:
    realized = conn.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(pnl_sim),0) AS pnl,
                  COALESCE(SUM(pnl_sim > 0),0) AS wins
           FROM shadow_trades WHERE status='resolved'"""
    ).fetchone()

    # Mark-to-market for the open book against latest snapshots.
    from datetime import timedelta

    from lab.store.snapshots import utc_date_str
    from lab.util import now_utc

    latest = store.latest_per_market(
        [utc_date_str(now_utc() - timedelta(days=d)) for d in range(2)]
    )
    mids = {r["condition_id"]: r["mid"] for r in latest.to_dicts()} if not latest.is_empty() else {}
    unrealized = 0.0
    open_rows = conn.execute(
        "SELECT condition_id, token_side, entry_price, stake_sim FROM shadow_trades WHERE status='open'"
    ).fetchall()
    for r in open_rows:
        mid = mids.get(r["condition_id"])
        if mid is None:
            continue
        mark = mid if r["token_side"] == "YES" else 1 - mid
        unrealized += (r["stake_sim"] / r["entry_price"]) * mark - r["stake_sim"]

    # Max drawdown over the realized P&L path (by exit time).
    path = [r["pnl_sim"] for r in conn.execute(
        "SELECT pnl_sim FROM shadow_trades WHERE status='resolved' ORDER BY exit_ts"
    )]
    peak = dd = cum = 0.0
    for pnl in path:
        cum += pnl
        peak = max(peak, cum)
        dd = min(dd, cum - peak)

    return {
        "label": "SIMULATION",
        "bankroll_sim": config["shadow"]["bankroll_sim_usd"],
        "resolved_trades": realized["n"],
        "realized_pnl_sim": realized["pnl"],
        "hit_rate": (realized["wins"] / realized["n"]) if realized["n"] else None,
        "open_trades": len(open_rows),
        "unrealized_pnl_sim": unrealized,
        "max_drawdown_sim": dd,
    }
