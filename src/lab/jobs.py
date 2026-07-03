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
    """Full forecast pass: base models + M6 coherence + M4 ensemble."""
    from lab.forecast import build_default_models, run_forecasts
    from lab.learn.refit import load_active_artifact
    from lab.models.m4_ensemble import M4Ensemble
    from lab.models.m6_consistency import scan_universe, write_m6_forecasts

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    try:
        counts = run_forecasts(conn, store, build_default_models(conn, config, store), config)
        findings = asyncio.run(scan_universe(conn, store, config))
        counts["m6_written"] = write_m6_forecasts(conn, store, findings, config)
        m4 = M4Ensemble(conn, load_active_artifact(config, "m4_weights"))
        counts["m4_written"] = run_forecasts(conn, store, [m4], config)["written"]
    finally:
        conn.close()
    log.info("forecast job complete", extra={"ctx": counts})
    return counts


def run_eval_job(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Score resolved paired forecasts."""
    from lab.eval.run import run_eval

    conn = db.connect(config["storage"]["db_path"])
    try:
        summaries = run_eval(conn, config)
    finally:
        conn.close()
    log.info("eval job complete", extra={"ctx": {"models": len(summaries)}})
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
