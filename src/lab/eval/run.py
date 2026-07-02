"""`lab eval`: score resolved forecasts per model and window, persist eval_runs."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import numpy as np

from lab.eval.calibration import calibration_bins
from lab.eval.scoring import paired_skill
from lab.store import db as dbmod
from lab.util import now_utc, now_utc_iso

log = logging.getLogger(__name__)

WINDOWS = {"all_time": None, "trailing_90d": 90}


def resolved_forecast_rows(conn, model_id: str, window_days: int | None,
                           null_control_ids: set[str] | None = None,
                           invert_null_control: bool = False) -> list[dict]:
    """Paired rows: forecast + resolution outcome for one model."""
    query = """
        SELECT f.condition_id, f.p_yes, f.p_market_at_ts, r.payout_yes
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id
        WHERE f.model_id = ? AND r.disputed = 0
    """
    params: list[Any] = [model_id]
    if window_days is not None:
        query += " AND f.ts >= ?"
        params.append((now_utc() - timedelta(days=window_days)).isoformat(timespec="seconds"))
    rows = [dict(r) for r in conn.execute(query, params)]
    if null_control_ids is not None:
        if invert_null_control:
            rows = [r for r in rows if r["condition_id"] in null_control_ids]
        else:
            rows = [r for r in rows if r["condition_id"] not in null_control_ids]
    return rows


def evaluate_model(conn, model_id: str, window_label: str, rows: list[dict],
                   config: dict[str, Any]) -> dict[str, Any] | None:
    if not rows:
        return None
    result = paired_skill(
        p_model=np.array([r["p_yes"] for r in rows]),
        p_market=np.array([r["p_market_at_ts"] for r in rows]),
        y=np.array([r["payout_yes"] for r in rows]),
        condition_ids=np.array([r["condition_id"] for r in rows]),
        iterations=config["eval"]["bootstrap_iterations"],
    )
    bins = calibration_bins(
        np.array([r["p_yes"] for r in rows]),
        np.array([r["payout_yes"] for r in rows]),
        n_bins=config["eval"]["calibration_bins"],
    )
    conn.execute(
        """
        INSERT INTO eval_runs (ts, model_id, window_label, n, brier, brier_market,
                               skill, skill_ci_lo, skill_ci_hi, log_loss,
                               log_loss_market, calibration_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (now_utc_iso(), model_id, window_label, result.n, result.brier_model,
         result.brier_market, result.skill, result.skill_ci_lo, result.skill_ci_hi,
         result.log_loss_model, result.log_loss_market, json.dumps(bins)),
    )
    return {"model_id": model_id, "window": window_label, "result": result, "bins": bins}


def run_eval(conn, config: dict[str, Any]) -> list[dict[str, Any]]:
    from lab.forecast import null_control_ids as nc_ids_fn

    nc_ids = nc_ids_fn(conn, config)
    model_ids = [r["model_id"] for r in conn.execute(
        "SELECT DISTINCT model_id FROM forecasts ORDER BY model_id"
    )]
    out: list[dict[str, Any]] = []
    for model_id in model_ids:
        for label, days in WINDOWS.items():
            rows = resolved_forecast_rows(conn, model_id, days, nc_ids)
            summary = evaluate_model(conn, model_id, label, rows, config)
            if summary:
                out.append(summary)
        # Null control scored separately, same math, shown side by side.
        nc_rows = resolved_forecast_rows(conn, model_id, None, nc_ids, invert_null_control=True)
        nc_summary = evaluate_model(conn, model_id, "null_control", nc_rows, config)
        if nc_summary:
            out.append(nc_summary)
    conn.commit()
    log.info("eval complete", extra={"ctx": {"summaries": len(out)}})
    return out
