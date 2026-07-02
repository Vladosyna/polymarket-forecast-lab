"""Static HTML report via jinja2: health block, skill table, calibration plots."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
from jinja2 import Environment

from lab.collect.status import gather_status
from lab.eval.calibration import plot_reliability
from lab.eval.clv import clv_drift
from lab.eval.scoring import honesty_tier
from lab.util import PROJECT_ROOT, now_utc_iso

log = logging.getLogger(__name__)

TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Forecast Lab report</title>
<style>
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 1100px; color: #1a1a1a; }
h1, h2 { border-bottom: 1px solid #ddd; padding-bottom: .3rem; }
table { border-collapse: collapse; margin: 1rem 0; width: 100%; }
th, td { border: 1px solid #ccc; padding: .4rem .6rem; text-align: right; font-size: .9rem; }
th { background: #f5f5f5; } td:first-child, th:first-child { text-align: left; }
.tier-INSUFFICIENT { color: #b00; font-weight: 600; }
.tier-PRELIMINARY { color: #b60; font-weight: 600; }
.tier-STANDARD { color: #070; font-weight: 600; }
.health { background: #f8f9fa; border: 1px solid #ddd; padding: 1rem; white-space: pre-wrap;
          font-family: monospace; font-size: .85rem; }
.note { color: #666; font-size: .85rem; }
img { max-width: 100%; }
</style></head><body>
<h1>Polymarket Forecast Lab — report</h1>
<p class="note">Generated {{ generated_at }} (UTC). All figures are research measurements;
shadow-portfolio numbers, where present, are SIMULATION only.</p>

<h2>Data health</h2>
<div class="health">{{ health }}</div>

<h2>Skill vs market (paired Brier)</h2>
<p class="note">skill = mean(brier_market − brier_model); positive = beating the market.
CI: cluster bootstrap by market. A skill estimate smaller than its own MDE is noise by construction.</p>
{% if skill_rows %}
<table>
<tr><th>model</th><th>window</th><th>n rows</th><th>n markets</th><th>tier</th>
<th>brier model</th><th>brier market</th><th>skill</th><th>95% CI</th><th>MDE</th>
<th>log loss</th><th>log loss mkt</th></tr>
{% for r in skill_rows %}
<tr><td>{{ r.model_id }}</td><td>{{ r.window }}</td><td>{{ r.n }}</td><td>{{ r.n_markets }}</td>
<td class="tier-{{ r.tier.split(' ')[0] }}">{{ r.tier }}</td>
<td>{{ "%.4f"|format(r.brier_model) }}</td><td>{{ "%.4f"|format(r.brier_market) }}</td>
<td>{{ "%.4f"|format(r.skill) }}</td>
<td>[{{ "%.4f"|format(r.ci_lo) }}, {{ "%.4f"|format(r.ci_hi) }}]</td>
<td>{{ "%.4f"|format(r.mde) if r.mde == r.mde else "—" }}</td>
<td>{{ "%.4f"|format(r.log_loss) }}</td><td>{{ "%.4f"|format(r.log_loss_market) }}</td></tr>
{% endfor %}
</table>
<p class="note">Rows labeled <b>null_control</b> are the sports control sample: statistically
significant skill there invalidates the run pending investigation.</p>
{% else %}
<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no resolved paired forecasts yet.</p>
{% endif %}

<h2>Calibration</h2>
{% if calibration_plot %}<img src="{{ calibration_plot }}" alt="reliability diagram">
{% else %}<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no resolved forecasts to plot.</p>{% endif %}

<h2>CLV-style early signal</h2>
<p class="note">Mean signed market drift toward the model's view at t+24h / t+72h.
Positive = the model tends to be early. Needs no resolutions.</p>
{% if clv_rows %}
<table>
<tr><th>model</th><th>horizon</th><th>n</th><th>mean signed drift</th></tr>
{% for r in clv_rows %}
<tr><td>{{ r.model_id }}</td><td>t+{{ r.horizon }}h</td><td>{{ r.n }}</td>
<td>{{ "%.4f"|format(r.drift) if r.drift == r.drift else "—" }}</td></tr>
{% endfor %}
</table>
{% else %}<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no forecasts with t+24h snapshots yet.</p>{% endif %}

<h2>Lessons digest (trailing quarter)</h2>
{% if lessons.n %}
<p class="note">{{ lessons.n }} post-mortems in the last {{ lessons.window_days }} days.
Miss error sources: {{ lessons.miss_error_sources }}</p>
<ul>{% for note in lessons.sample_notes %}<li class="note">{{ note }}</li>{% endfor %}</ul>
{% else %}<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no post-mortems yet.</p>{% endif %}

<h2>LLM spend</h2>
<p>Cumulative: ${{ "%.2f"|format(llm_total) }} — today: ${{ "%.2f"|format(llm_today) }}
(daily cap ${{ "%.2f"|format(llm_cap) }})</p>
</body></html>
"""


