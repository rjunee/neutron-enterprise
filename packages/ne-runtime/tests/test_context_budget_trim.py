# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-3 deterministic trim semantics (design §3.5).

Drives ``GatewayModel.request`` directly over a scripted gateway + a recording
audit sink and asserts the pairing-safe whole-turn trim: legacy byte-identity
(budget=None), oldest-first whole-turn drops, protected head/instructions/suffix
survival, no dangling ``ToolReturnPart``, the in-band catalog counted in the
estimate, and the typed raise when head+suffix alone overflow. Turn content is
sized in KB so per-turn token cost (~2k) dwarfs structural noise — the drop
boundaries are unambiguous.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic_ai.messages import InstructionPart, ToolCallPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from ne.contracts.errors import ContextBudgetExceededError
from ne.contracts.frozen import FrozenDict
from ne.runtime.config import RuntimeSettings
from ne.runtime.model import GatewayModel

from .conftest import (
    RecordingAuditSink,
    ScriptedGateway,
    estimate_request,
    make_budget,
    req_first,
    req_user,
    resp_text,
    text_completion,
    tool_turn,
    turn,
)

# One prior turn costs ~2k tokens; head+suffix+instructions ~50 — boundaries wide.
_FILL = "x" * 3000


def _tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="read.docs.search",
            description="Search docs",
            parameters_json_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )
    ]


async def _noop_hook(name: str, args: Mapping[str, Any]) -> None:  # pragma: no cover - unused
    return None


def _params(*, with_tools: bool = False, instr: str | None = "SYS-INSTR") -> ModelRequestParameters:
    return ModelRequestParameters(
        function_tools=_tools() if with_tools else [],
        instruction_parts=[InstructionPart(content=instr)] if instr is not None else [],
    )


def _run(
    model: GatewayModel,
    messages: list,
    *,
    with_tools: bool = False,
    model_settings: ModelSettings | None = None,
    instr: str | None = "SYS-INSTR",
) -> Any:
    return asyncio.run(
        model.request(list(messages), model_settings, _params(with_tools=with_tools, instr=instr))
    )


def _budgeted(
    gw: ScriptedGateway, sink: RecordingAuditSink, *, input_budget_tokens: int
) -> GatewayModel:
    # context_window = input_budget + reserve + margin; pick a fixed reserve/margin.
    budget = make_budget(
        context_window=input_budget_tokens + 100 + 1024,
        max_output_tokens=100,
        overhead_margin=1024,
    )
    assert budget.input_budget == input_budget_tokens
    return GatewayModel(gw, RuntimeSettings(), on_unknown_tool=_noop_hook, budget=budget, sink=sink)


# --- (1) budget=None ⇒ byte-identical legacy -------------------------------


def test_budget_none_is_byte_identical_legacy() -> None:
    messages = [req_first("SYS", "old " + _FILL), resp_text("a"), req_user("now")]
    ref_gw = ScriptedGateway([text_completion("done")])
    ref = GatewayModel(ref_gw, RuntimeSettings(), on_unknown_tool=_noop_hook)
    _run(ref, messages)

    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("done")])
    # sink present but budget=None ⇒ never used (construction is allowed).
    model = GatewayModel(gw, RuntimeSettings(), on_unknown_tool=_noop_hook, sink=sink)
    _run(model, messages)

    assert [m.model_dump() for m in gw.requests[-1].messages] == [
        m.model_dump() for m in ref_gw.requests[-1].messages
    ]
    assert gw.requests[-1].params == FrozenDict()
    assert sink.events == []


# --- (2) construction guard: budget without sink ---------------------------


def test_budget_without_sink_is_unconstructable() -> None:
    with pytest.raises(ValueError, match="sink"):
        GatewayModel(
            ScriptedGateway([]),
            RuntimeSettings(),
            on_unknown_tool=_noop_hook,
            budget=make_budget(context_window=8192, max_output_tokens=512, overhead_margin=1024),
        )


# --- (3) oldest-first whole-turn drops -------------------------------------


