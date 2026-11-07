# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GovernedSubAgentDispatcher — typed dispatch, governed sub-agent runs, memory isolation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.models import CompletionRequest
from ne.contracts.runtime import SubAgentDispatcher, SubAgentResult, SubAgentSpec
from ne.runtime.config import RuntimeSettings
from ne.runtime.dispatch import AgentDefinition, GovernedSubAgentDispatcher
from ne.runtime.errors import UnknownAgentTypeError
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.processes import ProcessRegistry
from ne.runtime.store import ensure_runtime_schema, ne_agent_session_messages
from ne.schema import AuditOutcome

from .conftest import (
    AppendCountFailingSink,
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
    text_completion,
    tool_call_completion,
)

_DEFINITIONS = {
    "researcher": AgentDefinition(
        name="researcher",
        instructions="You research things.",
        toolset=("read.docs.search",),
    ),
    "notetaker": AgentDefinition(
        name="notetaker",
        instructions="You take notes.",
        toolset=("write.internal.notes.append",),
    ),
}


def _dispatcher(
    sink: Any,
    *,
    gateway: Any,
    log: ToolLog | None = None,
    registry: ProcessRegistry | None = None,
    settings: RuntimeSettings | None = None,
) -> GovernedSubAgentDispatcher:
    log = log or ToolLog()
    pdp = make_pdp(sink)
    executor = GovernedToolExecutor(
        standard_bindings(log),
        pdp=pdp,
        guardrails=make_guardrails(sink),
        approval_gate=NamedApproverGate("alice"),
        sink=sink,
    )
    return GovernedSubAgentDispatcher(
        definitions=_DEFINITIONS,
        executor=executor,
        gateway=gateway,
        registry=registry or ProcessRegistry(),
        sink=sink,
        settings=settings or RuntimeSettings(),
        principal=make_principal(),
        pdp=pdp,
    )


def test_dispatch_returns_subagent_result_from_isolated_run() -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "widgets"}), text_completion("found 3")]
    )
    dispatcher = _dispatcher(sink, gateway=gateway)
    result = asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="find widgets")))
    assert isinstance(result, SubAgentResult)
    assert result.agent == "researcher"
    assert result.output == "found 3"
    assert result.succeeded is True


def test_subagent_tool_call_routes_through_governed_executor() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gateway = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "x"}), text_completion("done")]
    )
    dispatcher = _dispatcher(sink, gateway=gateway, log=log)
    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="t")))

    # The sub-agent's in-set tool call ran the FULL governed pipeline.
    assert log.names() == ["read.docs.search"]
    assert sink.with_action("pdp.decide")
    executes = sink.with_action("tool.execute")
    assert len(executes) == 1 and executes[0].outcome == AuditOutcome.success.value


def test_unknown_agent_type_audited_denied_and_raises() -> None:
    sink = RecordingAuditSink()
    dispatcher = _dispatcher(sink, gateway=ScriptedGateway([]))
    with pytest.raises(UnknownAgentTypeError):
        asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="ghost", task="t")))
    denied = sink.with_action("subagent.dispatch")
    assert len(denied) == 1
    assert denied[0].outcome == AuditOutcome.denied.value
    assert denied[0].resource == "subagent:ghost"


def test_memory_isolation_child_sees_no_parent_history_and_writes_no_session_rows() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        ensure_runtime_schema(engine)

        def _count() -> int:
            with engine.connect() as conn:
                return int(
                    conn.execute(
                        sa.select(sa.func.count()).select_from(ne_agent_session_messages)
                    ).scalar()
                    or 0
                )

        before = _count()
        gateway = ScriptedGateway([text_completion("isolated")])
        dispatcher = _dispatcher(RecordingAuditSink(), gateway=gateway)
        asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="solo")))

        # The sub-agent wrote NO session rows (it never touches the session store).
        assert _count() == before == 0
        # The child's first model request carries ONLY its own prompt — no parent
        # history messages precede it (memory isolation by empty history).
        req = gateway.requests[0]
        user_msgs = [m for m in req.messages if m.role == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0].content.startswith("You research things.")
        assert "Task: solo" in user_msgs[0].content
    finally:
        engine.dispose()


def test_spec_context_is_rendered_into_child_prompt() -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("ok")])
    dispatcher = _dispatcher(sink, gateway=gateway)
    asyncio.run(
        dispatcher.dispatch(
            SubAgentSpec(agent="researcher", task="t", context={"budget_usd": "100"})
        )
    )
    user_msg = next(m for m in gateway.requests[0].messages if m.role == "user")
    # The sanctioned parent→child channel: spec.context rendered as a JSON block.
    assert "budget_usd" in user_msg.content
    assert "100" in user_msg.content


def test_dispatch_and_complete_audit_events_emitted() -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("ok")])
    dispatcher = _dispatcher(sink, gateway=gateway)
    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="t")))

    dispatched = sink.with_action("subagent.dispatch")
    completed = sink.with_action("subagent.complete")
    assert len(dispatched) == 1 and dispatched[0].outcome == AuditOutcome.success.value
    assert len(completed) == 1 and completed[0].outcome == AuditOutcome.success.value


def test_child_run_error_returns_failed_result_with_error_class_only() -> None:
    class _RaisingGateway:
        def __init__(self) -> None:
            self.requests: list[CompletionRequest] = []

        async def complete(self, request: CompletionRequest) -> Any:
            self.requests.append(request)
            raise ValueError("super-secret internal detail")

        def stream(self, request: CompletionRequest) -> Any:
            raise NotImplementedError

    sink = RecordingAuditSink()
    dispatcher = _dispatcher(sink, gateway=_RaisingGateway())
    result = asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="t")))

    assert result.succeeded is False
    # Error CLASS only — no exception message / values leak.
    assert result.output == "ERROR: ValueError"
    assert "super-secret" not in result.output
    completed = sink.with_action("subagent.complete")
    assert len(completed) == 1 and completed[0].outcome == AuditOutcome.error.value


def test_dispatch_audit_append_failure_fails_closed_no_model_call() -> None:
    log = ToolLog()
    sink = AppendCountFailingSink(fail_on_action="subagent.dispatch")
    gateway = ScriptedGateway([text_completion("never")])
    pdp = make_pdp(sink)
    executor = GovernedToolExecutor(
        standard_bindings(log),
        pdp=pdp,
        guardrails=make_guardrails(sink),
        approval_gate=NamedApproverGate("alice"),
        sink=sink,
    )
    registry = ProcessRegistry()
    dispatcher = GovernedSubAgentDispatcher(
        definitions=_DEFINITIONS,
        executor=executor,
        gateway=gateway,
        registry=registry,
        sink=sink,
        settings=RuntimeSettings(),
        principal=make_principal(),
        pdp=pdp,
    )

    from ne.runtime.errors import RuntimeAuditError

    with pytest.raises(RuntimeAuditError):
        asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="t")))

    # The dispatch audit failed BEFORE the run — the model was never called and the
    # registry record was finished (nothing left live).
    assert gateway.requests == []
    assert registry.live() == ()


def test_dispatcher_satisfies_frozen_protocol() -> None:
    dispatcher = _dispatcher(RecordingAuditSink(), gateway=ScriptedGateway([]))
    # Typed assignment — pyright enforces GovernedSubAgentDispatcher: SubAgentDispatcher.
    _d: SubAgentDispatcher = dispatcher
    assert _d is dispatcher
