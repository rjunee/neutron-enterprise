# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CronScheduler — sync schedule/cancel/list with atomic outbox auditing (NE-034)."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.runtime import ScheduledJob, Scheduler
from ne.events.outbox import ne_outbox_events
from ne.runtime.config import RuntimeSettings
from ne.runtime.dispatch import AgentDefinition
from ne.runtime.errors import SchedulingError
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.principals import StaticAgentPrincipalProvider
from ne.runtime.processes import ProcessRegistry
from ne.runtime.scheduler import CronScheduler, ensure_scheduler_schema, ne_scheduled_jobs

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
)

_DEFINITIONS = {
    "researcher": AgentDefinition(
        name="researcher", instructions="Research.", toolset=("read.docs.search",)
    ),
}


def _scheduler(sink: Any, engine: sa.Engine, *, log: ToolLog | None = None) -> CronScheduler:
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
        gateway=ScriptedGateway([]),
        registry=ProcessRegistry(),
        sink=sink,
        settings=RuntimeSettings(),
        principals=StaticAgentPrincipalProvider({"researcher": make_principal()}),
    )


def _job(
    name: str = "daily-research", toolset: tuple[str, ...] = ("read.docs.search",)
) -> ScheduledJob:
    return ScheduledJob(name=name, cron="*/5 * * * *", agent="researcher", toolset=toolset)


def _outbox_actions(engine: sa.Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(ne_outbox_events.c.payload)).all()
    return [dict(r.payload).get("action", "") for r in rows]


def _job_row_count(engine: sa.Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(sa.select(sa.func.count()).select_from(ne_scheduled_jobs)).scalar() or 0
        )


def test_schedule_persists_job_and_appends_job_schedule_event_atomically() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        job_id = scheduler.schedule(_job())
        assert job_id
        assert _job_row_count(engine) == 1
        assert "job.schedule" in _outbox_actions(engine)
    finally:
        engine.dispose()


def test_invalid_cron_rejected_nothing_persisted_no_event() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        bad = ScheduledJob(name="x", cron="not a cron", agent="researcher", toolset=())
        with pytest.raises(SchedulingError):
            scheduler.schedule(bad)
        assert _job_row_count(engine) == 0
        assert _outbox_actions(engine) == []
    finally:
        engine.dispose()


def test_toolset_with_unregistered_tool_name_rejected() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        with pytest.raises(SchedulingError):
            scheduler.schedule(_job(toolset=("no.such.tool",)))
        assert _job_row_count(engine) == 0
    finally:
        engine.dispose()


def test_unknown_agent_type_rejected() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        job = ScheduledJob(name="x", cron="*/5 * * * *", agent="ghost", toolset=())
        with pytest.raises(SchedulingError):
            scheduler.schedule(job)
        assert _job_row_count(engine) == 0
    finally:
        engine.dispose()


def test_duplicate_job_name_rejected() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        scheduler.schedule(_job(name="dup"))
        with pytest.raises(SchedulingError):
            scheduler.schedule(_job(name="dup"))
        assert _job_row_count(engine) == 1
    finally:
        engine.dispose()


def test_cancel_deletes_row_and_appends_job_cancel_event() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        job_id = scheduler.schedule(_job())
        scheduler.cancel(job_id)
        assert _job_row_count(engine) == 0
        assert "job.cancel" in _outbox_actions(engine)
    finally:
        engine.dispose()


def test_cancel_unknown_job_id_raises_schedulingerror() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        with pytest.raises(SchedulingError):
            scheduler.cancel("no-such-id")
    finally:
        engine.dispose()


def test_list_jobs_round_trips_with_job_id_and_toolset() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        job_id = scheduler.schedule(_job(name="j1", toolset=("read.docs.search",)))
        jobs = scheduler.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].job_id == job_id
        assert jobs[0].name == "j1"
        assert jobs[0].toolset == ("read.docs.search",)
        assert jobs[0].agent == "researcher"
    finally:
        engine.dispose()


def test_scheduler_satisfies_frozen_protocol() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine)
        _s: Scheduler = scheduler  # pyright-enforced conformance
        assert _s is scheduler
    finally:
        engine.dispose()
