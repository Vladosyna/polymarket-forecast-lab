"""M5 macro surprise-distribution fit (learn/refit.py): pairing, sd fit, and
the `lab learn` wiring that runs it only when FRED_API_KEY is present.
"""

from __future__ import annotations

import pytest

from lab.learn.loop import refit_statistical_models
from lab.learn.refit import (
    WalkForwardError,
    fit_m5_macro_sd,
    fit_m5_macro_sds,
    pair_nowcast_surprises,
)
from lab.store import db
from lab.util import load_config


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


def test_pair_nowcast_surprises_takes_last_nowcast_before_next_quarter():
    nowcast = [
        {"date": "2024-01-05", "value": 2.0},
        {"date": "2024-02-10", "value": 2.3},
        {"date": "2024-03-25", "value": 2.6},  # last nowcast still inside Q1's calendar window
        {"date": "2024-04-05", "value": 1.0},  # first Q2 nowcast
    ]
    actual = [
        {"date": "2024-01-01", "value": 2.8},   # Q1 2024
        {"date": "2024-04-01", "value": 1.5},   # Q2 2024, no later actual to bound the window
    ]
    pairs = pair_nowcast_surprises(nowcast, actual)
    assert len(pairs) == 2
    q1 = next(p for p in pairs if p["period"] == "2024-01-01")
    assert q1["nowcast"] == 2.6
    assert q1["surprise"] == pytest.approx(2.8 - 2.6)
    q2 = next(p for p in pairs if p["period"] == "2024-04-01")
    assert q2["nowcast"] == 1.0  # only nowcast row on/after 2024-04-01


def test_pair_nowcast_surprises_skips_quarters_with_no_nowcast():
    nowcast = [{"date": "2024-01-05", "value": 2.0}]
    actual = [{"date": "2023-01-01", "value": 3.0}, {"date": "2024-01-01", "value": 2.5}]
    pairs = pair_nowcast_surprises(nowcast, actual)
    assert len(pairs) == 1
    assert pairs[0]["period"] == "2024-01-01"


def test_fit_m5_macro_sd_requires_validation_window():
    train = [{"surprise": 0.1}, {"surprise": -0.2}]
    with pytest.raises(WalkForwardError):
        fit_m5_macro_sd(train, [])
    with pytest.raises(WalkForwardError):
        fit_m5_macro_sd([], train)


def test_fit_m5_macro_sd_matches_empirical_std():
    train = [{"surprise": s} for s in (0.5, -0.5, 1.0, -1.0)]
    validation = [{"surprise": 0.2}, {"surprise": -0.3}]
    fit = fit_m5_macro_sd(train, validation)
    assert fit["sd"] == pytest.approx(0.9128709, rel=1e-4)  # sample std, ddof=1
    assert fit["n_train"] == 4
    assert fit["n_validation"] == 2
    assert fit["val_nll"] > 0


def _fake_fetch(quarters=20):
    """Synthetic GDPNOW-shaped nowcast history + a matching actual series.

    Surprise alternates +-0.2 by quarter parity so the fitted sd is a real,
    hand-checkable number rather than the (0, floored) degenerate case.
    """
    def fetch(series_id, api_key):
        assert api_key == "test-key"
        dates = [f"20{10 + q // 4:02d}-{(q % 4) * 3 + 1:02d}-15" for q in range(quarters)]
        nowcast_values = [2.0 for _ in range(quarters)]
        if series_id in ("GDPNOW", "PCENOW"):
            return [{"date": d, "value": v} for d, v in zip(dates, nowcast_values)]
        # actual release series: one row per quarter start, offset by +-0.2 (alternating)
        return [
            {"date": d[:8] + "01", "value": v + (0.2 if q % 2 == 0 else -0.2)}
            for q, (d, v) in enumerate(zip(dates, nowcast_values))
        ]
    return fetch


def test_fit_m5_macro_sds_skips_series_below_min_quarters():
    artifact = fit_m5_macro_sds("test-key", min_quarters=12, validation_quarters=4,
                                fetch=_fake_fetch(quarters=5))
    assert artifact["series"] == {}  # 5 paired quarters < min_quarters=12


def test_fit_m5_macro_sds_fits_both_series_with_enough_history():
    artifact = fit_m5_macro_sds("test-key", min_quarters=12, validation_quarters=4,
                                fetch=_fake_fetch(quarters=20))
    assert set(artifact["series"]) == {"GDPNOW", "PCENOW"}
    for spec in artifact["series"].values():
        # 16 training quarters, 8 at +0.2 and 8 at -0.2 -> sample std (ddof=1):
        # sqrt(16 * 0.2**2 / 15)
        assert spec["sd"] == pytest.approx(0.2065591, rel=1e-4)
        assert spec["n_train"] == 16
        assert spec["n_validation"] == 4
        assert spec["actual_series"] in ("A191RL1Q225SBEA", "DPCERL1Q225SBEA")


def test_refit_statistical_models_skips_m5_without_fred_key(config, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    conn = db.connect(config["storage"]["db_path"])
    results = refit_statistical_models(conn, config, apply=False)
    assert results["m5_macro_sd"] == {"skipped": "no_fred_api_key"}
    conn.close()


def test_refit_statistical_models_runs_m5_when_key_and_data_present(config, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    monkeypatch.setattr(
        "lab.learn.loop.fit_m5_macro_sds",
        lambda api_key, **kw: {"kind": "m5_macro_sd", "series": {
            "GDPNOW": {"sd": 0.6, "n_train": 16, "n_validation": 4, "val_nll": 1.0,
                       "actual_series": "A191RL1Q225SBEA"},
        }},
    )
    conn = db.connect(config["storage"]["db_path"])
    results = refit_statistical_models(conn, config, apply=False)
    assert results["m5_macro_sd"]["promoted"] is True  # first version, no champion yet
    assert results["m5_macro_sd"]["series"] == ["GDPNOW"]
    conn.close()
