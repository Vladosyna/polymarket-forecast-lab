"""Shadow portfolio (SIMULATION): entry filters, Kelly math, slippage, caps."""

from __future__ import annotations

import pytest

from lab.shadow.portfolio import (
    entry_check,
    kelly_stake,
    run_shadow_entries,
    settle_resolved,
    slippage_haircut,
)
from lab.store import db
from lab.store.snapshots import SnapshotStore, floor_ts_bucket
from lab.util import load_config, now_utc


@pytest.fixture()
def config(tmp_path):
    cfg = load_config()
    cfg["storage"] = {
        "db_path": str(tmp_path / "lab.db"),
        "snapshots_dir": str(tmp_path / "snapshots"),
        "models_dir": str(tmp_path / "models"),
        "logs_dir": str(tmp_path / "logs"),
        "reports_dir": str(tmp_path / "reports"),
    }
    return cfg


def test_kelly_hand_computed():
    # p=0.6, price=0.5 -> f* = 0.1/0.5 = 0.2; 0.2x Kelly on $10k = $400
    assert kelly_stake(10000, 0.6, 0.5, kelly_mult=0.2, per_market_cap=0.05) == pytest.approx(400)
    # per-market cap binds: f*=0.5 -> 0.2x = 0.1 > 5% cap -> $500
    assert kelly_stake(10000, 0.75, 0.5, 0.2, 0.05) == pytest.approx(500)
    assert kelly_stake(10000, 0.4, 0.5, 0.2, 0.05) == 0.0  # negative edge


def test_slippage_haircut():
    assert slippage_haircut(20, 1000, coefficient=0.5, cap=0.02) == pytest.approx(0.01)
    assert slippage_haircut(10000, 1000, 0.5, 0.02) == 0.02  # capped
    assert slippage_haircut(100, 0, 0.5, 0.02) == 0.02       # no depth -> worst case


def test_entry_filters(config):
    ok = dict(p_model=0.70, p_market=0.60, spread=0.02, depth_entry_usd=800.0)
    assert entry_check(**ok, config=config) == "YES"
    assert entry_check(0.50, 0.60, 0.02, 800.0, config) == "NO"
    # Each filter individually blocks entry:
    assert entry_check(0.63, 0.60, 0.02, 800.0, config) is None   # edge < 0.05
    assert entry_check(0.70, 0.60, 0.05, 800.0, config) is None   # spread > 0.03
    assert entry_check(0.70, 0.60, 0.02, 100.0, config) is None   # depth < $500
    assert entry_check(0.99, 0.96, 0.02, 800.0, config) is None   # tail price
    assert entry_check(0.10, 0.03, 0.02, 800.0, config) is None   # tail price low


def _seed_market_with_forecast(conn, store, cid, p_model, p_market, spread=0.02,
                               depth=2000.0, category="politics"):
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, tier, active, closed)
           VALUES (?, ?, 'Q?', ?, 'liquid', 1, 0)""",
        (cid, f"s-{cid}", category),
    )
    ts_bucket = floor_ts_bucket(now_utc(), 5)
    store.append([{
        "ts": ts_bucket, "condition_id": cid, "token_id_yes": f"t-{cid}",
        "best_bid": p_market - spread / 2, "best_ask": p_market + spread / 2,
        "mid": p_market, "spread": spread,
        "bid_depth_usd": depth, "ask_depth_usd": depth, "last_trade_price": None,
    }])
    db.append_forecast(conn, {
        "ts": now_utc().isoformat(timespec="seconds"), "condition_id": cid,
        "model_id": "m4_ensemble", "p_yes": p_model, "p_market_at_ts": p_market,
        "spread_at_ts": spread,
    })
    conn.commit()


def test_entries_only_when_all_filters_pass(config):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_market_with_forecast(conn, store, "0xgood", 0.72, 0.60)
    _seed_market_with_forecast(conn, store, "0xsmalledge", 0.62, 0.60)
    _seed_market_with_forecast(conn, store, "0xwide", 0.72, 0.60, spread=0.08)
    _seed_market_with_forecast(conn, store, "0xthin", 0.72, 0.60, depth=50.0)

    opened = run_shadow_entries(conn, store, config)
    trades = conn.execute("SELECT * FROM shadow_trades").fetchall()
    assert opened == 1
    assert len(trades) == 1
    t = trades[0]
    assert t["condition_id"] == "0xgood"
    assert t["token_side"] == "YES"
    assert t["status"] == "open"
    # Fill at best ask (0.61) plus slippage.
    assert t["entry_price"] > 0.61
    conn.close()


def test_settlement_pnl(config):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_market_with_forecast(conn, store, "0xwin", 0.72, 0.60)
    run_shadow_entries(conn, store, config)
    db.record_resolution(conn, "0xwin", "2026-07-03T00:00:00+00:00", 1.0, False, "gamma")

    assert settle_resolved(conn) == 1
    t = conn.execute("SELECT * FROM shadow_trades").fetchone()
    assert t["status"] == "resolved"
    assert t["exit_price"] == 1.0
    # shares = stake/entry; pnl = shares - stake > 0 for a winning YES.
    assert t["pnl_sim"] == pytest.approx(t["stake_sim"] / t["entry_price"] - t["stake_sim"])
    assert t["pnl_sim"] > 0
    conn.close()
