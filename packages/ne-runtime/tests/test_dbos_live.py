# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE end-to-end test against real DBOS 2.26 over Postgres (NE-103c: CI-gated).

Gated on ``NE_RUNTIME_DBOS_DATABASE_URL`` being SET and reachable (the
``test_approvals_concurrency_postgres.py`` env-gate precedent) — NO longer an
``NE_LIVE_DBOS=1`` opt-in. It EXECUTES in the NE-103c ``live-postgres`` CI job (with
``NE_RUNTIME_DURABILITY=dbos``) under a must-run-not-skip gate; locally it skips
cleanly when no Postgres is configured. To run for real:

    NE_RUNTIME_DURABILITY=dbos \
        NE_RUNTIME_DBOS_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/ne_runtime_dbos \
        uv run pytest packages/ne-runtime/tests/test_dbos_live.py -q

It launches the real DBOS runtime and runs an ``@ne_workflow`` composed of an
``@ne_step`` that increments a counter, asserting exactly-once completion. The
real-frontier agent run stays parked with NO test file — the stub gateway is the CI
boundary (see README).
"""

from __future__ import annotations

import os

import pytest


def _postgres_reachable(url: str) -> bool:
    try:
        import sqlalchemy as sa

        engine = sa.create_engine(url)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


_URL = os.environ.get("NE_RUNTIME_DBOS_DATABASE_URL")

if not (_URL and _postgres_reachable(_URL)):
    pytest.skip(
        "pending-live-verification: set NE_RUNTIME_DBOS_DATABASE_URL to a reachable "
        "Postgres and NE_RUNTIME_DURABILITY=dbos (the live-postgres CI lane runs this) "
        f"(url {_URL!r} unset/unreachable)",
        allow_module_level=True,
    )

assert _URL is not None  # narrows for type-checkers (unreachable when skipped)


def test_durable_workflow_runs_exactly_once() -> None:
    from dbos import DBOS

    from ne.runtime import launch_durable_runtime, ne_step, ne_workflow

    assert _URL is not None
    # This IN-PROCESS DBOS launches the default admin server on :3001 and background
    # queue/notification threads. DBOS.destroy() in the finally frees that port and
    # stops those threads so they don't leak into the rest of the pytest session —
    # in particular so the ne-worker live proof (same invocation) can assert its OWN
    # subprocess ran NO admin server on :3001 without seeing this one's leftover.
    launch_durable_runtime(name="ne-runtime-live", database_url=_URL)
    try:
        calls: list[int] = []

        @ne_step
        def increment(n: int) -> int:
            calls.append(n)
            return n + 1

        @ne_workflow(name="live-turn")
        def turn(start: int) -> int:
            return increment(start)

        assert turn(41) == 42
        assert calls == [41]
    finally:
        DBOS.destroy()
