# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Protocol-residue containment (live-isolated 2026-08-04, GPU demo lane).

With hybrid thinking ON, Qwen3.6-35B intermittently emits an in-band tool_call
object WITHOUT its ``name`` key after a tool return (observed live:
``{"tool_call": {"arguments": {"query": "SLA"}}}``). ``GatewayModel._parse``
correctly rejects the CALL (strict shape, never widened) and demotes it to
prose — but pre-fix that residue became the user-visible FINAL ANSWER and
persisted into session history, where subsequent turns imitated it (observed
4/4 in the poisoned live session).

Pinned here:

* :func:`ne.runtime.model.is_malformed_tool_call_residue` — the deliberately
  NARROW trigger predicate (lone tool_call object failing the strict shape;
  all `_parse` transports honored; prose/arrays/extra-key objects are never
  flagged);
* the session output validator: residue ⇒ corrective ``ModelRetry`` (the
  retry request carries the protocol feedback), a compliant re-emission wins,
  and exhausted retries surface pydantic-ai's LOUD ``UnexpectedModelBehavior``
  — never residue as an answer, never residue as the persisted final text;
* :func:`ne.runtime.model._render_tool_call` renders ``name`` FIRST — history
  exemplars now match _TOOL_INSTRUCTION's canonical shape instead of the old
  sorted (arguments-first) render that nudged models toward name-dropping.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai import UnexpectedModelBehavior

from ne.runtime.agent import _RESIDUE_RETRY_MSG, DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.model import _render_tool_call, is_malformed_tool_call_residue
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

_MISSING_NAME = '{"tool_call": {"arguments": {"query": "SLA"}}}'
_MISSING_ARGS = '{"tool_call": {"name": "read.docs.search"}}'


# ---------------------------------------------------------------------------
# The trigger predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        _MISSING_NAME,
        _MISSING_ARGS,
        # arguments present but not an object
        '{"tool_call": {"name": "read.docs.search", "arguments": "notadict"}}',
        # name present but empty
        '{"tool_call": {"name": "", "arguments": {}}}',
        # tool_call payload not a dict at all
        '{"tool_call": "read.docs.search"}',
        # fenced residue (the fence transport must not hide it)
        f"```json\n{_MISSING_NAME}\n```",
        # first-line residue followed by fabricated prose (first-line transport)
        _MISSING_NAME + "\nHere is what I found: ...",
        # trailing-brace-incomplete residue (brace-completion transport)
        '{"tool_call": {"arguments": {"query": "SLA"}',
    ],
)
def test_residue_detected(text: str) -> None:
    assert is_malformed_tool_call_residue(text)


@pytest.mark.parametrize(
    "text",
    [
        # CONTROL: a WELL-FORMED call is not residue (it never reaches the
        # validator anyway — it became a ToolCallPart).
        '{"tool_call": {"name": "read.docs.search", "arguments": {"q": "x"}}}',
        # plain prose
        "The SLA guarantees a 15-minute response for P1 incidents.",
        # prose that merely mentions tool_call JSON mid-text
        'as discussed, {"tool_call": {"arguments": {}}} was rejected',
        # a JSON array is not a lone tool_call object
        '[{"tool_call": {"arguments": {}}}]',
        # extra keys beside tool_call
        '{"tool_call": {"arguments": {}}, "note": "x"}',
        # empty / whitespace
        "",
        "   \n  ",
    ],
)
def test_non_residue_not_flagged(text: str) -> None:
    assert not is_malformed_tool_call_residue(text)


def test_render_tool_call_name_first() -> None:
    """History exemplars show the canonical shape: ``name`` BEFORE ``arguments``
    (the old sorted render put arguments first — the shape the live model
    degraded toward). Byte-exact pin, both directions."""
    line = _render_tool_call("read.docs.search", {"q": "x"})
    assert line == '{"tool_call":{"name":"read.docs.search","arguments":{"q":"x"}}}'
    assert line.index('"name"') < line.index('"arguments"')


# ---------------------------------------------------------------------------
# The session-level validator
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_runtime_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _session(engine: sa.Engine, gateway: ScriptedGateway) -> DepartmentAgentSession:
    store = SqlAlchemySessionStore(engine)
    executor = make_executor(
        standard_bindings(ToolLog()), RecordingAuditSink(), approval_gate=NamedApproverGate("a")
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
    )


def _request_texts(request: Any) -> str:
    return json.dumps([m.model_dump() for m in request.messages], default=str)


def test_residue_earns_corrective_retry_and_clean_answer(engine: sa.Engine) -> None:
    gw = ScriptedGateway([text_completion(_MISSING_NAME), text_completion("clean final answer")])
    session = _session(engine, gw)
    result = asyncio.run(
        session.run("what is the SLA?", principal=make_principal(department="eng"))
    )
    assert result.output == "clean final answer"
    # The retry request carried the corrective protocol feedback.
    assert len(gw.requests) == 2
    assert _RESIDUE_RETRY_MSG[:40] in _request_texts(gw.requests[1])
    # The residue is NEVER the final persisted answer text of the turn.
    assert result.output != _MISSING_NAME


def test_residue_after_tool_return_recovers(engine: sa.Engine) -> None:
    """The live failure shape: proper call -> tool return -> residue. The
    validator retries and the compliant re-emission is the final answer."""
    gw = ScriptedGateway(
        [
            tool_call_completion("read.docs.search", {"q": "sla"}),
            text_completion(_MISSING_NAME),
            text_completion("grounded final answer"),
        ]
    )
    session = _session(engine, gw)
    result = asyncio.run(
        session.run("what is the SLA?", principal=make_principal(department="eng"))
    )
    assert result.output == "grounded final answer"
    assert len(gw.requests) == 3


def test_persistent_residue_fails_loud_never_shown(engine: sa.Engine) -> None:
    """Exhausted retries surface pydantic-ai's UnexpectedModelBehavior — a loud
    failure, never residue returned as an answer."""
    gw = ScriptedGateway([text_completion(_MISSING_NAME)] * 5)
    session = _session(engine, gw)
    with pytest.raises(UnexpectedModelBehavior):
        asyncio.run(session.run("what is the SLA?", principal=make_principal(department="eng")))


def test_plain_answer_unaffected_single_request(engine: sa.Engine) -> None:
    """CONTROL: a legitimate plain-text answer flows through byte-identically,
    one model request, no retry."""
    gw = ScriptedGateway([text_completion("just an answer")])
    session = _session(engine, gw)
    result = asyncio.run(session.run("hi", principal=make_principal(department="eng")))
    assert result.output == "just an answer"
    assert len(gw.requests) == 1
