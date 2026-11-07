# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CronScheduler.run_due_jobs — cron-as-isolated-agent execution, gated + audited (NE-034)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateTable

import ne.runtime.scheduler as scheduler_module
from ne.audit import ensure_audit_schema
from ne.contracts import get_config_hash
from ne.contracts.models import CompletionRequest
from ne.contracts.runtime import ScheduledJob, ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.events import ensure_outbox_schema
from ne.events.outbox import ne_outbox_events
from ne.govern import create_guardrail_engine
from ne.govern.config import ToolArgPolicy
from ne.govern.errors import AuditEmissionError
from ne.runtime.config import RuntimeSettings
from ne.runtime.dispatch import AgentDefinition
from ne.runtime.errors import RuntimeAuditError, SchedulingError
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.principals import StaticAgentPrincipalProvider
from ne.runtime.processes import ProcessRegistry
from ne.runtime.scheduler import (
    _LEASE_RENEW_INTERVAL_S,
    _POSTGRES_SCHEMA_LOCK_SQL,
    _RUN_LEASE,
    CronScheduler,
    ManualJobRequestStore,
    _lease_column_ddl,
    ensure_scheduler_schema,
    ne_job_requests,
    ne_job_runs,
    ne_scheduled_jobs,
)
from ne.runtime.tools import ToolBinding

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    govern_settings,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
    text_completion,
    tool_call_completion,
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
    gateway: Any,
    log: ToolLog | None = None,
    registry: ProcessRegistry | None = None,
    principals: Any = None,
    bindings: list[ToolBinding] | None = None,
    guardrails: Any = None,
    pdp: Any = None,
    definitions: dict[str, AgentDefinition] | None = None,
) -> CronScheduler:
    pdp = pdp or make_pdp(sink)
    executor = GovernedToolExecutor(
        bindings or standard_bindings(log or ToolLog()),
        pdp=pdp,
        guardrails=guardrails or make_guardrails(sink),
        approval_gate=NamedApproverGate("alice"),
        sink=sink,
    )
    ensure_scheduler_schema(engine)
    return CronScheduler(
        engine=engine,
        definitions=_DEFINITIONS if definitions is None else definitions,
        executor=executor,
        pdp=pdp,
        gateway=gateway,
        registry=registry or ProcessRegistry(),
        sink=sink,
        settings=RuntimeSettings(),
        principals=principals
        or StaticAgentPrincipalProvider({"researcher": make_principal("agent:researcher")}),
        clock=lambda: _T0,
    )


def _job(
    name: str = "cron-research",
    cron: str = "* * * * *",
    toolset: tuple[str, ...] = ("read.docs.search",),
) -> ScheduledJob:
    return ScheduledJob(name=name, cron=cron, agent="researcher", toolset=toolset)


def _run_rows(engine: sa.Engine) -> list[sa.Row[Any]]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(ne_job_runs)).all())


def _outbox_actions(engine: sa.Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(ne_outbox_events.c.payload)).all()
    return [dict(r.payload).get("action", "") for r in rows]


def _outbox_payloads(engine: sa.Engine, action: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(ne_outbox_events.c.payload)).all()
    payloads = [dict(row.payload) for row in rows]
    return [payload for payload in payloads if payload.get("action") == action]


def _create_pre_lease_scheduler_schema(engine: sa.Engine, *, include_requests: bool = True) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """CREATE TABLE ne_scheduled_jobs (
            job_id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL UNIQUE,
            cron VARCHAR NOT NULL,
            agent VARCHAR NOT NULL,
            toolset TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            next_run_at TIMESTAMP NOT NULL
            )"""
        )
        conn.exec_driver_sql(
            """CREATE TABLE ne_job_runs (
            run_id VARCHAR PRIMARY KEY,
            job_id VARCHAR NOT NULL,
            started_at TIMESTAMP NOT NULL,
            finished_at TIMESTAMP,
            succeeded BOOLEAN,
            error_class VARCHAR,
            output TEXT
            )"""
        )
        if include_requests:
            conn.exec_driver_sql(
                """CREATE TABLE ne_job_requests (
            request_id VARCHAR(36) PRIMARY KEY,
            job_id VARCHAR NOT NULL,
            job_name VARCHAR(256) NOT NULL,
            source VARCHAR(128) NOT NULL,
            requested_action VARCHAR(256) NOT NULL,
            tool_name VARCHAR(256) NOT NULL,
            requester_id VARCHAR(512) NOT NULL,
            requester_groups TEXT NOT NULL,
            config_hash VARCHAR(128) NOT NULL,
            policy_evidence VARCHAR(64) NOT NULL,
            created_at TIMESTAMP NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            status VARCHAR(16) NOT NULL,
            run_id VARCHAR,
            finished_at TIMESTAMP,
            succeeded BOOLEAN,
            error_class VARCHAR(128)
            )"""
            )


def _lease_column_names(engine: sa.Engine, table_name: str) -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(engine).get_columns(table_name)
        if column["name"] in {"claim_owner", "lease_expires_at"}
    }


