"""M7 cross-venue signal (Phase 9): mapping propose-then-confirm flow, the
deterministic log-odds pool, and the ledger writer -- fixture-driven per the
brief's Phase 9 acceptance criterion ("fixtures acceptable")."""

from __future__ import annotations

import asyncio
import json

import pytest

from lab.learn.refit import save_artifact, sigmoid
from lab.models.base import ForecastResult
from lab.models.m7_crossvenue import (
    confirm_match,
    confirmed_by_condition,
    kalshi_propose_candidates,
    link_confirmed_event,
    load_markets_map,
    load_pmxt_candidates,
    pool_log_odds,
    propose_matches,
    reject_match,
    save_markets_map,
    scan_confirmed_pairs,
    verify_pmxt_candidates,
    write_m7_forecasts,
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


def test_pool_log_odds_averages_in_logit_space():
    # Two venues agreeing exactly: pool equals that shared value.
    assert pool_log_odds([0.7, 0.7]) == pytest.approx(0.7)
    # One confident YES, one confident NO -> pools back toward 0.5.
    assert pool_log_odds([0.9, 0.1]) == pytest.approx(0.5, abs=1e-9)
    with pytest.raises(ValueError):
        pool_log_odds([])


def test_pool_log_odds_a_eff_default_is_identity():
    from lab.learn.refit import logit, sigmoid

    plain = pool_log_odds([0.6, 0.8])
    extremized = pool_log_odds([0.6, 0.8], a_eff=2.0)
    assert plain == pytest.approx(pool_log_odds([0.6, 0.8], a_eff=1.0))
    expected = float(sigmoid(2.0 * (logit(0.6) + logit(0.8)) / 2))
    assert extremized == pytest.approx(expected)
    assert extremized != pytest.approx(plain)


def test_markets_map_roundtrip(tmp_path):
    path = tmp_path / "markets_map.yaml"
    data = {"confirmed": [{"condition_id": "0x1", "venue": "kalshi", "external_id": "T1"}],
            "proposed": []}
    save_markets_map(data, path)
    loaded = load_markets_map(path)
    assert loaded["confirmed"] == data["confirmed"]
    assert loaded["proposed"] == []


def test_load_markets_map_missing_file_returns_empty(tmp_path):
    loaded = load_markets_map(tmp_path / "does_not_exist.yaml")
    assert loaded == {"confirmed": [], "proposed": []}


def test_confirm_match_moves_proposed_to_confirmed():
    data = {
        "confirmed": [],
        "proposed": [{"condition_id": "0x1", "question": "q", "venue": "kalshi",
                     "external_id": "T1", "external_question": "eq",
                     "rationale": "same event", "confidence": 0.9,
                     "proposed_ts": "2026-07-01T00:00:00+00:00"}],
    }
    assert confirm_match(data, "0x1", "kalshi") is True
    assert data["proposed"] == []
    assert len(data["confirmed"]) == 1
    entry = data["confirmed"][0]
    assert entry["condition_id"] == "0x1" and entry["external_id"] == "T1"
    assert "confirmed_ts" in entry
    # LLM-only fields don't belong on a confirmed (human-owned) entry.
    assert "rationale" not in entry and "confidence" not in entry


def test_confirm_match_idempotent():
    data = {"confirmed": [{"condition_id": "0x1", "venue": "kalshi", "external_id": "T1",
                          "confirmed_ts": "t"}], "proposed": []}
    assert confirm_match(data, "0x1", "kalshi") is True
    assert len(data["confirmed"]) == 1  # no duplicate


def test_confirm_match_hand_curated_without_prior_proposal():
    """A human can confirm a Metaculus pair directly -- `propose` can't reach
    Metaculus (api/metaculus.py), so this is the only path for that venue."""
    data = {"confirmed": [], "proposed": []}
    assert confirm_match(data, "0x1", "metaculus", external_id="12345") is True
    assert data["confirmed"][0]["external_id"] == "12345"


def test_confirm_match_returns_false_with_nothing_to_confirm():
    data = {"confirmed": [], "proposed": []}
    assert confirm_match(data, "0x1", "kalshi") is False
    assert data["confirmed"] == []


def test_reject_match_removes_a_proposed_entry():
    """The human's other verdict: a real observed case is the LLM proposing a
    pair whose own rationale says the events don't match (different office,
    different year) yet still returning confidence=1.0 -- reject removes it
    from `proposed` without ever touching `confirmed`."""
    data = {
        "confirmed": [],
        "proposed": [{"condition_id": "0x1", "venue": "kalshi", "external_id": "T1",
                     "rationale": "different offices, do not match", "confidence": 1.0}],
    }
    assert reject_match(data, "0x1", "kalshi", "T1") is True
    assert data["proposed"] == []
    assert data["confirmed"] == []


def test_reject_match_returns_false_when_nothing_to_reject():
    data = {"confirmed": [], "proposed": []}
    assert reject_match(data, "0x1", "kalshi", "T1") is False


def test_reject_match_only_removes_the_matching_external_id():
    """Two candidates proposed for the same condition_id/venue (e.g. the
    Tom Steyer / two different CA governor tickers case) -- rejecting one
    must not remove the other."""
    data = {
        "confirmed": [],
        "proposed": [
            {"condition_id": "0x1", "venue": "kalshi", "external_id": "T1"},
            {"condition_id": "0x1", "venue": "kalshi", "external_id": "T2"},
        ],
    }
    assert reject_match(data, "0x1", "kalshi", "T1") is True
    assert len(data["proposed"]) == 1
    assert data["proposed"][0]["external_id"] == "T2"


def test_link_confirmed_event_creates_event_linking_both_markets(config):
    """Phase 10 acceptance: a confirmed match creates an event linking >=2
    venue-markets, using the Polymarket market's own question as the title."""
    conn = db.connect(config["storage"]["db_path"])
    db.upsert_market(conn, {
        "condition_id": "0x1", "venue": "polymarket", "venue_native_id": "0x1",
        "slug": "s", "question": "Will X happen?", "category": "politics",
        "description": "d", "end_date_iso": "2026-12-31T00:00:00+00:00",
        "token_id_yes": "1", "token_id_no": "2", "neg_risk": 0,
        "active": 1, "closed": 0, "liquidity_num": 1.0, "volume_num": 1.0,
        "tier": "liquid",
    })
    conn.commit()

    event_id = link_confirmed_event(conn, "0x1", "kalshi", "T1")

    rows = {r["condition_id"]: r["event_id"]
           for r in conn.execute("SELECT condition_id, event_id FROM markets")}
    assert rows["0x1"] == event_id
    assert rows["kalshi:T1"] == event_id
    ev = conn.execute("SELECT title FROM events WHERE event_id = ?", (event_id,)).fetchone()
    assert ev["title"] == "Will X happen?"
    conn.close()


def test_confirmed_by_condition_excludes_proposed():
    """The core of 'a proposed-but-unconfirmed pair is NOT forecast': the
    scan path only ever sees confirmed_by_condition()'s output."""
    data = {
        "confirmed": [{"condition_id": "0xA", "venue": "kalshi", "external_id": "T1"}],
        "proposed": [{"condition_id": "0xB", "venue": "kalshi", "external_id": "T2"}],
    }
    by_cid = confirmed_by_condition(data)
    assert set(by_cid) == {"0xA"}


def _seed_market_with_snapshot(conn, store, cid: str, mid: float = 0.5):
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES (?, ?, ?, 'politics', 'd', '2026-12-31T00:00:00+00:00', ?, 'liquid', 1, 0,
                   200000, 2000000)""",
        (cid, cid, f"Question for {cid}?", f"tok-{cid}"),
    )
    store.append([{
        "ts": floor_ts_bucket(now_utc(), 5), "condition_id": cid, "token_id_yes": f"tok-{cid}",
        "best_bid": mid - 0.02, "best_ask": mid + 0.02, "mid": mid, "spread": 0.04,
        "bid_depth_usd": 1000.0, "ask_depth_usd": 1000.0, "last_trade_price": None,
    }])


def test_write_m7_forecasts_writes_five_confirmed_pairs_and_skips_unconfirmed(config):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    cids = [f"0x{i}" for i in range(5)]
    for cid in cids:
        _seed_market_with_snapshot(conn, store, cid)
    # A 6th market stands in for a proposed-but-unconfirmed pair: it has its
    # own snapshot but no entry in `results`, so it must never be forecast.
    _seed_market_with_snapshot(conn, store, "0xUNCONFIRMED")
    conn.commit()

    results = {
        cid: ForecastResult(
            p_yes=0.6 + 0.01 * i,
            meta={"quotes": [{"venue": "kalshi", "external_id": f"T{i}", "price": 0.6 + 0.01 * i,
                              "fetched_ts": "2026-07-03T00:00:00+00:00"}], "n_pooled": 1},
        )
        for i, cid in enumerate(cids)
    }

    written = write_m7_forecasts(conn, store, results, config)
    assert written == 5

    rows = conn.execute("SELECT * FROM forecasts WHERE model_id='m7_crossvenue'").fetchall()
    assert len(rows) == 5
    assert {r["condition_id"] for r in rows} == set(cids)
    assert all(r["p_market_at_ts"] == pytest.approx(0.5) for r in rows)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM forecasts WHERE condition_id='0xUNCONFIRMED'"
    ).fetchone()["n"] == 0
    conn.close()


class FakeMetaculusClient:
    def __init__(self, bucket, raw_cp: float = 0.5) -> None:
        self.raw_cp = raw_cp

    async def question(self, question_id):
        return type("Q", (), {"community_prediction": self.raw_cp})()

    async def aclose(self) -> None:
        pass


class FakeKalshiClientNoMatch:
    def __init__(self, bucket) -> None:
        pass

    async def market(self, ticker):
        return None

    async def aclose(self) -> None:
        pass


def test_scan_confirmed_pairs_recalibrates_metaculus_cp_via_m1_hier(config, tmp_path, monkeypatch):
    """Phase 12: a confirmed Metaculus pair's community prediction is
    recalibrated through the m1_hier_curves metaculus offset before pooling,
    when an active artifact with a matching bucket fit is present."""
    monkeypatch.setattr("lab.api.metaculus.MetaculusClient", FakeMetaculusClient)
    monkeypatch.setattr("lab.api.kalshi.KalshiClient", FakeKalshiClientNoMatch)

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_market_with_snapshot(conn, store, "0x1")  # end_date_iso far out -> gt90d bucket
    conn.commit()

    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [{"condition_id": "0x1", "venue": "metaculus", "external_id": "123"}],
         "proposed": []},
        map_path,
    )
    save_artifact(config, "m1_hier_curves", {
        "kind": "m1_hier_curves",
        "buckets": {
            "gt90d": {
                "global": {"alpha": 0.0, "beta": 1.0, "n": 500},
                "venues": {"metaculus": {"alpha_offset": 1.0, "beta_offset": 0.0, "n": 40}},
            },
        },
    })

    results = asyncio.run(scan_confirmed_pairs(conn, store, config, markets_map_path=map_path))

    assert "0x1" in results
    quote = results["0x1"].meta["quotes"][0]
    assert quote["recalibrated"] is True
    assert quote["price"] == pytest.approx(float(sigmoid(1.0)))  # alpha_offset=1.0, raw cp=0.5 -> logit=0
    conn.close()


def test_scan_confirmed_pairs_leaves_cp_unchanged_without_artifact(config, tmp_path, monkeypatch):
    monkeypatch.setattr("lab.api.metaculus.MetaculusClient", FakeMetaculusClient)
    monkeypatch.setattr("lab.api.kalshi.KalshiClient", FakeKalshiClientNoMatch)

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_market_with_snapshot(conn, store, "0x1")
    conn.commit()

    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [{"condition_id": "0x1", "venue": "metaculus", "external_id": "123"}],
         "proposed": []},
        map_path,
    )
    # No m1_hier_curves artifact saved -> ACTIVE.json has no entry for it.

    results = asyncio.run(scan_confirmed_pairs(conn, store, config, markets_map_path=map_path))

    assert "0x1" in results
    quote = results["0x1"].meta["quotes"][0]
    assert quote["recalibrated"] is False
    assert quote["price"] == pytest.approx(0.5)  # raw CP, unchanged
    conn.close()


def test_scan_confirmed_pairs_applies_m7_extremization_artifact(config, tmp_path, monkeypatch):
    """Phase 13: an active m7_extremization artifact extremizes the pooled
    quote using the ACTUAL number of venues pooled for this market."""
    monkeypatch.setattr("lab.api.metaculus.MetaculusClient", FakeMetaculusClient)

    class FakeKalshiClientWithMatch:
        def __init__(self, bucket) -> None:
            pass

        async def market(self, ticker):
            return type("M", (), {"yes_price": 0.6})()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr("lab.api.kalshi.KalshiClient", FakeKalshiClientWithMatch)

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_market_with_snapshot(conn, store, "0x1")
    conn.commit()

    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [
            {"condition_id": "0x1", "venue": "metaculus", "external_id": "123"},
            {"condition_id": "0x1", "venue": "kalshi", "external_id": "T1"},
        ], "proposed": []},
        map_path,
    )
    save_artifact(config, "m7_extremization", {
        "kind": "m7_extremization",
        "categories": {"_all": {"a": 2.0, "rho_bar": 0.0}},
    })

    results = asyncio.run(scan_confirmed_pairs(conn, store, config, markets_map_path=map_path))

    assert "0x1" in results
    plain_pooled = pool_log_odds([0.5, 0.6])  # raw metaculus CP + kalshi price, no extremization
    extremized_pooled = pool_log_odds([0.5, 0.6], a_eff=2.0)
    assert results["0x1"].meta["extremization_a_eff"] == pytest.approx(2.0)  # rho_bar=0, n=2 -> full a
    assert results["0x1"].p_yes == pytest.approx(extremized_pooled)
    assert results["0x1"].p_yes != pytest.approx(plain_pooled)
    conn.close()


class FakeKalshiCandidate:
    def __init__(self, ticker, title):
        self.ticker = ticker
        self.title = title


class FakeLlm:
    def __init__(self, response: dict):
        self.response = response
        self.calls = 0

    def complete(self, system, prompt, purpose, max_tokens=2000):
        self.calls += 1
        return json.dumps(self.response), {"tokens_in": 100, "tokens_out": 50, "cost_usd": 0.001}


class FakeKalshiCategoryClient:
    """Real Kalshi category -> series -> markets fan-out, faked. Also serves
    Sports/Entertainment/World series to prove they're correctly excluded --
    a market cap/priority-category filter bug would silently include them."""

    def __init__(self, series_by_cat: dict, markets_by_series: dict):
        self.series_by_cat = series_by_cat
        self.markets_by_series = markets_by_series
        self.categories_queried: list[str] = []

    async def series_by_category(self, category):
        self.categories_queried.append(category)
        return self.series_by_cat.get(category, [])

    async def markets_for_series(self, series_ticker, status="open", **kwargs):
        return self.markets_by_series.get(series_ticker, [])


def test_kalshi_propose_candidates_only_queries_priority_category_series(config):
    """The real live bug this fixes: a bare open_markets(limit=200) pulled
    whatever Kalshi considers globally 'open' -- verified live to be
    dominated by garbled multi-leg sports-combo products, crowding out the
    handful of real Economics/Politics/Weather markets entirely (0 candidates
    ever proposed). This must query ONLY the Kalshi category names whose
    categories.yaml mapping lands in our priority_categories."""
    kalshi = FakeKalshiCategoryClient(
        series_by_cat={
            "Economics": [{"ticker": "ECON-SERIES"}],
            "Politics": [{"ticker": "POL-SERIES"}],
            "Climate and Weather": [{"ticker": "WX-SERIES"}],
            "Elections": [{"ticker": "ELEC-SERIES"}],
            "Sports": [{"ticker": "SPORTS-SERIES"}],
            "Entertainment": [{"ticker": "ENT-SERIES"}],
            "World": [{"ticker": "WORLD-SERIES"}],
        },
        markets_by_series={
            "ECON-SERIES": [FakeKalshiCandidate("FEDMAR", "Fed cuts rates in March")],
            "POL-SERIES": [FakeKalshiCandidate("PRES28", "2028 presidential race")],
            "WX-SERIES": [FakeKalshiCandidate("TEMPNYC", "NYC high temp Friday")],
            "ELEC-SERIES": [FakeKalshiCandidate("SENATE28", "2028 Senate control")],
            "SPORTS-SERIES": [FakeKalshiCandidate("KXMVE1", "yes Argentina advances,yes 7+ corners")],
            "ENT-SERIES": [FakeKalshiCandidate("OSCAR", "Best Picture winner")],
            "WORLD-SERIES": [FakeKalshiCandidate("UN1", "UN Security Council vote")],
        },
    )

    candidates = asyncio.run(kalshi_propose_candidates(kalshi, config))

    # config.yaml's priority_categories covers P1-P4 (economics, weather,
    # politics, geopolitics, entertainment) -- Sports is the one Kalshi
    # category with no priority-category mapping (sports is the null
    # control, never a forecast target), so it's the one that must be
    # excluded here. Result is keyed by OUR internal category, not flat.
    all_tickers = {c.ticker for cat_list in candidates.values() for c in cat_list}
    assert all_tickers == {"FEDMAR", "PRES28", "TEMPNYC", "SENATE28", "UN1", "OSCAR"}
    assert "KXMVE1" not in all_tickers  # the real garbled sports-combo ticker this fix excludes
    assert "Sports" not in kalshi.categories_queried
    # Politics AND Elections both map to "politics" -- both contribute here.
    assert {c.ticker for c in candidates["politics"]} == {"PRES28", "SENATE28"}
    assert {c.ticker for c in candidates["economics"]} == {"FEDMAR"}
    assert {c.ticker for c in candidates["weather"]} == {"TEMPNYC"}


class RecordingFakeLlm:
    def __init__(self):
        self.prompts: list[str] = []

    def complete(self, system, prompt, purpose, max_tokens=2000):
        self.prompts.append(prompt)
        return json.dumps({"matches": []}), {"tokens_in": 10, "tokens_out": 5, "cost_usd": 0.0001}


def test_propose_matches_gives_every_priority_category_a_fair_share(config, tmp_path):
    """Real live bug this fixes: a single dominant negRisk event (many
    high-volume legs in one category, e.g. "will [name] win 2028") must not
    crowd out every propose slot via one global ORDER BY volume_num DESC --
    each priority category gets its own even share of top_k regardless."""
    conn = db.connect(config["storage"]["db_path"])
    for i in range(10):  # 10 high-volume politics markets
        conn.execute(
            """INSERT INTO markets (condition_id, slug, question, category, description,
                                    end_date_iso, token_id_yes, tier, active, closed,
                                    liquidity_num, volume_num)
               VALUES (?, ?, ?, 'politics', 'd', '2026-12-31T00:00:00+00:00', 'tok',
                       'liquid', 1, 0, 200000, ?)""",
            (f"0xpol{i}", f"pol{i}", f"Will person {i} win?", 50_000_000 - i),
        )
    for i in range(2):  # only 2, much lower-volume weather markets
        conn.execute(
            """INSERT INTO markets (condition_id, slug, question, category, description,
                                    end_date_iso, token_id_yes, tier, active, closed,
                                    liquidity_num, volume_num)
               VALUES (?, ?, ?, 'weather', 'd', '2026-12-31T00:00:00+00:00', 'tok',
                       'liquid', 1, 0, 200000, ?)""",
            (f"0xwx{i}", f"wx{i}", f"Will it rain in city {i}?", 1000 - i),
        )
    conn.commit()

    config["universe"]["priority_categories"] = ["politics", "weather"]
    config["cross_venue"]["propose_top_k"] = 4  # -> 2 slots per category

    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)

    llm = RecordingFakeLlm()
    candidates = {"politics": [FakeKalshiCandidate("X", "irrelevant")],
                 "weather": [FakeKalshiCandidate("Y", "irrelevant")]}
    propose_matches(conn, config, candidates, llm, markets_map_path=map_path)

    assert len(llm.prompts) == 4  # 2 politics + 2 weather, not 4 politics
    assert any("rain" in p for p in llm.prompts)  # weather got a slot at all
    assert sum("person" in p for p in llm.prompts) == 2  # politics didn't take all 4
    conn.close()


def test_propose_matches_dedupes_by_event_within_a_category(config, tmp_path):
    """Real live bug this fixes: one negRisk event's legs (e.g. 5 mutually
    exclusive "Fed hikes/cuts/holds" buckets, all high-volume, all one
    event_id) must not fill a whole category's per-category share by
    themselves -- distinct topics should get a chance too."""
    conn = db.connect(config["storage"]["db_path"])
    for i in range(5):  # one negRisk event, 5 legs, all high volume
        conn.execute(
            """INSERT INTO markets (condition_id, slug, question, category, description,
                                    end_date_iso, token_id_yes, tier, active, closed,
                                    liquidity_num, volume_num, event_id)
               VALUES (?, ?, ?, 'economics', 'd', '2026-12-31T00:00:00+00:00', 'tok',
                       'liquid', 1, 0, 200000, ?, 'evt_fed_july')""",
            (f"0xfed{i}", f"fed{i}", f"Will the Fed do thing {i} in July?", 10_000_000 - i),
        )
    # A distinct, lower-volume economics event that should still get a slot.
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num, event_id)
           VALUES ('0xcpi', 'cpi', 'Will CPI print above 3%?', 'economics', 'd',
                   '2026-12-31T00:00:00+00:00', 'tok', 'liquid', 1, 0, 200000, 500, 'evt_cpi')"""
    )
    conn.commit()

    config["universe"]["priority_categories"] = ["economics"]
    config["cross_venue"]["propose_top_k"] = 2  # -> 2 slots, all in economics

    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)

    llm = RecordingFakeLlm()
    candidates = {"economics": [FakeKalshiCandidate("X", "irrelevant")]}
    propose_matches(conn, config, candidates, llm, markets_map_path=map_path)

    assert len(llm.prompts) == 2
    assert any("CPI" in p for p in llm.prompts)  # the distinct event got a slot
    assert sum("Fed do thing" in p for p in llm.prompts) == 1  # only ONE Fed leg, not both slots
    conn.close()


