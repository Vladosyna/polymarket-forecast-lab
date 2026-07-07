"""Manual entry point for lab.publish.publish_results.

By default mirrors curated results only (reports/exports/model artifacts).
The automated nightly job (jobs.py::run_publish_job) can now also push
snapshots daily and the db every N days on its own (publish.raw_data.* in
config.yaml) -- this script's --raw-data / --snapshots-only / --db-only
flags remain for an immediate, on-demand push outside that schedule.

Usage: uv run python scripts/publish_results.py [--no-push] [--raw-data | --snapshots-only | --db-only]
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
                        help="ALSO back up both data/lab.db and data/snapshots/")
    parser.add_argument("--snapshots-only", action="store_true",
                        help="ALSO back up data/snapshots/ (not the db)")
    parser.add_argument("--db-only", action="store_true",
                        help="ALSO back up data/lab.db (not snapshots)")
    args = parser.parse_args()

    include_snapshots = args.raw_data or args.snapshots_only
    include_db = args.raw_data or args.db_only

    config = load_config()
    conn = db.connect(config["storage"]["db_path"])
    try:
        result = publish_results(
            config, conn, push=not args.no_push,
            include_snapshots=include_snapshots, include_db=include_db,
        )
    finally:
        conn.close()
    print(result)


if __name__ == "__main__":
    main()
