"""Parameter fits: M1 logistic recalibration curves and M2 category base rates.

Used by the Phase 2 bootstrap and re-used by the monthly `lab learn` job.
Artifacts are versioned JSON under data/models/; the ACTIVE.json pointer
selects which version each model loads. Fitters never touch live artifacts --
promotion is an explicit pointer update.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import minimize

from lab.util import PROJECT_ROOT, now_utc_iso

log = logging.getLogger(__name__)

HORIZON_BUCKETS = {
    "lt7d": (0, 7),
    "7to30d": (7, 30),
    "30to90d": (30, 90),
    "gt90d": (90, 10_000),
}
EPS = 1e-6


def logit(p: np.ndarray | float) -> np.ndarray | float:
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def sigmoid(x: np.ndarray | float) -> np.ndarray | float:
    return 1 / (1 + np.exp(-x))


def bucket_for_days(days: float) -> str | None:
    for name, (lo, hi) in HORIZON_BUCKETS.items():
        if lo <= days < hi:
            return name
    return None


def fit_logistic_recalibration(p_market: np.ndarray, y: np.ndarray) -> dict[str, float]:
    """MLE fit of y ~ sigmoid(alpha + beta * logit(p_market)).

    beta > 1 means the market is underconfident (extremizing helps).
    """
    x = logit(np.asarray(p_market, dtype=float))
    y = np.asarray(y, dtype=float)

    def nll(params: np.ndarray) -> float:
        a, b = params
        p = np.clip(sigmoid(a + b * x), EPS, 1 - EPS)
        return -float(np.sum(y * np.log(p) + (1 - y) * np.log(1 - p)))

    res = minimize(nll, x0=np.array([0.0, 1.0]), method="Nelder-Mead")
    alpha, beta = res.x
    return {"alpha": float(alpha), "beta": float(beta), "n": int(len(y)), "nll": float(res.fun)}


def isotonic_fit(p_market: np.ndarray, y: np.ndarray, n_bins: int = 20) -> list[dict]:
    """Binned empirical rates made monotone via pool-adjacent-violators.

    Sanity companion to the logistic fit -- large disagreement between the two
    flags a badly specified parametric curve.
    """
    order = np.argsort(p_market)
    p_sorted, y_sorted = p_market[order], y[order]
    bins = np.array_split(np.arange(len(p_sorted)), min(n_bins, max(1, len(p_sorted) // 10)))
    centers = [float(np.mean(p_sorted[b])) for b in bins if len(b)]
    rates = [float(np.mean(y_sorted[b])) for b in bins if len(b)]
    weights = [len(b) for b in bins if len(b)]

    # Pool adjacent violators to enforce monotone non-decreasing rates.
    vals = [[r, w] for r, w in zip(rates, weights)]
    i = 0
    while i < len(vals) - 1:
        if vals[i][0] > vals[i + 1][0] + EPS:
            merged_w = vals[i][1] + vals[i + 1][1]
            merged_r = (vals[i][0] * vals[i][1] + vals[i + 1][0] * vals[i + 1][1]) / merged_w
            vals[i] = [merged_r, merged_w]
            del vals[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    # Expand pooled values back over the original bins.
    iso_rates: list[float] = []
    idx = 0
    remaining = vals[idx][1] if vals else 0
    for w in weights:
        while remaining <= 0 and idx + 1 < len(vals):
            idx += 1
            remaining = vals[idx][1]
        iso_rates.append(vals[idx][0] if vals else 0.0)
        remaining -= w
    return [
        {"p_center": c, "empirical": r, "isotonic": ir, "n": w}
        for c, r, ir, w in zip(centers, rates, iso_rates, weights)
    ]


def fit_m1_curves(observations: list[dict]) -> dict[str, Any]:
    """Fit per-horizon-bucket recalibration curves.

    observations: [{p_market, outcome, days_to_resolution}, ...]
    """
    artifact: dict[str, Any] = {"kind": "m1_curves", "fitted_at": now_utc_iso(), "buckets": {}}
    if not observations:
        log.warning("m1 fit: no observations")
        return artifact
    obs = np.array(
        [(o["p_market"], o["outcome"], o["days_to_resolution"]) for o in observations], dtype=float
    )
    for name in HORIZON_BUCKETS:
        lo, hi = HORIZON_BUCKETS[name]
        mask = (obs[:, 2] >= lo) & (obs[:, 2] < hi)
        subset = obs[mask]
        if len(subset) < 100:
            log.warning("m1 fit: bucket has too few observations, skipping",
                        extra={"ctx": {"bucket": name, "n": int(len(subset))}})
            continue
        fit = fit_logistic_recalibration(subset[:, 0], subset[:, 1])
        iso = isotonic_fit(subset[:, 0], subset[:, 1])
        iso_dev = max(
            (abs(float(sigmoid(fit["alpha"] + fit["beta"] * logit(b["p_center"]))) - b["isotonic"])
             for b in iso),
            default=0.0,
        )
        artifact["buckets"][name] = {**fit, "isotonic_bins": iso, "max_iso_deviation": iso_dev}
    return artifact


def fit_m2_baserates(rows: list[dict], min_n: int = 50) -> dict[str, Any]:
    """Category base rates from resolved markets: [{category, outcome}, ...]."""
    artifact: dict[str, Any] = {"kind": "m2_baserates", "fitted_at": now_utc_iso(), "categories": {}}
    by_cat: dict[str, list[float]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(float(r["outcome"]))
    for cat, outcomes in sorted(by_cat.items()):
        if len(outcomes) < min_n:
            continue
        artifact["categories"][cat] = {
            "base_rate": float(np.mean(outcomes)),
            "n": len(outcomes),
        }
    return artifact


# --- artifact store -------------------------------------------------------

def models_dir(config: dict[str, Any]) -> Path:
    d = Path(config["storage"]["models_dir"])
    d = d if d.is_absolute() else PROJECT_ROOT / d
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_artifact(config: dict[str, Any], name: str, artifact: dict[str, Any],
                  promote: bool = True) -> Path:
    """Write the next version of `name`; optionally promote it in ACTIVE.json."""
    d = models_dir(config)
    existing = sorted(d.glob(f"{name}_v*.json"))
    version = len(existing) + 1
    artifact = {**artifact, "version": version}
    path = d / f"{name}_v{version}.json"
    path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    if promote:
        active_path = d / "ACTIVE.json"
        active = json.loads(active_path.read_text(encoding="utf-8")) if active_path.exists() else {}
        active[name] = path.name
        active_path.write_text(json.dumps(active, indent=2), encoding="utf-8")
    log.info("artifact saved", extra={"ctx": {"name": name, "version": version, "promoted": promote}})
    return path


def load_active_artifact(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    d = models_dir(config)
    active_path = d / "ACTIVE.json"
    if not active_path.exists():
        return None
    active = json.loads(active_path.read_text(encoding="utf-8"))
    if name not in active:
        return None
    return json.loads((d / active[name]).read_text(encoding="utf-8"))