def test_oldest_turn_dropped_first() -> None:
    messages = [
        req_first("SYSTEM", "TURN1 " + _FILL),
        resp_text("ans1"),
        *turn("TURN2 " + _FILL, answer="ans2"),
        *turn("TURN3 " + _FILL, answer="ans3"),
        req_user("CURRENT"),
    ]
    gw = ScriptedGateway([text_completion("done")])
    sink = RecordingAuditSink()
    # Keep exactly two newest turns (~2k each) + head/suffix (~50): budget 5000.
    model = _budgeted(gw, sink, input_budget_tokens=5000)
    _run(model, messages)

    sent = "".join(m.content for m in gw.requests[-1].messages)
    assert "TURN1" not in sent  # oldest dropped
    assert "TURN2" in sent and "TURN3" in sent  # newest survive (contiguous suffix)
    assert "CURRENT" in sent  # current prompt always survives
    assert estimate_request(gw.requests[-1]) <= 5000
    budget_events = sink.with_action("context.budget")
    assert len(budget_events) == 1
    import json

    assert json.loads(budget_events[0].detail)["dropped_turns"] == 1


# --- (4) protected head + instructions + suffix survive every trim ---------


def test_head_instructions_and_current_tool_returns_survive() -> None:
    # A current-turn tool-using suffix: user → tool_call → BIG tool return → (model
    # about to answer). The tool return is in the SUFFIX and must never be dropped.
    from .conftest import req_tool_return, resp_tool_call

    messages = [
        req_first("SYSTEM-PROMPT", "TURN1 " + _FILL),
        resp_text("ans1"),
        *turn("TURN2 " + _FILL, answer="ans2"),
        # current turn (suffix): starts at this user request onward
        req_user("CURRENT-Q"),
        resp_tool_call("read.docs.search", {"q": "z"}),
        req_tool_return("read.docs.search", "CURRENT-RETURN " + _FILL),
    ]
    gw = ScriptedGateway([text_completion("done")])
    sink = RecordingAuditSink()
    # Small budget forces dropping every prior turn; suffix (incl. the ~2k current
    # tool return) + head + instructions still fit and are always counted.
    model = _budgeted(gw, sink, input_budget_tokens=2500)
    _run(model, messages, instr="MY-INSTRUCTIONS")

    sent = gw.requests[-1].messages
    joined = "\n".join(m.content for m in sent)
    assert "SYSTEM-PROMPT" in joined  # protected head system prompt
    assert "MY-INSTRUCTIONS" in joined  # mapped instructions message
    assert "CURRENT-Q" in joined  # current user prompt
    assert "CURRENT-RETURN" in joined  # current-turn tool return (un-trimmable)
    assert "TURN1" not in joined and "TURN2" not in joined  # prior turns dropped


# --- (5) pairing: no dangling tool-return; pair drops together -------------


def test_whole_turn_drop_keeps_tool_pairs_together() -> None:
    # A 2-prior-turn history where turn 1 is a tool turn (user → tool_call → tool
    # return → answer); the small budget keeps only the newest turn.
    messages = [
        *tool_turn("TURN1 " + _FILL, tool="read.docs.search", args={"q": "a"}, result="RET1"),
        *turn("TURN2 " + _FILL, answer="ans2"),
        req_user("CURRENT"),
    ]
    gw = ScriptedGateway([text_completion("done")])
    sink = RecordingAuditSink()
    model = _budgeted(gw, sink, input_budget_tokens=3000)  # keep only newest turn
    _run(model, messages)

    sent = gw.requests[-1].messages
    # The dropped tool turn removed BOTH its assistant tool_call rendering AND its
    # tool-return message: no role='tool' message may lack a preceding assistant.
    roles = [m.role for m in sent]
    for i, role in enumerate(roles):
        if role == "tool":
            assert i > 0 and roles[i - 1] == "assistant", "dangling tool return (no tool_call)"
    joined = "\n".join(m.content for m in sent)
    assert "TURN1" not in joined and "RET1" not in joined  # whole tool turn gone
    assert "TURN2" in joined