def test_scheduler_schema_initialization_serializes_create_and_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    database = tmp_path / "scheduler.db"
    url = f"sqlite:///{database}"
    setup = sa.create_engine(url, connect_args={"timeout": 10})
    _create_pre_lease_scheduler_schema(setup, include_requests=False)
    ensure_outbox_schema(setup)
    ensure_audit_schema(setup)
    setup.dispose()

    engines = [
        sa.create_engine(url, connect_args={"timeout": 10}),
        sa.create_engine(url, connect_args={"timeout": 10}),
    ]
    barrier = threading.Barrier(2)
    statements: list[str] = []
    statement_lock = threading.Lock()
    active_initializers = 0
    max_active_initializers = 0
    real_initialize = scheduler_module._initialize_scheduler_schema

    def _tracked_initialize(conn: sa.Connection) -> None:
        nonlocal active_initializers, max_active_initializers
        with statement_lock:
            active_initializers += 1
            max_active_initializers = max(max_active_initializers, active_initializers)
        try:
            # Widen the race so removing the production serialization admits both
            # independent engine initializers before either completes its checks.
            time.sleep(0.05)
            real_initialize(conn)
        finally:
            with statement_lock:
                active_initializers -= 1

    def _record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        with statement_lock:
            statements.append(statement)

    for engine in engines:
        event.listen(engine, "before_cursor_execute", _record)
    monkeypatch.setattr(scheduler_module, "_initialize_scheduler_schema", _tracked_initialize)

    def _initialize(engine: sa.Engine) -> None:
        barrier.wait(timeout=5)
        ensure_scheduler_schema(engine)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_initialize, engine) for engine in engines]
            for future in futures:
                future.result(timeout=15)

        verify = sa.create_engine(url)
        try:
            expected = {"claim_owner", "lease_expires_at"}
            assert _lease_column_names(verify, "ne_job_runs") == expected
            assert _lease_column_names(verify, "ne_job_requests") == expected
        finally:
            verify.dispose()
        assert max_active_initializers == 1
        assert not any(
            statement.startswith("ALTER TABLE") and "IF NOT EXISTS" in statement
            for statement in statements
        )
    finally:
        for engine in engines:
            engine.dispose()


