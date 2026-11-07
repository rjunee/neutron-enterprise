# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Cron lifecycle dual-append into the tamper-evident hash chain (NE-CKD4).

Checkpoint-D boundary-audit finding #4: ``CronScheduler`` used to write its
``job.schedule`` / ``job.cancel`` / ``job.run`` ``AuditEvent`` ONLY to the mutable
``ne_outbox_events``, never into the trigger-protected, hash-chained
``ne_audit_log`` — so cron governance facts could be silently mutated/deleted while
``verify_chain()`` stayed green. This unit dual-appends the SAME event object into
the chain on the SAME transaction as the outbox write (fail-closed: a chain-append
failure rolls the whole cron mutation back). These tests run against a REAL SQLite
``ne_audit_log`` with real append-only triggers (no mock past the seam); the only
monkeypatch injects failure AT the ``append_record`` seam, mirroring the sanctioned
pattern in ``test_scheduler_runs.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DatabaseError

from ne.audit import verify_chain
from ne.audit.store import append_record as real_append_record
from ne.audit.store import ne_audit_log
from ne.contracts import get_config_hash
from ne.contracts.runtime import ScheduledJob
from ne.events.outbox import ne_outbox_events
from ne.runtime.config import RuntimeSettings
from ne.runtime.dispatch import AgentDefinition
from ne.runtime.errors import RuntimeAuditError
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.principals import StaticAgentPrincipalProvider
from ne.runtime.processes import ProcessRegistry
from ne.runtime.scheduler import (
    CronScheduler,
    ensure_scheduler_schema,
    ne_job_runs,
    ne_scheduled_jobs,
)

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
    text_completion,
)

_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
_LATER = datetime(2026, 7, 6, 13, 0, tzinfo=UTC)

_DEFINITIONS = {
    "researcher": AgentDefinition(
        name="researcher", instructions="Research.", toolset=("read.docs.search",)
    ),
}


def _scheduler(
    sink: Any,
    engine: sa.Engine,
    *,
    gateway: Any = None,
    log: ToolLog | None = None,
) -> CronScheduler:
    pdp = make_pdp(sink)
    executor = GovernedToolExecutor(
        standard_bindings(log or ToolLog()),
        pdp=pdp,
        guardrails=make_guardrails(sink),
        approval_gate=NamedApproverGate("alice"),
        sink=sink,
    )
    ensure_scheduler_schema(engine)
    return CronScheduler(
        engine=engine,
        definitions=_DEFINITIONS,
        executor=executor,
        pdp=pdp,
        gateway=gateway if gateway is not None else ScriptedGateway([]),
        registry=ProcessRegistry(),
        sink=sink,
        settings=RuntimeSettings(),
        principals=StaticAgentPrincipalProvider({"researcher": make_principal("agent:researcher")}),
        clock=lambda: _T0,
    )


def _job(
    name: str = "cron-research",
    cron: str = "* * * * *",
    toolset: tuple[str, ...] = ("read.docs.search",),
) -> ScheduledJob:
    return ScheduledJob(name=name, cron=cron, agent="researcher", toolset=toolset)


# -- read helpers -----------------------------------------------------------


def _chain_actions(engine: sa.Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(ne_audit_log.c.action)).all()
    return [r.action for r in rows]


def _chain_event_ids(engine: sa.Engine, action: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(ne_audit_log.c.event_id).where(ne_audit_log.c.action == action)
        ).all()
    return [r.event_id for r in rows]


def _chain_count(engine: sa.Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(ne_audit_log)).scalar() or 0)


def _outbox_event_ids(engine: sa.Engine, action: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(ne_outbox_events.c.event_id, ne_outbox_events.c.payload)
        ).all()
    return [r.event_id for r in rows if dict(r.payload).get("action") == action]


def _outbox_actions(engine: sa.Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(ne_outbox_events.c.payload)).all()
    return [dict(r.payload).get("action", "") for r in rows]


