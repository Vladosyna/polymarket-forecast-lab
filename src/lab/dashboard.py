"""Phase 8 -- optional Streamlit dashboard (mostly read-only view of the lab's
data, with one narrow exception below).

Run with:  uv run streamlit run src/lab/dashboard.py
Reads the same SQLite/Parquet as the CLI.

Phase 14 added a sidebar mode selector so the wealth-economy visuals sit
alongside the existing sections as separate views instead of one long page
(brief Phase 8: "as interactive views of what `lab report` already renders
statically") -- the Wealth Economy mode reuses the SAME `eval/wealth_plots.py`
functions the static report calls, rather than re-implementing charts here.

"Cross-Venue Matching (M7)" is the one mode that writes: confirming/rejecting
a proposed pair edits data/markets_map.yaml through the exact same
confirm_match/reject_match functions `lab map confirm` uses -- the human-in-
the-loop gate M7 requires (brief section 6/9: "a human confirms every pair
before it's live") is the whole point of surfacing proposals here instead of
only via the CLI.
"""

from __future__ import annotations

import json
from datetime import timedelta

import polars as pl
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from lab.collect.status import gather_status
from lab.eval.report import latest_eval_rows
from lab.eval.scoring import honesty_tier
from lab.eval.wealth_plots import (
    m4_attribution_snapshot,
    plot_wealth_curves,
    plot_wealth_drawdown,
    sleeping_expert_rankings,
)
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
    "Read-only research dashboard. Shadow-portfolio and wealth-ledger figures are "
    "SIMULATION-adjacent only — no real money exists anywhere in this system."
)

MODES = ["Overview", "Forecasts vs Market", "Calibration & Skill", "Wealth Economy",
        "Shadow Portfolio", "Cross-Venue Matching (M7)"]
mode = st.sidebar.radio("View", MODES)

if mode == "Overview":
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

elif mode == "Forecasts vs Market":
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

elif mode == "Calibration & Skill":
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

elif mode == "Wealth Economy":
    st.header("Virtual prediction economy — wealth ledger (SIMULATION-adjacent)")
    st.caption(
        "Kelly log-wealth accounting per (model, category) -- a scoring/selection layer "
        "over every model's already-written forecasts, not a new signal. Compare models "
        "by cum_log_wealth / n_forecasts (coverage-normalized), never the raw cumulative "
        "total. The 'sports' category is the null-control reference: skill there should "
        "stay near zero."
    )
    rankings = sleeping_expert_rankings(conn)
    if not rankings:
        st.info("INSUFFICIENT DATA — no resolved forecasts scored into the wealth ledger yet.")
    else:
        st.dataframe(pl.DataFrame(rankings).to_pandas(), use_container_width=True)
        curves_path = plot_wealth_curves(conn, config)
        if curves_path:
            st.image(str(curves_path), caption="Wealth curves per model per category")
        drawdown_path = plot_wealth_drawdown(conn, config)
        if drawdown_path:
            st.image(str(drawdown_path), caption="Wealth drawdown per model per category")
        attribution = m4_attribution_snapshot(conn, config)
        if attribution:
            st.subheader("M4 attribution snapshot (today's pool, linear log-odds)")
            st.dataframe(pl.DataFrame(attribution).to_pandas(), use_container_width=True)

elif mode == "Shadow Portfolio":
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

elif mode == "Cross-Venue Matching (M7)":
    st.header("Cross-venue matching (M7) — propose, review, confirm")
    st.caption(
        "Propose-then-confirm: nothing here is live for M7 until a human confirms it. "
        "Read every rationale before confirming — the LLM occasionally proposes a pair "
        "whose own rationale says the events don't actually match (different office, "
        "different year) yet still returns high confidence. That's exactly why this "
        "gate exists."
    )
    from lab.models.m7_crossvenue import (
        confirm_match,
        link_confirmed_event,
        load_markets_map,
        reject_match,
        save_markets_map,
    )

    def _question_for(cid: str) -> str:
        row = conn.execute("SELECT question FROM markets WHERE condition_id = ?", (cid,)).fetchone()
        return row["question"] if row else cid

    map_data = load_markets_map()

    st.subheader(f"Confirmed ({len(map_data['confirmed'])})")
    if map_data["confirmed"]:
        conf_rows = [{
            "question": _question_for(e["condition_id"]),
            "venue": e["venue"], "external_id": e["external_id"],
            "confirmed_ts": e.get("confirmed_ts", ""),
        } for e in map_data["confirmed"]]
        st.dataframe(pl.DataFrame(conf_rows).to_pandas(), use_container_width=True)
    else:
        st.info("No confirmed pairs yet.")

    st.subheader(f"Proposed, awaiting review ({len(map_data['proposed'])})")
    if not map_data["proposed"]:
        st.info("No proposals waiting — run `lab map propose` (CLI) or the button below.")
    else:
        for e in map_data["proposed"]:
            key = f"{e['condition_id']}:{e['venue']}:{e['external_id']}"
            with st.container(border=True):
                st.markdown(f"**Polymarket:** {_question_for(e['condition_id'])}")
                st.markdown(f"**Kalshi ({e['external_id']}):** {e.get('external_question', '')}")
                st.caption(f"confidence={e.get('confidence', 0):.2f} — {e.get('rationale', '')}")
                c1, c2 = st.columns(2)
                if c1.button("Confirm", key=f"confirm_{key}"):
                    fresh = load_markets_map()
                    if confirm_match(fresh, e["condition_id"], e["venue"], external_id=e["external_id"]):
                        save_markets_map(fresh)
                        entry = next(x for x in fresh["confirmed"]
                                    if x["condition_id"] == e["condition_id"] and x["venue"] == e["venue"])
                        event_id = link_confirmed_event(conn, e["condition_id"], e["venue"], entry["external_id"])
                        st.success(f"Confirmed — event_id={event_id}")
                        st.rerun()
                if c2.button("Reject", key=f"reject_{key}"):
                    fresh = load_markets_map()
                    reject_match(fresh, e["condition_id"], e["venue"], e["external_id"])
                    save_markets_map(fresh)
                    st.rerun()

    st.divider()
    st.caption("Runs real Kalshi + LLM API calls (small $ cost, within the daily cap).")
    if st.button("Run `lab map propose` now"):
        import asyncio

        from lab.api.http import TokenBucket
        from lab.api.kalshi import KalshiClient
        from lab.models.m7_crossvenue import kalshi_propose_candidates, propose_matches
        from lab.news.extract import create_llm_client

        async def _fetch_candidates():
            bucket = TokenBucket(rate=config["collect"]["rate_limit"]["requests_per_second"],
                                 burst=config["collect"]["rate_limit"]["burst"])
            kalshi = KalshiClient(bucket)
            try:
                return await kalshi_propose_candidates(kalshi, config)
            finally:
                await kalshi.aclose()

        llm = create_llm_client(conn, config)
        if llm is None:
            st.error("No LLM configured — see llm.api_key_env in config.yaml.")
        else:
            with st.spinner("Fetching Kalshi candidates and asking the LLM..."):
                candidates = asyncio.run(_fetch_candidates())
                new_proposals = propose_matches(conn, config, candidates, llm)
            st.success(f"{len(new_proposals)} new candidate(s) added.")
            st.rerun()

st.caption(
    f"Snapshot store: {config['storage']['snapshots_dir']} — "
    f"DB: {config['storage']['db_path']} — generated {now_utc().isoformat(timespec='seconds')}"
)
