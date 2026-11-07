# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""D-2 (NE-SKILL-1b): the session.run terminal audit event + failure semantics.

``DepartmentAgentSession(sink=...)`` emits a ``session.run`` audit event in the
run's finally, inside the run trace (so ``trace_id`` matches the run's
``tool.execute`` events — the Skill Forge joins on it). ``sink=None`` is
byte-identical (no event). Failure semantics: sink failure AFTER a successful run
raises ``RuntimeAuditError`` (C6); a sink failure while an exception is already
propagating is suppressed so the original error wins.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import RuntimeAuditError
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


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_runtime_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _session(
    engine: sa.Engine,
    gateway: ScriptedGateway,
    executor_sink: RecordingAuditSink,
    log: ToolLog,
    *,
    session_sink: object | None,
) -> DepartmentAgentSession:
    store = SqlAlchemySessionStore(engine)
    owner = "user:ada"
    conversation = asyncio.run(store.create_conversation("assistant", "eng", owner))
    executor = make_executor(
        standard_bindings(log), executor_sink, approval_gate=NamedApproverGate("alice")
    )
    return DepartmentAgentSession(
        session_id=conversation.conversation_id,
        agent_name="assistant",
        department="eng",
        owner_principal_id=owner,
        store=store,
        executor=executor,
        gateway=gateway,
        settings=RuntimeSettings(),
        sink=session_sink,  # type: ignore[arg-type]
    )


def test_session_run_emitted_inside_run_trace(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("final")])
    session = _session(engine, gw, sink, log, session_sink=sink)
    asyncio.run(session.run("hi", principal=make_principal(department="eng")))

    events = sink.with_action("session.run")
    assert len(events) == 1
    ev = events[0]
    assert ev.trace_id is not None
    # trace_id matches the run's tool events (if any tool ran) — at minimum it is
    # a non-null run-scoped trace shared with everything else this run emitted.
    other_traces = {e.trace_id for e in sink.events if getattr(e, "action", "") != "session.run"}
    if other_traces:
        assert ev.trace_id in other_traces


def test_session_run_detail_is_names_only(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("no secrets here")])
    session = _session(engine, gw, sink, log, session_sink=sink)
    asyncio.run(session.run("my secret prompt text", principal=make_principal(department="eng")))
    ev = sink.with_action("session.run")[0]
    detail = json.loads(ev.detail)
    assert detail == {
        "agent": "assistant",
        "department": "eng",
        "session_id": session.session_id,
        "succeeded": True,
    }
    # The prompt text never appears in the terminal event.
    assert "secret prompt" not in ev.detail


def test_sink_none_is_byte_identical_no_event(engine: sa.Engine) -> None:
    log = ToolLog()
    executor_sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("final")])
    session = _session(engine, gw, executor_sink, log, session_sink=None)
    asyncio.run(session.run("hi", principal=make_principal(department="eng")))
    assert executor_sink.with_action("session.run") == []


def test_sink_failure_after_success_raises_runtime_audit_error(engine: sa.Engine) -> None:
    log = ToolLog()
    executor_sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("final")])

    class _FailOnSessionRun:
        async def append(self, event: object) -> None:
            if getattr(event, "action", "") == "session.run":
                raise RuntimeError("sink down")

        async def verify(self, *, start: object = None, end: object = None) -> object:
            raise NotImplementedError

    session = _session(engine, gw, executor_sink, log, session_sink=_FailOnSessionRun())
    with pytest.raises(RuntimeAuditError):
        asyncio.run(session.run("hi", principal=make_principal(department="eng")))
    assert (
        asyncio.run(
            session._store.load_conversation_history(
                session.session_id, "user:ada", "eng", "assistant"
            )
        )
        == []
    )


class _BoomError(RuntimeError):
    pass


class _RaisingGateway:
    """A gateway whose model request raises — an in-flight run error."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    async def complete(self, request: object) -> object:
        raise _BoomError("gateway exploded mid-run")

    def stream(self, request: object) -> object:
        raise NotImplementedError


def test_sink_failure_during_inflight_exception_lets_original_error_win(
    engine: sa.Engine,
) -> None:
    # An error raised INSIDE the run body reaches the finally with the exception
    # in flight; the terminal-emit failure must be suppressed so the ORIGINAL
    # error wins (never masked by a RuntimeAuditError).
    log = ToolLog()

    class _AlwaysFail:
        async def append(self, event: object) -> None:
            raise RuntimeError("sink down")

        async def verify(self, *, start: object = None, end: object = None) -> object:
            raise NotImplementedError

    store = SqlAlchemySessionStore(engine)
    owner = "user:ada"
    conversation = asyncio.run(store.create_conversation("assistant", "eng", owner))
    session = DepartmentAgentSession(
        session_id=conversation.conversation_id,
        agent_name="assistant",
        department="eng",
        owner_principal_id=owner,
        store=store,
        executor=make_executor(
            standard_bindings(log),
            RecordingAuditSink(),
            approval_gate=NamedApproverGate("alice"),
        ),
        gateway=_RaisingGateway(),  # type: ignore[arg-type]
        settings=RuntimeSettings(),
        sink=_AlwaysFail(),  # type: ignore[arg-type]
    )
    with pytest.raises(_BoomError):
        asyncio.run(session.run("hi", principal=make_principal(department="eng")))