def test_scheduler_schema_upgrade_handles_partial_and_existing_columns(tmp_path: Any) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path}/partial.db")
    try:
        _create_pre_lease_scheduler_schema(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE ne_job_runs ADD COLUMN claim_owner VARCHAR(36)")
            conn.exec_driver_sql(
                "ALTER TABLE ne_job_requests ADD COLUMN lease_expires_at TIMESTAMP"
            )

        ensure_scheduler_schema(engine)
        expected = {"claim_owner", "lease_expires_at"}
        assert _lease_column_names(engine, "ne_job_runs") == expected
        assert _lease_column_names(engine, "ne_job_requests") == expected
        ensure_scheduler_schema(engine)
        assert _lease_column_names(engine, "ne_job_runs") == expected
        assert _lease_column_names(engine, "ne_job_requests") == expected
    finally:
        engine.dispose()


def test_scheduler_schema_upgrade_propagates_unrelated_sql_error(tmp_path: Any) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path}/broken.db")
    _create_pre_lease_scheduler_schema(engine)
    ensure_outbox_schema(engine)
    ensure_audit_schema(engine)

    def _fail_unrelated(
        _conn: Any,
        _cursor: Any,
        statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if statement == "ALTER TABLE ne_job_runs ADD COLUMN claim_owner VARCHAR(36)":
            raise OperationalError(
                statement, parameters, sqlite3.OperationalError("disk I/O error")
            )

    event.listen(engine, "before_cursor_execute", _fail_unrelated)
    try:
        with pytest.raises(OperationalError, match="disk I/O error"):
            ensure_scheduler_schema(engine)
    finally:
        event.remove(engine, "before_cursor_execute", _fail_unrelated)
        engine.dispose()


def test_postgres_scheduler_schema_lock_and_timestamptz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []

    class _Dialect:
        name = "postgresql"

    class _Connection:
        dialect = _Dialect()

        def exec_driver_sql(self, statement: str) -> None:
            events.append(("sql", statement))

    connection = _Connection()

    class _Begin:
        def __enter__(self) -> _Connection:
            return connection

        def __exit__(self, *_args: Any) -> None:
            return None

    class _Engine:
        dialect = _Dialect()

        def begin(self) -> _Begin:
            return _Begin()

    monkeypatch.setattr(
        "ne.runtime.scheduler._initialize_scheduler_schema",
        lambda conn: events.append(("initialize", conn)),
    )
    monkeypatch.setattr("ne.runtime.scheduler.ensure_outbox_schema", lambda _engine: None)
    monkeypatch.setattr("ne.runtime.scheduler.ensure_audit_schema", lambda _engine: None)

    ensure_scheduler_schema(_Engine())  # type: ignore[arg-type]

    assert events == [
        ("sql", _POSTGRES_SCHEMA_LOCK_SQL),
        ("initialize", connection),
    ]
    additive = _lease_column_ddl("postgresql", "ne_job_runs", "lease_expires_at")
    assert additive.endswith("lease_expires_at TIMESTAMP WITH TIME ZONE")
    create_sql = str(CreateTable(ne_job_runs).compile(dialect=postgresql.dialect()))
    assert "lease_expires_at TIMESTAMP WITH TIME ZONE" in create_sql


def test_due_job_runs_as_isolated_agent_with_gated_toolset() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        log = ToolLog()
        sink = RecordingAuditSink()
        gateway = ScriptedGateway(
            [tool_call_completion("read.docs.search", {"q": "x"}), text_completion("done")]
        )
        scheduler = _scheduler(sink, engine, gateway=gateway, log=log)
        scheduler.schedule(_job())

        outcomes = asyncio.run(scheduler.run_due_jobs(now=_LATER))

        assert len(outcomes) == 1
        assert outcomes[0].succeeded is True
        assert outcomes[0].name == "cron-research"
        # The gated in-set tool call ran the full governed pipeline.
        assert log.names() == ["read.docs.search"]
        assert sink.with_action("pdp.decide")
        assert sink.with_action("tool.execute")
        assert len(gateway.requests) == 2
    finally:
        engine.dispose()


def test_due_sole_tool_job_invokes_once_without_model_arbitration() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        log = ToolLog()
        sink = RecordingAuditSink()
        gateway = ScriptedGateway([])
        definitions = {
            "researcher": AgentDefinition(
                name="researcher",
                instructions="Run the maintenance tool.",
                toolset=("read.docs.search",),
                scheduled_execution="sole-tool",
            )
        }
        scheduler = _scheduler(
            sink,
            engine,
            gateway=gateway,
            log=log,
            definitions=definitions,
        )
        scheduler.schedule(_job())

        outcomes = asyncio.run(scheduler.run_due_jobs(now=_LATER))

        assert len(outcomes) == 1
        assert outcomes[0].succeeded is True
        assert log.names() == ["read.docs.search"]
        assert gateway.requests == []
        assert len(sink.with_action("pdp.decide")) == 1
        assert len(sink.with_action("tool.execute")) == 1
        run_events = _outbox_payloads(engine, "job.run")
        assert len(run_events) == 1
        assert run_events[0]["actor"] == "agent:researcher"
    finally:
        engine.dispose()


def test_due_sole_tool_job_refuses_persisted_toolset_definition_drift() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        log = ToolLog()
        sink = RecordingAuditSink()
        scheduled_definitions = {
            "researcher": AgentDefinition(
                name="researcher",
                instructions="Run the original maintenance tool.",
                toolset=("read.docs.search",),
                scheduled_execution="sole-tool",
            )
        }
        _scheduler(
            sink,
            engine,
            gateway=ScriptedGateway([]),
            log=log,
            definitions=scheduled_definitions,
        ).schedule(_job())

        current_definitions = {
            "researcher": AgentDefinition(
                name="researcher",
                instructions="Run the replacement maintenance tool.",
                toolset=("write.internal.notes.append",),
                scheduled_execution="sole-tool",
            )
        }
        gateway = ScriptedGateway([])
        restarted = _scheduler(
            sink,
            engine,
            gateway=gateway,
            log=log,
            definitions=current_definitions,
        )

        outcomes = asyncio.run(restarted.run_due_jobs(now=_LATER))

        assert len(outcomes) == 1
        assert outcomes[0].succeeded is False
        assert outcomes[0].output == "ERROR: SchedulingError"
        assert log.calls == []
        assert gateway.requests == []
        assert sink.with_action("pdp.decide") == []
        assert sink.with_action("tool.execute") == []
        rows = _run_rows(engine)
        assert rows[0].error_class == "SchedulingError"
        run_events = _outbox_payloads(engine, "job.run")
        assert len(run_events) == 1
        assert run_events[0]["outcome"] == "error"
    finally:
        engine.dispose()


def test_due_sole_tool_job_rechecks_pdp_and_refuses_denial() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        log = ToolLog()
        sink = RecordingAuditSink()
        denied = make_pdp(
            sink,
            overrides={
                "read.docs.search": {
                    "effect": "deny",
                    "policy_version": "revoked",
                    "reason": "revoked",
                    "obligations": {},
                    "tier": 0,
                }
            },
        )
        definitions = {
            "researcher": AgentDefinition(
                name="researcher",
                instructions="Run the maintenance tool.",
                toolset=("read.docs.search",),
                scheduled_execution="sole-tool",
            )
        }
        gateway = ScriptedGateway([])
        scheduler = _scheduler(
            sink,
            engine,
            gateway=gateway,
            log=log,
            pdp=denied,
            definitions=definitions,
        )
        scheduler.schedule(_job())

        outcomes = asyncio.run(scheduler.run_due_jobs(now=_LATER))

        assert len(outcomes) == 1
        assert outcomes[0].succeeded is False
        assert outcomes[0].output == "ERROR: ToolDeniedError"
        assert log.calls == []
        assert gateway.requests == []
        rows = _run_rows(engine)
        assert rows[0].error_class == "ToolDeniedError"
    finally:
        engine.dispose()


def test_due_sole_tool_job_records_one_handler_failure_without_model_call() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        log = ToolLog()
        bindings = standard_bindings(log)
        search = bindings[0]
        bindings[0] = ToolBinding(
            spec=search.spec,
            handler=log.raising_handler("read.docs.search", ConnectionError("private detail")),
        )
        definitions = {
            "researcher": AgentDefinition(
                name="researcher",
                instructions="Run the maintenance tool.",
                toolset=("read.docs.search",),
                scheduled_execution="sole-tool",
            )
        }
        gateway = ScriptedGateway([])
        scheduler = _scheduler(
            RecordingAuditSink(),
            engine,
            gateway=gateway,
            log=log,
            bindings=bindings,
            definitions=definitions,
        )
        scheduler.schedule(_job())

        outcomes = asyncio.run(scheduler.run_due_jobs(now=_LATER))

        assert len(outcomes) == 1
        assert outcomes[0].succeeded is False
        assert outcomes[0].output == "ERROR: ToolExecutionError"
        assert log.names() == ["read.docs.search"]
        assert gateway.requests == []
    finally:
        engine.dispose()


def test_due_sole_tool_audit_failure_keeps_scheduled_run_unfinished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine("sqlite://")
    try:
        sink = RecordingAuditSink()
        definitions = {
            "researcher": AgentDefinition(
                name="researcher",
                instructions="Run the maintenance tool.",
                toolset=("read.docs.search",),
                scheduled_execution="sole-tool",
            )
        }
        scheduler = _scheduler(
            sink,
            engine,
            gateway=ScriptedGateway([]),
            definitions=definitions,
        )
        scheduler.schedule(_job())

        async def _fail_emit(_event: Any) -> None:
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(sink, "append", _fail_emit)
        with pytest.raises(AuditEmissionError):
            asyncio.run(scheduler.run_due_jobs(now=_LATER))

        rows = _run_rows(engine)
        assert len(rows) == 1
        assert rows[0].finished_at is None
        assert rows[0].succeeded is None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "toolset",
    [
        (),
        ("write.internal.notes.append",),
        ("read.docs.search", "write.internal.notes.append"),
    ],
)
def test_sole_tool_job_rejects_nonmatching_grants_before_persistence(
    toolset: tuple[str, ...],
) -> None:
    engine = sa.create_engine("sqlite://")
    try:
        definitions = {
            "researcher": AgentDefinition(
                name="researcher",
                instructions="Run the maintenance tool.",
                toolset=("read.docs.search",),
                scheduled_execution="sole-tool",
            )
        }
        scheduler = _scheduler(
            RecordingAuditSink(),
            engine,
            gateway=ScriptedGateway([]),
            definitions=definitions,
        )

        with pytest.raises(SchedulingError, match="must grant exactly"):
            scheduler.schedule(_job(toolset=toolset))

        assert scheduler.list_jobs() == ()
        assert _outbox_actions(engine) == []
    finally:
        engine.dispose()


