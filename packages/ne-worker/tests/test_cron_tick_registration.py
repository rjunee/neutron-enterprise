# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""P1-6: the DBOS-mode cron tick must be a REAL workflow, not a plain function.

Default-lane (no-Postgres) proofs that :func:`ne.worker.main._register_cron_tick`
wraps :func:`ne.worker.main._dbos_cron_tick` with ``DBOS.workflow(name='cron.tick')``
BEFORE ``DBOS.scheduled`` and hands the WRAPPED workflow to ``scheduled`` (identity
assertion — fails if reverted to scheduling the plain function), and that the tick is
an async coroutine that awaits the shared scheduler exactly once (no ``asyncio.run``
nesting — fails on revert because the old tick was sync).
"""

from __future__ import annotations

import inspect
import types
from typing import Any

from ne.worker import main as worker_main

from .conftest import run


class _FakeDBOS:
    """Records how ``workflow`` and ``scheduled`` are applied by the helper."""

    workflow_name: Any = None
    workflow_target: Any = None
    workflow_wrapper: Any = None
    scheduled_cron: Any = None
    scheduled_target: Any = None

    @staticmethod
    def workflow(*, name: str) -> Any:
        _FakeDBOS.workflow_name = name

        def _decorator(fn: Any) -> Any:
            _FakeDBOS.workflow_target = fn
            # A DISTINCT sentinel wrapper — never the original function — so the
            # identity check on `scheduled`'s target proves the wrapped workflow (not
            # the plain function) is what gets scheduled.
            wrapper = types.SimpleNamespace(kind="cron.tick-workflow", wraps=fn)
            _FakeDBOS.workflow_wrapper = wrapper
            return wrapper

        return _decorator

    @staticmethod
    def scheduled(cron: str) -> Any:
        _FakeDBOS.scheduled_cron = cron

        def _decorator(target: Any) -> Any:
            _FakeDBOS.scheduled_target = target
            return target

        return _decorator


def test_register_cron_tick_wraps_workflow_before_scheduled() -> None:
    fake_mod = types.SimpleNamespace(DBOS=_FakeDBOS)
    returned = worker_main._register_cron_tick(fake_mod)

    assert _FakeDBOS.workflow_name == "cron.tick"
    # The function handed to DBOS.workflow(...) IS the module-level tick.
    assert _FakeDBOS.workflow_target is worker_main._dbos_cron_tick
    assert _FakeDBOS.scheduled_cron == "* * * * *"
    # The object handed to DBOS.scheduled(...) IS the WRAPPED workflow — NOT the plain
    # function. This is the assertion that fails if P1-6 is reverted to scheduling
    # `_dbos_cron_tick` directly.
    assert _FakeDBOS.scheduled_target is _FakeDBOS.workflow_wrapper
    assert _FakeDBOS.scheduled_target is not worker_main._dbos_cron_tick
    assert returned is _FakeDBOS.workflow_wrapper


def test_cron_tick_is_async_and_awaits_scheduler(monkeypatch: Any) -> None:
    # Fails on revert: the old tick was a sync function using asyncio.run(...).
    assert inspect.iscoroutinefunction(worker_main._dbos_cron_tick)

    calls: list[str] = []

    class _StubScheduler:
        async def run_requested_jobs(self) -> Any:
            calls.append("requested")
            return ()

        async def run_due_jobs(self) -> Any:
            calls.append("due")
            return ()

    monkeypatch.setattr(worker_main, "_scheduler", _StubScheduler())
    run(worker_main._dbos_cron_tick(None, None))
    assert calls == ["requested", "due"]


def test_cron_tick_without_scheduler_is_noop(monkeypatch: Any) -> None:
    monkeypatch.setattr(worker_main, "_scheduler", None)
    run(worker_main._dbos_cron_tick(None, None))
