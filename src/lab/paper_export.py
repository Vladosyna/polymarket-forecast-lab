"""Phase 15 addendum (v2.8): automated weekly `lab export --paper` snapshot,
committed and pushed to the public repo -- closes the gap where the manual
`lab export --paper` CLI flow (Phase 15) was never scheduled or auto-committed
anywhere. Writes docs/paper_exports/YYYY-MM-DD.jsonl + a matching
<same-name>.jsonl.meta.json manifest -- the identical "<out>.meta.json" naming
convention `lab export --paper --out <path>` already uses -- reusing
export.py's export_paper_jsonl / paper_export_manifest verbatim. Committed to
the SAME public repo docs/ledger_commitments.jsonl already lives in, for the
same independent-verifiability rationale (see ledger_commitment.py's own
docstring).

Design notes:
- A local _run_git mirrors ledger_commitment.py's and publish.py's own copies
  of the same three-line subprocess wrapper rather than importing either --
  this codebase's own precedent (publish.py already duplicates it rather than
  sharing with ledger_commitment.py) is to keep this trivial, well-understood
  helper local to each caller instead of introducing a shared abstraction.
- Revert-on-failure differs from ledger_commitment.py on purpose:
  ledger_commitment reverts by trimming N appended JSONL lines from one
  ever-growing file; this module instead deletes the two whole files it just
  created, since each week's export is its own dated file pair, not an
  append.
- Idempotent per calendar date: if today's dated file pair already exists,
  the job is a no-op -- the health check's overdue-service catch-up must not
  produce a second, conflicting export for a date already snapshotted.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from lab.util import PROJECT_ROOT, now_utc

log = logging.getLogger(__name__)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def paper_export_paths(config: dict[str, Any], dt=None) -> tuple[Path, Path]:
    """(jsonl_path, meta_path) for "today" (or dt) -- meta_path mirrors the
    CLI's own "<out>.meta.json" convention exactly (out + ".meta.json")."""
    dt = dt or now_utc()
    out_dir = PROJECT_ROOT / config.get("paper_export", {}).get("dir", "docs/paper_exports")
    stamp = dt.date().isoformat()
    jsonl_path = out_dir / f"{stamp}.jsonl"
    meta_path = Path(f"{jsonl_path}.meta.json")
    return jsonl_path, meta_path


def write_paper_export(conn, config: dict[str, Any]) -> dict[str, Any]:
    """Write today's dated JSONL + manifest; no-op if they already exist."""
    from lab.export import export_paper_jsonl, paper_export_manifest

    jsonl_path, meta_path = paper_export_paths(config)
    if jsonl_path.exists():
        return {"written": False, "reason": "already_exists", "path": str(jsonl_path)}

    lines = list(export_paper_jsonl(conn))
    manifest = paper_export_manifest(conn, len(lines))

    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    meta_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"written": True, "path": str(jsonl_path), "row_count": len(lines),
            "code_version": manifest["code_version"]}


def _revert_new_files(*paths: Path) -> None:
    """Undo file creation if the git add/commit step fails -- mirrors
    ledger_commitment._revert_ledger_append's contract (a retry must see this
    date as not-yet-exported, never permanently orphaned)."""
    for p in paths:
        p.unlink(missing_ok=True)


def commit_and_push_paper_export(config: dict[str, Any], conn) -> dict[str, Any]:
    """Write (if new) and commit+push today's paper-export snapshot."""
    written = write_paper_export(conn, config)
    if not written.get("written"):
        return {"committed": False, **written}

    jsonl_path, meta_path = paper_export_paths(config)
    rel_paths = [str(p.relative_to(PROJECT_ROOT)) for p in (jsonl_path, meta_path)]

    try:
        add = _run_git(["add", *rel_paths], PROJECT_ROOT)
        if add.returncode != 0:
            _revert_new_files(jsonl_path, meta_path)
            return {"error": "git_add_failed", "stderr": add.stderr}

        commit = _run_git(["commit", "-m", f"Paper export snapshot: {jsonl_path.stem}"], PROJECT_ROOT)
        if commit.returncode != 0:
            _revert_new_files(jsonl_path, meta_path)
            return {"error": "git_commit_failed", "stderr": commit.stderr}
    except Exception as exc:
        _revert_new_files(jsonl_path, meta_path)
        log.exception("paper export git step failed")
        return {"error": "git_step_exception", "detail": str(exc)}

    result: dict[str, Any] = {"committed": True, "path": str(jsonl_path),
                              "row_count": written["row_count"]}
    if config.get("paper_export", {}).get("push", True):
        try:
            pushed = _run_git(["push"], PROJECT_ROOT)
            result["pushed"] = pushed.returncode == 0
            if not result["pushed"]:
                result["push_stderr"] = pushed.stderr
        except Exception as exc:
            result["pushed"] = False
            result["push_error"] = str(exc)
    return result