def test_not_due_job_skipped() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine, gateway=ScriptedGateway([]))
        scheduler.schedule(_job())
        # now is BEFORE next_run_at (which is _T0 + 1 minute) → nothing due.
        outcomes = asyncio.run(scheduler.run_due_jobs(now=_T0))
        assert outcomes == ()
        assert _run_rows(engine) == []
    finally:
        engine.dispose()


def test_cron_agent_out_of_set_tool_call_denied_and_audited_job_still_completes() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        log = ToolLog()
        sink = RecordingAuditSink()
        # Model emits an OUT-OF-SET tool (registered in the executor, not in the job grant).
        gateway = ScriptedGateway(
            [
                tool_call_completion("write.internal.notes.append", {"note": "x"}),
                text_completion("recovered"),
            ]
        )
        scheduler = _scheduler(sink, engine, gateway=gateway, log=log)
        scheduler.schedule(_job(toolset=("read.docs.search",)))

        outcomes = asyncio.run(scheduler.run_due_jobs(now=_LATER))

        assert len(outcomes) == 1 and outcomes[0].succeeded is True
        assert outcomes[0].output == "recovered"
        # Gating enforced: the out-of-set call was audited and the handler never ran.
        assert sink.with_action("tool.toolset_denied")
        assert "write.internal.notes.append" not in log.names()
    finally:
        engine.dispose()


def test_empty_toolset_job_runs_with_zero_tools() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        log = ToolLog()
        scheduler = _scheduler(
            RecordingAuditSink(),
            engine,
            gateway=ScriptedGateway([text_completion("answered")]),
            log=log,
        )
        scheduler.schedule(_job(toolset=()))
        outcomes = asyncio.run(scheduler.run_due_jobs(now=_LATER))
        assert len(outcomes) == 1 and outcomes[0].succeeded is True
        assert log.calls == []
    finally:
        engine.dispose()


def test_missed_windows_collapse_to_single_run_and_next_run_at_advances_past_now() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(
            RecordingAuditSink(), engine, gateway=ScriptedGateway([text_completion("ok")])
        )
        scheduler.schedule(_job(cron="* * * * *"))  # every minute; many windows missed by _LATER
        outcomes = asyncio.run(scheduler.run_due_jobs(now=_LATER))
        # 60 missed minute-windows collapse to EXACTLY ONE run (no catch-up storm).
        assert len(outcomes) == 1
        with engine.connect() as conn:
            nra = conn.execute(sa.select(ne_scheduled_jobs.c.next_run_at)).scalar()
        # next_run_at advanced strictly past now (compared as naive on SQLite).
        assert nra is not None and nra > _LATER.replace(tzinfo=None)
    finally:
        engine.dispose()


def test_run_rows_written_started_and_finished_with_job_run_event() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(
            RecordingAuditSink(), engine, gateway=ScriptedGateway([text_completion("ok")])
        )
        scheduler.schedule(_job())
        asyncio.run(scheduler.run_due_jobs(now=_LATER))
        rows = _run_rows(engine)
        assert len(rows) == 1
        assert rows[0].started_at is not None
        assert rows[0].finished_at is not None
        assert rows[0].succeeded is True
        assert "job.run" in _outbox_actions(engine)
    finally:
        engine.dispose()


def test_job_failure_records_error_class_only_and_error_outcome() -> None:
    class _RaisingGateway:
        def __init__(self) -> None:
            self.requests: list[CompletionRequest] = []

        async def complete(self, request: CompletionRequest) -> Any:
            self.requests.append(request)
            raise ValueError("leaky secret detail")

        def stream(self, request: CompletionRequest) -> Any:
            raise NotImplementedError

    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(RecordingAuditSink(), engine, gateway=_RaisingGateway())
        scheduler.schedule(_job())
        outcomes = asyncio.run(scheduler.run_due_jobs(now=_LATER))
        assert outcomes[0].succeeded is False
        assert outcomes[0].output == "ERROR: ValueError"
        rows = _run_rows(engine)
        assert rows[0].error_class == "ValueError"
        assert rows[0].output == "ERROR: ValueError"
        assert "leaky" not in (rows[0].output or "")
    finally:
        engine.dispose()


def test_tool_handler_error_keeps_model_recovery_but_fails_scheduler_terminal() -> None:
    """A safe BLOCKED tool return cannot make the durable job outcome green."""
    engine = sa.create_engine("sqlite://")
    try:
        log = ToolLog()
        bindings = standard_bindings(log)
        search = bindings[0]
        bindings[0] = ToolBinding(
            spec=search.spec,
            handler=log.raising_handler(
                "read.docs.search", ConnectionError("https://user:secret@source.invalid")
            ),
        )
        gateway = ScriptedGateway(
            [tool_call_completion("read.docs.search", {"q": "x"}), text_completion("recovered")]
        )
        scheduler = _scheduler(
            RecordingAuditSink(), engine, gateway=gateway, log=log, bindings=bindings
        )
        scheduler.schedule(_job())

        outcomes = asyncio.run(scheduler.run_due_jobs(now=_LATER))

        assert len(outcomes) == 1
        assert outcomes[0].succeeded is False
        assert outcomes[0].output == "ERROR: ToolExecutionError"
        rows = _run_rows(engine)
        assert rows[0].succeeded is False
        assert rows[0].error_class == "ToolExecutionError"
        assert rows[0].output == "ERROR: ToolExecutionError"
        assert "secret" not in (rows[0].output or "")
        run_events = _outbox_payloads(engine, "job.run")
        assert len(run_events) == 1
        assert run_events[0]["outcome"] == "error"
    finally:
        engine.dispose()