def test_kalshi_propose_candidates_gives_every_category_a_series_share(config):
    """Real live bug found via the user's own question: a single global
    series_seen counter across ALL Kalshi categories meant one category with
    many series (Economics has 601 on real Kalshi) silently consumed the
    entire budget, so every prior live run in this session only ever fetched
    Economics series -- Weather/Politics/Elections/World/Entertainment got
    zero, despite the category filter itself working. Each Kalshi category
    gets its own guaranteed series share (propose_series_per_category),
    decoupled from the hourly universe-sync's max_series_per_sync."""
    kalshi = FakeKalshiCategoryClient(
        series_by_cat={
            # Economics alone has more series than the whole per-category cap.
            "Economics": [{"ticker": f"ECON-{i}"} for i in range(20)],
            "Politics": [{"ticker": "POL-SERIES"}],
            "Climate and Weather": [{"ticker": "WX-SERIES"}],
        },
        markets_by_series={
            **{f"ECON-{i}": [FakeKalshiCandidate(f"E{i}", f"econ market {i}")] for i in range(20)},
            "POL-SERIES": [FakeKalshiCandidate("PRES28", "2028 presidential race")],
            "WX-SERIES": [FakeKalshiCandidate("TEMPNYC", "NYC high temp Friday")],
        },
    )
    config["universe"]["priority_categories"] = ["economics", "politics", "weather"]
    config["cross_venue"]["propose_series_per_category"] = 2

    candidates = asyncio.run(kalshi_propose_candidates(kalshi, config))

    all_tickers = {c.ticker for cat_list in candidates.values() for c in cat_list}
    assert "PRES28" in all_tickers  # would be starved to zero before this fix
    assert "TEMPNYC" in all_tickers  # would be starved to zero before this fix
    assert len(candidates["economics"]) == 2  # Economics capped too, not all 20


