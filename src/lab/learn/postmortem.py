"""Post-mortems: monthly structured analyses of top-decile misses and wins.

Selection is mechanical (paired Brier difference deciles among resolved
forecasts in the window); the LLM only explains, never adjusts anything.
Lessons feed versioned changes via humans -- no automatic parameter nudges.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from lab.news.extract import BudgetExceeded, LlmClient
from lab.util import now_utc, now_utc_iso

log = logging.getLogger(__name__)

POSTMORTEM_SYSTEM = """You analyze why a probability forecast beat or lost to the market.
Respond ONLY with JSON:
{"error_source": "evidence"|"weighting"|"resolution_reading"|"category"|"horizon"|"none",
 "evidence_quality": 1|2|3, "resolution_reading": "correct"|"questionable"|"wrong",
 "notes": str}
For wins use error_source "none" and describe what the model got right in notes."""


def select_candidates(conn, window_days: int, decile: float) -> dict[str, list[dict]]:
    """Top-decile misses and wins by paired Brier difference, resolved only."""
    since = (now_utc() - timedelta(days=window_days)).isoformat(timespec="seconds")
    rows = [dict(r) for r in conn.execute(
        """
        SELECT f.id, f.condition_id, f.model_id, f.p_yes, f.p_market_at_ts, f.ts,
               f.evidence_run_id, r.payout_yes, m.question, m.category
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id AND r.disputed = 0
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.ts >= ? AND f.model_id != 'm0_market'
        """,
        (since,),
    )]
    if not rows:
        return {"miss": [], "win": []}
    for r in rows:
        y = r["payout_yes"]
        r["brier_model"] = (r["p_yes"] - y) ** 2
        r["brier_market"] = (r["p_market_at_ts"] - y) ** 2
        r["diff"] = r["brier_market"] - r["brier_model"]  # positive = win
    rows.sort(key=lambda r: r["diff"])
    k = max(1, int(len(rows) * decile))
    return {"miss": rows[:k], "win": rows[-k:]}


def _prompt(row: dict, dossier: str | None) -> str:
    lines = [
        f"QUESTION: {row['question']}",
        f"CATEGORY: {row['category']}",
        f"MODEL: {row['model_id']}  FORECAST p_yes={row['p_yes']:.3f}",
        f"MARKET AT FREEZE: {row['p_market_at_ts']:.3f}",
        f"OUTCOME: {'YES' if row['payout_yes'] == 1.0 else 'NO'}",
        f"PAIRED BRIER DIFF (market - model): {row['diff']:+.4f}",
    ]
    if dossier:
        lines.append(f"EVIDENCE DOSSIER (truncated): {dossier[:3000]}")
    return "\n".join(lines)


def run_postmortems(conn, config: dict[str, Any], llm: LlmClient | None,
                    window_days: int = 30) -> int:
    """Generate and store post-mortems. Returns count written."""
    if llm is None:
        log.warning("postmortem: no LLM available, skipping")
        return 0
    decile = config["learn"]["postmortem_decile"]
    candidates = select_candidates(conn, window_days, decile)
    written = 0
    for kind in ("miss", "win"):
        for row in candidates[kind]:
            exists = conn.execute(
                """SELECT 1 FROM postmortems WHERE condition_id=? AND model_id=? AND kind=?""",
                (row["condition_id"], row["model_id"], kind),
            ).fetchone()
            if exists:
                continue
            dossier = None
            if row["evidence_run_id"]:
                d = conn.execute("SELECT dossier_json FROM evidence_runs WHERE id=?",
                                 (row["evidence_run_id"],)).fetchone()
                dossier = d["dossier_json"] if d else None
            try:
                text, usage = llm.complete(POSTMORTEM_SYSTEM, _prompt(row, dossier),
                                           purpose="postmortem", max_tokens=600)
            except BudgetExceeded:
                log.warning("postmortem: budget cap reached, stopping")
                return written
            try:
                analysis = json.loads(text.strip().strip("`").removeprefix("json"))
            except json.JSONDecodeError:
                log.warning("postmortem: invalid JSON, skipping one")
                continue
            conn.execute(
                """INSERT INTO postmortems (ts, condition_id, model_id, kind, brier_model,
                                            brier_market, analysis_json, llm_model, cost_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now_utc_iso(), row["condition_id"], row["model_id"], kind,
                 row["brier_model"], row["brier_market"],
                 json.dumps(analysis, ensure_ascii=False), llm.model, usage["cost_usd"]),
            )
            written += 1
    conn.commit()
    log.info("postmortems written", extra={"ctx": {"count": written}})
    return written


def lessons_digest(conn, window_days: int = 90) -> dict[str, Any]:
    """Quarterly digest: error-source counts + sampled notes, for the report."""
    since = (now_utc() - timedelta(days=window_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT kind, analysis_json FROM postmortems WHERE ts >= ?", (since,)
    ).fetchall()
    sources: dict[str, int] = {}
    notes: list[str] = []
    for r in rows:
        try:
            a = json.loads(r["analysis_json"])
        except json.JSONDecodeError:
            continue
        if r["kind"] == "miss":
            sources[a.get("error_source", "unknown")] = sources.get(
                a.get("error_source", "unknown"), 0) + 1
        if a.get("notes") and len(notes) < 10:
            notes.append(f"[{r['kind']}] {a['notes'][:200]}")
    return {"window_days": window_days, "n": len(rows),
            "miss_error_sources": sources, "sample_notes": notes}
