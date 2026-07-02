"""Phase 2 report artifacts: calibration slope by horizon, price vs outcome."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from lab.learn.refit import HORIZON_BUCKETS, logit, sigmoid
from lab.util import PROJECT_ROOT

log = logging.getLogger(__name__)


def reports_dir(config: dict[str, Any]) -> Path:
    d = Path(config["storage"]["reports_dir"])
    d = d if d.is_absolute() else PROJECT_ROOT / d
    d.mkdir(parents=True, exist_ok=True)
    return d


def plot_m1_curves(artifact: dict[str, Any], config: dict[str, Any]) -> list[Path]:
    """Fitted recalibration curve + empirical bins per horizon bucket, and a
    slope-by-horizon summary."""
    out: list[Path] = []
    buckets = artifact.get("buckets", {})
    if not buckets:
        log.warning("plot_m1_curves: no fitted buckets to plot")
        return out

    fig, axes = plt.subplots(1, len(buckets), figsize=(5 * len(buckets), 4.5), squeeze=False)
    grid = np.linspace(0.02, 0.98, 200)
    for ax, (name, fit) in zip(axes[0], buckets.items()):
        curve = sigmoid(fit["alpha"] + fit["beta"] * logit(grid))
        ax.plot(grid, grid, "k--", lw=1, label="identity")
        ax.plot(grid, curve, "b-", lw=2,
                label=f"fit α={fit['alpha']:.2f} β={fit['beta']:.2f}")
        bins = fit.get("isotonic_bins", [])
        if bins:
            ax.scatter([b["p_center"] for b in bins], [b["empirical"] for b in bins],
                       s=[max(10, min(200, b["n"] / 5)) for b in bins],
                       c="darkorange", alpha=0.7, label="empirical (size=n)")
        ax.set_title(f"{name} (n={fit['n']})")
        ax.set_xlabel("market price")
        ax.set_ylabel("outcome rate")
        ax.legend(fontsize=8)
    fig.suptitle("M1 recalibration: price vs outcome per horizon bucket")
    fig.tight_layout()
    path = reports_dir(config) / "m1_price_vs_outcome.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    out.append(path)

    fig, ax = plt.subplots(figsize=(6, 4))
    names = [n for n in HORIZON_BUCKETS if n in buckets]
    betas = [buckets[n]["beta"] for n in names]
    ns = [buckets[n]["n"] for n in names]
    ax.bar(names, betas, color="steelblue")
    ax.axhline(1.0, color="k", ls="--", lw=1)
    for i, (b, n) in enumerate(zip(betas, ns)):
        ax.text(i, b + 0.02, f"n={n}", ha="center", fontsize=8)
    ax.set_ylabel("calibration slope β")
    ax.set_title("Calibration slope by horizon (β>1 = market underconfident)")
    fig.tight_layout()
    path = reports_dir(config) / "m1_slope_by_horizon.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    out.append(path)
    return out