def test_missing_agent_principal_fails_run_audited() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        sink = RecordingAuditSink()
        scheduler = _scheduler(
            sink,
            engine,
            gateway=ScriptedGateway([text_completion("ok")]),
            principals=StaticAgentPrincipalProvider({}),  # no identity for 'researcher'
        )
        scheduler.schedule(_job())
        outcomes = asyncio.run(scheduler.run_due_jobs(now=_LATER))
        assert outcomes[0].succeeded is False
        rows = _run_rows(engine)
        assert rows[0].error_class == "LookupError"
        # The job.run event was recorded under the scheduler actor (no agent identity).
        run_events = [e for e in sink.events if getattr(e, "action", "") == "job.run"]
        # (audit lives in the outbox for the sync finish; assert via outbox instead)
        assert "job.run" in _outbox_actions(engine)
        _ = run_events
    finally:
        engine.dispose()


def test_cas_claim_lost_race_skips_job() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(
            RecordingAuditSink(), engine, gateway=ScriptedGateway([text_completion("ok")])
        )
        scheduler.schedule(_job())
        # Read the due row, then simulate another worker advancing next_run_at BEFORE
        # our CAS claim — the CAS matches zero rows and the job is skipped.
        with engine.connect() as conn:
            row = conn.execute(sa.select(ne_scheduled_jobs)).first()
        assert row is not None
        with engine.begin() as conn:
            conn.execute(
                ne_scheduled_jobs.update()
                .where(ne_scheduled_jobs.c.job_id == row.job_id)
                .values(next_run_at=_LATER)  # a competing advance
            )
        claimed = scheduler._claim(row, _LATER)  # pyright: ignore[reportPrivateUsage]
        assert claimed is None
        assert _run_rows(engine) == []
    finally:
        engine.dispose()


def test_active_claim_preserves_due_window_and_killed_owner_expires_before_next_window() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(
            RecordingAuditSink(), engine, gateway=ScriptedGateway([text_completion("ok")])
        )
        scheduler.schedule(_job(cron="* * * * *"))
        due_at = _T0 + timedelta(minutes=1)
        with engine.begin() as conn:
            job = conn.execute(sa.select(ne_scheduled_jobs)).one()
            conn.execute(
                ne_job_runs.insert().values(
                    run_id="killed-owner",
                    job_id=job.job_id,
                    started_at=_T0,
                    claim_owner="killed-owner",
                    lease_expires_at=due_at + _RUN_LEASE,
                )
            )

        assert timedelta(minutes=1) > _RUN_LEASE
        assert timedelta(seconds=_LEASE_RENEW_INTERVAL_S * 2) < _RUN_LEASE
        assert scheduler._claim(job, due_at) is None  # pyright: ignore[reportPrivateUsage]
        with engine.connect() as conn:
            preserved = conn.execute(
                sa.select(ne_scheduled_jobs.c.next_run_at).where(
                    ne_scheduled_jobs.c.job_id == job.job_id
                )
            ).scalar_one()
        assert preserved == due_at.replace(tzinfo=None)

        next_window = due_at + timedelta(minutes=1)
        claimed = scheduler._claim(job, next_window)  # pyright: ignore[reportPrivateUsage]
        assert claimed is not None
        rows = _run_rows(engine)
        assert [row.run_id for row in rows] == ["killed-owner", claimed]
    finally:
        engine.dispose()


def test_cron_claim_locks_job_before_overlap_check_and_due_window_advance() -> None:
    engine = sa.create_engine("sqlite://")
    statements: list[Any] = []

    def _record_statement(
        _conn: Any,
        clauseelement: Any,
        _multiparams: Any,
        _params: Any,
        _execution_options: Any,
    ) -> None:
        statements.append(clauseelement)

    try:
        scheduler = _scheduler(
            RecordingAuditSink(), engine, gateway=ScriptedGateway([text_completion("ok")])
        )
        scheduler.schedule(_job())
        with engine.connect() as conn:
            job = conn.execute(sa.select(ne_scheduled_jobs)).one()
        event.listen(engine, "before_execute", _record_statement)

        assert scheduler._claim(job, _LATER) is not None  # pyright: ignore[reportPrivateUsage]

        sql = [
            str(statement.compile(dialect=postgresql.dialect()))
            for statement in statements
            if isinstance(statement, (sa.sql.Select, sa.sql.Update, sa.sql.Insert))
        ]
        assert sql[0].startswith("SELECT ne_scheduled_jobs.job_id")
        assert "FOR UPDATE" in sql[0]
        assert sql[1].startswith("SELECT ne_job_runs.run_id")
        assert sql[2].startswith("UPDATE ne_scheduled_jobs")
        assert sql[3].startswith("INSERT INTO ne_job_runs")
    finally:
        event.remove(engine, "before_execute", _record_statement)
        engine.dispose()


def test_postgres_job_row_lock_serializes_manual_and_cron_claims() -> None:
    """Prove the formerly overlapping manual-vs-cron interleaving on real PostgreSQL."""
    url = os.environ.get("NE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("NE_TEST_POSTGRES_URL is required for the PostgreSQL claim-race proof")

    admin = sa.create_engine(url)
    schema = f"test_scheduler_claim_{uuid.uuid4().hex}"
    try:
        with admin.begin() as conn:
            conn.execute(sa.schema.CreateSchema(schema))
    except Exception as exc:
        admin.dispose()
        pytest.skip(f"NE_TEST_POSTGRES_URL is not reachable: {type(exc).__name__}")

    engine = sa.create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    cron_checked_active = threading.Event()
    release_cron = threading.Event()
    results: dict[str, Any] = {}
    listener_registered = False

    def _pause_cron_after_job_lock(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "cron-claim"
            and statement.lstrip().startswith("SELECT")
            and "FROM ne_job_runs" in statement
        ):
            cron_checked_active.set()
            assert release_cron.wait(timeout=5)

    try:
        scheduler, store, _log, _gateway = _manual_sync_fixture(engine, RecordingAuditSink())
        request = _request_manual_sync(store)
        with engine.connect() as conn:
            job = conn.execute(sa.select(ne_scheduled_jobs)).one()
            request_row = conn.execute(
                sa.select(ne_job_requests).where(ne_job_requests.c.request_id == request.request_id)
            ).one()
        event.listen(engine, "before_cursor_execute", _pause_cron_after_job_lock)
        listener_registered = True

        def _cron_claim() -> None:
            results["cron"] = scheduler._claim(  # pyright: ignore[reportPrivateUsage]
                job, _LATER
            )

        def _manual_claim() -> None:
            results["manual"] = scheduler._claim_request(  # pyright: ignore[reportPrivateUsage]
                request_row, _LATER
            )

        cron = threading.Thread(target=_cron_claim, name="cron-claim")
        manual = threading.Thread(target=_manual_claim, name="manual-claim")
        cron.start()
        assert cron_checked_active.wait(timeout=5)
        manual.start()
        try:
            manual.join(timeout=0.25)
            assert manual.is_alive(), "manual claim bypassed the cron-held scheduled-job row lock"
        finally:
            release_cron.set()
            cron.join(timeout=5)
            manual.join(timeout=5)

        assert not cron.is_alive() and not manual.is_alive()
        assert results["cron"] is not None
        assert results["manual"] is None
        with engine.connect() as conn:
            active = conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_job_runs)
                .where(ne_job_runs.c.finished_at.is_(None))
            ).scalar_one()
        assert active == 1
    finally:
        if listener_registered:
            event.remove(engine, "before_cursor_execute", _pause_cron_after_job_lock)
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(sa.schema.DropSchema(schema, cascade=True, if_exists=True))
        admin.dispose()


