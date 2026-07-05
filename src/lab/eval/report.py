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

<h2>Skill vs market (paired Brier), by venue and category</h2>
<p class="note">skill = mean(brier_market − brier_model); positive = beating the market.
95% CI: cluster bootstrap by event (fallback market) -- descriptive only as of Phase 11.
n markets counts resolved event clusters, not venue-market rows. A skill estimate smaller
than its own MDE is noise by construction. skill_pw is the precision-weighted stratified
estimator (brief section 7); a skill claim requires the anytime-valid CS AND skill_pw's own
CI to both exclude zero and agree in direction.</p>
{% if skill_rows %}
<table>
<tr><th>model</th><th>venue</th><th>category</th><th>window</th><th>n rows</th><th>n markets</th><th>tier</th>
<th>brier model</th><th>brier market</th><th>skill</th><th>95% CI</th><th>MDE</th>
<th>skill_pw</th><th>skill_pw 95% CI</th><th>n strata</th>
<th>anytime CS</th><th>CS covers 0?</th>
<th>log loss</th><th>log loss mkt</th></tr>
{% for r in skill_rows %}
<tr><td>{{ r.model_id }}</td><td>{{ r.venue }}</td><td>{{ r.category }}</td>
<td>{{ r.window }}</td><td>{{ r.n }}</td><td>{{ r.n_markets }}</td>
<td class="tier-{{ r.tier.split(' ')[0] }}">{{ r.tier }}</td>
<td>{{ "%.4f"|format(r.brier_model) }}</td><td>{{ "%.4f"|format(r.brier_market) }}</td>
<td>{{ "%.4f"|format(r.skill) }}</td>
<td>[{{ "%.4f"|format(r.ci_lo) }}, {{ "%.4f"|format(r.ci_hi) }}]</td>
<td>{{ "%.4f"|format(r.mde) if r.mde == r.mde else "—" }}</td>
<td>{{ "%.4f"|format(r.skill_pw) if r.skill_pw is not none else "insufficient data" }}</td>
<td>{% if r.skill_pw_ci_lo is not none %}[{{ "%.4f"|format(r.skill_pw_ci_lo) }}, {{ "%.4f"|format(r.skill_pw_ci_hi) }}]{% else %}—{% endif %}</td>
<td>{{ r.n_strata_pw if r.n_strata_pw is not none else "—" }}</td>
<td>{% if r.cs_lo is not none %}[{{ "%.4f"|format(r.cs_lo) }}, {{ "%.4f"|format(r.cs_hi) }}]{% else %}—{% endif %}</td>
<td class="{{ 'tier-INSUFFICIENT' if r.cs_covers_zero else 'tier-STANDARD' }}">
{% if r.cs_covers_zero is not none %}{{ "yes" if r.cs_covers_zero else "no" }}{% else %}—{% endif %}</td>
<td>{{ "%.4f"|format(r.log_loss) }}</td><td>{{ "%.4f"|format(r.log_loss_market) }}</td></tr>
{% endfor %}
</table>
<p class="note">Rows labeled <b>null_control</b> are the sports control sample: statistically
significant skill there invalidates the run pending investigation. Rows with venue/category
"(legacy)" predate Phase 11 and were never re-scored per-venue.</p>
{% else %}
<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no resolved paired forecasts yet.</p>
{% endif %}

<h2>Pooling / extremization diagnostics</h2>
<p class="note">Per-category extremization exponent applied to M4's pool and to M7's external
venue pool (brief section 6, Phase 13). a=1.0 means no extremization (today's plain log-odds
pool). rho_bar is the mean pairwise correlation of pooled sources on matched events/forecasts;
n_eff is the correlation-discounted effective source count actually used to scale how much of
the fitted a gets applied at forecast time.</p>
{% if pooling_rows %}
<table>
<tr><th>pool</th><th>category</th><th>a (fitted)</th><th>rho_bar</th><th>n members</th><th>n_eff</th></tr>
{% for r in pooling_rows %}
<tr><td>{{ r.pool }}</td><td>{{ r.category }}</td><td>{{ "%.3f"|format(r.a) }}</td>
<td>{{ "%.3f"|format(r.rho_bar) }}</td><td>{{ "%.1f"|format(r.n_members) }}</td>
<td>{{ "%.2f"|format(r.n_eff) }}</td></tr>
{% endfor %}
</table>
{% else %}
<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no extremization exponent fitted yet.</p>
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