def _job_row_count(engine: sa.Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(sa.select(sa.func.count()).select_from(ne_scheduled_jobs)).scalar() or 0
        )


def _run_rows(engine: sa.Engine) -> list[sa.Row[Any]]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(ne_job_runs)).all())


def _chain_payload(engine: sa.Engine, action: str) -> dict[str, Any]:
    with engine.connect() as conn:
        payload = conn.execute(
            sa.select(ne_audit_log.c.payload).where(ne_audit_log.c.action == action)
        ).scalar_one()
    return dict(payload)


def _drop_triggers(conn: sa.Connection) -> None:
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_delete")


# -- dual-append happy path -------------------------------------------------


def test_schedule_dual_appends_same_event_to_chain_and_outbox() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        scheduler.schedule(_job())

        chain_ids = _chain_event_ids(engine, "job.schedule")
        outbox_ids = _outbox_event_ids(engine, "job.schedule")
        assert len(chain_ids) == 1
        # SAME event object → SAME event_id in both stores (alignment, not duplication).
        assert chain_ids == outbox_ids
        assert verify_chain(engine).valid is True
        # The chain payload carries the composed FROZEN platform hash (NE-CKC12).
        assert _chain_payload(engine, "job.schedule")["config_hash"] == get_config_hash()
    finally:
        engine.dispose()


def test_cancel_dual_appends_job_cancel_to_chain() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        job_id = scheduler.schedule(_job())
        scheduler.cancel(job_id)

        assert "job.cancel" in _chain_actions(engine)
        assert _chain_event_ids(engine, "job.cancel") == _outbox_event_ids(engine, "job.cancel")
        assert verify_chain(engine).valid is True
        assert _job_row_count(engine) == 0
    finally:
        engine.dispose()


def test_job_run_dual_appends_job_run_to_chain() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(
            RecordingAuditSink(), engine, gateway=ScriptedGateway([text_completion("ok")])
        )
        scheduler.schedule(_job())
        asyncio.run(scheduler.run_due_jobs(now=_LATER))

        assert "job.run" in _chain_actions(engine)
        assert _chain_event_ids(engine, "job.run") == _outbox_event_ids(engine, "job.run")
        # verify_chain covers schedule + run records.
        assert verify_chain(engine).valid is True
    finally:
        engine.dispose()


# -- tamper detection -------------------------------------------------------


def test_mutated_cron_chain_record_is_detected() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        scheduler.schedule(_job())
        assert verify_chain(engine).valid is True

        # Drop the DB-level append-only triggers, then forge the actor column of the
        # job.schedule chain row WITHOUT touching the hash-covered payload.
        with engine.begin() as conn:
            _drop_triggers(conn)
            conn.execute(
                ne_audit_log.update()
                .where(ne_audit_log.c.action == "job.schedule")
                .values(actor="mallory")
            )

        cv = verify_chain(engine)
        assert cv.valid is False
        assert "actor" in cv.detail
    finally:
        engine.dispose()


def test_deleted_cron_chain_record_is_detected() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        job_id = scheduler.schedule(_job())
        scheduler.cancel(job_id)  # a second record so the deletion leaves a gap
        assert verify_chain(engine).valid is True

        with engine.begin() as conn:
            _drop_triggers(conn)
            conn.execute(ne_audit_log.delete().where(ne_audit_log.c.action == "job.schedule"))

        assert verify_chain(engine).valid is False
    finally:
        engine.dispose()


def test_append_only_triggers_block_mutation() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        scheduler.schedule(_job())

        # WITHOUT dropping triggers, a direct UPDATE is rejected at the DB level —
        # the cron records inherit the same tamper-proofing as every other chain row.
        with pytest.raises(DatabaseError), engine.begin() as conn:
            conn.execute(
                ne_audit_log.update()
                .where(ne_audit_log.c.action == "job.schedule")
                .values(actor="mallory")
            )
        with pytest.raises(DatabaseError), engine.begin() as conn:
            conn.execute(ne_audit_log.delete().where(ne_audit_log.c.action == "job.schedule"))
    finally:
        engine.dispose()


