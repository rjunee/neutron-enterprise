# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The business-outcome tag is DECLARED CONFIGURATION, not caller-supplied (C8).

The whitepaper claims AI spend is traceable to named business outcomes. That is
only true if the thing spending the money cannot choose its own label — an agent
that picks its own ``kpi_tag`` per request can dress arbitrary spend up as any
outcome the buyer is watching.

So the tag is declared exactly where the routing task and lane are already
declared: on the TRUSTED, server-registered :class:`AgentDefinition` (frozen,
``extra='forbid'``), with :class:`RuntimeSettings` supplying the deployment-level
default a definition inherits when it declares none. ``SubAgentSpec`` — the
model-emitted dispatch request — has no say, exactly as it has no say over a
lane widen.

These tests pin the tag's ARRIVAL on the wire ``CompletionRequest``. The gateway
side (stamped onto the CostEvent, never forwarded to litellm) is already pinned
by ``ne-gateway/tests/test_cost_events.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ne.contracts.runtime import SubAgentSpec
from ne.runtime.config import RuntimeSettings
from ne.runtime.dispatch import AgentDefinition, GovernedSubAgentDispatcher
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.processes import ProcessRegistry

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
    text_completion,
)

_DEFINITIONS = {
    # Declares its own business outcome.
    "renewals": AgentDefinition(
        name="renewals",
        instructions="You draft renewal outreach.",
        toolset=(),
        route_task="ap.draft",
        route_lane="private-vllm",
        kpi_tag="renewal-retention",
    ),
    # Declares NO outcome — inherits whatever the deployment declared.
    "generalist": AgentDefinition(
        name="generalist",
        instructions="You do general work.",
        toolset=(),
        route_lane="private-vllm",
    ),
}


def _dispatcher(
    sink: Any,
    *,
    gateway: Any,
    settings: RuntimeSettings | None = None,
) -> GovernedSubAgentDispatcher:
    log = ToolLog()
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
        settings=settings or RuntimeSettings(),
        principal=make_principal(),
        pdp=pdp,
    )


def test_definition_declared_tag_reaches_the_completion_request() -> None:
    """The end-to-end claim: a declared outcome arrives on the wire, where the
    gateway stamps it onto the CostEvent."""
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("drafted")])
    dispatcher = _dispatcher(sink, gateway=gateway)

    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="renewals", task="draft it")))

    assert gateway.requests[0].params["kpi_tag"] == "renewal-retention"


def test_undeclared_agent_sends_no_tag_rather_than_a_default_outcome() -> None:
    """An agent nobody tagged must produce UNTAGGED spend, not spend labelled
    with a placeholder. A default tag would launder unattributed spend into a
    named bucket at the source."""
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("done")])
    dispatcher = _dispatcher(sink, gateway=gateway)

    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="generalist", task="do it")))

    assert "kpi_tag" not in gateway.requests[0].params


def test_definition_tag_overrides_the_deployment_default() -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("drafted")])
    settings = RuntimeSettings(kpi_tag="platform-baseline")
    dispatcher = _dispatcher(sink, gateway=gateway, settings=settings)

    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="renewals", task="draft it")))

    assert gateway.requests[0].params["kpi_tag"] == "renewal-retention"


def test_undeclared_definition_inherits_the_deployment_default_tag() -> None:
    """Same inheritance rule ``route_task``/``route_lane`` already use: ``None``
    on the definition means inherit the parent RuntimeSettings value."""
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("done")])
    settings = RuntimeSettings(kpi_tag="platform-baseline")
    dispatcher = _dispatcher(sink, gateway=gateway, settings=settings)

    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="generalist", task="do it")))

    assert gateway.requests[0].params["kpi_tag"] == "platform-baseline"


def test_spec_field_named_kpi_tag_is_dropped_and_never_becomes_an_attribute() -> None:
    """``SubAgentSpec`` is model-emitted and therefore untrusted, and it has no
    ``kpi_tag`` field.

    NOTE the exact guarantee: ``ne.contracts`` models are ``frozen=True`` with
    pydantic's DEFAULT ``extra='ignore'``, so a spec carrying ``kpi_tag`` is
    silently DROPPED, not rejected. That is weaker than a loud refusal — this
    test pins the property that actually protects attribution (the value never
    becomes readable state), not a rejection the contract does not make.
    Tightening ``_Frozen`` to ``extra='forbid'`` is a breaking change to a frozen
    package (additive-only + ADR + version bump) and is deliberately not done here.
    """
    spec = SubAgentSpec(agent="renewals", task="t", kpi_tag="board-priority")  # type: ignore[call-arg]

    assert not hasattr(spec, "kpi_tag")
    assert "kpi_tag" not in spec.model_dump()


def test_spec_context_named_kpi_tag_does_not_change_the_stamped_outcome() -> None:
    """``spec.context`` IS a sanctioned model→child channel (it is rendered into
    the prompt). A model that plants ``kpi_tag`` there must still spend under the
    outcome its DEFINITION declares."""
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("drafted")])
    dispatcher = _dispatcher(sink, gateway=gateway)

    asyncio.run(
        dispatcher.dispatch(
            SubAgentSpec(
                agent="renewals",
                task="draft it",
                context={"kpi_tag": "board-priority"},
            )
        )
    )

    assert gateway.requests[0].params["kpi_tag"] == "renewal-retention"


def test_route_audit_records_the_tag_and_where_it_was_declared() -> None:
    """The routing audit already records ``task_source``/``lane_source``; the
    outcome attribution is a governed decision too (C10) and is recorded the
    same way, so a reviewer can see which config named the outcome."""
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("drafted")])
    dispatcher = _dispatcher(sink, gateway=gateway)

    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="renewals", task="draft it")))

    (route_event,) = sink.with_action("subagent.route")
    detail = json.loads(route_event.detail)
    assert detail["kpi_tag"] == "renewal-retention"
    assert detail["kpi_tag_source"] == "definition"


def test_route_audit_names_settings_as_the_source_when_inherited() -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("done")])
    dispatcher = _dispatcher(sink, gateway=gateway, settings=RuntimeSettings(kpi_tag="baseline"))

    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="generalist", task="do it")))

    (route_event,) = sink.with_action("subagent.route")
    detail = json.loads(route_event.detail)
    assert detail["kpi_tag"] == "baseline"
    assert detail["kpi_tag_source"] == "settings"


def test_parent_settings_untouched_by_a_tagged_dispatch() -> None:
    """The per-child tag rides a ``model_copy``; the shared parent settings
    object is never mutated (the existing routing discipline)."""
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("drafted")])
    settings = RuntimeSettings(kpi_tag="platform-baseline")
    before = settings.model_dump_json()
    dispatcher = _dispatcher(sink, gateway=gateway, settings=settings)

    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="renewals", task="draft it")))

    assert settings.model_dump_json() == before
