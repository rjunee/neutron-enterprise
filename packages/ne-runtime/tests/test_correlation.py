# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Run-scoped correlation id (NE-166): mint/inherit/explicit/reset semantics, and
threading through GatewayModel, GovernedToolExecutor, GovernedSubAgentDispatcher,
and DepartmentAgentSession."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from ne.contracts.runtime import SubAgentSpec
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import (
    action_scope,
    current_action_scope,
    current_run_principal_id,
    current_run_trace_id,
    run_principal_scope,
    run_trace_scope,
)
from ne.runtime.dispatch import AgentDefinition, GovernedSubAgentDispatcher
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.model import GatewayModel
from ne.runtime.processes import ProcessRegistry
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_executor,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
    text_completion,
    tool_call_completion,
)

# --- (1)/(2) run_trace_scope mint/inherit/explicit/reset -------------------


def test_run_trace_scope_mints_fresh_32_hex_id_and_resets_on_exit() -> None:
    assert current_run_trace_id() is None
    with run_trace_scope() as tid:
        assert isinstance(tid, str)
        assert len(tid) == 32
        int(tid, 16)  # valid hex
        assert current_run_trace_id() == tid
    assert current_run_trace_id() is None


def test_nested_run_trace_scope_inherits_same_id() -> None:
    with run_trace_scope() as outer:
        with run_trace_scope() as inner:
            assert inner == outer
        # Still inside the outer scope after the nested one exits.
        assert current_run_trace_id() == outer
    assert current_run_trace_id() is None


def test_explicit_trace_id_argument_wins() -> None:
    with run_trace_scope() as outer:
        with run_trace_scope("explicit-id") as inner:
            assert inner == "explicit-id"
            assert inner != outer
        assert current_run_trace_id() == outer


def test_current_run_trace_id_none_outside_any_scope() -> None:
    assert current_run_trace_id() is None


# --- action_scope (NE-ACTION-DETERMINISTIC-REQID) ---------------------------


def test_action_scope_sets_and_resets() -> None:
    assert current_action_scope() is None
    with action_scope("turn-x") as scope:
        assert scope == "turn-x"
        assert current_action_scope() == "turn-x"
    assert current_action_scope() is None

    # Reset happens on the exception path too.
    with pytest.raises(ValueError), action_scope("turn-y"):
        assert current_action_scope() == "turn-y"
        raise ValueError("boom")
    assert current_action_scope() is None


def test_nested_action_scope_innermost_wins() -> None:
    with action_scope("outer"):
        assert current_action_scope() == "outer"
        with action_scope("inner"):
            assert current_action_scope() == "inner"
        # After the inner scope exits, the ambient scope is 'outer' again.
        assert current_action_scope() == "outer"
    assert current_action_scope() is None


# --- run_principal_scope (NE-COST-PRINCIPAL) --------------------------------


def test_current_run_principal_id_none_outside_any_scope() -> None:
    assert current_run_principal_id() is None


def test_run_principal_scope_sets_and_resets() -> None:
    assert current_run_principal_id() is None
    with run_principal_scope("user:ada") as pid:
        assert pid == "user:ada"
        assert current_run_principal_id() == "user:ada"
    assert current_run_principal_id() is None

    # Reset happens on the exception path too.
    with pytest.raises(ValueError), run_principal_scope("user:bob"):
        assert current_run_principal_id() == "user:bob"
        raise ValueError("boom")
    assert current_run_principal_id() is None


def test_nested_run_principal_scope_innermost_wins() -> None:
    with run_principal_scope("user:ada"):
        assert current_run_principal_id() == "user:ada"
        with run_principal_scope("user:bob"):
            assert current_run_principal_id() == "user:bob"
        # After the inner scope exits, the ambient principal is 'user:ada' again.
        assert current_run_principal_id() == "user:ada"
    assert current_run_principal_id() is None


def test_run_principal_scope_is_task_local_under_concurrency() -> None:
    """The carrier must be a ContextVar, not a process-global cell.

    Every other test here drives runs SEQUENTIALLY, and a module-level global
    that saves-and-restores the prior value is sequentially indistinguishable
    from a ContextVar — review confirmed such a mutant passes the entire suite.
    That gap matters because the API serves concurrent turns on one event loop:
    under a global carrier, user A's completion is built after user B's run root
    has already overwritten the cell, and A's spend is billed to B on the very
    screen whose purpose is measurement discipline.

    TWO barriers, deliberately. A single barrier plus `sleep(0)` is VACUOUS: the
    last task to arrive runs to completion and restores the prior value before
    the first resumes, so the global mutant survives it. Both scopes must be
    open SIMULTANEOUSLY and each task must read its own value while the other
    is still inside its scope.
    """
    entered = asyncio.Barrier(2)
    read = asyncio.Barrier(2)
    observed: dict[str, str | None] = {}

    async def run_as(principal_id: str) -> None:
        with run_principal_scope(principal_id):
            await entered.wait()  # both scopes now open at once
            observed[principal_id] = current_run_principal_id()
            await read.wait()  # neither scope exits before both have read

    async def both() -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(run_as("user:ada"))
            tg.create_task(run_as("user:bob"))

    asyncio.run(both())

    assert observed == {"user:ada": "user:ada", "user:bob": "user:bob"}
    assert current_run_principal_id() is None


# --- (3) GatewayModel.request stamps the ambient id onto CompletionRequest --