def test_cron_run_registered_in_process_registry_as_cron_job() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        registry = ProcessRegistry()
        scheduler = _scheduler(
            RecordingAuditSink(),
            engine,
            gateway=ScriptedGateway([text_completion("ok")]),
            registry=registry,
        )
        scheduler.schedule(_job())
        asyncio.run(scheduler.run_due_jobs(now=_LATER))
        assert registry.live() == ()
        finished = list(registry.finished())
        cron_records = [r for r in finished if r.kind == "cron-job"]
        assert len(cron_records) == 1
        assert cron_records[0].name == "cron-research"
        assert cron_records[0].succeeded is True
    finally:
        engine.dispose()


def test_scheduled_job_run_audit_failure_keeps_run_unfinished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ne.events import append as orig_append

    engine = sa.create_engine("sqlite://")
    try:
        scheduler = _scheduler(
            RecordingAuditSink(), engine, gateway=ScriptedGateway([text_completion("ok")])
        )
        scheduler.schedule(_job())

        def failing_append(conn: Any, event: Any) -> int:
            if getattr(event, "action", "") == "job.run":
                raise RuntimeError("outbox down for job.run")
            return orig_append(conn, event)

        monkeypatch.setattr("ne.runtime.scheduler.append", failing_append)

        with pytest.raises(RuntimeAuditError):
            asyncio.run(scheduler.run_due_jobs(now=_LATER))

        # Ordinary scheduled execution preserves the established audit guarantee:
        # a failed audited finish remains unfinished rather than being rewritten
        # through the manual-request recovery exception.
        rows = _run_rows(engine)
        assert len(rows) == 1
        assert rows[0].started_at is not None
        assert rows[0].finished_at is None
        assert rows[0].succeeded is None
        assert rows[0].error_class is None
    finally:
        engine.dispose()


def _manual_sync_fixture(
    engine: sa.Engine,
    sink: RecordingAuditSink,
    *,
    pdp: Any = None,
    seed: bool = True,
) -> tuple[CronScheduler, ManualJobRequestStore, ToolLog, ScriptedGateway]:
    log = ToolLog()
    tool = "write.internal.connect.sync_fixture"
    report = (
        '{"mode":"incremental","records_seen":2,"written":1,'
        '"skipped_unchanged":1,"invalid":0,"quarantined":0}'
    )
    bindings = [
        ToolBinding(
            spec=ToolSpec(name=tool, description="Sync fixture.", tier=ApprovalTier.TIER_1),
            handler=log.handler(tool, report),
        )
    ]
    settings = govern_settings()
    guardrail_settings = settings.guardrails.model_copy(
        update={"tools": {**settings.guardrails.tools, tool: ToolArgPolicy()}}
    )
    guardrails = create_guardrail_engine(
        settings.model_copy(update={"guardrails": guardrail_settings}), sink=sink
    )
    gateway = ScriptedGateway([])
    scheduler = _scheduler(
        sink,
        engine,
        gateway=gateway,
        log=log,
        bindings=bindings,
        guardrails=guardrails,
        pdp=pdp,
    )
    if seed:
        scheduler.schedule(_job(name="connect-sync-fixture", toolset=(tool,)))
    return scheduler, ManualJobRequestStore(engine, clock=lambda: _T0), log, gateway


def _request_manual_sync(
    store: ManualJobRequestStore,
    *,
    config_hash: str | None = None,
) -> Any:
    return store.request(
        "connect-sync-fixture",
        source="fixture",
        requested_action="write.internal.connect.request_sync_fixture",
        tool_name="write.internal.connect.sync_fixture",
        principal=Principal(id="user:ops", groups=("ops",)),
        config_hash=config_hash or get_config_hash(),
    )


def test_manual_request_invokes_sole_tool_without_model_and_preserves_requester() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        sink = RecordingAuditSink()
        scheduler, store, log, gateway = _manual_sync_fixture(engine, sink)
        request = _request_manual_sync(store)

        outcomes = asyncio.run(scheduler.run_requested_jobs(now=_T0))

        assert len(outcomes) == 1 and outcomes[0].succeeded is True
        assert log.names() == ["write.internal.connect.sync_fixture"]
        assert gateway.requests == []
        terminal = store.get(request.request_id)
        assert terminal is not None
        assert terminal.status == "succeeded"
        assert terminal.output == outcomes[0].output
        decisions = sink.with_action("pdp.decide")
        assert decisions and decisions[-1].actor == "user:ops"
    finally:
        engine.dispose()


