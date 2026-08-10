"""Single-instance guard: code version, registry, and the pure decision logic."""

from __future__ import annotations

import os

from lab import process_guard
from lab.process_guard import classify_cmdline, code_version, stale_or_redundant


def test_code_version_deterministic():
    assert code_version() == code_version()
    assert len(code_version()) == 12


def test_classify_cmdline_roles():
    assert classify_cmdline(["python.exe", "-m", "lab", "run"]) == "orchestrator"
    assert classify_cmdline(["python.exe", "-m", "lab", "collect"]) == "collector"
    assert classify_cmdline(["python", "-m", "uv", "run", "lab", "collect"]) == "collector"
    assert classify_cmdline(["python.exe", "-m", "lab", "watchdog"]) == "watchdog"
    assert classify_cmdline(
        ["python", "-m", "streamlit", "run", "src/lab/dashboard.py"]) == "dashboard"
    assert classify_cmdline(["python", "-m", "lab", "status"]) is None
    assert classify_cmdline(["python", "somethingelse.py"]) is None


def test_registry_round_trip(tmp_path):
    config = {"storage": {"db_path": str(tmp_path / "lab.db")}}
    assert process_guard._load_registry(config) == []
    process_guard.register_self(config, "orchestrator")
    entries = process_guard._load_registry(config)
    assert len(entries) == 1
    assert entries[0]["role"] == "orchestrator"
    assert entries[0]["pid"] == os.getpid()
    assert entries[0]["code_version"] == code_version()


def _entry(pid, role, ver, start_ts):
    return {"pid": pid, "role": role, "code_version": ver, "start_ts": start_ts}


def test_no_action_when_single_current():
    entries = [_entry(100, "orchestrator", "v1", 1000.0)]
    assert stale_or_redundant(entries, self_pid=100, current_version="v1") == []


def test_outdated_version_selected():
    entries = [_entry(200, "collector", "old", 1000.0)]
    assert stale_or_redundant(entries, self_pid=999, current_version="v1") == []
    assert stale_or_redundant(
        entries, self_pid=999, current_version="v1", retire_sole_outdated=True,
    ) == [200]


def test_self_never_selected_even_if_stale():
    entries = [_entry(300, "orchestrator", "old", 1000.0)]
    assert stale_or_redundant(entries, self_pid=300, current_version="v1") == []


def test_duplicate_role_keeps_newest():
    entries = [
        _entry(1, "orchestrator", "v1", 1000.0),
        _entry(2, "orchestrator", "v1", 2000.0),
    ]
    # self is unrelated; the older (pid 1) stands down, newest (pid 2) survives
    assert stale_or_redundant(entries, self_pid=999, current_version="v1") == [1]


def test_collector_redundant_when_orchestrator_alive():
    entries = [
        _entry(10, "orchestrator", "v1", 5000.0),
        _entry(11, "collector", "v1", 4000.0),
        _entry(12, "collector", "v1", 6000.0),
    ]
    assert stale_or_redundant(entries, self_pid=999, current_version="v1") == [11, 12]


def test_duplicate_self_newest_stops_older():
    entries = [
        _entry(20, "orchestrator", "v1", 9000.0),
        _entry(21, "orchestrator", "v1", 1000.0),
    ]
    assert stale_or_redundant(entries, self_pid=20, current_version="v1") == [21]


def test_duplicate_self_older_never_stops_self():
    entries = [
        _entry(20, "orchestrator", "v1", 9000.0),
        _entry(21, "orchestrator", "v1", 1000.0),
    ]
    assert stale_or_redundant(entries, self_pid=21, current_version="v1") == []


def test_none_version_not_treated_as_outdated():
    entries = [_entry(40, "orchestrator", None, 1000.0)]
    assert stale_or_redundant(entries, self_pid=999, current_version="v1") == []


def test_unmanaged_duplicate_dashboard_stops_older():
    entries = [
        _entry(50, "dashboard", None, 1000.0),
        _entry(51, "dashboard", None, 2000.0),
    ]
    assert stale_or_redundant(entries, self_pid=-1, current_version="v1") == [50]


def test_launcher_worker_pair_treated_as_one_instance(monkeypatch):
    # pid 70 = launcher stub, pid 71 = its real worker child (Windows venv
    # quirk, see _ancestor_pids). Neither registers -- streamlit never calls
    # register_self -- so both show up unmanaged with code_version=None.
    # Without ancestry-awareness this used to retire the launcher every single
    # guard cycle (hourly), killing a healthy pair along with its child.
    def fake_ancestors(pid, max_depth=4):
        return {70} if pid == 71 else set()

    monkeypatch.setattr(process_guard, "_ancestor_pids", fake_ancestors)
    entries = [
        _entry(70, "dashboard", None, 1000.0),
        _entry(71, "dashboard", None, 1000.001),
    ]
    assert stale_or_redundant(entries, self_pid=-1, current_version="v1") == []


