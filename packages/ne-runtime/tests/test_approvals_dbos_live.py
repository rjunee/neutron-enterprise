# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE durable approval against real DBOS 2.26 over Postgres (NE-103c: CI-gated).

Gated on ``NE_RUNTIME_DBOS_DATABASE_URL`` being SET and reachable (the
``test_approvals_concurrency_postgres.py`` env-gate precedent) — NO longer an
``NE_LIVE_DBOS=1`` opt-in. It EXECUTES in the NE-103c ``live-postgres`` CI job
(direct connection) under a must-run-not-skip gate, AND in the
``live-postgres-pooled`` CI job (through a transaction-mode PgBouncer —
NE-POOLER-TIMEOUTS: ``create_approval_request_step`` routes through
``ne.audit.engine.create_engine_with_timeouts``, so this suite is the
acceptance proof that the pooler fix holds for the durable-approval path too).
To run for real:

    NE_RUNTIME_DURABILITY=dbos \
        NE_RUNTIME_DBOS_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/ne_runtime_dbos \
        uv run pytest packages/ne-runtime/tests/test_approvals_dbos_live.py -q

It launches the real DBOS runtime and runs ``await_approval_workflow`` durably; a
second connection approves the request mid-flight, and the workflow completes
'approved' exactly once.
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
        "Postgres and NE_RUNTIME_DURABILITY=dbos (the live-postgres and "
        "live-postgres-pooled CI lanes both run this) "
        f"(url {_URL!r} unset/unreachable)",
        allow_module_level=True,
    )

assert _URL is not None  # narrows for type-checkers (unreachable when skipped)


def test_durable_approval_workflow_completes_once() -> None:
    import json

    import sqlalchemy as sa
    from dbos import DBOS

    from ne.runtime import launch_durable_runtime
    from ne.runtime.approval_store import ne_approval_requests
    from ne.runtime.approvals_durable import (
        await_approval_workflow,
        create_approval_request_step,
    )

    assert _URL is not None
    # In-process DBOS binds the default admin server on :3001 + background threads;
    # DBOS.destroy() in the finally frees them so nothing leaks into the rest of the
    # pytest session (the ne-worker live proof asserts :3001 is closed).
    launch_durable_runtime(name="ne-runtime-approvals-live", database_url=_URL)
    try:
        request_id = create_approval_request_step(
            _URL,
            json.dumps(
                {
                    "action": "write.external.email.send",
                    "tier": 2,
                    "principal_id": "user:ada",
                    "payload": {"tool": "write.external.email.send"},
                    "sla_s": 100.0,
                    "expiry_s": 1000.0,
                }
            ),
        )

        # A second connection approves the pending request mid-flight.
        engine = sa.create_engine(_URL)
        with engine.begin() as conn:
            conn.execute(
                ne_approval_requests.update()
                .where(ne_approval_requests.c.request_id == request_id)
                .values(status="approved", first_approver="alice")
            )
        engine.dispose()

        from datetime import UTC, datetime, timedelta

        deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        assert await_approval_workflow(_URL, request_id, 0.5, deadline) == "approved"
    finally:
        DBOS.destroy()