def test_manual_request_deduplicates_per_source_before_claim() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler, store, log, _gateway = _manual_sync_fixture(engine, RecordingAuditSink())
        first = _request_manual_sync(store)
        second = _request_manual_sync(store)
        assert second.request_id == first.request_id
        assert first.created is True and second.created is False

        outcomes = asyncio.run(scheduler.run_requested_jobs(now=_T0))
        assert len(outcomes) == 1
        assert log.names() == ["write.internal.connect.sync_fixture"]
    finally:
        engine.dispose()


def test_manual_request_waits_for_active_source_run_without_overlap() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler, store, log, _gateway = _manual_sync_fixture(engine, RecordingAuditSink())
        request = _request_manual_sync(store)
        with engine.begin() as conn:
            job_id = conn.execute(
                sa.select(ne_scheduled_jobs.c.job_id).where(
                    ne_scheduled_jobs.c.name == "connect-sync-fixture"
                )
            ).scalar_one()
            conn.execute(
                ne_job_runs.insert().values(
                    run_id="already-running",
                    job_id=job_id,
                    started_at=_T0 - timedelta(seconds=1),
                    claim_owner="already-running",
                    lease_expires_at=_T0 + timedelta(minutes=1),
                )
            )

        assert asyncio.run(scheduler.run_requested_jobs(now=_T0)) == ()
        queued = store.get(request.request_id)
        assert queued is not None and queued.status == "queued"
        assert log.calls == []

        with engine.begin() as conn:
            conn.execute(
                ne_job_runs.update()
                .where(ne_job_runs.c.run_id == "already-running")
                .values(finished_at=_T0, succeeded=True, output="{}")
            )
        outcomes = asyncio.run(scheduler.run_requested_jobs(now=_T0))
        assert len(outcomes) == 1 and outcomes[0].succeeded is True
        assert log.names() == ["write.internal.connect.sync_fixture"]
    finally:
        engine.dispose()


def test_expired_claim_reconciles_after_restart_without_rerunning_request() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        first, store, first_log, _gateway = _manual_sync_fixture(engine, RecordingAuditSink())
        request = _request_manual_sync(store)
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_job_requests).where(ne_job_requests.c.request_id == request.request_id)
            ).one()
        assert first._claim_request(row, _T0) is not None
        assert first_log.calls == []
        joined = _request_manual_sync(store)
        assert joined.request_id == request.request_id
        assert joined.created is False

        restarted, restarted_store, restarted_log, _gateway = _manual_sync_fixture(
            engine, RecordingAuditSink(), seed=False
        )
        assert asyncio.run(restarted.run_requested_jobs(now=_T0 + timedelta(minutes=3))) == ()
        interrupted = restarted_store.get(request.request_id)
        assert interrupted is not None
        assert interrupted.status == "failed"
        assert interrupted.error_class == "InterruptedJobRun"
        assert restarted_log.calls == []

        replacement = _request_manual_sync(restarted_store)
        assert replacement.request_id != request.request_id
        outcomes = asyncio.run(restarted.run_requested_jobs(now=_T0 + timedelta(minutes=3)))
        assert len(outcomes) == 1 and outcomes[0].succeeded is True
        assert restarted_log.names() == ["write.internal.connect.sync_fixture"]
    finally:
        engine.dispose()


def test_expired_manual_claim_releases_when_reconciliation_audit_stays_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler, store, log, _gateway = _manual_sync_fixture(engine, RecordingAuditSink())
        request = _request_manual_sync(store)
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_job_requests).where(ne_job_requests.c.request_id == request.request_id)
            ).one()
        assert scheduler._claim_request(row, _T0) is not None

        def _fail_run_audit(_conn: Any, event: Any) -> None:
            if getattr(event, "action", "") == "job.run":
                raise RuntimeError("chain remains unavailable for job.run")

        monkeypatch.setattr("ne.runtime.scheduler.append_record", _fail_run_audit)

        with pytest.raises(RuntimeAuditError, match="interrupted manual run"):
            asyncio.run(scheduler.run_requested_jobs(now=_T0 + timedelta(minutes=3)))

        terminal = store.get(request.request_id)
        assert terminal is not None
        assert terminal.status == "failed"
        assert terminal.error_class == "InterruptedJobRun"
        assert log.calls == []
        replacement = _request_manual_sync(store)
        assert replacement.created is True
        assert replacement.request_id != request.request_id
    finally:
        engine.dispose()


def test_active_execution_renews_its_claim_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler, store, _log, _gateway = _manual_sync_fixture(engine, RecordingAuditSink())
        request = _request_manual_sync(store)
        tool = "write.internal.connect.sync_fixture"
        original = scheduler._executor._bindings[tool]
        monkeypatch.setattr("ne.runtime.scheduler._LEASE_RENEW_INTERVAL_S", 0)

        async def _exercise() -> None:
            execution_started = asyncio.Event()
            release_execution = asyncio.Event()
            renewal_started = asyncio.Event()

            async def _blocked_counts(_args: Any) -> str:
                execution_started.set()
                await release_execution.wait()
                return (
                    '{"mode":"incremental","records_seen":0,"written":0,'
                    '"skipped_unchanged":0,"invalid":0,"quarantined":0}'
                )

            scheduler._executor._bindings[tool] = ToolBinding(
                spec=original.spec, handler=_blocked_counts
            )

            def _renewal_clock() -> datetime:
                renewal_started.set()
                return _T0 + timedelta(minutes=1)

            scheduler._clock = _renewal_clock
            task = asyncio.create_task(scheduler.run_requested_jobs(now=_T0))
            try:
                await asyncio.wait_for(execution_started.wait(), timeout=5)
                await asyncio.wait_for(renewal_started.wait(), timeout=5)
                # Event.set() does not transfer control. The renewal task reaches its
                # next await only after both lease UPDATEs commit, so observing this
                # event proves the query below cannot race the heartbeat write.
                with engine.connect() as conn:
                    lease = conn.execute(
                        sa.select(ne_job_requests.c.lease_expires_at).where(
                            ne_job_requests.c.request_id == request.request_id
                        )
                    ).scalar_one()
                assert lease == (_T0 + timedelta(minutes=1) + _RUN_LEASE).replace(tzinfo=None)
            finally:
                release_execution.set()
            outcomes = await task
            assert len(outcomes) == 1 and outcomes[0].succeeded is True

        asyncio.run(_exercise())
    finally:
        engine.dispose()


