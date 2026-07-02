"""Reliability diagrams: binned calibration data + matplotlib plots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def calibration_bins(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> list[dict]:
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    edges = np.linspace(0, 1, n_bins + 1)
    bins: list[dict] = []
    for i in range(n_bins):
        mask = (p >= edges[i]) & (p < edges[i + 1] if i < n_bins - 1 else p <= edges[i + 1])
        n = int(mask.sum())
        bins.append({
            "bin_lo": float(edges[i]),
            "bin_hi": float(edges[i + 1]),
            "n": n,
            "p_mean": float(np.mean(p[mask])) if n else None,
            "y_rate": float(np.mean(y[mask])) if n else None,
        })
    return bins


def plot_reliability(bins_by_model: dict[str, list[dict]], out_path: Path,
                     title: str = "Reliability diagram") -> Path:
    fig, ax = plt.subplots(figsize=(6, 6))
    ax.plot([0, 1], [0, 1], "k--", lw=1, label="perfect")
    for model_id, bins in bins_by_model.items():
        xs = [b["p_mean"] for b in bins if b["n"]]
        ys = [b["y_rate"] for b in bins if b["n"]]
        ax.plot(xs, ys, "o-", label=model_id)
    ax.set_xlabel("forecast probability")
    ax.set_ylabel("observed frequency")
    ax.set_title(title)
    ax.legend(fontsize=8)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path
