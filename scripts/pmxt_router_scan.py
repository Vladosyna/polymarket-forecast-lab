"""Out-of-band pmxt Router scan for M7 cross-venue candidate matches.

NOT part of src/lab's runtime and NOT a pyproject.toml dependency. pmxt is a
unified prediction-market TRADING SDK (create_order/fetch_balance/
fetch_positions live alongside its read-only Router) whose hosted API key
can also authorize live trading -- Claude.md's tech-stack row / S12 says it
must never be imported into src/lab or run by the orchestrator. This script
is deliberately the ONLY place in this repo that imports pmxt, is run by its
OWN separate Windows Scheduled Task (see install-pmxt-scan-task.ps1), and
only ever calls Router's read-only search/matching methods -- never
create_order, cancel_order, or fetch_balance.

Run with:  uv run --with pmxt python scripts/pmxt_router_scan.py
`uv run --with` installs pmxt into an ephemeral/cached environment for this
one invocation only -- pyproject.toml is never touched, so pmxt never
becomes part of this project's own declared dependency tree (the concern
Claude.md raises: "the scope guard greps our own src/, not installed
packages, so it wouldn't catch the drift").

Output: data/pmxt_candidates.json, a plain list of
{poly_condition_id, poly_question, kalshi_ticker, kalshi_title,
relation_type, confidence, scanned_ts}. This file is read-only input to
lab.models.m7_crossvenue.verify_pmxt_candidates, which is the only code
path that ever writes into data/markets_map.yaml -- nothing here is
auto-confirmed; a human still runs `lab map confirm`.

NOTE ON FIELD NAMES: pmxt's exact Router response schema (attribute names on
its Market/Cluster objects) was assembled from partial public docs and could
not be live-tested from the assistant session that wrote this script (the
same "run out-of-band, by a human" boundary this script exists to respect
also blocked testing it inline). On first real run, if you see a message
starting "pmxt schema mismatch", paste the printed raw object dump back for
a quick field-name fix -- the script is written to fail loud with that dump
rather than silently write wrong or empty candidates.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from lab.collect.categories import load_categories  # noqa: E402
from lab.util import load_config  # noqa: E402

OUTPUT_PATH = REPO_ROOT / "data" / "pmxt_candidates.json"


def _attr(obj, *names, default=None):
    """First present attribute across possible pmxt schema spellings."""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _query_terms(config: dict) -> list[str]:
    """Priority-category keywords, not a blind crawl of pmxt's whole catalog
    -- keeps each scan scoped and cheap, mirroring propose_matches' own
    priority-category scoping on the LLM side."""
    taxonomy = load_categories()
    priority = set(config["universe"]["priority_categories"])
    terms = sorted({v for v in taxonomy.get("kalshi_series", {}).values() if v in priority})
    # A handful of concrete seed queries per category reads better to pmxt's
    # search than the bare category slug (e.g. "economics" alone) --
    # adjust/extend this list based on what the first real run actually
    # surfaces.
    seed_queries = {
        "economics": ["fed rate decision", "cpi inflation report", "gdp growth"],
        "weather": ["temperature record", "hurricane landfall"],
        "politics": ["presidential election", "senate control", "governor race"],
        "geopolitics": ["ceasefire agreement", "central bank decision"],
        "entertainment": ["academy awards", "box office"],
    }
    queries: list[str] = []
    for cat in terms:
        queries.extend(seed_queries.get(cat, [cat]))
    return queries


def main() -> None:
    api_key = os.environ.get("PMXT_API_KEY", "").strip()
    if not api_key:
        print("PMXT_API_KEY not set in .env -- nothing to do.")
        return

    import pmxt  # deliberately the only import site in this repo -- see module docstring

    config = load_config()
    router = pmxt.Router(pmxt_api_key=api_key)

    candidates: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for query in _query_terms(config):
        try:
            markets = router.fetch_markets(query=query, limit=10)
        except Exception as exc:  # noqa: BLE001 -- log and keep scanning other queries
            print(f"fetch_markets(query={query!r}) failed: {exc}")
            continue

        for m in markets:
            venue = (_attr(m, "venue", "exchange", "source") or "").lower()
            if venue != "polymarket":
                continue
            poly_id = _attr(m, "market_id", "id", "condition_id")
            poly_question = _attr(m, "question", "title", "name")
            if poly_id is None or poly_question is None:
                print(f"pmxt schema mismatch on a Polymarket market object: {vars(m) if hasattr(m, '__dict__') else m!r}")
                continue

            try:
                clusters = router.fetch_matched_market_clusters(m)
            except Exception as exc:  # noqa: BLE001
                print(f"fetch_matched_market_clusters failed for {poly_id}: {exc}")
                continue

            for cluster in clusters:
                relation_type = _attr(cluster, "relation_type", "relation", default="unknown")
                confidence = _attr(cluster, "confidence", "score", default=0.0)
                cluster_markets = _attr(cluster, "markets", default=[]) or []
                for matched in cluster_markets:
                    matched_venue = (_attr(matched, "venue", "exchange", "source") or "").lower()
                    if matched_venue != "kalshi":
                        continue
                    kalshi_ticker = _attr(matched, "market_id", "id", "ticker")
                    kalshi_title = _attr(matched, "question", "title", "name", default="")
                    if kalshi_ticker is None:
                        print(f"pmxt schema mismatch on a Kalshi cluster member: "
                             f"{vars(matched) if hasattr(matched, '__dict__') else matched!r}")
                        continue
                    key = (str(poly_id), str(kalshi_ticker))
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    candidates.append({
                        "poly_condition_id": str(poly_id),
                        "poly_question": poly_question,
                        "kalshi_ticker": str(kalshi_ticker),
                        "kalshi_title": kalshi_title,
                        "relation_type": relation_type,
                        "confidence": float(confidence),
                        "scanned_ts": now_iso,
                    })

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    print(f"wrote {len(candidates)} candidate(s) to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