def test_legacy_unleased_orphan_no_longer_suppresses_scheduled_window() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        log = ToolLog()
        gateway = ScriptedGateway(
            [tool_call_completion("read.docs.search", {"q": "x"}), text_completion("done")]
        )
        scheduler = _scheduler(RecordingAuditSink(), engine, gateway=gateway, log=log)
        scheduler.schedule(_job())
        with engine.begin() as conn:
            job_id = conn.execute(sa.select(ne_scheduled_jobs.c.job_id)).scalar_one()
            conn.execute(
                ne_job_runs.insert().values(
                    run_id="legacy-orphan",
                    job_id=job_id,
                    started_at=_T0,
                )
            )

        outcomes = asyncio.run(scheduler.run_due_jobs(now=_LATER))
        assert len(outcomes) == 1 and outcomes[0].succeeded is True
        rows = {str(row.run_id): row for row in _run_rows(engine)}
        assert rows["legacy-orphan"].error_class == "InterruptedJobRun"
        assert rows["legacy-orphan"].finished_at is not None
        assert log.names() == ["read.docs.search"]
    finally:
        engine.dispose()


def test_manual_request_is_pollable_and_terminally_audited_if_job_disappears() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler, store, log, _gateway = _manual_sync_fixture(engine, RecordingAuditSink())
        request = _request_manual_sync(store)
        (job,) = scheduler.list_jobs()
        assert job.job_id is not None
        scheduler.cancel(job.job_id)

        assert asyncio.run(scheduler.run_requested_jobs(now=_T0)) == ()
        terminal = store.get(request.request_id)
        assert terminal is not None
        assert terminal.name == "connect-sync-fixture"
        assert terminal.status == "failed"
        assert terminal.error_class == "SchedulingError"
        assert terminal.output == "ERROR: SchedulingError"
        assert "job.run" in _outbox_actions(engine)
        assert log.calls == []
    finally:
        engine.dispose()


def test_manual_request_expiry_and_config_drift_fail_before_tool() -> None:
    for drift, now in ((False, _T0 + timedelta(minutes=6)), (True, _T0)):
        engine = sa.create_engine("sqlite://")
        try:
            scheduler, store, log, _gateway = _manual_sync_fixture(engine, RecordingAuditSink())
            request = _request_manual_sync(store, config_hash="different-config" if drift else None)
            outcomes = asyncio.run(scheduler.run_requested_jobs(now=now))
            if drift:
                assert len(outcomes) == 1 and outcomes[0].succeeded is False
            else:
                assert outcomes == ()
            assert log.calls == []
            terminal = store.get(request.request_id)
            assert terminal is not None and terminal.status == "failed"
            assert terminal.error_class == ("SchedulingError" if drift else "ManualRequestExpired")
        finally:
            engine.dispose()


def test_finish_audit_failure_releases_claim_without_duplicate_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine("sqlite://")
    try:
        scheduler, store, log, _gateway = _manual_sync_fixture(engine, RecordingAuditSink())
        request = _request_manual_sync(store)

        def _fail_append(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("audit unavailable")

        with monkeypatch.context() as mutation:
            mutation.setattr("ne.runtime.scheduler.append_record", _fail_append)
            with pytest.raises(RuntimeAuditError):
                asyncio.run(scheduler.run_requested_jobs(now=_T0))

        terminal = store.get(request.request_id)
        assert terminal is not None
        assert terminal.status == "failed"
        assert terminal.error_class == "RuntimeAuditError"
        assert log.names() == ["write.internal.connect.sync_fixture"]

        replacement = _request_manual_sync(store)
        assert replacement.request_id != request.request_id
        outcomes = asyncio.run(scheduler.run_requested_jobs(now=_T0))
        assert len(outcomes) == 1 and outcomes[0].succeeded is True
        assert log.names() == [
            "write.internal.connect.sync_fixture",
            "write.internal.connect.sync_fixture",
        ]
    finally:
        engine.dispose()


def test_executor_audit_failure_releases_claim_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine("sqlite://")
    try:
        sink = RecordingAuditSink()
        scheduler, store, log, _gateway = _manual_sync_fixture(engine, sink)
        request = _request_manual_sync(store)

        async def _fail_emit(_event: Any) -> None:
            raise RuntimeError("audit unavailable")

        with monkeypatch.context() as mutation:
            mutation.setattr(sink, "append", _fail_emit)
            with pytest.raises(AuditEmissionError):
                asyncio.run(scheduler.run_requested_jobs(now=_T0))

        terminal = store.get(request.request_id)
        assert terminal is not None
        assert terminal.status == "failed"
        assert terminal.error_class == "AuditEmissionError"
        assert log.calls == []

        replacement = _request_manual_sync(store)
        assert replacement.request_id != request.request_id
        outcomes = asyncio.run(scheduler.run_requested_jobs(now=_T0))
        assert len(outcomes) == 1 and outcomes[0].succeeded is True
        assert log.names() == ["write.internal.connect.sync_fixture"]
    finally:
        engine.dispose()


def test_manual_request_rechecks_pdp_at_execution_and_refuses_revocation() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        sink = RecordingAuditSink()
        denied = make_pdp(
            sink,
            overrides={
                "write.internal.connect.sync_fixture": {
                    "effect": "deny",
                    "policy_version": "revoked",
                    "reason": "revoked",
                    "obligations": {},
                    "tier": 1,
                }
            },
        )
        scheduler, store, log, _gateway = _manual_sync_fixture(engine, sink, pdp=denied)
        request = _request_manual_sync(store)
        outcomes = asyncio.run(scheduler.run_requested_jobs(now=_T0))
        assert len(outcomes) == 1 and outcomes[0].succeeded is False
        assert log.calls == []
        terminal = store.get(request.request_id)
        assert terminal is not None and terminal.error_class == "ToolDeniedError"
    finally:
        engine.dispose()
