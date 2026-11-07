# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Crash/rollback durability: the log row + head advance/revert atomically.

Test design informed by ABI versionstore test suite (MIT, Copyright (C) 2025
Naas.ai) — shapes only, no code copied.
"""

from __future__ import annotations

from collections.abc import Callable

import sqlalchemy as sa

from ne.audit import append_record, verify_chain
from ne.audit.store import ne_audit_head, ne_audit_log
from ne.events import append as outbox_append
from ne.events import ensure_outbox_schema
from ne.schema import AuditEvent


def _count(engine: sa.Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(ne_audit_log)).scalar_one()


def _head_seq(engine: sa.Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            sa.select(ne_audit_head.c.seq).where(ne_audit_head.c.id == 1)
        ).scalar_one()


def test_rolled_back_append_leaves_no_trace(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    class _Boom(RuntimeError):
        pass

    try:
        with engine.begin() as conn:
            append_record(conn, audit(event_id="doomed"))
            raise _Boom
    except _Boom:
        pass

    assert _count(engine) == 0
    assert _head_seq(engine) == 0
    # The chain continues cleanly afterwards.
    with engine.begin() as conn:
        rec = append_record(conn, audit(event_id="clean"))
    assert rec.seq == 1
    assert verify_chain(engine).valid


def test_aborted_append_plus_outbox_leaves_neither(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    ensure_outbox_schema(engine)

    class _Boom(RuntimeError):
        pass

    ev = audit(event_id="both")
    try:
        with engine.begin() as conn:
            append_record(conn, ev)
            outbox_append(conn, ev)
            raise _Boom
    except _Boom:
        pass

    assert _count(engine) == 0
    from ne.events.outbox import ne_outbox_events

    with engine.connect() as conn:
        outbox_count = conn.execute(
            sa.select(sa.func.count()).select_from(ne_outbox_events)
        ).scalar_one()
    assert outbox_count == 0


def test_interleaved_commit_rollback_dense_chain(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    class _Boom(RuntimeError):
        pass

    # commit, rollback, commit, rollback, commit
    for i, should_commit in enumerate([True, False, True, False, True]):
        try:
            with engine.begin() as conn:
                append_record(conn, audit(event_id=f"e{i}"))
                if not should_commit:
                    raise _Boom
        except _Boom:
            pass

    assert _count(engine) == 3
    assert _head_seq(engine) == 3
    cv = verify_chain(engine)
    assert cv.valid
    assert cv.checked == 3
