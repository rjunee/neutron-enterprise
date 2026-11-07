# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Database-level append-only enforcement (SQLite triggers) + app-level surface."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ne.audit import append_record, ensure_audit_schema
from ne.audit.store import ne_audit_head, ne_audit_log
from ne.schema import AuditEvent


def _seed(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    with engine.begin() as conn:
        append_record(conn, audit(event_id="e1"))


def test_update_raises(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            ne_audit_log.update().where(ne_audit_log.c.event_id == "e1").values(actor="attacker")
        )


def test_delete_raises(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.event_id == "e1"))


def test_ensure_schema_idempotent(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit)
    ensure_audit_schema(engine)
    ensure_audit_schema(engine)
    with engine.connect() as conn:
        head_rows = conn.execute(sa.select(sa.func.count()).select_from(ne_audit_head)).scalar_one()
        log_rows = conn.execute(sa.select(sa.func.count()).select_from(ne_audit_log)).scalar_one()
    assert head_rows == 1  # genesis not duplicated
    assert log_rows == 1
    # triggers still intact after a re-run
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.event_id == "e1"))


def test_module_exposes_no_update_or_delete_api() -> None:
    import ne.audit

    for name in dir(ne.audit):
        assert "update" not in name.lower()
        assert "delete" not in name.lower()
