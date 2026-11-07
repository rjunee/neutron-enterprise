# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The session instructions seam (design §10-Q1): instructions reach the model.

``DepartmentAgentSession(instructions=...)`` must pass the string to BOTH ``Agent()``
constructions — the plain branch AND the grounding branch — so the composed skills
index reaches the model request. Missing either branch silently drops skills for
grounded sessions. ``instructions=None`` is byte-identical to pre-unit (the
``ModelRequest.instructions`` stays ``None``).

NE-CTXB-0 wires the LIVE forwarding: ``GatewayModel._map_history`` derives the
instructions via pydantic-ai's ``Model._get_instruction_parts`` and prepends them
as ONE system :class:`~ne.contracts.types.ChatMessage` at the head of the mapped
chat, so the fake gateway (and, in production, the real provider) actually
receives the skills index — not just the internal ``ModelRequest.instructions``
attribute a capturing subclass can see. The tests below assert directly on
``ScriptedGateway.requests[...].messages`` (the ``CompletionRequest`` the gateway
receives), which is the end-to-end proof the DoD requires.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, ClassVar

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

import ne.runtime.agent as agent_mod
from ne.govern.errors import GuardrailViolation
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.factory import create_department_session, rehydrate_department_session
from ne.runtime.grounding import GroundingPolicy
from ne.runtime.model import GatewayModel
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_executor,
    make_pdp,
    make_principal,
    standard_bindings,
    text_completion,
    tool_call_completion,
)

SENTINEL = "SKILLS-INDEX-SENTINEL"


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_runtime_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


class _CapturingGatewayModel(GatewayModel):
    """Records the ModelRequest.instructions each request sees, then delegates."""

    captured: ClassVar[list[str | None]] = []

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: Any,
        model_request_parameters: Any,
    ) -> ModelResponse:
        for message in messages:
            if isinstance(message, ModelRequest):
                _CapturingGatewayModel.captured.append(getattr(message, "instructions", None))
        return await super().request(messages, model_settings, model_request_parameters)


def _render_fallback(answers: Sequence[Mapping[str, Any]]) -> str:
    return "FALLBACK"


class _GateEngine:
    async def enforce(self, payload: Mapping[str, Any]) -> object:
        if payload.get("grounded_gate") is True and payload.get("grounded") is False:
            raise GuardrailViolation(())
        return None


def _session(
    engine: sa.Engine,
    gateway: ScriptedGateway,
    sink: Any,
    log: ToolLog,
    *,
    instructions: str | None,
    grounding: GroundingPolicy | None = None,
) -> DepartmentAgentSession:
    store = SqlAlchemySessionStore(engine)
    executor = make_executor(standard_bindings(log), sink, approval_gate=NamedApproverGate("alice"))
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
        grounding=grounding,
        instructions=instructions,
    )


@pytest.fixture(autouse=True)
def _reset_capture() -> Iterator[None]:
    _CapturingGatewayModel.captured = []
    yield
    _CapturingGatewayModel.captured = []


def test_instructions_reach_plain_branch(
    engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_mod, "GatewayModel", _CapturingGatewayModel)
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("final")])
    session = _session(engine, gw, sink, log, instructions=SENTINEL)
    asyncio.run(session.run("hi", principal=make_principal(department="eng")))
    assert SENTINEL in _CapturingGatewayModel.captured


def test_instructions_reach_grounding_branch(
    engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_mod, "GatewayModel", _CapturingGatewayModel)
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("grounded final")])
    policy = GroundingPolicy(
        granted_tools=("read.docs.search",),
        render_fallback=_render_fallback,
        guardrail_engine=_GateEngine(),
        sink=sink,
        pdp=make_pdp(sink),
    )
    session = _session(engine, gw, sink, log, instructions=SENTINEL, grounding=policy)
    asyncio.run(session.run("hi", principal=make_principal(department="eng")))
    assert SENTINEL in _CapturingGatewayModel.captured


def test_instructions_none_byte_identical(
    engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_mod, "GatewayModel", _CapturingGatewayModel)
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("final")])
    session = _session(engine, gw, sink, log, instructions=None)
    asyncio.run(session.run("hi", principal=make_principal(department="eng")))
    # No instructions were ever attached to the model request (pre-unit behavior).
    assert _CapturingGatewayModel.captured == [None]
    # The session ALWAYS binds tools (standard_bindings), so the in-band D3
    # catalog legitimately occupies the head as the sole system message; the
    # instructions-absent guarantee is "no instructions text anywhere", not
    # "no system message" (the C3 merge must not forbid all system messages).
    messages = gw.requests[0].messages
    system_msgs = [m for m in messages if m.role == "system"]
    assert len(system_msgs) == 1
    assert messages[0].role == "system"
    assert "Available tools (JSON):" in messages[0].content
    assert SENTINEL not in messages[0].content
    assert messages[1].role == "user"


def test_instructions_system_message_reaches_gateway_plain(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("final")])
    session = _session(engine, gw, sink, log, instructions=SENTINEL)
    asyncio.run(session.run("hi", principal=make_principal(department="eng")))
    messages = gw.requests[0].messages
    assert messages[0].role == "system"
    # The session always binds tools, so the catalog merges into the SAME
    # leading system message as the instructions (C3 message-shape contract).
    assert messages[0].content.startswith(SENTINEL + "\n\n")
    assert sum(SENTINEL in m.content for m in messages) == 1


