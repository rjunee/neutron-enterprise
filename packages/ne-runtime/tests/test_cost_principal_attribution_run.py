# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE NE-COST-PRINCIPAL acceptance test: two governed, isolated agent runs over
the REAL ``LiteLLMGateway`` (router monkeypatched below the litellm seam,
template ``test_trace_correlation_run.py``), each driven by a DIFFERENT
principal, asserting every ``CostEvent`` produced by run 1 carries run 1's
principal and every one from run 2 carries run 2's principal — then a direct
gateway call OUTSIDE any run scope proves the background/system path inherits
nothing and stays service-attributed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from ne.contracts import ChatMessage, CompletionRequest, Lane
from ne.contracts.governance import RedactionResult
from ne.gateway import GatewaySettings, LiteLLMGateway, ModelDef, TaskRoute
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import current_run_principal_id
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.runner import run_isolated_agent

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ToolLog,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
)


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


def _build_gateway(cost_sink: RecordingCostSink, audit_sink: RecordingAuditSink) -> LiteLLMGateway:
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
    gw = LiteLLMGateway(
        gateway_settings, cost_sink, audit_sink=audit_sink, redactor=_PassthroughRedactor()
    )

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="final answer"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
            _hidden_params={"response_cost": 0.0},
            model="gpt-3.5-turbo",
        )

    gw._router.acompletion = fake_acompletion  # type: ignore[method-assign]
    return gw


def _executor(audit_sink: RecordingAuditSink) -> GovernedToolExecutor:
    pdp = make_pdp(audit_sink)
    log = ToolLog()
    return GovernedToolExecutor(
        standard_bindings(log),
        pdp=pdp,
        guardrails=make_guardrails(audit_sink),
        approval_gate=NamedApproverGate("alice"),
        sink=audit_sink,
    )


def test_two_isolated_runs_each_cost_event_carries_its_own_principal() -> None:
    cost_sink = RecordingCostSink()
    audit_sink = RecordingAuditSink()
    gw = _build_gateway(cost_sink, audit_sink)
    executor = _executor(audit_sink)
    settings = RuntimeSettings()

    # Run 1: user:ada.
    output_1 = asyncio.run(
        run_isolated_agent(
            task="find widgets",
            principal=make_principal("user:ada"),
            executor=executor,
            gateway=gw,
            settings=settings,
        )
    )
    assert output_1 == "final answer"
    assert len(cost_sink.events) == 1
    assert cost_sink.events[0].actor == "user:ada"

    # Run 2: user:bob, same gateway + sink instance. Run 1's principal must not
    # leak forward.
    output_2 = asyncio.run(
        run_isolated_agent(
            task="find widgets again",
            principal=make_principal("user:bob"),
            executor=executor,
            gateway=gw,
            settings=settings,
        )
    )
    assert output_2 == "final answer"
    assert len(cost_sink.events) == 2
    assert cost_sink.events[0].actor == "user:ada"
    assert cost_sink.events[1].actor == "user:bob"

    # Outside any run scope: a direct gateway call is the background/system
    # path. It must be service-attributed, NEVER inherit run 2's principal.
    assert current_run_principal_id() is None
    asyncio.run(
        gw.complete(
            CompletionRequest(
                messages=(ChatMessage(role="user", content="background sync"),),
                task="agent",
            )
        )
    )
    assert len(cost_sink.events) == 3
    assert cost_sink.events[2].actor == "ne-gateway"

    assert current_run_principal_id() is None