def test_gateway_model_request_stamps_ambient_trace_id() -> None:
    gw = ScriptedGateway([text_completion("done")])
    model = GatewayModel(gw, RuntimeSettings(), on_unknown_tool=_unused_hook)
    with run_trace_scope() as tid:
        asyncio.run(
            model.request(
                [ModelRequest(parts=[UserPromptPart(content="hi")])],
                None,
                ModelRequestParameters(),
            )
        )
    assert gw.requests[0].trace_id == tid


def test_gateway_model_request_stamps_ambient_principal_id() -> None:
    gw = ScriptedGateway([text_completion("done")])
    model = GatewayModel(gw, RuntimeSettings(), on_unknown_tool=_unused_hook)
    with run_principal_scope("user:ada"):
        asyncio.run(
            model.request(
                [ModelRequest(parts=[UserPromptPart(content="hi")])],
                None,
                ModelRequestParameters(),
            )
        )
    assert gw.requests[0].principal_id == "user:ada"


def test_gateway_model_request_stamps_none_principal_id_outside_any_scope() -> None:
    gw = ScriptedGateway([text_completion("done")])
    model = GatewayModel(gw, RuntimeSettings(), on_unknown_tool=_unused_hook)
    assert current_run_principal_id() is None
    asyncio.run(
        model.request(
            [ModelRequest(parts=[UserPromptPart(content="hi")])],
            None,
            ModelRequestParameters(),
        )
    )
    assert gw.requests[0].principal_id is None


async def _unused_hook(name: str, args: Any) -> None:
    raise AssertionError("unknown-tool hook should not be invoked in this test")


# --- (4) GovernedToolExecutor emits tool.execute with ambient id, None outside


def test_middleware_emits_ambient_trace_id_inside_scope() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = GovernedToolExecutor(
        standard_bindings(log),
        pdp=make_pdp(sink),
        guardrails=make_guardrails(sink),
        approval_gate=NamedApproverGate("alice"),
        sink=sink,
    )
    with run_trace_scope() as tid:
        asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))
    executes = sink.with_action("tool.execute")
    assert len(executes) == 1
    assert executes[0].trace_id == tid


def test_middleware_emits_none_trace_id_outside_any_scope() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = GovernedToolExecutor(
        standard_bindings(log),
        pdp=make_pdp(sink),
        guardrails=make_guardrails(sink),
        approval_gate=NamedApproverGate("alice"),
        sink=sink,
    )
    assert current_run_trace_id() is None
    asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))
    executes = sink.with_action("tool.execute")
    assert len(executes) == 1
    assert executes[0].trace_id is None


# --- (5)/(6) dispatch: standalone mints one shared id; nested inherits -----

_DEFINITIONS = {
    "researcher": AgentDefinition(
        name="researcher",
        instructions="You research things.",
        toolset=("read.docs.search",),
    ),
}


def _dispatcher(
    sink: Any, *, gateway: Any, log: ToolLog | None = None
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
        registry=ProcessRegistry(),
        sink=sink,
        settings=RuntimeSettings(),
        principal=make_principal(),
        pdp=pdp,
    )


def test_standalone_dispatch_mints_one_shared_trace_id() -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "widgets"}), text_completion("found 3")]
    )
    dispatcher = _dispatcher(sink, gateway=gateway)
    assert current_run_trace_id() is None

    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="find widgets")))

    dispatched = sink.with_action("subagent.dispatch")
    completed = sink.with_action("subagent.complete")
    executed = sink.with_action("tool.execute")
    assert len(dispatched) == 1
    assert len(completed) == 1
    assert len(executed) == 1
    trace_ids = {
        dispatched[0].trace_id,
        completed[0].trace_id,
        executed[0].trace_id,
        *[r.trace_id for r in gateway.requests],
    }
    assert len(trace_ids) == 1
    assert None not in trace_ids
    assert current_run_trace_id() is None


def test_dispatch_inside_enclosing_scope_inherits_exact_id() -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "widgets"}), text_completion("found 3")]
    )
    dispatcher = _dispatcher(sink, gateway=gateway)

    with run_trace_scope() as tid:
        asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="find widgets")))

    dispatched = sink.with_action("subagent.dispatch")
    completed = sink.with_action("subagent.complete")
    executed = sink.with_action("tool.execute")
    assert dispatched[0].trace_id == tid
    assert completed[0].trace_id == tid
    assert executed[0].trace_id == tid
    assert all(r.trace_id == tid for r in gateway.requests)


# --- (7) DepartmentAgentSession.run establishes a scope ---------------------


def test_session_run_stamps_non_null_trace_id_on_request_and_tool_execute() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        ensure_runtime_schema(engine)
        store = SqlAlchemySessionStore(engine)
        log = ToolLog()
        sink = RecordingAuditSink()
        gw = ScriptedGateway(
            [tool_call_completion("read.docs.search", {"q": "q3"}), text_completion("final")]
        )
        executor = make_executor(
            standard_bindings(log), sink, approval_gate=NamedApproverGate("alice")
        )
        session = DepartmentAgentSession(
            session_id=asyncio.run(
                store.create_conversation("assistant", "eng", "user:ada")
            ).conversation_id,
            agent_name="assistant",
            department="eng",
            owner_principal_id="user:ada",
            store=store,
            executor=executor,
            gateway=gw,
            settings=RuntimeSettings(),
        )

        asyncio.run(session.run("please search", principal=make_principal(department="eng")))

        assert current_run_trace_id() is None
        request_trace_id = gw.requests[0].trace_id
        assert request_trace_id is not None
        executed = sink.with_action("tool.execute")
        assert len(executed) == 1
        assert executed[0].trace_id == request_trace_id
    finally:
        engine.dispose()