def test_propose_matches_never_shows_a_market_another_categorys_candidates(config, tmp_path):
    """The bug the user suspected: kalshi_candidates used to be one flat list
    shown to EVERY Polymarket market regardless of category -- a weather
    market got shown politics/entertainment candidates too, diluting the
    LLM's judgment and burning tokens on options that can never be a real
    match. Candidates must be scoped to each market's own category."""
    conn = db.connect(config["storage"]["db_path"])
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES ('0xwx', 'wx', 'Will it rain in NYC?', 'weather', 'd',
                   '2026-12-31T00:00:00+00:00', 'tok', 'liquid', 1, 0, 200000, 5000000)"""
    )
    conn.commit()
    config["universe"]["priority_categories"] = ["weather"]
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)

    llm = RecordingFakeLlm()
    # Politics candidates present, but NOT under "weather" -- must never
    # reach the weather market's prompt.
    candidates = {"politics": [FakeKalshiCandidate("PRES28", "2028 presidential race")]}
    propose_matches(conn, config, candidates, llm, markets_map_path=map_path)

    assert len(llm.prompts) == 0  # no weather candidates at all -> market skipped, LLM never called
    conn.close()


def test_propose_matches_appends_to_proposed_not_confirmed(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES ('0x1', 's', 'Will the Fed cut rates in March?', 'economics', 'd',
                   '2026-12-31T00:00:00+00:00', 'tok', 'liquid', 1, 0, 200000, 5000000)"""
    )
    conn.commit()
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)

    llm = FakeLlm({"matches": [{"external_id": "FEDMAR", "confidence": 0.85,
                               "rationale": "same FOMC meeting"}]})
    candidates = {"economics": [FakeKalshiCandidate("FEDMAR", "Fed cuts rates in March FOMC meeting")]}

    proposals = propose_matches(conn, config, candidates, llm, markets_map_path=map_path)
    assert len(proposals) == 1
    assert proposals[0]["external_id"] == "FEDMAR"
    assert proposals[0]["venue"] == "kalshi"

    data = load_markets_map(map_path)
    assert data["confirmed"] == []  # propose never writes to confirmed
    assert len(data["proposed"]) == 1
    conn.close()


