"""Graceful shutdown of the long-running processes (collect / orchestrator).

Regression cover for the 2026-07-25 bug: `_install_signal_handlers` installed
a handler that only logged. Installing ANY Python handler replaces a signal's
default disposition, so SIGTERM -- whose default is "terminate" -- became a
no-op: the process ran on until systemd's TimeoutStopSec expired and SIGKILL
arrived ("State 'stop-sigterm' timed out. Killing." in the unit's journal),
which meant the `finally:` cleanup in run_collect/run_orchestrator never ran.

The two platforms take genuinely different code paths -- POSIX uses
`loop.add_signal_handler` (asyncio owns the C-level hook and dispatches
through its self-pipe), Windows falls back to `signal.signal` plus a
thread-safe hop -- so each is covered by the test that can actually exercise
it. The POSIX case is an end-to-end subprocess test against a real signal,
because that is the only way to test what actually broke; a regression there
kills the child, not the test run.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap

import pytest

from lab.collect.runner import (
    _idle_until_stopped,
    _install_signal_handlers,
    _run_until_stopped,
)

_POSIX = os.name != "nt"


# --- abandoning long startup work: portable ---------------------------------

def test_run_until_stopped_abandons_long_work():
    """A stop must not have to wait out the startup collection cycle -- the
    tail-tier pass alone runs for tens of minutes, well past any service
    manager's stop timeout."""
    cancelled = []

    async def scenario():
        stop = asyncio.Event()

        async def long_work():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        async def stop_soon():
            await asyncio.sleep(0.05)
            stop.set()

        asyncio.ensure_future(stop_soon())
        await asyncio.wait_for(_run_until_stopped(long_work(), stop), timeout=5)

    asyncio.run(scenario())
    assert cancelled == [True], "long-running startup work was not abandoned"


def test_run_until_stopped_lets_work_finish_when_no_stop():
    """No stop request -> the work runs to completion, untouched."""
    done = []

    async def scenario():
        stop = asyncio.Event()

        async def work():
            await asyncio.sleep(0.01)
            done.append(True)

        await asyncio.wait_for(_run_until_stopped(work(), stop), timeout=5)

    asyncio.run(scenario())
    assert done == [True]


def test_run_until_stopped_surfaces_a_genuine_failure():
    """A real error must still propagate -- abandoning on stop must not turn
    into swallowing every exception."""
    async def scenario():
        stop = asyncio.Event()

        async def boom():
            raise RuntimeError("collector blew up")

        with pytest.raises(RuntimeError, match="collector blew up"):
            await _run_until_stopped(boom(), stop)

    asyncio.run(scenario())


# --- the idle loop: portable, both platforms --------------------------------

def test_idle_loop_returns_promptly_when_stopped():
    """The loop must observe the stop request rather than sleeping out its
    full interval -- with the old `while True: await asyncio.sleep(60)` there
    was no way to observe it at all."""
    async def scenario():
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(_idle_until_stopped(stop, interval=3600), timeout=2)

    asyncio.run(scenario())


def test_idle_loop_ticks_until_stopped():
    """Ticks keep firing on the interval while running, and the loop exits on
    the tick after the stop arrives (the heartbeat write is such a tick)."""
    ticks = []

    async def scenario():
        stop = asyncio.Event()

        def on_tick():
            ticks.append(len(ticks))
            if len(ticks) == 3:
                stop.set()

        await asyncio.wait_for(
            _idle_until_stopped(stop, on_tick=on_tick, interval=0.01), timeout=5)

    asyncio.run(scenario())
    assert len(ticks) == 3


def test_idle_loop_does_not_tick_when_already_stopped():
    """A stop that arrives during startup must not buy one more tick's worth
    of work before the loop notices it."""
    ticks = []

    async def scenario():
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(
            _idle_until_stopped(stop, on_tick=lambda: ticks.append(1), interval=0.01),
            timeout=2)

    asyncio.run(scenario())
    assert ticks == []


# --- POSIX: real signal, real process ---------------------------------------

_CHILD = """
import asyncio, sys
from lab.collect.runner import (
    _idle_until_stopped, _install_signal_handlers, _run_until_stopped)

BUSY = {busy}

async def main():
    stop = asyncio.Event()
    _install_signal_handlers(stop.set)

    async def phases():
        if BUSY:
            # Stand in for _startup_collection_cycle: long, and reached before
            # the idle loop -- the shape that made the first fix insufficient.
            await asyncio.sleep(3600)
        await _idle_until_stopped(stop, interval=3600)

    try:
        print("READY", flush=True)
        await _run_until_stopped(phases(), stop)
    finally:
        print("CLEANUP_RAN", flush=True)

asyncio.run(main())
"""


