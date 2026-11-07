# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The offloaded cron tick must run on ONE persistent loop, off the DBOS loop.

Regression proofs for the 2026-08-28 loop-affinity incident: a7a4343d offloaded
the tick body with a FRESH ``asyncio.run`` loop per tick (closed after every
tick), so any event-loop-affine resource the tick's jobs touched — the shared
chunk store's ``AsyncQdrantClient``, the TEI embedder's cached
``httpx.AsyncClient``, the scheduler's own OPA PDP client — was bound to a dead
loop, and BOTH the next tick and the DBOS loop's own ``read.knowledge.search``
died with "bound to a different event loop" / "Event loop is closed" (observed
live on ne-pilot-ne-worker-1).

Two invariants, each of which failed under one historical regression:

* ``test_cron_tick_survives_a_loop_affine_resource_across_ticks`` — a resource
  that binds to the loop that first awaits it (exactly like the Qdrant client)
  must keep working across consecutive ticks, i.e. every tick body runs on the
  SAME never-closed loop. Fails under a7a4343d's per-tick ``asyncio.run``.
* ``test_cron_tick_body_never_runs_on_the_dbos_loop`` — the tick body must NOT
  run on the caller's (DBOS's) event loop. Fails under the pre-a7a4343d inline
  ``await``, which is what wedged the S5 approval Runs (cron.tick blocking the
  DBOS loop for a measured 182-653s per sweep).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import Any

import pytest

from ne.worker import main as worker_main

from .conftest import run


@pytest.fixture(autouse=True)
def _fresh_tick_runner(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Give each test its own runner; stop its loop thread afterwards."""
    runner = worker_main._CronTickLoopRunner()
    monkeypatch.setattr(worker_main, "_cron_tick_runner", runner)
    yield runner
    runner.stop()


class _LoopAffineScheduler:
    """A stub scheduler holding a resource that binds to its first-use loop.

    Replicates ``asyncio.mixins._LoopBoundMixin._get_loop`` — the mechanism
    inside the real ``AsyncQdrantClient``/httpx internals that produced the
    live failures — byte-for-message: bind on first use, raise
    "bound to a different event loop" on cross-loop reuse, and raise
    "Event loop is closed" when the bound loop has been closed.
    """

    def __init__(self) -> None:
        self._home_loop: asyncio.AbstractEventLoop | None = None
        self.body_loops: list[asyncio.AbstractEventLoop] = []

    async def _touch_loop_affine_state(self) -> None:
        loop = asyncio.get_running_loop()
        if self._home_loop is None:
            self._home_loop = loop
        if loop is not self._home_loop:
            raise RuntimeError(f"{self!r} is bound to a different event loop")
        if self._home_loop.is_closed():
            raise RuntimeError("Event loop is closed")

    async def run_requested_jobs(self) -> Any:
        self.body_loops.append(asyncio.get_running_loop())
        await self._touch_loop_affine_state()
        return ()

    async def run_due_jobs(self) -> Any:
        await self._touch_loop_affine_state()
        return ()


def test_cron_tick_survives_a_loop_affine_resource_across_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive ticks share ONE persistent, never-closed body loop.

    Fails under the a7a4343d per-tick ``asyncio.run`` offload: the second tick's
    ``Event.wait()`` raises "is bound to a different event loop" because tick 1's
    loop was closed — which is precisely what poisoned the shared Qdrant client
    for the DBOS loop's ``read.knowledge.search`` in production.
    """
    scheduler = _LoopAffineScheduler()
    monkeypatch.setattr(worker_main, "_scheduler", scheduler)

    run(worker_main._dbos_cron_tick(None, None))
    run(worker_main._dbos_cron_tick(None, None))  # raised RuntimeError pre-fix

    assert len(scheduler.body_loops) == 2
    first, second = scheduler.body_loops
    assert first is second, "tick bodies must share one persistent event loop"
    # The body loop must survive the ticks — a closed loop is what re-breaks
    # every loop-affine client bound to it.
    assert not first.is_closed(), "the persistent tick loop must never be closed"


def test_cron_tick_body_never_runs_on_the_dbos_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The offload itself is preserved (the fix for the wedged S5 Runs).

    Fails if the tick is ever reverted to awaiting the scheduler inline on the
    caller's (DBOS's) loop, which blocked step bookkeeping for 182-653s sweeps.
    """
    scheduler = _LoopAffineScheduler()
    monkeypatch.setattr(worker_main, "_scheduler", scheduler)

    async def _caller() -> asyncio.AbstractEventLoop:
        await worker_main._dbos_cron_tick(None, None)
        return asyncio.get_running_loop()

    caller_loop = run(_caller())

    assert scheduler.body_loops, "tick body did not run"
    assert scheduler.body_loops[0] is not caller_loop, (
        "the tick body must be offloaded off the DBOS event loop"
    )


def test_stopped_runner_recovers_on_next_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a stop() (shutdown path), a later tick starts a fresh loop cleanly."""
    scheduler = _LoopAffineScheduler()
    monkeypatch.setattr(worker_main, "_scheduler", scheduler)

    run(worker_main._dbos_cron_tick(None, None))
    worker_main._cron_tick_runner.stop()
    # The loop-affine state was bound to the stopped loop: fresh state mirrors
    # real behavior (per-loop clients are rebuilt for a new loop by their owners).
    scheduler._home_loop = None
    run(worker_main._dbos_cron_tick(None, None))
    assert len(scheduler.body_loops) == 2
    assert scheduler.body_loops[0] is not scheduler.body_loops[1]