<h2>Virtual prediction economy — wealth ledger (SIMULATION-adjacent, no real stakes)</h2>
<p class="note">Kelly log-wealth accounting per (model, category) -- a scoring/selection layer
over every model's already-written forecasts, not a new signal (brief section 6/14). Compare
models by <b>cum_log_wealth / n_forecasts</b> (coverage-normalized), never the raw cumulative
total -- a model covering fewer markets shouldn't be rewarded or penalized for coverage alone.
The "sports" category is the null-control reference: skill there should stay near zero.</p>
{% if wealth_rankings %}
<table>
<tr><th>model</th><th>category</th><th>cum log-wealth</th><th>n forecasts</th><th>avg log-wealth/forecast</th></tr>
{% for r in wealth_rankings %}
<tr><td>{{ r.model_id }}</td><td>{{ r.category }}</td>
<td>{{ "%.4f"|format(r.cum_log_wealth) }}</td><td>{{ r.n_forecasts }}</td>
<td>{{ "%.5f"|format(r.avg_log_wealth) }}</td></tr>
{% endfor %}
</table>
{% if wealth_curves_plot %}<img src="{{ wealth_curves_plot }}" alt="wealth curves per model per category">{% endif %}
{% if wealth_drawdown_plot %}<img src="{{ wealth_drawdown_plot }}" alt="wealth drawdown per model per category">{% endif %}
{% if wealth_attribution %}
<h3>M4 attribution snapshot (today's pool, linear log-odds)</h3>
<table>
<tr><th>model</th><th>category</th><th>mean contribution</th><th>n markets</th></tr>
{% for r in wealth_attribution %}
<tr><td>{{ r.model_id }}</td><td>{{ r.category }}</td>
<td>{{ "%.4f"|format(r.mean_contribution) }}</td><td>{{ r.n_markets }}</td></tr>
{% endfor %}
</table>
{% endif %}
{% else %}
<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no resolved forecasts scored into the wealth ledger yet.</p>
{% endif %}

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
    """Most recent eval_runs row per (model, window, venue, category).

    `IS` (not `=`) for venue/category: SQLite's `=` is NULL-unsafe, and a
    legacy pre-Phase-11 row has NULL venue/category -- `IS` groups those
    together correctly instead of dropping them from every join match.
    """
    rows = conn.execute(
        """
        SELECT e.* FROM eval_runs e
        JOIN (SELECT model_id, window_label, venue, category, MAX(ts) AS ts
              FROM eval_runs GROUP BY model_id, window_label, venue, category) latest
        ON latest.model_id = e.model_id AND latest.window_label = e.window_label
           AND latest.venue IS e.venue AND latest.category IS e.category
           AND latest.ts = e.ts
        ORDER BY e.model_id, e.venue, e.category, e.window_label
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
        if r["n_event_clusters"] is not None:
            # Phase 11 row: the event-cluster count is already computed and
            # persisted by eval/run.py's evaluate_model -- no need to redo it.
            n_markets = r["n_event_clusters"]
        else:
            # Legacy pre-Phase-11 row (venue/category NULL): fall back to the
            # old unscoped condition_id count.
            n_markets = len({
                row["condition_id"] for row in conn.execute(
                    """SELECT DISTINCT f.condition_id FROM forecasts f
                       JOIN resolutions res ON res.condition_id = f.condition_id
                       WHERE f.model_id = ?""", (r["model_id"],))
            })
        skill_rows.append({
            "model_id": r["model_id"], "window": r["window_label"],
            "venue": r["venue"] or "(legacy)", "category": r["category"] or "(legacy)",
            "n": r["n"], "n_markets": n_markets,
            "tier": honesty_tier(n_markets, config["eval"]["n_insufficient"],
                                 config["eval"]["n_preliminary"]),
            "brier_model": r["brier"], "brier_market": r["brier_market"],
            "skill": r["skill"], "ci_lo": r["skill_ci_lo"], "ci_hi": r["skill_ci_hi"],
            "mde": float("nan"),
            "skill_pw": r["skill_pw"], "skill_pw_ci_lo": r["skill_pw_ci_lo"],
            "skill_pw_ci_hi": r["skill_pw_ci_hi"], "n_strata_pw": r["n_strata_pw"],
            "cs_lo": r["cs_lo"], "cs_hi": r["cs_hi"], "cs_covers_zero": r["cs_covers_zero"],
            "log_loss": r["log_loss"], "log_loss_market": r["log_loss_market"],
        })
        if r["window_label"] == "all_time" and r["calibration_json"]:
            bins_by_model[r["model_id"]] = json.loads(r["calibration_json"])

    # Recompute MDE inline from stored paired rows (cheap at current scale),
    # scoped to each row's own venue/category and keyed by event-cluster.
    from lab.eval.run import resolved_forecast_rows
    for row, r in zip(skill_rows, latest_eval_rows(conn)):
        if row["window"] != "all_time":
            continue
        venue_filter = r["venue"]
        category_filter = None if r["category"] in (None, "ALL") else r["category"]
        pairs = resolved_forecast_rows(
            conn, row["model_id"], None, venue=venue_filter, category=category_filter
        )
        if len(pairs) < 2:
            continue
        diffs_by_cluster: dict[str, list[float]] = {}
        for p in pairs:
            d = (p["p_market_at_ts"] - p["payout_yes"]) ** 2 - (p["p_yes"] - p["payout_yes"]) ** 2
            cluster_id = p["event_id"] or p["condition_id"]
            diffs_by_cluster.setdefault(cluster_id, []).append(d)
        per_cluster = np.array([np.mean(v) for v in diffs_by_cluster.values()])
        if len(per_cluster) > 1:
            row["mde"] = float(2.8 * np.std(per_cluster, ddof=1) / np.sqrt(len(per_cluster)))

    from lab.learn.pooling import effective_source_count
    from lab.learn.refit import load_active_artifact as _load_active_artifact

    pooling_rows = []
    for pool_label, artifact_key in (("M4 ensemble", "m4_extremization"),
                                     ("M7 cross-venue", "m7_extremization")):
        artifact = _load_active_artifact(config, artifact_key)
        if not artifact:
            continue
        for cat, spec in artifact.get("categories", {}).items():
            n_members = spec.get("n_members_at_fit", 1)
            rho_bar = spec.get("rho_bar", 0.0)
            pooling_rows.append({
                "pool": pool_label, "category": cat, "a": spec["a"], "rho_bar": rho_bar,
                "n_members": n_members, "n_eff": effective_source_count(n_members, rho_bar),
            })

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

    from lab.eval.wealth_plots import (
        m4_attribution_snapshot,
        plot_wealth_curves,
        plot_wealth_drawdown,
        sleeping_expert_rankings,
    )

    wealth_rankings = sleeping_expert_rankings(conn)
    wealth_curves_plot = None
    wealth_drawdown_plot = None
    wealth_attribution: list[dict] = []
    if wealth_rankings:
        curves_path = plot_wealth_curves(conn, config)
        wealth_curves_plot = curves_path.name if curves_path else None
        drawdown_path = plot_wealth_drawdown(conn, config)
        wealth_drawdown_plot = drawdown_path.name if drawdown_path else None
        wealth_attribution = m4_attribution_snapshot(conn, config)

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
        pooling_rows=pooling_rows,
        calibration_plot=calibration_plot,
        clv_rows=clv_rows,
        wealth_rankings=wealth_rankings,
        wealth_curves_plot=wealth_curves_plot,
        wealth_drawdown_plot=wealth_drawdown_plot,
        wealth_attribution=wealth_attribution,
        llm_total=llm_total,
        llm_today=llm_today,
        llm_cap=config["llm"]["daily_cost_cap_usd"],
    )
    out = reports / "report.html"
    out.write_text(html, encoding="utf-8")
    log.info("report rendered", extra={"ctx": {"path": str(out)}})
    return out
