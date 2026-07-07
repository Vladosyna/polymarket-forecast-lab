"""Self-contained analytics jobs shared by the CLI and the orchestrator.

Each function opens its own database connection, does one unit of work, and
closes it -- so they are safe to run on a worker thread while the collector
holds its own connection on the event loop (SQLite WAL + busy_timeout).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from lab.store import db
from lab.store.snapshots import SnapshotStore

log = logging.getLogger(__name__)


def run_forecast_job(config: dict[str, Any]) -> dict[str, Any]:
    """Full forecast pass: base models + M6 coherence + M7 cross-venue + M4 ensemble."""
    from lab.forecast import build_default_models, run_forecasts
    from lab.learn.refit import load_active_artifact
    from lab.models.m4_ensemble import M4Ensemble
    from lab.models.m6_consistency import scan_universe, write_m6_forecasts
    from lab.models.m7_crossvenue import scan_confirmed_pairs, write_m7_forecasts

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    try:
        counts = run_forecasts(conn, store, build_default_models(conn, config, store), config)
        findings = asyncio.run(scan_universe(conn, store, config))
        counts["m6_written"] = write_m6_forecasts(conn, store, findings, config)
        m7_results = asyncio.run(scan_confirmed_pairs(conn, store, config))
        counts["m7_written"] = write_m7_forecasts(conn, store, m7_results, config)
        m4 = M4Ensemble(conn, load_active_artifact(config, "m4_weights"),
                        load_active_artifact(config, "m4_extremization"))
        counts["m4_written"] = run_forecasts(conn, store, [m4], config)["written"]
    finally:
        conn.close()
    log.info("forecast job complete", extra={"ctx": counts})
    return counts


def run_eval_job(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Score resolved paired forecasts; also updates the wealth ledger
    (Phase 14), the shadow MWU ensemble-weight challenger (Phase 14.1), and
    the CLV validity flag (Phase 17 item 4) -- all "wired into the existing
    nightly lab eval step -- no new CLI command." Guardrail 17: MWU is the
    one process permitted to update m4_weights between monthly `lab learn`
    cycles."""
    from lab.economy.mwu import update_mwu_challenger
    from lab.economy.wealth import update_wealth_ledger
    from lab.eval.clv import update_clv_trust_flag
    from lab.eval.run import run_eval
    from lab.store.snapshots import SnapshotStore

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    try:
        summaries = run_eval(conn, config)
        wealth_summary = update_wealth_ledger(conn, config)
        mwu_summary = update_mwu_challenger(conn, config)
        clv_trust = update_clv_trust_flag(conn, config, store)
    finally:
        conn.close()
    log.info("eval job complete", extra={"ctx": {"models": len(summaries), "wealth": wealth_summary,
                                                  "mwu": mwu_summary, "clv_trust": clv_trust}})
    return summaries


def run_report_job(config: dict[str, Any]) -> str:
    """Render the static HTML report."""
    from lab.eval.report import render_report

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    try:
        path = render_report(conn, store, config)
    finally:
        conn.close()
    log.info("report job complete", extra={"ctx": {"path": str(path)}})
    return str(path)


def run_shadow_job(config: dict[str, Any]) -> dict[str, Any]:
    """Simulated shadow portfolio: settle resolved, open new entries."""
    from lab.shadow.portfolio import portfolio_summary, run_shadow_entries, settle_resolved

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    try:
        settled = settle_resolved(conn)
        opened = run_shadow_entries(conn, store, config)
        summary = portfolio_summary(conn, store, config)
    finally:
        conn.close()
    result = {"opened": opened, "settled": settled, "summary": summary}
    log.info("shadow job complete", extra={"ctx": {"opened": opened, "settled": settled}})
    return result


def run_learn_job(config: dict[str, Any], apply: bool = False) -> Any:
    """Monthly learning loop: batch refits, champion/challenger, post-mortems.

    Dry-run by default (writes nothing); pass apply=True to persist and promote.
    The orchestrator's scheduled run leaves apply=False, so every learning cycle
    is a reviewable diff rather than a silent mutation (brief section 6).
    """
    from lab.learn.loop import run_learn
    from lab.news.extract import create_llm_client

    conn = db.connect(config["storage"]["db_path"])
    llm = create_llm_client(conn, config)
    try:
        summary = run_learn(conn, config, llm, apply=apply)
    finally:
        conn.close()
    log.info("learn job complete", extra={"ctx": {"apply": apply, "summary": str(summary)[:200]}})
    return summary


