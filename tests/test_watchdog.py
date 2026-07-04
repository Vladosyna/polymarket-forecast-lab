"""`run_watchdog` supervises `lab run` as a child process: on any exit it waits
`watchdog.restart_delay_seconds` and relaunches, until interrupted. No real
subprocess is spawned here -- `subprocess.Popen` and `time.sleep` are stubbed
so the loop's decisions are tested deterministically and fast."""

from __future__ import annotations

import pytest

from lab.collect import runner


class _FakeProc:
    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        return self._exit_code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_restarts_after_delay_on_exit(monkeypatch):
    calls = {"popen": 0, "sleeps": []}
    exits = iter([1, 1, KeyboardInterrupt])  # crash, crash, then operator stops it

    def fake_popen(cmd, *a, **kw):
        calls["popen"] += 1
        outcome = next(exits)
        if outcome is KeyboardInterrupt:
            class _RaisingProc(_FakeProc):
                def wait(self, timeout=None):
                    raise KeyboardInterrupt
            return _RaisingProc(0)
        return _FakeProc(outcome)

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runner.time, "sleep", lambda s: calls["sleeps"].append(s))

    with pytest.raises(KeyboardInterrupt):
        runner.run_watchdog({"watchdog": {"restart_delay_seconds": 600}})

    assert calls["popen"] == 3
    assert calls["sleeps"] == [600, 600]


def test_stands_down_prior_watchdog_instances_before_looping(monkeypatch):
    """A second `lab watchdog` must not just start spawning its own orchestrator
    child alongside an existing watchdog's -- two supervisors each restarting
    their own child fight over which child survives via process_guard, which
    is exactly the crash-restart loop this guard call exists to prevent."""
    calls = {"guard_role": None}

    def fake_guard(config, role):
        calls["guard_role"] = role
        return {"stopped": [123]}

    monkeypatch.setattr(runner, "_enforce_instance_guard", fake_guard)
    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda cmd, *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        runner.run_watchdog({})

    assert calls["guard_role"] == "watchdog"


def test_default_delay_is_ten_minutes(monkeypatch):
    def fake_popen(cmd, *a, **kw):
        return _FakeProc(0) if fake_popen.calls == 0 else (_ for _ in ()).throw(KeyboardInterrupt)

    fake_popen.calls = 0
    seen_delays = []

    def counting_popen(cmd, *a, **kw):
        fake_popen.calls += 1
        if fake_popen.calls == 1:
            return _FakeProc(0)
        raise AssertionError("should not be reached before interrupt")

    monkeypatch.setattr(runner.subprocess, "Popen", counting_popen)

    def fake_sleep(seconds):
        seen_delays.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        runner.run_watchdog({})  # no watchdog config -> falls back to 600s default

    assert seen_delays == [600]
