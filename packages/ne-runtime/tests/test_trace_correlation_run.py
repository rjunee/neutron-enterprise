# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE NE-166 acceptance test: a full governed sub-agent run over the REAL
``LiteLLMGateway`` (router monkeypatched below the litellm seam) under the
DEFAULT no-op OTel tracer, asserting every gateway cost/audit event AND every
runtime dispatch/middleware audit event on the run shares exactly ONE
non-null ``trace_id``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from opentelemetry import trace

from ne.contracts import Lane
from ne.contracts.governance import RedactionResult
from ne.contracts.runtime import SubAgentSpec
from ne.gateway import GatewaySettings, LiteLLMGateway, ModelDef, TaskRoute
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import current_run_trace_id
from ne.runtime.dispatch import AgentDefinition, GovernedSubAgentDispatcher
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.processes import ProcessRegistry

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ToolLog,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
)

# NE-DEMO-PDP-TRACE: ``pdp.decide`` joined this set once ``GovernedPdp`` began
# stamping the ambient run trace id. Before that it was the ONE governed event on
# the run that carried ``trace_id: null``, so the trace walk showed the gated tool
# execution WITHOUT the decision that authorized it.
_STAMPED_ACTIONS = {
    "subagent.dispatch",
    "subagent.route",
    "subagent.complete",
    "pdp.decide",
    "tool.execute",
    "ne.gateway.complete",
}


class RecordingCostSink:
    """A real CostSink implementation that appends emitted events to a list."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


class _PassthroughRedactor:
    """NE-162: a ``Redactor`` that never redacts anything — ne-runtime tests
    must not import test doubles from another package's tests/, so this
    mirrors (rather than imports) ne-gateway's own conftest stub."""

    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        return RedactionResult(text=text)


def test_full_governed_run_shares_one_trace_id_under_no_op_tracer() -> None:
    # The DEFAULT no-op tracer: no live span, so gateway span-derivation alone
    # would yield None for every event.
    assert trace.get_current_span().get_span_context().is_valid is False

    gateway_settings = GatewaySettings(
        models=[
            ModelDef(
                name="stub",
                litellm_model="gpt-3.5-turbo",
                provider="openai",
                lane=Lane.FRONTIER_API,
                extra_params={"api_key": "test", "mock_response": "ok"},
                context_window=32768,
            )
        ],
        tasks={"agent": TaskRoute(model="stub")},
    )
    cost_sink = RecordingCostSink()
    audit_sink = RecordingAuditSink()
    gw = LiteLLMGateway(
        gateway_settings, cost_sink, audit_sink=audit_sink, redactor=_PassthroughRedactor()
    )

    tool_call_line = json.dumps(
        {"tool_call": {"name": "read.docs.search", "arguments": {"q": "w"}}}
    )
    responses = [
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=tool_call_line))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
            _hidden_params={"response_cost": 0.0},
            model="gpt-3.5-turbo",
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="found it"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
            _hidden_params={"response_cost": 0.0},
            model="gpt-3.5-turbo",
        ),
    ]

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return responses.pop(0)

    gw._router.acompletion = fake_acompletion  # type: ignore[method-assign]

    pdp = make_pdp(audit_sink)
    log = ToolLog()
    executor = GovernedToolExecutor(
        standard_bindings(log),
        pdp=pdp,
        guardrails=make_guardrails(audit_sink),
        approval_gate=NamedApproverGate("alice"),
        sink=audit_sink,
    )
    dispatcher = GovernedSubAgentDispatcher(
        definitions={
            "researcher": AgentDefinition(
                name="researcher",
                instructions="You research.",
                toolset=("read.docs.search",),
            )
        },
        executor=executor,
        gateway=gw,
        registry=ProcessRegistry(),
        sink=audit_sink,
        settings=RuntimeSettings(),
        principal=make_principal(),
        pdp=pdp,
    )

    result = asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="find widgets")))
    assert result.succeeded is True

    assert len(cost_sink.events) == 2
    trace_ids = {e.trace_id for e in cost_sink.events}
    stamped_actions: set[str] = set()
    for event in audit_sink.events:
        action = getattr(event, "action", None)
        if action in _STAMPED_ACTIONS:
            stamped_actions.add(str(action))
            trace_ids.add(event.trace_id)

    # The run really did produce a governance decision, so the pdp.decide arm of
    # the assertion below is not vacuously satisfied by an absent event.
    assert "pdp.decide" in stamped_actions

    assert len(trace_ids) == 1
    (only_trace_id,) = trace_ids
    assert only_trace_id is not None

    assert current_run_trace_id() is None