def _spawn_child(busy: bool = False):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_CHILD).format(busy=busy)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    assert proc.stdout.readline().strip() == "READY", "child never started"
    return proc


@pytest.mark.skipif(not _POSIX, reason="needs real POSIX signal delivery")
def test_sigterm_shuts_down_cleanly_end_to_end():
    """THE regression test, in the terms the bug was actually reported in:
    send SIGTERM, the process must exit promptly AND run its cleanup.

    Against the pre-fix code the child survives the signal entirely and this
    fails on the timeout -- which is exactly what systemd saw."""
    proc = _spawn_child()
    proc.send_signal(signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("process ignored SIGTERM -- this is the bug systemd hit")

    assert proc.returncode == 0, f"unclean exit: {proc.returncode}"
    assert "CLEANUP_RAN" in out, "process exited without running its finally block"


@pytest.mark.skipif(not _POSIX, reason="needs real POSIX signal delivery")
def test_sigterm_during_long_startup_still_exits_promptly():
    """The second half of the bug, found on the VPS after the first fix:
    the handler fired, but the process sat in _startup_collection_cycle --
    tens of minutes of tail-tier collection -- and only checked the stop
    request afterwards, so systemd still hit its timeout and SIGKILLed.

    Shutdown must not be hostage to whatever startup phase is in flight."""
    proc = _spawn_child(busy=True)
    proc.send_signal(signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("SIGTERM during startup did not stop the process")

    assert "CLEANUP_RAN" in out, "process exited without running its finally block"


@pytest.mark.skipif(not _POSIX, reason="needs real POSIX signal delivery")
def test_second_sigterm_still_kills_a_wedged_shutdown():
    """The first signal restores the default disposition, so an operator (or
    systemd) is never trapped by a cleanup path that itself wedged. Verified
    by signalling a child that is already shutting down."""
    proc = _spawn_child()
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=10)
    assert "CLEANUP_RAN" in out

    # Second delivery to a fresh child that has NOT been asked to stop proves
    # the handler is armed; the restore-on-first-signal behaviour is what makes
    # a repeat delivery fall through to the default action rather than be
    # swallowed. Assert the disposition was actually handed back.
    proc2 = _spawn_child()
    proc2.send_signal(signal.SIGTERM)
    proc2.send_signal(signal.SIGTERM)
    out2, _ = proc2.communicate(timeout=10)
    assert proc2.returncode is not None, "process survived two SIGTERMs"


# --- Windows: the signal.signal fallback path -------------------------------

@pytest.fixture()
def restore_signal_handlers():
    """Installing real handlers in-process would otherwise leak into the rest
    of the suite (and into pytest's own Ctrl+C handling)."""
    names = ("SIGTERM", "SIGINT", "SIGBREAK")
    saved = {}
    for name in names:
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                saved[sig] = signal.getsignal(sig)
            except (ValueError, OSError):
                pass
    yield
    for sig, handler in saved.items():
        if handler is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


@pytest.mark.skipif(_POSIX, reason="POSIX uses loop.add_signal_handler; see the subprocess tests")
def test_windows_fallback_handler_requests_stop(restore_signal_handlers):
    """On Windows there is no add_signal_handler, so the handler goes in via
    signal.signal and must hop back onto the loop. Invoking it the way the OS
    would must request a stop -- the pre-fix handler only logged."""
    stopped = asyncio.Event()

    async def scenario():
        _install_signal_handlers(stopped.set)
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler), "no handler installed"
        handler(signal.SIGTERM, None)
        await asyncio.sleep(0)  # let call_soon_threadsafe land
        assert stopped.is_set(), "SIGTERM did not request a shutdown"

    asyncio.run(scenario())


@pytest.mark.skipif(_POSIX, reason="POSIX uses loop.add_signal_handler; see the subprocess tests")
def test_windows_fallback_restores_default_and_is_idempotent(restore_signal_handlers):
    """After the first signal the default disposition is back (so a hung
    shutdown stays killable), and a duplicate delivery does not re-enter the
    shutdown path."""
    calls = []

    async def scenario():
        _install_signal_handlers(lambda: calls.append(1))
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        await asyncio.sleep(0)
        assert signal.getsignal(signal.SIGTERM) is not handler, (
            "handler still installed after first signal -- a hung shutdown "
            "could not be interrupted"
        )
        handler(signal.SIGTERM, None)  # duplicate delivery
        await asyncio.sleep(0)
        assert calls == [1]

    asyncio.run(scenario())