def latest_eval_rows(conn) -> list[dict]:
    """Most recent eval_runs row per (model, window)."""
    rows = conn.execute(
        """
        SELECT e.* FROM eval_runs e
        JOIN (SELECT model_id, window_label, MAX(ts) AS ts FROM eval_runs
              GROUP BY model_id, window_label) latest
        ON latest.model_id = e.model_id AND latest.window_label = e.window_label
           AND latest.ts = e.ts
        ORDER BY e.model_id, e.window_label
        """
    ).fetchall()
    return [dict(r) for r in rows]


def render_report(conn, store, config: dict[str, Any]) -> Path:
    reports = Path(config["storage"]["reports_dir"])
    reports = reports if reports.is_absolute() else PROJECT_ROOT / reports
    reports.mkdir(parents=True, exist_ok=True)

    from lab.collect.status import format_status
    health = format_status(gather_status(config))

    skill_rows = []
    bins_by_model: dict[str, list[dict]] = {}
    for r in latest_eval_rows(conn):
        n_markets = len({
            row["condition_id"] for row in conn.execute(
                """SELECT DISTINCT f.condition_id FROM forecasts f
                   JOIN resolutions res ON res.condition_id = f.condition_id
                   WHERE f.model_id = ?""", (r["model_id"],))
        })
        skill_rows.append({
            "model_id": r["model_id"], "window": r["window_label"], "n": r["n"],
            "n_markets": n_markets,
            "tier": honesty_tier(n_markets, config["eval"]["n_insufficient"],
                                 config["eval"]["n_preliminary"]),
            "brier_model": r["brier"], "brier_market": r["brier_market"],
            "skill": r["skill"], "ci_lo": r["skill_ci_lo"], "ci_hi": r["skill_ci_hi"],
            "mde": float("nan"),
            "log_loss": r["log_loss"], "log_loss_market": r["log_loss_market"],
        })
        if r["window_label"] == "all_time" and r["calibration_json"]:
            bins_by_model[r["model_id"]] = json.loads(r["calibration_json"])

    # Recompute MDE inline from stored paired rows (cheap at current scale).
    from lab.eval.run import resolved_forecast_rows
    for row in skill_rows:
        if row["window"] != "all_time":
            continue
        pairs = resolved_forecast_rows(conn, row["model_id"], None)
        if len(pairs) < 2:
            continue
        diffs_by_market: dict[str, list[float]] = {}
        for p in pairs:
            d = (p["p_market_at_ts"] - p["payout_yes"]) ** 2 - (p["p_yes"] - p["payout_yes"]) ** 2
            diffs_by_market.setdefault(p["condition_id"], []).append(d)
        per_market = np.array([np.mean(v) for v in diffs_by_market.values()])
        if len(per_market) > 1:
            row["mde"] = float(2.8 * np.std(per_market, ddof=1) / np.sqrt(len(per_market)))

    calibration_plot = None
    if bins_by_model:
        plot_path = plot_reliability(bins_by_model, reports / "reliability.png")
        calibration_plot = plot_path.name

    clv_rows = []
    model_ids = [r["model_id"] for r in conn.execute("SELECT DISTINCT model_id FROM forecasts")]
    for model_id in model_ids:
        forecasts = [dict(r) for r in conn.execute(
            "SELECT ts, condition_id, model_id, p_yes, p_market_at_ts FROM forecasts "
            "WHERE model_id = ? ORDER BY ts DESC LIMIT 2000", (model_id,))]
        for horizon, stats in clv_drift(forecasts, store, config["eval"]["clv_horizons_hours"]).items():
            if stats["n"]:
                clv_rows.append({"model_id": model_id, "horizon": horizon,
                                 "n": stats["n"], "drift": stats["mean_signed_drift"]})

    llm_total = conn.execute("SELECT COALESCE(SUM(cost_usd),0) AS t FROM llm_spend").fetchone()["t"]
    from lab.store.db import llm_spend_today
    from lab.store.snapshots import utc_date_str
    from lab.util import now_utc
    llm_today = llm_spend_today(conn, utc_date_str(now_utc()))

    from lab.learn.postmortem import lessons_digest

    html = Environment().from_string(TEMPLATE).render(
        generated_at=now_utc_iso(),
        lessons=lessons_digest(conn),
        health=health,
        skill_rows=skill_rows,
        calibration_plot=calibration_plot,
        clv_rows=clv_rows,
        llm_total=llm_total,
        llm_today=llm_today,
        llm_cap=config["llm"]["daily_cost_cap_usd"],
    )
    out = reports / "report.html"
    out.write_text(html, encoding="utf-8")
    log.info("report rendered", extra={"ctx": {"path": str(out)}})
    return out