# --- (6) head+suffix alone exceed ⇒ typed raise, no gateway call -----------


def test_head_plus_suffix_overflow_raises_typed() -> None:
    huge_suffix = "y" * 30000  # ~20k tokens, dwarfs any budget
    messages = [
        *turn("TURN1 " + _FILL, answer="a1"),
        *turn("TURN2 " + _FILL, answer="a2"),
        req_user("CURRENT " + huge_suffix),
    ]
    gw = ScriptedGateway([text_completion("done")])
    sink = RecordingAuditSink()
    model = _budgeted(gw, sink, input_budget_tokens=1000)
    with pytest.raises(ContextBudgetExceededError) as ei:
        _run(model, messages)
    assert ei.value.stage == "runtime-trim"
    assert ei.value.dropped_turns == 2  # both prior turns were droppable groups
    assert gw.requests == []  # never reached the gateway


# --- (7) the in-band tool catalog is counted in the estimate ---------------


def test_catalog_is_counted_in_estimate() -> None:
    # A history that FITS without tools but must trim once a fat catalog is added.
    messages = [
        *turn("TURN1 " + _FILL, answer="a1"),
        req_user("CURRENT"),
    ]
    fat = "read.docs.search " + ("d" * 4000)  # description bloats the catalog message

    def _fat_tools() -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="read.docs.search",
                description=fat,
                parameters_json_schema={"type": "object", "properties": {}},
            )
        ]

    # Budget fits head + suffix + 1 turn (~2k) but NOT + a ~2.7k catalog message.
    gw_no = ScriptedGateway([text_completion("done")])
    sink_no = RecordingAuditSink()
    model_no = _budgeted(gw_no, sink_no, input_budget_tokens=3500)
    # Without tools: fits, no trim.
    asyncio.run(
        model_no.request(
            list(messages), None, ModelRequestParameters(function_tools=[], instruction_parts=[])
        )
    )
    assert sink_no.with_action("context.budget") == []

    gw_t = ScriptedGateway([text_completion("done")])
    sink_t = RecordingAuditSink()
    model_t = _budgeted(gw_t, sink_t, input_budget_tokens=3500)
    asyncio.run(
        model_t.request(
            list(messages),
            None,
            ModelRequestParameters(function_tools=_fat_tools(), instruction_parts=[]),
        )
    )
    # With the fat catalog counted, the prior turn had to be dropped.
    events = sink_t.with_action("context.budget")
    assert events and int(estimate_request(gw_t.requests[-1])) <= 3500
    assert "TURN1" not in "\n".join(m.content for m in gw_t.requests[-1].messages)


# --- (8) single-request history (suffix_start==0) ⇒ immediate raise --------


def test_single_over_budget_request_raises_nothing_dropped() -> None:
    messages = [req_user("ONLY " + "z" * 30000)]
    gw = ScriptedGateway([text_completion("done")])
    sink = RecordingAuditSink()
    model = _budgeted(gw, sink, input_budget_tokens=500)
    with pytest.raises(ContextBudgetExceededError) as ei:
        _run(model, messages)
    assert ei.value.dropped_turns == 0
    assert gw.requests == []
    err_events = sink.with_action("context.budget")
    assert len(err_events) == 1
    from ne.schema import AuditOutcome

    assert err_events[0].outcome == AuditOutcome.error.value


# --- a known-tool call still parses on the budgeted path -------------------


def test_budgeted_path_still_parses_tool_call() -> None:
    import json

    line = json.dumps({"tool_call": {"name": "read.docs.search", "arguments": {"q": "x"}}})
    messages = [req_user("go")]
    gw = ScriptedGateway([text_completion(line)])
    sink = RecordingAuditSink()
    model = _budgeted(gw, sink, input_budget_tokens=100000)
    resp = _run(model, messages, with_tools=True)
    assert isinstance(resp.parts[0], ToolCallPart)
    assert sink.with_action("context.budget") == []  # fit, no trim