def test_propose_matches_skips_already_proposed_pair(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES ('0x1', 's', 'Will the Fed cut rates in March?', 'economics', 'd',
                   '2026-12-31T00:00:00+00:00', 'tok', 'liquid', 1, 0, 200000, 5000000)"""
    )
    conn.commit()
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [], "proposed": [{"condition_id": "0x1", "venue": "kalshi",
                                       "external_id": "FEDMAR", "confidence": 0.5}]},
        map_path,
    )
    llm = FakeLlm({"matches": [{"external_id": "FEDMAR", "confidence": 0.85, "rationale": "x"}]})
    candidates = {"economics": [FakeKalshiCandidate("FEDMAR", "Fed cuts rates in March FOMC meeting")]}

    proposals = propose_matches(conn, config, candidates, llm, markets_map_path=map_path)
    assert proposals == []  # already proposed -- LLM not asked to re-propose it
    assert llm.calls == 0
    conn.close()


def test_load_pmxt_candidates_missing_file_returns_empty(tmp_path):
    assert load_pmxt_candidates(tmp_path / "does_not_exist.json") == []


def test_load_pmxt_candidates_malformed_json_returns_empty(tmp_path):
    path = tmp_path / "pmxt_candidates.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert load_pmxt_candidates(path) == []


def _insert_market(conn, condition_id="0x1", question="Will the Fed cut rates in March?",
                   description="FOMC statement decides"):
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES (?, 's', ?, 'economics', ?, '2026-12-31T00:00:00+00:00', 'tok',
                   'liquid', 1, 0, 200000, 5000000)""",
        (condition_id, question, description),
    )
    conn.commit()


