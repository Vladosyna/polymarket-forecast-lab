"""Phase 14 report artifacts: wealth-ledger equity curves, drawdown,
bootstrap wealth bands, sleeping-expert rankings, M4 attribution snapshot.

Mirrors `learn/plots.py`'s matplotlib/Agg style. The wealth ledger's
cum_log_wealth is already additive log-space, so a plain linear axis on it
*is* the "log scale" wealth plot the brief asks for -- no np.exp() needed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from lab.util import PROJECT_ROOT

log = logging.getLogger(__name__)


def _reports_dir(config: dict[str, Any]) -> Path:
    d = Path(config["storage"]["reports_dir"])
    d = d if d.is_absolute() else PROJECT_ROOT / d
    d.mkdir(parents=True, exist_ok=True)
    return d


def sleeping_expert_rankings(conn) -> list[dict[str, Any]]:
    """Latest (model_id, category) cumulative state, ranked within each
    category by cum_log_wealth/n_forecasts -- the coverage-normalized
    comparison the brief requires ("always compare... never the raw
    cumulative total")."""
    rows = conn.execute(
        """
        SELECT w.model_id, w.category, w.cum_log_wealth, w.n_forecasts
        FROM wealth_ledger w
        JOIN (SELECT model_id, category, MAX(id) AS id FROM wealth_ledger
              GROUP BY model_id, category) latest
          ON latest.model_id = w.model_id AND latest.category = w.category AND latest.id = w.id
        """
    ).fetchall()
    out = [
        {"model_id": r["model_id"], "category": r["category"],
         "cum_log_wealth": r["cum_log_wealth"], "n_forecasts": r["n_forecasts"],
         "avg_log_wealth": r["cum_log_wealth"] / r["n_forecasts"]}
        for r in rows
    ]
    out.sort(key=lambda r: (r["category"], -r["avg_log_wealth"]))
    return out


def bootstrap_wealth_bands(deltas: list[float], iterations: int = 500, seed: int = 0
                           ) -> tuple[np.ndarray, np.ndarray]:
    """Permutation bootstrap ("resample forecast order" per the brief):
    shuffle the delta sequence and take the cumulative path each time,
    returning the 5th/95th percentile band per step. This targets PATH/
    drawdown uncertainty, not endpoint uncertainty -- the final sum is
    order-invariant, so resampling VALUES with replacement would be the
    wrong operation for this purpose.
    """
    arr = np.asarray(deltas, dtype=float)
    n = len(arr)
    if n == 0:
        return np.array([]), np.array([])
    rng = np.random.default_rng(seed)
    paths = np.empty((iterations, n))
    for i in range(iterations):
        paths[i] = np.cumsum(rng.permutation(arr))
    return np.quantile(paths, 0.05, axis=0), np.quantile(paths, 0.95, axis=0)


def plot_wealth_curves(conn, config: dict[str, Any]) -> Path | None:
    """One subplot per category, one line per model_id: cum_log_wealth vs
    n_forecasts, with a bootstrap band and the null-control category
    (config universe.null_control.category, i.e. sports) styled distinctly
    as the zero-skill reference."""
    rows = [dict(r) for r in conn.execute(
        "SELECT model_id, category, n_forecasts, cum_log_wealth FROM wealth_ledger "
        "ORDER BY category, model_id, id"
    )]
    if not rows:
        log.warning("plot_wealth_curves: no wealth_ledger rows yet")
        return None

    by_category: dict[str, dict[str, list[tuple[int, float]]]] = {}
    for r in rows:
        by_category.setdefault(r["category"], {}).setdefault(r["model_id"], []).append(
            (r["n_forecasts"], r["cum_log_wealth"])
        )
    null_control_category = config["universe"]["null_control"]["category"]
    iterations = int(config.get("eval", {}).get("bootstrap_iterations", 500))
    categories = sorted(by_category)

    fig, axes = plt.subplots(1, len(categories), figsize=(5.5 * len(categories), 4.5), squeeze=False)
    for ax, cat in zip(axes[0], categories):
        for model_id, path in sorted(by_category[cat].items()):
            xs = [p[0] for p in path]
            ys = [p[1] for p in path]
            deltas = np.diff([0.0, *ys])
            is_null = cat == null_control_category
            label = f"{model_id} (null control)" if is_null else model_id
            ax.plot(xs, ys, "--" if is_null else "-", lw=1.5, label=label)
            if len(deltas) >= 5:
                lo, hi = bootstrap_wealth_bands(deltas.tolist(), iterations=min(iterations, 500))
                ax.fill_between(xs, lo, hi, alpha=0.15)
        ax.axhline(0.0, color="k", lw=0.8, ls=":")
        ax.set_title(cat)
        ax.set_xlabel("n forecasts")
        ax.set_ylabel("cumulative log-wealth")
        ax.legend(fontsize=7)
    fig.suptitle("Virtual wealth curves (SIMULATION-adjacent Kelly staking, per brief section 6/14)")
    fig.tight_layout()
    path = _reports_dir(config) / "wealth_curves.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def plot_wealth_drawdown(conn, config: dict[str, Any]) -> Path | None:
    """Peak-to-trough drawdown per (model_id, category) on the cum_log_wealth path."""
    rows = [dict(r) for r in conn.execute(
        "SELECT model_id, category, cum_log_wealth FROM wealth_ledger ORDER BY model_id, category, id"
    )]
    if not rows:
        return None
    by_group: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        by_group.setdefault((r["model_id"], r["category"]), []).append(r["cum_log_wealth"])

    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    for (model_id, category), path in sorted(by_group.items()):
        peak = -float("inf")
        dd = []
        for v in path:
            peak = max(peak, v)
            dd.append(v - peak)
        ax.plot(range(1, len(dd) + 1), dd, lw=1.2, label=f"{model_id}/{category}")
    ax.set_title("Wealth drawdown per model/category (peak-to-trough)")
    ax.set_xlabel("step")
    ax.set_ylabel("drawdown (log-wealth)")
    ax.legend(fontsize=6, ncol=2)
    fig.tight_layout()
    path = _reports_dir(config) / "wealth_drawdown.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def m4_attribution_snapshot(conn, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Today's actual M4 pool composition, linear log-odds attribution
    (contribution_i = mean(w_i * logit(p_i))) -- computed fresh each render,
    since per-forecast member weights/logits aren't persisted historically
    (the same limitation already accepted for M7's quotes pre-Phase-12; a
    live snapshot, not a historical waterfall -- see the Phase 14 plan)."""
    from lab.learn.refit import load_active_artifact, logit
    from lab.models.m4_ensemble import POOLABLE

    weights_artifact = load_active_artifact(config, "m4_weights") or {"categories": {}}
    placeholders = ",".join("?" for _ in POOLABLE)
    rows = conn.execute(
        f"""
        SELECT f.condition_id, f.model_id, f.p_yes, m.category AS category
        FROM forecasts f
        JOIN (SELECT condition_id, model_id, MAX(ts) AS ts FROM forecasts
              WHERE model_id IN ({placeholders}) AND date(ts) = date('now')
              GROUP BY condition_id, model_id) latest
          ON latest.condition_id = f.condition_id AND latest.model_id = f.model_id
             AND latest.ts = f.ts
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id IN ({placeholders})
        """,
        (*POOLABLE, *POOLABLE),
    ).fetchall()

    by_market: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        by_market.setdefault((r["condition_id"], r["category"]), {})[r["model_id"]] = r["p_yes"]

    contributions: dict[tuple[str, str], list[float]] = {}
    for (_cid, category), pool in by_market.items():
        if len(pool) < 2:
            continue
        members = sorted(pool)
        cat_weights = weights_artifact.get("categories", {}).get(category, {}).get("weights")
        if cat_weights:
            w = np.array([cat_weights.get(m, 0.0) for m in members])
            if w.sum() <= 0:
                w = np.ones(len(members))
        else:
            w = np.ones(len(members))
        w = w / w.sum()
        for member, wi in zip(members, w):
            contributions.setdefault((member, category), []).append(float(wi * logit(pool[member])))

    return [
        {"model_id": model_id, "category": category,
         "mean_contribution": float(np.mean(vals)), "n_markets": len(vals)}
        for (model_id, category), vals in sorted(contributions.items())
    ]