def test_separate_launcher_worker_pairs_dedup_by_whole_cluster(monkeypatch):
    # Two genuinely separate launcher+worker pairs (e.g. a stale cycle not yet
    # cleaned up alongside a fresh one) -- the older pair retires as a whole,
    # never split so that only its launcher or only its worker survives.
    ancestry = {80: set(), 81: {80}, 90: set(), 91: {90}}
    monkeypatch.setattr(process_guard, "_ancestor_pids", lambda pid, max_depth=4: ancestry.get(pid, set()))
    entries = [
        _entry(80, "dashboard", None, 1000.0),
        _entry(81, "dashboard", None, 1000.001),
        _entry(90, "dashboard", None, 2000.0),
        _entry(91, "dashboard", None, 2000.001),
    ]
    result = stale_or_redundant(entries, self_pid=-1, current_version="v1")
    assert sorted(result) == [80, 81]


def test_gather_merges_managed_and_unmanaged(tmp_path, monkeypatch):
    config = {"storage": {"db_path": str(tmp_path / "lab.db")}}
    process_guard.register_self(config, "orchestrator")

    def fake_unmanaged(_config):
        return [{"role": "dashboard", "pid": 9999, "start_ts": 1.0, "code_version": None}]

    monkeypatch.setattr(process_guard, "find_unmanaged", fake_unmanaged)
    gathered = process_guard._gather_all_live_instances(config)
    roles = {e["pid"]: e["role"] for e in gathered}
    assert roles[os.getpid()] == "orchestrator"
    assert roles[9999] == "dashboard"
    assert gathered[0].get("managed") is True
    assert any(e.get("pid") == 9999 and not e.get("managed") for e in gathered)


def test_cleanup_stops_duplicate_without_register(tmp_path, monkeypatch):
    config = {"storage": {"db_path": str(tmp_path / "lab.db")}}
    stopped: list[int] = []

    def fake_gather(_config):
        return [
            _entry(60, "orchestrator", "v1", 1000.0),
            _entry(61, "orchestrator", "v1", 2000.0),
        ]

    def fake_terminate(pid, timeout=5.0):
        stopped.append(pid)
        return True

    monkeypatch.setattr(process_guard, "_gather_all_live_instances", fake_gather)
    monkeypatch.setattr(process_guard, "_terminate", fake_terminate)
    monkeypatch.setattr(process_guard, "code_version", lambda: "v1")

    result = process_guard.cleanup(config)
    assert result["stopped"] == [60]
    assert stopped == [60]


# --- one-shot subcommands are not duplicate orchestrators (2026-08-10) ------

def test_uv_run_lab_subcommands_are_not_classified_as_orchestrators():
    """`uv` contributes its own "run" token, and the classifier matched "run"
    anywhere -- so `uv run lab eval`, `... report`, `... learn` and
    `... shadow` all came back "orchestrator", looked like duplicates of the
    live one, and were SIGTERMed by it.

    Observed twice on 2026-08-10: a manual `lab eval` died at ~12 minutes with
    a bare exit 143, guard log `stopped=[115023]` at the exact second. The same
    mechanism would have killed `lab-learn.service` mid-run on its monthly
    timer -- the unit created that same day precisely so the job would be
    reliable.
    """
    from lab.process_guard import classify_cmdline

    for sub in ("eval", "report", "learn", "shadow", "status", "export"):
        assert classify_cmdline(["/root/.local/bin/uv", "run", "lab", sub]) is None, (
            f"`uv run lab {sub}` must not look like a second orchestrator"
        )


def test_the_long_running_roles_are_still_recognised():
    """The guard's actual job -- standing down a leftover orchestrator or a
    collector made redundant by one -- must be untouched."""
    from lab.process_guard import classify_cmdline

    assert classify_cmdline(["/root/.local/bin/uv", "run", "lab", "run"]) == "orchestrator"
    assert classify_cmdline(["python", "-m", "lab", "run"]) == "orchestrator"
    assert classify_cmdline(["/root/.local/bin/uv", "run", "lab", "collect"]) == "collector"
    assert classify_cmdline(["python", "-m", "streamlit", "run",
                             "src/lab/dashboard.py"]) == "dashboard"
    # not ours at all
    assert classify_cmdline(["python", "-m", "http.server"]) is None
    assert classify_cmdline(["/root/.local/bin/uv", "run", "--with", "pmxt",
                             "python", "scripts/pmxt_router_scan.py"]) is None


def test_the_orchestrators_own_out_of_process_children_are_not_candidates():
    """`_run_lab_command_out_of_process` spawns `python -m lab report`. It is
    protected as a descendant anyway, but it must not be classifiable either --
    belt and braces on the path that already took the host down once."""
    from lab.process_guard import classify_cmdline

    assert classify_cmdline(["python", "-m", "lab", "report"]) is None
