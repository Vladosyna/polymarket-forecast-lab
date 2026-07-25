"""Graceful shutdown of the long-running processes (collect / orchestrator).

Regression cover for the 2026-07-25 bug: `_install_signal_handlers` installed
a handler that only logged. Installing ANY Python handler replaces a signal's
default disposition, so SIGTERM -- whose default is "terminate" -- became a
no-op: the process ran on until systemd's TimeoutStopSec expired and SIGKILL
arrived ("State 'stop-sigterm' timed out. Killing." in the unit's journal),
which meant the `finally:` cleanup in run_collect/run_orchestrator never ran.

The tests below pin the two halves of the fix: the handler must actually
request a stop, and the idle loop must actually observe that request.
"""

from __future__ import annotations

import asyncio
import signal

import pytest

from lab.collect.runner import _idle_until_stopped, _install_signal_handlers


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


def _fire(sig: int) -> None:
    """Invoke whatever handler is currently installed for `sig`, the way the
    OS would -- without actually signalling this process (which on Unix would
    hit asyncio's own handler and on Windows would terminate the test run)."""
    handler = signal.getsignal(sig)
    assert callable(handler), f"no Python handler installed for {sig}"
    handler(sig, None)


def test_signal_handler_requests_stop(restore_signal_handlers):
    """THE regression test: the handler must initiate shutdown, not just log.

    Against the pre-fix code this fails at the call site -- the old
    _install_signal_handlers took no stop callback at all, because it had
    nothing to do with stopping."""
    stopped = asyncio.Event()

    async def scenario():
        _install_signal_handlers({}, stopped.set)
        _fire(signal.SIGTERM)
        # The Windows fallback hops through call_soon_threadsafe, so let the
        # loop turn once before asserting.
        await asyncio.sleep(0)
        assert stopped.is_set(), "SIGTERM did not request a shutdown"

    asyncio.run(scenario())


def test_second_signal_restores_default_disposition(restore_signal_handlers):
    """A shutdown that wedges must still be killable: after the first signal
    the default disposition is back, so a second one is not swallowed."""
    async def scenario():
        _install_signal_handlers({}, lambda: None)
        installed = signal.getsignal(signal.SIGTERM)
        assert callable(installed)

        _fire(signal.SIGTERM)
        await asyncio.sleep(0)

        assert signal.getsignal(signal.SIGTERM) is not installed, (
            "handler still installed after first signal -- a hung shutdown "
            "could not be interrupted"
        )

    asyncio.run(scenario())


def test_repeated_signal_requests_stop_only_once(restore_signal_handlers):
    """Idempotence: the stop request fires once, so a duplicate signal cannot
    re-enter the shutdown path."""
    calls = []

    async def scenario():
        _install_signal_handlers({}, lambda: calls.append(1))
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        await asyncio.sleep(0)
        # Same handler object again, as a duplicate delivery would.
        handler(signal.SIGTERM, None)
        await asyncio.sleep(0)
        assert calls == [1]

    asyncio.run(scenario())


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
