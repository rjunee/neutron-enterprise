# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ProcessRegistry — lifecycle, atomic caps, lineage, bounded history (NE-034)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.models import CompletionRequest
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import DispatchCapExceededError
from ne.runtime.processes import ProcessRegistry
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_executor,
    make_principal,
    standard_bindings,
    text_completion,
)


def test_register_live_finish_lifecycle() -> None:
    registry = ProcessRegistry()
    record = registry.register(kind="session", name="assistant", principal_id="user:ada")
    assert record.finished_at is None
    assert record.succeeded is None
    assert registry.live() == (record,)
    assert registry.get(record.process_id) == record

    registry.finish(record.process_id, succeeded=True)
    assert registry.live() == ()
    finished = registry.get(record.process_id)
    assert finished is not None
    assert finished.finished_at is not None
    assert finished.succeeded is True


def test_active_count_by_kind() -> None:
    registry = ProcessRegistry()
    registry.register(kind="session", name="s", principal_id="p")
    a = registry.register(kind="subagent", name="a", principal_id="p")
    registry.register(kind="subagent", name="b", principal_id="p")
    assert registry.active_count("session") == 1
    assert registry.active_count("subagent") == 2
    assert registry.active_count("cron-job") == 0
    registry.finish(a.process_id, succeeded=True)
    assert registry.active_count("subagent") == 1


def test_register_at_concurrency_cap_raises_atomically() -> None:
    registry = ProcessRegistry()
    registry.register(kind="subagent", name="a", principal_id="p", concurrency_cap=2)
    registry.register(kind="subagent", name="b", principal_id="p", concurrency_cap=2)
    before = registry.active_count("subagent")
    with pytest.raises(DispatchCapExceededError) as exc:
        registry.register(kind="subagent", name="c", principal_id="p", concurrency_cap=2)
    # No record was added on rejection (atomic check-then-insert).
    assert registry.active_count("subagent") == before
    assert exc.value.reason == "concurrency"
    assert exc.value.scope == "server"
    # A different kind is not blocked by the subagent cap.
    registry.register(kind="session", name="s", principal_id="p", concurrency_cap=2)


def test_lineage_parent_id_and_depth_recorded() -> None:
    registry = ProcessRegistry()
    parent = registry.register(kind="session", name="root", principal_id="p")
    child = registry.register(
        kind="subagent", name="child", principal_id="p", parent_id=parent.process_id, depth=1
    )
    assert child.parent_id == parent.process_id
    assert child.depth == 1
    assert parent.parent_id is None
    assert parent.depth == 0


def test_finished_history_bounded_deque() -> None:
    registry = ProcessRegistry()
    for i in range(300):
        rec = registry.register(kind="cron-job", name=f"j{i}", principal_id="p")
        registry.finish(rec.process_id, succeeded=True)
    finished = list(registry.finished())
    assert len(finished) == 256  # bounded ring — oldest evicted
    # The most recent finish is retained; the oldest 44 were evicted.
    assert finished[-1].name == "j299"
    assert finished[0].name == "j44"


class _RaisingGateway:
    """A gateway whose completion always raises (drives the session failure path)."""

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Any:
        self.requests.append(request)
        raise RuntimeError("gateway boom")

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


def _session(engine: sa.Engine, gateway: Any, registry: ProcessRegistry) -> DepartmentAgentSession:
    ensure_runtime_schema(engine)
    store = SqlAlchemySessionStore(engine)
    executor = make_executor(
        standard_bindings(ToolLog()), RecordingAuditSink(), approval_gate=NamedApproverGate("alice")
    )
    return DepartmentAgentSession(
        session_id=asyncio.run(
            store.create_conversation("assistant", "eng", "user:ada")
        ).conversation_id,
        agent_name="assistant",
        department="eng",
        owner_principal_id="user:ada",
        store=store,
        executor=executor,
        gateway=gateway,
        settings=RuntimeSettings(),
        registry=registry,
    )


def test_session_run_registers_and_finishes_in_registry() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        registry = ProcessRegistry()
        session = _session(engine, ScriptedGateway([text_completion("done")]), registry)
        result = asyncio.run(session.run("go", principal=make_principal(department="eng")))
        assert result.output == "done"
        # Registered as a session and finished successfully — nothing left live.
        assert registry.live() == ()
        finished = list(registry.finished())
        assert len(finished) == 1
        assert finished[0].kind == "session"
        assert finished[0].name == "assistant"
        assert finished[0].succeeded is True
    finally:
        engine.dispose()


def test_session_run_failure_finishes_succeeded_false() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        registry = ProcessRegistry()
        session = _session(engine, _RaisingGateway(), registry)
        with pytest.raises(RuntimeError):
            asyncio.run(session.run("go", principal=make_principal(department="eng")))
        assert registry.live() == ()
        finished = list(registry.finished())
        assert len(finished) == 1
        assert finished[0].succeeded is False
    finally:
        engine.dispose()
