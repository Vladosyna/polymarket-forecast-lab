"""Manual entry point for lab.publish.publish_results.

By default mirrors curated results only (reports/exports/model artifacts) --
the same thing the orchestrator does automatically every night. Pass
--raw-data to ALSO back up data/lab.db + data/snapshots/ to the private
results repo: this is an intentionally manual, user-run step (never done
automatically) since it bulk-copies and pushes the full collected dataset.

Usage: uv run python scripts/publish_results.py [--no-push] [--raw-data]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.publish import publish_results  # noqa: E402
from lab.store import db  # noqa: E402
from lab.util import load_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--raw-data", action="store_true",
                        help="ALSO back up data/lab.db and data/snapshots/ (manual opt-in only)")
    args = parser.parse_args()

    config = load_config()
    conn = db.connect(config["storage"]["db_path"])
    try:
        result = publish_results(
            config, conn, push=not args.no_push, include_raw_data=args.raw_data,
        )
    finally:
        conn.close()
    print(result)


if __name__ == "__main__":
    main()
