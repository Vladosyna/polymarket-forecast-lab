"""Shared git plumbing for the jobs that push to the public repo.

Split out on 2026-08-18, after the same failure happened twice in one day: the
VPS committed a ledger commitment at 02:44 and a paper-export snapshot at
11:05, and neither could be pushed because a docs commit had landed on the
public repo from the operator's laptop in between. Both jobs logged
`pushed: false` and returned normally, so nothing surfaced -- and a ledger
commitment that is committed but not published is worth exactly nothing, which
`config.yaml`'s own comment says in as many words ("an unpushed commitment on
a public repo verifies nothing").

This will keep happening: two hosts write to one repo by design (§13's public
record), and the laptop pushes documentation whenever the operator works on it.
A rejection is therefore the normal case, not an exception, and the jobs have
to handle it rather than report it.

**Deliberately NOT used by publish.py.** That job pushes the private results
mirror, whose parquet snapshots and `lab.db` are Git LFS objects. A
`git pull --rebase` there checks files out, which fires the LFS smudge filter
and downloads objects -- against a repository already carrying 10.8GB of LFS
history on a 1GB free-tier allowance (see config.yaml's publish.raw_data
comment). Auto-rebasing that repo to fix a collision it has never actually had
-- only this host pushes to it -- would risk the quota to solve nothing.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _is_non_fast_forward(stderr: str) -> bool:
    """Whether a failed push was rejected because the remote moved ahead.

    Narrow on purpose. A push can also fail on auth, network, a pre-push hook
    or a protected branch, and rebasing in those cases would be a silent
    history rewrite in response to an error that has nothing to do with
    divergence. Git prints `[rejected]` for this class and names the reason
    alongside it, so require both.
    """
    return "[rejected]" in stderr and ("fetch first" in stderr or "non-fast-forward" in stderr)


def push_with_rebase(cwd: Path) -> subprocess.CompletedProcess:
    """`git push`; on a non-fast-forward rejection, rebase once and retry.

    Returns the CompletedProcess of the final push attempt, so callers keep
    reading `returncode`/`stderr` exactly as they did with a bare push. When
    the rebase itself fails, the returned stderr carries both messages rather
    than only the original rejection -- the rebase is the more useful half.

    `--autostash` is not optional here. `data/markets_map.yaml` is routinely
    modified-but-uncommitted on the collecting host (that is by design: the
    proposal is durably on disk before its git step runs, so a failed commit
    just waits for the next call), and `git pull --rebase` refuses to start
    with a dirty tree. Without autostash this helper would fail precisely on
    the host it exists for.

    Retries once, not in a loop: if a second push races in during the rebase,
    the next scheduled run picks the work up. Nothing is lost by waiting --
    the commit is already local.
    """
    push = run_git(["push"], cwd)
    if push.returncode == 0:
        return push

    stderr = push.stderr or ""
    if not _is_non_fast_forward(stderr):
        return push

    log.warning("push rejected as non-fast-forward; rebasing and retrying once",
                extra={"ctx": {"cwd": str(cwd)}})
    pull = run_git(["pull", "--rebase", "--autostash"], cwd)
    if pull.returncode != 0:
        log.error("rebase before push retry failed", extra={"ctx": {"cwd": str(cwd)}})
        return subprocess.CompletedProcess(
            push.args, push.returncode, push.stdout,
            f"{stderr}\n[pull --rebase --autostash failed]\n{pull.stderr}",
        )

    retry = run_git(["push"], cwd)
    if retry.returncode != 0:
        return subprocess.CompletedProcess(
            retry.args, retry.returncode, retry.stdout,
            f"{retry.stderr}\n[first attempt, before rebase]\n{stderr}",
        )
    log.info("push succeeded after rebase", extra={"ctx": {"cwd": str(cwd)}})
    return retry
