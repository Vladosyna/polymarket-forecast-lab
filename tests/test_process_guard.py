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
    assert stale_or_redundant(entries, self_pid=999, current_version="v1") == [200]


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