def run_map_propose_job(config: dict[str, Any]) -> dict[str, Any]:
    """M7: LLM proposes candidate Kalshi matches into markets_map.yaml's `proposed`
    list -- never touches `confirmed`. Safe to run unattended: a pair only
    starts feeding M7 once a human runs `lab map confirm` on it (brief section 6,
    Phase 9 acceptance: "a proposed-but-unconfirmed pair is NOT forecast").
    Skips cleanly (no-op) if no LLM is configured, same as a missing API key
    would for M3."""
    from lab.api.http import TokenBucket
    from lab.api.kalshi import KalshiClient
    from lab.models.m7_crossvenue import propose_matches
    from lab.news.extract import create_llm_client

    conn = db.connect(config["storage"]["db_path"])
    try:
        llm = create_llm_client(conn, config)
        if llm is None:
            log.info("map propose job: no LLM configured, skipping")
            return {"skipped": "no_llm"}
        async def _fetch_candidates() -> list[Any]:
            bucket = TokenBucket(rate=config["collect"]["rate_limit"]["requests_per_second"],
                                 burst=config["collect"]["rate_limit"]["burst"])
            kalshi = KalshiClient(bucket)
            try:
                return await kalshi.open_markets(limit=200)
            finally:
                await kalshi.aclose()

        candidates = asyncio.run(_fetch_candidates())
        proposals = propose_matches(conn, config, candidates, llm)
    finally:
        conn.close()
    result = {"new_proposals": len(proposals)}
    log.info("map propose job complete", extra={"ctx": result})
    return result


def run_publish_job(config: dict[str, Any]) -> dict[str, Any]:
    """Mirror reports/exports/model artifacts to the private results repo and
    push. Curated results only -- never the raw db/snapshots; that bulk copy
    is a manual, user-run step (scripts/publish_results.py --raw-data), not
    something this automatic nightly job does on its own. Never raises: a
    publish failure (e.g. no network) must not block or re-trigger the
    forecast/eval/report bundle it follows."""
    from lab.publish import publish_results

    if not config.get("publish", {}).get("enabled", False):
        return {"skipped": "disabled"}
    conn = db.connect(config["storage"]["db_path"])
    try:
        result = publish_results(config, conn, include_raw_data=False)
    except Exception:
        log.exception("publish job failed")
        return {"error": "publish_failed"}
    finally:
        conn.close()
    log.info("publish job complete", extra={"ctx": result})
    return result


def run_ledger_commitment_job(config: dict[str, Any]) -> dict[str, Any]:
    """Phase 15: nightly cryptographic pre-registration commitment.

    Commits (and pushes) a sha256 over each closed UTC day's appended
    forecasts rows to THIS repo's docs/ledger_commitments.jsonl -- not the
    private results mirror publish.py targets. Never raises: a failed git
    step here must not block or re-trigger the forecast/eval/report bundle
    it follows, same contract as run_publish_job.
    """
    from lab.ledger_commitment import commit_and_push

    if not config.get("ledger", {}).get("enabled", True):
        return {"skipped": "disabled"}
    conn = db.connect(config["storage"]["db_path"])
    try:
        result = commit_and_push(config, conn)
    except Exception:
        log.exception("ledger commitment job failed")
        return {"error": "ledger_commitment_failed"}
    finally:
        conn.close()
    log.info("ledger commitment job complete", extra={"ctx": result})
    return result


def run_rollback_job(config: dict[str, Any], model_id: str,
                     to_version_tag: str | None = None) -> dict[str, Any]:
    """Manually revert a model's active version to a prior one (outside the cycle)."""
    from lab.learn import registry

    conn = db.connect(config["storage"]["db_path"])
    try:
        restored = registry.rollback(conn, config, model_id, reason="rollback",
                                     to_version_tag=to_version_tag)
    finally:
        conn.close()
    result = {"model_id": model_id,
              "restored": None if restored is None else restored["version_tag"]}
    log.info("rollback job complete", extra={"ctx": result})
    return result