# -- fail-closed rollback ---------------------------------------------------


def test_schedule_chain_append_failure_rolls_back_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)

        def failing_append_record(conn: Any, event: Any) -> Any:
            raise RuntimeError("chain down")

        monkeypatch.setattr("ne.runtime.scheduler.append_record", failing_append_record)

        with pytest.raises(RuntimeAuditError):
            scheduler.schedule(_job())

        # Nothing half-written: no job row, no outbox event, no chain record.
        assert _job_row_count(engine) == 0
        assert _outbox_actions(engine) == []
        assert _chain_count(engine) == 0
    finally:
        engine.dispose()


def test_cancel_chain_append_failure_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        job_id = scheduler.schedule(_job())  # succeeds (append_record not yet patched)

        def failing_for_cancel(conn: Any, event: Any) -> Any:
            if getattr(event, "action", "") == "job.cancel":
                raise RuntimeError("chain down for job.cancel")
            return real_append_record(conn, event)

        monkeypatch.setattr("ne.runtime.scheduler.append_record", failing_for_cancel)

        with pytest.raises(RuntimeAuditError):
            scheduler.cancel(job_id)

        # The delete rolled back — the job STILL EXISTS and no job.cancel landed anywhere.
        assert _job_row_count(engine) == 1
        assert "job.cancel" not in _outbox_actions(engine)
        assert "job.cancel" not in _chain_actions(engine)
    finally:
        engine.dispose()


def test_finish_run_chain_append_failure_keeps_run_unfinished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(
            RecordingAuditSink(), engine, gateway=ScriptedGateway([text_completion("ok")])
        )
        scheduler.schedule(_job())  # succeeds (append_record not yet patched)

        def failing_for_run(conn: Any, event: Any) -> Any:
            if getattr(event, "action", "") == "job.run":
                raise RuntimeError("chain down for job.run")
            return real_append_record(conn, event)

        monkeypatch.setattr("ne.runtime.scheduler.append_record", failing_for_run)

        with pytest.raises(RuntimeAuditError):
            asyncio.run(scheduler.run_due_jobs(now=_LATER))

        # The finish transaction rolled back — the run row stays honestly UNFINISHED
        # and the job.run event is absent from BOTH stores.
        rows = _run_rows(engine)
        assert len(rows) == 1
        assert rows[0].started_at is not None
        assert rows[0].finished_at is None
        assert "job.run" not in _chain_actions(engine)
        assert "job.run" not in _outbox_actions(engine)
    finally:
        engine.dispose()


def test_expired_scheduled_audit_failure_stays_unfinished_and_does_not_block_next_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(
            RecordingAuditSink(),
            engine,
            gateway=ScriptedGateway([text_completion("first"), text_completion("second")]),
        )
        scheduler.schedule(_job())

        def failing_for_run(conn: Any, event: Any) -> Any:
            if getattr(event, "action", "") == "job.run":
                raise RuntimeError("chain remains unavailable for job.run")
            return real_append_record(conn, event)

        monkeypatch.setattr("ne.runtime.scheduler.append_record", failing_for_run)

        with pytest.raises(RuntimeAuditError):
            asyncio.run(scheduler.run_due_jobs(now=_LATER))
        [first] = _run_rows(engine)
        assert first.finished_at is None

        # The first lease is expired. Its delayed reconciliation must not rewrite
        # scheduled history without audit, and the stale row must not suppress the
        # next due window even while the audit path remains broken.
        with pytest.raises(RuntimeAuditError):
            asyncio.run(scheduler.run_due_jobs(now=_LATER + timedelta(minutes=3)))

        rows = _run_rows(engine)
        assert len(rows) == 2
        assert all(row.finished_at is None for row in rows)
        assert str(first.run_id) in {str(row.run_id) for row in rows}
        assert "job.run" not in _chain_actions(engine)
        assert "job.run" not in _outbox_actions(engine)
    finally:
        engine.dispose()
