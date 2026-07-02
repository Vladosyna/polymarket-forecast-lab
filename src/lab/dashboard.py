"""Phase 8 -- optional Streamlit dashboard (read-only view of the lab's data).

Run with:  uv run streamlit run src/lab/dashboard.py
Reads the same SQLite/Parquet as the CLI; writes nothing.
"""

from __future__ import annotations

import json
from datetime import timedelta

import polars as pl
import streamlit as st

from lab.collect.status import gather_status
from lab.eval.report import latest_eval_rows
from lab.eval.scoring import honesty_tier
from lab.export import export_rows
from lab.store import db as dbmod
from lab.store.snapshots import SnapshotStore, utc_date_str
from lab.util import load_config, now_utc

st.set_page_config(page_title="Polymarket Forecast Lab", layout="wide")

config = load_config()


# One connection per script run: Streamlit reruns land on different threads,
# and SQLite connections are thread-bound.
conn = dbmod.connect(config["storage"]["db_path"])
store = SnapshotStore(config["storage"]["snapshots_dir"])

st.title("Polymarket Forecast Lab")
st.caption(
    "Read-only research dashboard. Shadow-portfolio figures are SIMULATION only — "
    "no real money exists anywhere in this system."
)

# --- Health ---------------------------------------------------------------
st.header("Data health")
status = gather_status(config)
cols = st.columns(4)
cols[0].metric("Forecast rows", status["forecast_rows"])
cols[1].metric("Resolutions", status["resolutions"])
liquid = status["tiers"]["liquid"]
age = liquid["last_snapshot_age_min"]
cols[2].metric("Liquid snapshot age (min)", age if age is not None else "never",
               delta=None, delta_color="off")
cols[3].metric("LLM spend today ($)",
               f"{status['llm_spend_today_usd']} / {status['llm_daily_cap_usd']}")
tier_table = [
    {"tier": t, **s} for t, s in status["tiers"].items()
]
st.dataframe(pl.DataFrame(tier_table).to_pandas(), use_container_width=True)

# --- Universe ---------------------------------------------------------------
st.header("Live universe")
universe = pl.DataFrame(
    [dict(r) for r in conn.execute(
        """SELECT condition_id, question, category, tier, liquidity_num, volume_num,
                  end_date_iso FROM markets
           WHERE active = 1 AND closed = 0 AND tier IN ('liquid','tail')
           ORDER BY liquidity_num DESC"""
    )]
)
if universe.is_empty():
    st.info("No tracked markets yet — run `lab sync`.")
else:
    tier_filter = st.multiselect("Tier", ["liquid", "tail"], default=["liquid"])
    view = universe.filter(pl.col("tier").is_in(tier_filter)) if tier_filter else universe
    st.caption(f"{len(view)} markets")
    st.dataframe(view.head(300).to_pandas(), use_container_width=True)

# --- Latest forecasts vs market ------------------------------------------------
st.header("Latest forecasts vs market")
forecast_df = pl.DataFrame(list(export_rows(conn)))
if forecast_df.is_empty():
    st.info("No forecasts in the ledger yet — run `lab forecast`.")
else:
    models = sorted(forecast_df["model_id"].unique().to_list())
    model = st.selectbox("Model", models,
                         index=models.index("m4_ensemble") if "m4_ensemble" in models else 0)
    sub = forecast_df.filter(pl.col("model_id") == model).with_columns(
        (pl.col("p_yes") - pl.col("p_market_at_ts")).alias("disagreement")
    )
    c1, c2 = st.columns([1, 1])
    with c1:
        st.scatter_chart(
            sub.select("p_market_at_ts", "p_yes").to_pandas(),
            x="p_market_at_ts", y="p_yes", height=380,
        )
        st.caption("Points off the diagonal = model disagrees with the market.")
    with c2:
        top = sub.sort(pl.col("disagreement").abs(), descending=True).head(20)
        st.caption("Largest disagreements")
        st.dataframe(
            top.select("question", "category", "p_market_at_ts", "p_yes",
                       "disagreement").to_pandas(),
            use_container_width=True,
        )

# --- Calibration ---------------------------------------------------------------
st.header("Calibration (resolved forecasts)")
eval_rows = [r for r in latest_eval_rows(conn) if r["window_label"] == "all_time"]
if not eval_rows:
    st.info("INSUFFICIENT DATA — no resolved paired forecasts yet. "
            "First stats arrive as short-horizon markets resolve (~2–4 weeks).")
else:
    skill_table = []
    chart_rows = []
    for r in eval_rows:
        n_markets = conn.execute(
            """SELECT COUNT(DISTINCT f.condition_id) AS n FROM forecasts f
               JOIN resolutions res ON res.condition_id = f.condition_id
               WHERE f.model_id = ?""", (r["model_id"],)
        ).fetchone()["n"]
        skill_table.append({
            "model": r["model_id"], "n": r["n"], "n_markets": n_markets,
            "tier": honesty_tier(n_markets, config["eval"]["n_insufficient"],
                                 config["eval"]["n_preliminary"]),
            "brier": round(r["brier"], 4), "brier_market": round(r["brier_market"], 4),
            "skill": round(r["skill"], 4),
            "ci": f"[{r['skill_ci_lo']:.4f}, {r['skill_ci_hi']:.4f}]",
        })
        for b in json.loads(r["calibration_json"] or "[]"):
            if b["n"]:
                chart_rows.append({"model": r["model_id"], "forecast": b["p_mean"],
                                   "observed": b["y_rate"]})
    st.dataframe(pl.DataFrame(skill_table).to_pandas(), use_container_width=True)
    if chart_rows:
        st.scatter_chart(pl.DataFrame(chart_rows).to_pandas(),
                         x="forecast", y="observed", color="model", height=380)
        st.caption("Perfect calibration lies on the diagonal.")

# --- Shadow book -----------------------------------------------------------------
st.header("Shadow book — SIMULATION")
from lab.shadow.portfolio import portfolio_summary

summary = portfolio_summary(conn, store, config)
cols = st.columns(5)
cols[0].metric("Open trades (sim)", summary["open_trades"])
cols[1].metric("Resolved trades (sim)", summary["resolved_trades"])
cols[2].metric("Realized P&L (sim $)", round(summary["realized_pnl_sim"], 2))
cols[3].metric("Unrealized P&L (sim $)", round(summary["unrealized_pnl_sim"], 2))
cols[4].metric("Max drawdown (sim $)", round(summary["max_drawdown_sim"], 2))
trades = pl.DataFrame([dict(r) for r in conn.execute(
    "SELECT * FROM shadow_trades ORDER BY opened_ts DESC LIMIT 200")])
if trades.is_empty():
    st.info("No simulated trades yet — run `lab shadow` after forecasts accumulate.")
else:
    st.dataframe(trades.to_pandas(), use_container_width=True)

st.caption(
    f"Snapshot store: {config['storage']['snapshots_dir']} — "
    f"DB: {config['storage']['db_path']} — generated {now_utc().isoformat(timespec='seconds')}"
)
