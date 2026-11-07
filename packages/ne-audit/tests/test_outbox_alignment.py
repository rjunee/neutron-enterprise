# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The decided sink-vs-outbox relationship (plan §1.4 event-spine rule).

The audit sink is the durable AUTHORITATIVE store; the ne-events outbox is the
EXPORT transport. An emitter appends the SAME event to BOTH on ONE connection, so
they commit and roll back atomically — alignment, not duplication.
"""

from __future__ import annotations

from collections.abc import Callable

import sqlalchemy as sa

from ne.audit import append_record
from ne.audit.store import ne_audit_log
from ne.events import append as outbox_append
from ne.events import ensure_outbox_schema
from ne.events.outbox import ne_outbox_events
from ne.schema import AuditEvent


def _audit_count(engine: sa.Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(ne_audit_log)).scalar_one()


def _outbox_count(engine: sa.Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(ne_outbox_events)).scalar_one()


def test_atomic_commit_lands_in_both(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    ensure_outbox_schema(engine)
    ev = audit(event_id="shared-1")
    with engine.begin() as conn:
        append_record(conn, ev)
        outbox_append(conn, ev)

    assert _audit_count(engine) == 1
    assert _outbox_count(engine) == 1
    # Same event_id in both stores.
    with engine.connect() as conn:
        audit_id = conn.execute(sa.select(ne_audit_log.c.event_id)).scalar_one()
        outbox_id = conn.execute(sa.select(ne_outbox_events.c.event_id)).scalar_one()
    assert audit_id == outbox_id == "shared-1"


def test_atomic_rollback_lands_in_neither(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    ensure_outbox_schema(engine)

    class _Boom(RuntimeError):
        pass

    ev = audit(event_id="shared-2")
    try:
        with engine.begin() as conn:
            append_record(conn, ev)
            outbox_append(conn, ev)
            raise _Boom
    except _Boom:
        pass

    assert _audit_count(engine) == 0
    assert _outbox_count(engine) == 0