def test_instructions_system_message_reaches_gateway_grounding(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("grounded final")])
    policy = GroundingPolicy(
        granted_tools=("read.docs.search",),
        render_fallback=_render_fallback,
        guardrail_engine=_GateEngine(),
        sink=sink,
        pdp=make_pdp(sink),
    )
    session = _session(engine, gw, sink, log, instructions=SENTINEL, grounding=policy)
    asyncio.run(session.run("hi", principal=make_principal(department="eng")))
    messages = gw.requests[0].messages
    assert messages[0].role == "system"
    # The grounding branch also always binds a tool set (recorder.tool_specs),
    # so the catalog merges into the SAME leading system message.
    assert messages[0].content.startswith(SENTINEL + "\n\n")
    assert sum(SENTINEL in m.content for m in messages) == 1


def test_instructions_once_per_request_in_tool_loop(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "x"}), text_completion("final")]
    )
    session = _session(engine, gw, sink, log, instructions=SENTINEL)
    asyncio.run(session.run("please search", principal=make_principal(department="eng")))
    assert len(gw.requests) == 2
    for request in gw.requests:
        sentinel_messages = [m for m in request.messages if SENTINEL in m.content]
        assert len(sentinel_messages) == 1
        assert request.messages[0].role == "system"
        assert request.messages[0].content.startswith(SENTINEL + "\n\n")


def test_instructions_once_across_persisted_turns(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("first"), text_completion("second")])
    session = _session(engine, gw, sink, log, instructions=SENTINEL)
    asyncio.run(session.run("turn one", principal=make_principal(department="eng")))
    asyncio.run(session.run("turn two", principal=make_principal(department="eng")))
    assert len(gw.requests) == 2
    last = gw.requests[-1].messages
    sentinel_messages = [m for m in last if SENTINEL in m.content]
    assert len(sentinel_messages) == 1
    assert last[0].role == "system"
    assert last[0].content.startswith(SENTINEL + "\n\n")


def test_empty_instructions_prepends_nothing(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("final")])
    session = _session(engine, gw, sink, log, instructions="")
    asyncio.run(session.run("hi", principal=make_principal(department="eng")))
    # instructions="" ⇒ pydantic-ai treats it as absent, so no instructions text
    # reaches the model; the session's always-bound tool catalog legitimately
    # occupies the head as the sole system message (guard must not forbid all
    # system messages nor invent an empty one).
    messages = gw.requests[0].messages
    system_msgs = [m for m in messages if m.role == "system"]
    assert len(system_msgs) == 1
    assert messages[0].role == "system"
    assert "Available tools (JSON):" in messages[0].content
    assert SENTINEL not in messages[0].content
    assert messages[1].role == "user"


def test_direct_request_uses_history_instructions(engine: sa.Engine) -> None:
    del engine  # not needed for this model-level (no session) test

    class _NoOpUnknownTool:
        async def __call__(self, name: str, args: Mapping[str, Any]) -> None:
            raise AssertionError("no tool call is expected in this direct request")

    gw = ScriptedGateway([text_completion("ok")])
    model = GatewayModel(gw, RuntimeSettings(), on_unknown_tool=_NoOpUnknownTool())
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="hi")], instructions=SENTINEL)
    ]
    asyncio.run(model.request(messages, None, ModelRequestParameters()))
    assert gw.requests[0].messages[0].role == "system"
    assert gw.requests[0].messages[0].content == SENTINEL


# -- PRE-011B: the factory threads the composed instructions ---------------------


def _factory_parts(engine: sa.Engine, gw: ScriptedGateway) -> tuple[SqlAlchemySessionStore, Any]:
    store = SqlAlchemySessionStore(engine)
    executor = make_executor(
        standard_bindings(ToolLog()), RecordingAuditSink(), approval_gate=NamedApproverGate("alice")
    )
    return store, executor


def test_create_and_rehydrate_department_session_thread_instructions_to_the_gateway(
    engine: sa.Engine,
) -> None:
    """Both public session factories must carry ``instructions`` to the model.

    ``_compose_department_session`` is the single construction point for both, so
    a drop there silently un-skills every composed session.
    """
    gw = ScriptedGateway([text_completion("first"), text_completion("second")])
    store, executor = _factory_parts(engine, gw)
    created = asyncio.run(
        create_department_session(
            "assistant",
            "eng",
            owner_principal_id="user:ada",
            store=store,
            gateway=gw,
            executor=executor,
            instructions=SENTINEL,
        )
    )
    asyncio.run(created.run("hi", principal=make_principal(department="eng")))
    assert gw.requests[0].messages[0].role == "system"
    assert gw.requests[0].messages[0].content.startswith(SENTINEL + "\n\n")

    rehydrated = asyncio.run(
        rehydrate_department_session(
            created.session_id,
            "assistant",
            "eng",
            owner_principal_id="user:ada",
            store=store,
            gateway=gw,
            executor=executor,
            instructions=SENTINEL,
        )
    )
    assert rehydrated is not None
    asyncio.run(rehydrated.run("again", principal=make_principal(department="eng")))
    assert gw.requests[1].messages[0].content.startswith(SENTINEL + "\n\n")


def test_create_department_session_without_instructions_sends_no_instruction_text(
    engine: sa.Engine,
) -> None:
    """The omitted-argument default stays byte-identical to pre-unit behavior."""
    gw = ScriptedGateway([text_completion("final")])
    store, executor = _factory_parts(engine, gw)
    session = asyncio.run(
        create_department_session(
            "assistant",
            "eng",
            owner_principal_id="user:ada",
            store=store,
            gateway=gw,
            executor=executor,
        )
    )
    asyncio.run(session.run("hi", principal=make_principal(department="eng")))
    messages = gw.requests[0].messages
    assert [m for m in messages if m.role == "system"] == [messages[0]]
    assert "Available tools (JSON):" in messages[0].content
    assert SENTINEL not in messages[0].content
