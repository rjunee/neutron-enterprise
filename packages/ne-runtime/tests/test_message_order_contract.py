# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-AGENT-MSG-ORDER — THE owner-asked message-shape contract test.

Offline, always-on, built from the REAL agent path (never a hand-written
message list): the outgoing ``CompletionRequest.messages`` sequence a
governed ``DepartmentAgentSession`` turn actually sends must carry AT MOST
ONE system-role message, and when present it must be AT INDEX 0. This is the
C3 model-independence contract — a strict chat template (Qwen3.6-35B-A3B-FP8's
``--tool-call-parser qwen3_coder``) HTTP-400s ``"System message must be at
the beginning."`` on the SHIPPED shape: instructions become a leading system
message, and the in-band D3 tool catalog is appended as a SECOND, TRAILING
system message. This module is the guard that would have caught it.

Vacuity traps this module deliberately avoids (docs/testing/vacuous-guards.md):

* The contract is ``count <= 1 AND index == 0``, never just "a system message
  exists" — a test asserting only presence passes on the broken shape.
* Every assertion below is made against messages captured off the REAL
  ``DepartmentAgentSession``/``GatewayModel`` wiring (:class:`ScriptedGateway`
  is the ONLY test double in the (a)/(b) tests; the (c)/(d)/(e) tests drive
  ``GatewayModel.request`` directly, exactly as
  ``test_direct_request_uses_history_instructions`` does) — never a
  hand-assembled ``ChatMessage`` tuple.
* Controls (c)/(d) prove the guard DISCRIMINATES rather than forbidding every
  system message outright: a tool-free run legitimately keeps exactly one
  system message (the instructions), and a run with neither instructions nor
  tools legitimately has NONE — inventing a phantom empty system message
  would be its own bug.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.usage import RequestUsage

from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.model import GatewayModel
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
    tool_call_completion,
)

SENTINEL = "MSG-ORDER-INSTRUCTIONS-SENTINEL"


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
    sink: Any,
    log: ToolLog,
    *,
    instructions: str | None,
) -> DepartmentAgentSession:
    """The REAL agent wiring (byte-identical shape to
    ``test_agent_session_instructions.py``'s ``_session``): a governed
    tool-loop session over ``standard_bindings`` (always at least one bound
    tool, so the D3 catalog is always in play) — the fixture every assertion
    in this module is captured through, never a hand-built message list."""
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
        instructions=instructions,
    )


# --- (a) THE always-on guard: count <= 1 AND index == 0 -------------------


def test_agent_request_has_at_most_one_system_message_and_it_is_first(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "x"}), text_completion("final")]
    )
    session = _session(engine, gw, sink, log, instructions=SENTINEL)
    asyncio.run(session.run("please search", principal=make_principal(department="eng")))

    # A tool loop turn produced >= 2 outgoing requests (the pre-fix proof this
    # is exercising the real shape, not a degenerate single-turn no-op).
    assert len(gw.requests) >= 2
    for request in gw.requests:
        messages = request.messages
        system_count = sum(1 for m in messages if m.role == "system")
        assert system_count <= 1, (
            f"expected at most one system message, got {system_count}: {[m.role for m in messages]}"
        )
        if system_count:
            assert messages[0].role == "system", (
                f"the sole system message must be at index 0, got roles "
                f"{[m.role for m in messages]}"
            )
        # No system role anywhere BUT index 0 — the exact defect shape
        # (['system' at 0 or nowhere, ..., 'system' at the END]) this unit
        # exists to remove, checked positively rather than just by count.
        assert all(m.role != "system" for m in messages[1:])


# --- (b) the merge is lossless AND order-stable -----------------------------


def test_merged_system_head_is_lossless_and_order_stable(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "x"}), text_completion("final")]
    )
    session = _session(engine, gw, sink, log, instructions=SENTINEL)
    asyncio.run(session.run("please search", principal=make_principal(department="eng")))

    head = gw.requests[0].messages[0]
    assert head.role == "system"
    # Silently dropping either source is a worse bug than the one being
    # fixed — both the instructions sentinel AND the full tool catalog text
    # must survive the merge.
    assert SENTINEL in head.content
    assert "Available tools (JSON):" in head.content
    assert "read.docs.search" in head.content
    # Order-stable: the instructions were assembled BEFORE the catalog, so
    # they must precede it in the merged head.
    assert head.content.index(SENTINEL) < head.content.index("Available tools (JSON):")


# --- (c) control: a tool-free run keeps the instructions as the SOLE system
#     message (proves the guard discriminates, not "forbid all system msgs")


def test_no_tools_run_keeps_instructions_as_sole_system_message(engine: sa.Engine) -> None:
    del engine  # not needed: this is a direct model-level (no session) test

    class _NoOpUnknownTool:
        async def __call__(self, name: str, args: Mapping[str, Any]) -> None:
            raise AssertionError("no tool call is expected in this direct request")

    gw = ScriptedGateway([text_completion("ok")])
    model = GatewayModel(gw, RuntimeSettings(), on_unknown_tool=_NoOpUnknownTool())
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="hi")], instructions=SENTINEL)
    ]
    asyncio.run(model.request(messages, None, ModelRequestParameters()))

    sent = gw.requests[0].messages
    system_msgs = [m for m in sent if m.role == "system"]
    assert len(system_msgs) == 1
    assert sent[0].role == "system"
    assert sent[0].content == SENTINEL


# --- (d) control: no instructions + no tools -> ZERO system messages, never
#     a phantom empty one --------------------------------------------------


def test_no_instructions_no_tools_run_has_no_system_message(engine: sa.Engine) -> None:
    del engine

    class _NoOpUnknownTool:
        async def __call__(self, name: str, args: Mapping[str, Any]) -> None:
            raise AssertionError("no tool call is expected in this direct request")

    gw = ScriptedGateway([text_completion("ok")])
    model = GatewayModel(gw, RuntimeSettings(), on_unknown_tool=_NoOpUnknownTool())
    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hi")])]
    asyncio.run(model.request(messages, None, ModelRequestParameters()))

    sent = gw.requests[0].messages
    assert sent[0].role == "user"
    assert sum(1 for m in sent if m.role == "system") == 0


# --- (e) a mid-history SystemPromptPart folds into the leading system too --


def test_mid_history_system_prompt_parts_fold_into_leading_system(engine: sa.Engine) -> None:
    del engine

    class _NoOpUnknownTool:
        async def __call__(self, name: str, args: Mapping[str, Any]) -> None:
            raise AssertionError("no tool call is expected in this direct request")

    gw = ScriptedGateway([text_completion("ok")])
    model = GatewayModel(gw, RuntimeSettings(), on_unknown_tool=_NoOpUnknownTool())
    messages: list[ModelMessage] = [
        ModelRequest(parts=[SystemPromptPart(content="S1"), UserPromptPart(content="u1")]),
        ModelResponse(
            parts=[TextPart(content="resp")],
            usage=RequestUsage(input_tokens=1, output_tokens=1),
            model_name="prev",
        ),
        ModelRequest(parts=[SystemPromptPart(content="S2"), UserPromptPart(content="u2")]),
    ]
    asyncio.run(model.request(messages, None, ModelRequestParameters()))

    sent = gw.requests[0].messages
    system_msgs = [m for m in sent if m.role == "system"]
    assert len(system_msgs) == 1
    assert sent[0].role == "system"
    assert "S1" in sent[0].content
    assert "S2" in sent[0].content
    assert sent[0].content.index("S1") < sent[0].content.index("S2")
    assert all(m.role != "system" for m in sent[1:])