# ---------------------------------------------------------------------------
# 2026-08-29 U13 review (F5/F7): offloading the body freed the DBOS loop, which
# REMOVED the accidental-but-structural serialization the old inline await
# provided (tick N+1 could not fire while tick N blocked the loop; measured
# post-offload: 3 concurrent tick bodies). The runner itself must therefore
# bound in-flight bodies at one — the per-job lease only suppresses re-entry of
# the SAME job, so overlapping ticks ran DIFFERENT jobs concurrently over the
# one shared IngestStack/ConnectorStateStore.
# ---------------------------------------------------------------------------


class _SlowScheduler:
    """A stub whose tick body is still running when the next fires."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.bodies_run = 0

    async def run_requested_jobs(self) -> Any:
        self.bodies_run += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.25)  # a multi-minute sweep, scaled down
        finally:
            self.in_flight -= 1
        return ()

    async def run_due_jobs(self) -> Any:
        return ()


def test_overlapping_tick_fires_admit_exactly_one_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent fires: one body admitted, the rest SKIPPED (not queued).

    Pre-fix this measured max_in_flight == 3: every fire was submitted to the
    persistent loop unconditionally. Skipping (not queueing) is deliberate —
    the sweep is periodic, so the next admitted tick runs the same work,
    while queueing would build an unbounded backlog behind a slow crawl.
    """
    scheduler = _SlowScheduler()
    monkeypatch.setattr(worker_main, "_scheduler", scheduler)

    async def _burst() -> None:
        await asyncio.gather(*(worker_main._dbos_cron_tick(None, None) for _ in range(3)))

    run(_burst())
    assert scheduler.max_in_flight == 1, "tick bodies overlapped"
    assert scheduler.bodies_run == 1, "overlapped fires must be skipped, not queued"

    # Once the body finished, the next fire is admitted normally.
    run(worker_main._dbos_cron_tick(None, None))
    assert scheduler.bodies_run == 2


def _fire_tick_in_daemon_thread() -> threading.Thread:
    """Await one cron tick from a background caller (the DBOS-loop stand-in).

    daemon=True so a REGRESSED stop() (one that strands the body's future and
    leaves this thread awaiting forever) fails the calling test's assertions
    instead of wedging the whole pytest session at exit.
    """

    async def _fire() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await worker_main._dbos_cron_tick(None, None)

    caller = threading.Thread(target=lambda: asyncio.run(_fire()), daemon=True)
    caller.start()
    return caller


