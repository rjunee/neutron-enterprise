# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-3 DoD (d): the MANDATORY counts-only ``context.budget`` audit (C10/C12).

Every ``dropped_turns > 0`` trim AND every typed raise emits exactly one
``context.budget`` event with the exact five-key COUNTS-ONLY detail schema; no
message or tool text ever appears in the detail (C12 sentinel scan); a sink-append
failure fails CLOSED (``RuntimeAuditError``); no-trim requests emit NOTHING; and
the session surfaces the refusal while still emitting its ``session.run`` error.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import InstructionPart
from pydantic_ai.models import ModelRequestParameters

from ne.contracts.errors import ContextBudgetExceededError
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import RuntimeAuditError
from ne.runtime.model import GatewayModel
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.schema import AuditOutcome

from .conftest import (
    FailingAuditSink,
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    make_budget,
    make_executor,
    make_principal,
    req_first,
    req_user,
    resp_text,
    standard_bindings,
    text_completion,
    turn,
)

_FILL = "x" * 3000
_EXPECTED_KEYS = {"estimated_tokens", "input_budget", "context_window", "dropped_turns", "stage"}


async def _noop_hook(name: str, args: Mapping[str, Any]) -> None:  # pragma: no cover - unused
    return None


def _budget(input_budget: int):
    return make_budget(
        context_window=input_budget + 100 + 1024, max_output_tokens=100, overhead_margin=1024
    )


def _params() -> ModelRequestParameters:
    return ModelRequestParameters(
        function_tools=[], instruction_parts=[InstructionPart(content="INSTR")]
    )


def _model(gw: ScriptedGateway, sink: Any, input_budget: int) -> GatewayModel:
    return GatewayModel(
        gw, RuntimeSettings(), on_unknown_tool=_noop_hook, budget=_budget(input_budget), sink=sink
    )


def test_trim_emits_one_success_event_with_counts_only_schema() -> None:
    messages = [
        req_first("SYS", "TURN1 " + _FILL),
        resp_text("a1"),
        *turn("TURN2 " + _FILL, answer="a2"),
        req_user("CURRENT"),
    ]
    gw = ScriptedGateway([text_completion("done")])
    sink = RecordingAuditSink()
    model = _model(gw, sink, 2500)  # keep one newest turn, drop one
    asyncio.run(model.request(list(messages), None, _params()))

    events = sink.with_action("context.budget")
    assert len(events) == 1
    ev = events[0]
    assert ev.outcome == AuditOutcome.success.value
    assert ev.actor == "ne-runtime"
    detail = json.loads(ev.detail)
    assert set(detail) == _EXPECTED_KEYS
    assert detail["stage"] == "runtime-trim"
    for k in _EXPECTED_KEYS - {"stage"}:
        assert isinstance(detail[k], int)
    assert detail["dropped_turns"] >= 1


def test_typed_raise_emits_error_event_before_raising() -> None:
    messages = [
        *turn("TURN1 " + _FILL, answer="a1"),
        req_user("CURRENT " + "y" * 30000),
    ]
    gw = ScriptedGateway([text_completion("done")])
    sink = RecordingAuditSink()
    model = _model(gw, sink, 1000)
    with pytest.raises(ContextBudgetExceededError):
        asyncio.run(model.request(list(messages), None, _params()))
    events = sink.with_action("context.budget")
    assert len(events) == 1
    assert events[0].outcome == AuditOutcome.error.value


def test_detail_never_contains_message_text_c12() -> None:
    sentinel = "SENTINEL-SECRET-PROMPT-TEXT"
    messages = [
        req_first("SYS " + sentinel, "TURN1 " + sentinel + _FILL),
        resp_text("a1 " + sentinel),
        *turn("TURN2 " + sentinel + _FILL, answer="a2"),
        req_user("CURRENT " + sentinel),
    ]
    gw = ScriptedGateway([text_completion("done")])
    sink = RecordingAuditSink()
    model = _model(gw, sink, 2500)
    asyncio.run(model.request(list(messages), None, _params()))
    for ev in sink.with_action("context.budget"):
        assert sentinel not in ev.detail
        assert sentinel not in (ev.resource or "")


def test_no_trim_emits_no_event() -> None:
    messages = [req_user("small")]
    gw = ScriptedGateway([text_completion("done")])
    sink = RecordingAuditSink()
    model = _model(gw, sink, 100000)
    asyncio.run(model.request(list(messages), None, _params()))
    assert sink.with_action("context.budget") == []


def test_sink_append_failure_fails_closed() -> None:
    messages = [
        *turn("TURN1 " + _FILL, answer="a1"),
        req_user("CURRENT"),
    ]
    gw = ScriptedGateway([text_completion("done")])
    # input_budget forces TURN1 to drop (dropped>0), so the mandatory audit fires.
    model = _model(gw, FailingAuditSink(), 1000)  # trim will try to audit → append raises
    with pytest.raises(RuntimeAuditError):
        asyncio.run(model.request(list(messages), None, _params()))


def test_session_surfaces_refusal_and_still_emits_session_run_error() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        ensure_runtime_schema(engine)
        store = SqlAlchemySessionStore(engine)
        session_id = asyncio.run(
            store.create_conversation("assistant", "eng", "user:ada")
        ).conversation_id
        # A current prompt too big to ever fit ⇒ refusal even after trimming.
        sink = RecordingAuditSink()
        from .conftest import ToolLog

        executor = make_executor(
            standard_bindings(ToolLog()), sink, approval_gate=NamedApproverGate("alice")
        )
        session = DepartmentAgentSession(
            session_id=session_id,
            agent_name="assistant",
            department="eng",
            owner_principal_id="user:ada",
            store=store,
            executor=executor,
            gateway=ScriptedGateway([text_completion("never")]),
            settings=RuntimeSettings(context_budget=_budget(500)),
            instructions="INSTR",
            sink=sink,
        )
        with pytest.raises(ContextBudgetExceededError):
            asyncio.run(
                session.run("HUGE " + "z" * 30000, principal=make_principal(department="eng"))
            )
        # The mandatory context.budget error event AND the session.run error event.
        budget_events = sink.with_action("context.budget")
        assert budget_events and budget_events[0].outcome == AuditOutcome.error.value
        run_events = sink.with_action("session.run")
        assert run_events and run_events[-1].outcome == AuditOutcome.error.value
    finally:
        engine.dispose()
