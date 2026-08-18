"""push_with_rebase: recover from the non-fast-forward rejection that two
hosts writing to one public repo produce as a matter of course.

Regression for 2026-08-18, when it happened twice in a day -- a ledger
commitment at 02:44 and a paper-export snapshot at 11:05 both sat committed
but unpublished because a docs commit had landed from the laptop in between,
and both jobs logged `pushed: false` and returned normally.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from lab.gitutil import push_with_rebase, run_git


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    r = run_git(list(args), cwd)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")


def _two_clones(tmp_path: Path) -> tuple[Path, Path]:
    """A bare origin plus two clones of it -- the VPS and the laptop."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "master", str(origin))

    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(origin), str(seed))
    _identity(seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "master")

    vps, laptop = tmp_path / "vps", tmp_path / "laptop"
    for c in (vps, laptop):
        _git(tmp_path, "clone", str(origin), str(c))
        _identity(c)
    return vps, laptop


def test_push_recovers_from_a_non_fast_forward_rejection(tmp_path):
    """The laptop pushes docs; the VPS then commits a ledger line. A bare push
    is rejected -- which is what left a cryptographic pre-registration
    unpublished. It must rebase and land, keeping both commits."""
    vps, laptop = _two_clones(tmp_path)

    (laptop / "docs.md").write_text("laptop docs\n", encoding="utf-8")
    _git(laptop, "add", "docs.md")
    _git(laptop, "commit", "-m", "docs from the laptop")
    _git(laptop, "push", "origin", "master")

    (vps / "ledger.jsonl").write_text('{"date":"2026-08-18"}\n', encoding="utf-8")
    _git(vps, "add", "ledger.jsonl")
    _git(vps, "commit", "-m", "Ledger commitment: 2026-08-18")

    bare = run_git(["push"], vps)
    assert bare.returncode != 0 and "[rejected]" in bare.stderr, "fixture did not reproduce the collision"

    assert push_with_rebase(vps).returncode == 0

    log = run_git(["log", "--oneline", "origin/master"], vps).stdout
    assert "Ledger commitment: 2026-08-18" in log
    assert "docs from the laptop" in log


def test_push_rebases_with_a_dirty_tree(tmp_path):
    """data/markets_map.yaml is routinely modified-but-uncommitted on the
    collecting host by design, and `git pull --rebase` refuses to start with a
    dirty tree -- so without --autostash this helper would fail precisely on
    the host it exists for. The local edit must survive."""
    vps, laptop = _two_clones(tmp_path)

    (laptop / "docs.md").write_text("laptop docs\n", encoding="utf-8")
    _git(laptop, "add", "docs.md")
    _git(laptop, "commit", "-m", "docs from the laptop")
    _git(laptop, "push", "origin", "master")

    (vps / "markets_map.yaml").write_text("confirmed: []\n", encoding="utf-8")
    _git(vps, "add", "markets_map.yaml")
    _git(vps, "commit", "-m", "map: confirmed pairs")
    # ...and a further, still-uncommitted rewrite pending its next call
    (vps / "markets_map.yaml").write_text("confirmed: []\nproposed: [pending]\n", encoding="utf-8")

    assert push_with_rebase(vps).returncode == 0
    assert "proposed: [pending]" in (vps / "markets_map.yaml").read_text(encoding="utf-8")
    assert "map: confirmed pairs" in run_git(["log", "--oneline", "origin/master"], vps).stdout


def test_a_non_divergence_failure_is_never_rebased(tmp_path, monkeypatch):
    """Auth, network, a pre-push hook: rebasing on those would be a silent
    history rewrite in response to an error that has nothing to do with the
    remote having moved."""
    import lab.gitutil as gu

    calls: list[list[str]] = []

    def fake_run_git(args, cwd):
        calls.append(args)
        return subprocess.CompletedProcess(args, 128, "", "fatal: Authentication failed")

    monkeypatch.setattr(gu, "run_git", fake_run_git)
    result = gu.push_with_rebase(tmp_path)

    assert result.returncode == 128
    assert calls == [["push"]], "a non-divergence failure must not trigger a pull"


def test_a_clean_push_does_not_pull(tmp_path, monkeypatch):
    import lab.gitutil as gu

    calls: list[list[str]] = []

    def fake_run_git(args, cwd):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(gu, "run_git", fake_run_git)
    assert gu.push_with_rebase(tmp_path).returncode == 0
    assert calls == [["push"]]


def test_a_failed_rebase_reports_both_messages(tmp_path, monkeypatch):
    """The rebase failure is the more useful half; the original rejection alone
    would send the reader looking for the wrong problem."""
    import lab.gitutil as gu

    def fake_run_git(args, cwd):
        if args == ["push"]:
            return subprocess.CompletedProcess(args, 1, "", " ! [rejected] master -> master (fetch first)")
        return subprocess.CompletedProcess(args, 1, "", "error: could not apply abc123... conflict")

    monkeypatch.setattr(gu, "run_git", fake_run_git)
    result = gu.push_with_rebase(tmp_path)

    assert result.returncode != 0
    assert "[rejected]" in result.stderr
    assert "could not apply" in result.stderr