def test_verify_pmxt_candidates_no_file_returns_empty_without_calling_llm(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    llm = FakeLlm({"match": True, "confidence": 0.9, "rationale": "x"})
    proposals = verify_pmxt_candidates(conn, config, llm,
                                       candidates_path=tmp_path / "missing.json",
                                       markets_map_path=tmp_path / "markets_map.yaml")
    assert proposals == []
    assert llm.calls == 0
    conn.close()


def test_verify_pmxt_candidates_appends_matched_pair_with_pmxt_source(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    _insert_market(conn)
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)
    cand_path = tmp_path / "pmxt_candidates.json"
    cand_path.write_text(json.dumps([{
        "poly_condition_id": "0x1", "poly_question": "Will the Fed cut rates in March?",
        "kalshi_ticker": "FEDMAR", "kalshi_title": "Fed cuts rates in March FOMC meeting",
        "relation_type": "identity", "confidence": 0.77, "scanned_ts": "2026-07-08T00:00:00+00:00",
    }]), encoding="utf-8")

    llm = FakeLlm({"match": True, "confidence": 0.9, "rationale": "same FOMC decision"})
    proposals = verify_pmxt_candidates(conn, config, llm, candidates_path=cand_path,
                                       markets_map_path=map_path)

    assert len(proposals) == 1
    p = proposals[0]
    assert p["condition_id"] == "0x1" and p["venue"] == "kalshi" and p["external_id"] == "FEDMAR"
    assert p["source"] == "pmxt"
    assert p["source_meta"] == {"relation_type": "identity", "pmxt_confidence": 0.77}
    assert llm.calls == 1

    data = load_markets_map(map_path)
    assert data["confirmed"] == []  # never auto-confirms
    assert len(data["proposed"]) == 1

    # File consumed so a stale scan isn't re-verified forever.
    assert json.loads(cand_path.read_text(encoding="utf-8")) == []
    conn.close()


def test_verify_pmxt_candidates_skips_llm_rejected_pair(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    _insert_market(conn)
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)
    cand_path = tmp_path / "pmxt_candidates.json"
    cand_path.write_text(json.dumps([{
        "poly_condition_id": "0x1", "poly_question": "Will the Fed cut rates in March?",
        "kalshi_ticker": "FEDMAR", "kalshi_title": "totally unrelated market",
        "relation_type": "loose", "confidence": 0.4, "scanned_ts": "2026-07-08T00:00:00+00:00",
    }]), encoding="utf-8")

    llm = FakeLlm({"match": False, "confidence": 0.1, "rationale": "different events"})
    proposals = verify_pmxt_candidates(conn, config, llm, candidates_path=cand_path,
                                       markets_map_path=map_path)

    assert proposals == []
    assert load_markets_map(map_path)["proposed"] == []
    # Still consumed even when rejected -- a stale rejected pair shouldn't be
    # re-asked forever either.
    assert json.loads(cand_path.read_text(encoding="utf-8")) == []
    conn.close()


def test_verify_pmxt_candidates_skips_already_confirmed_or_proposed_pair(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    _insert_market(conn)
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [{"condition_id": "0x1", "venue": "kalshi", "external_id": "FEDMAR"}],
         "proposed": []},
        map_path,
    )
    cand_path = tmp_path / "pmxt_candidates.json"
    cand_path.write_text(json.dumps([{
        "poly_condition_id": "0x1", "poly_question": "Will the Fed cut rates in March?",
        "kalshi_ticker": "FEDMAR", "kalshi_title": "Fed cuts rates in March FOMC meeting",
        "relation_type": "identity", "confidence": 0.9, "scanned_ts": "2026-07-08T00:00:00+00:00",
    }]), encoding="utf-8")

    llm = FakeLlm({"match": True, "confidence": 0.9, "rationale": "x"})
    proposals = verify_pmxt_candidates(conn, config, llm, candidates_path=cand_path,
                                       markets_map_path=map_path)

    assert proposals == []
    assert llm.calls == 0  # already confirmed -- never re-asked
    conn.close()


def test_verify_pmxt_candidates_skips_malformed_entry_missing_fields(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    _insert_market(conn)
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)
    cand_path = tmp_path / "pmxt_candidates.json"
    cand_path.write_text(json.dumps([
        {"poly_question": "missing poly_condition_id", "kalshi_ticker": "X"},
        {"poly_condition_id": "0x1", "poly_question": "missing kalshi_ticker"},
    ]), encoding="utf-8")

    llm = FakeLlm({"match": True, "confidence": 0.9, "rationale": "x"})
    proposals = verify_pmxt_candidates(conn, config, llm, candidates_path=cand_path,
                                       markets_map_path=map_path)

    assert proposals == []
    assert llm.calls == 0
    conn.close()