def test_stop_waits_for_a_blocking_body_instead_of_cancelling_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F7, the cross-model finding (2026-08-29): the shutdown drain must DRAIN.

    The first fix cancelled the run_coroutine_threadsafe future and THEN waited
    on it — but a cancelled concurrent.futures.Future raises CancelledError
    immediately (measured: 4µs), so stop() proceeded after essentially zero
    wait while the body kept running, and the body was cancelled at its first
    await. The earlier test used asyncio.sleep — immediately cancellable — so
    cancel-then-wait LOOKED like a drain. This body is the shape the runner
    exists to host: a synchronous blocking sweep that never yields until done.
    stop() must let it FINISH (state: completed, never cancelled) and return
    only after it has.
    """
    state = {"completed": False, "cancelled": False}
    body_entered = threading.Event()

    class _BlockingScheduler:
        async def run_requested_jobs(self) -> Any:
            body_entered.set()
            time.sleep(0.6)  # the sync SQLAlchemy sweep stand-in: cancel-immune
            try:
                # The first yield point — exactly where a premature cancel lands.
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            state["completed"] = True
            return ()

        async def run_due_jobs(self) -> Any:
            return ()

    monkeypatch.setattr(worker_main, "_scheduler", _BlockingScheduler())
    caller = _fire_tick_in_daemon_thread()
    assert body_entered.wait(timeout=10.0), "tick body never started"

    worker_main._cron_tick_runner.stop()

    # By the time stop() returns the body has run to completion — not been
    # cancelled out from under live blocking work.
    assert state["completed"] is True, "stop() returned without draining the blocking body"
    assert state["cancelled"] is False, "stop() cancelled the body instead of draining it"
    caller.join(timeout=10.0)
    assert not caller.is_alive(), "the awaiting caller never observed the drained future"


def test_stop_cancels_a_nonyielding_body_only_after_the_drain_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the drain budget, cancel is the bounded LAST resort (not the first
    move) and a cancellable body honours it promptly."""
    body_started = threading.Event()
    cancelled = threading.Event()

    class _BlockedScheduler:
        async def run_requested_jobs(self) -> Any:
            body_started.set()
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return ()

        async def run_due_jobs(self) -> Any:
            return ()

    monkeypatch.setattr(worker_main, "_scheduler", _BlockedScheduler())
    runner = worker_main._cron_tick_runner
    runner._STOP_DRAIN_TIMEOUT_S = 0.3  # tighten the budgets for the test
    runner._STOP_CANCEL_GRACE_S = 5.0

    caller = _fire_tick_in_daemon_thread()
    assert body_started.wait(timeout=10.0), "tick body never started"

    started = time.monotonic()
    runner.stop()
    elapsed = time.monotonic() - started

    assert cancelled.is_set(), "the drain-budget overrun was not cancelled"
    # The drain budget was actually spent waiting before the cancel.
    assert elapsed >= 0.3, f"stop() cancelled before the drain budget ({elapsed:.3f}s)"
    assert elapsed < 10.0, f"stop() hung for {elapsed:.1f}s"
    caller.join(timeout=10.0)
    assert not caller.is_alive(), "the awaiting caller never observed the cancelled future"


def test_stop_abandons_an_uncancellable_body_loudly_after_both_bounds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A body wedged in blocking code past drain + cancel-grace is ABANDONED:
    stop() returns (bounded), says so at ERROR level, leaves the still-running
    loop unclosed, and the daemon loop thread exits once the body returns."""
    import logging

    body_entered = threading.Event()
    body_finished = threading.Event()

    class _WedgedScheduler:
        async def run_requested_jobs(self) -> Any:
            body_entered.set()
            time.sleep(1.2)  # no yield point at all until done: cancel can never land
            body_finished.set()
            return ()

        async def run_due_jobs(self) -> Any:
            return ()

    monkeypatch.setattr(worker_main, "_scheduler", _WedgedScheduler())
    runner = worker_main._cron_tick_runner
    runner._STOP_DRAIN_TIMEOUT_S = 0.15
    runner._STOP_CANCEL_GRACE_S = 0.15

    caller = _fire_tick_in_daemon_thread()
    assert body_entered.wait(timeout=10.0), "tick body never started"

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="ne.worker"):
        runner.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 1.1, f"stop() was not bounded ({elapsed:.2f}s)"
    assert not body_finished.is_set(), "test invalid: the body finished inside the bounds"
    assert any("abandoning" in record.message for record in caplog.records), (
        "the abandoned body was not reported loudly"
    )
    # The abandoned daemon thread drains on its own once the body returns.
    assert body_finished.wait(timeout=10.0), "the abandoned body never completed"
    caller.join(timeout=10.0)
    assert not caller.is_alive()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not any(t.name == "ne-cron-tick-loop" and t.is_alive() for t in threading.enumerate()):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the abandoned loop thread never exited")
