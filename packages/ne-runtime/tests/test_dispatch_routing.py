# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Per-child model routing keyed on AgentDefinition (NE-161, design §10.2).

Covers: routing to each tier (frontier/private/unpinned) off the TRUSTED
AgentDefinition; the narrow-only SubAgentSpec.route_lane_hint clamp (widen to
FRONTIER_API is always rejected; narrow to PRIVATE_VLLM always succeeds);
parent-settings byte-identity after a routed dispatch (model_copy, never
mutate); the always-emitted subagent.route audit (tier + trace_id); the
RUNTIME GATE that refuses a resolved 'frontier' tier unless the dispatcher
explicitly opts in via allow_frontier_subagent_routing (code-enforced, not
just documented); C6 fail-closed on an unrecordable routing decision; the
fail-loud unconfigured-route-task behavior over the REAL LiteLLMGateway; and
(NE-169) the ROUTE AUTHORITY PDP consult at the routing decision — a private
route is PDP-PERMITTED and the effect/policy_version are threaded into the
ONE subagent.route audit event, a frontier route is PDP-CHECKED and DENIED
BY DEFAULT (no grant) before the RUNTIME GATE is even reached, and a
require-approval effect also withholds the child (strict is-not-ALLOW).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from ne.contracts import Lane
from ne.contracts.runtime import SubAgentSpec
from ne.gateway import GatewaySettings, LiteLLMGateway, ModelDef, TaskRoute
from ne.runtime.config import RuntimeSettings
from ne.runtime.dispatch import (
    AgentDefinition,
    FrontierRoutingDisabledError,
    GovernedSubAgentDispatcher,
)
from ne.runtime.errors import RuntimeAuditError, SubAgentRoutePolicyDeniedError
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.processes import ProcessRegistry
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
)

# NE-169: a scripted PDP override that ALLOWS a frontier-resolved subagent.route
# (the real bundle default-denies frontier — see subagent.rego). Tests that
# exercise the RUNTIME GATE mechanics on an "adjudicator" (frontier-bound)
# dispatch need this override so the PDP consult doesn't deny the route BEFORE
# the gate is even reached.
_ALLOW_FRONTIER_ROUTE = {
    "subagent.route": {
        "effect": "allow",
        "tier": 0,
        "reason": "test-grant: frontier route allowed",
        "obligations": {},
        "policy_version": "1.1.0",
    }
}

_DEFINITIONS = {
    "adjudicator": AgentDefinition(
        name="adjudicator",
        instructions="You adjudicate.",
        toolset=(),
        route_task="ap.adjudicate",
        route_lane="frontier-api",
    ),
    "extractor": AgentDefinition(
        name="extractor",
        instructions="You extract.",
        toolset=(),
        route_task="ap.extract",
        route_lane="private-vllm",
    ),
    "researcher": AgentDefinition(
        name="researcher",
        instructions="You research things.",
        toolset=(),
    ),
}


def _dispatcher(
    sink: Any,
    *,
    gateway: Any,
    registry: ProcessRegistry | None = None,
    settings: RuntimeSettings | None = None,
    allow_frontier_subagent_routing: bool = True,
    pdp_overrides: dict[str, dict[str, Any]] | None = None,
) -> GovernedSubAgentDispatcher:
    # NOTE: tests default the RUNTIME GATE OPEN (allow_frontier_subagent_routing=True)
    # so the "adjudicator" frontier-bound definition is demonstrable end-to-end, as
    # the acceptance criteria below require. test_frontier_routing_blocked_by_default
    # is the one test that builds the dispatcher WITHOUT this override, proving the
    # gate defaults CLOSED for any composition root that doesn't explicitly opt in.
    log = ToolLog()
    pdp = make_pdp(sink, overrides=pdp_overrides)
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
        allow_frontier_subagent_routing=allow_frontier_subagent_routing,
    )


def test_child_routes_to_definition_frontier_task_and_lane() -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("adjudicated")])
    dispatcher = _dispatcher(sink, gateway=gateway, pdp_overrides=_ALLOW_FRONTIER_ROUTE)
    result = asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="adjudicator", task="review")))

    assert result.succeeded is True
    assert gateway.requests[0].task == "ap.adjudicate"
    assert gateway.requests[0].lane is Lane.FRONTIER_API


def test_child_routes_to_private_lane_and_default_inherits_parent() -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("extracted"), text_completion("researched")])
    dispatcher = _dispatcher(sink, gateway=gateway)

    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="extractor", task="pull")))
    assert gateway.requests[0].task == "ap.extract"
    assert gateway.requests[0].lane is Lane.PRIVATE_VLLM

    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="dig")))
    assert gateway.requests[1].task == "agent"  # settings.task default, inherited
    assert gateway.requests[1].lane is None


def test_spec_hint_narrows_frontier_definition_to_private() -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("adjudicated")])
    dispatcher = _dispatcher(sink, gateway=gateway)
    asyncio.run(
        dispatcher.dispatch(
            SubAgentSpec(agent="adjudicator", task="review", route_lane_hint=Lane.PRIVATE_VLLM)
        )
    )

    assert gateway.requests[0].lane is Lane.PRIVATE_VLLM
    (route_event,) = sink.with_action("subagent.route")
    detail = json.loads(route_event.detail)
    assert detail["lane_source"] == "spec-hint"
    assert detail["hint_clamped"] is False


@pytest.mark.parametrize("agent", ["researcher", "extractor"])
def test_spec_hint_cannot_widen_to_frontier(agent: str) -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("done")])
    dispatcher = _dispatcher(sink, gateway=gateway)
    asyncio.run(
        dispatcher.dispatch(SubAgentSpec(agent=agent, task="t", route_lane_hint=Lane.FRONTIER_API))
    )

    (route_event,) = sink.with_action("subagent.route")
    detail = json.loads(route_event.detail)
    assert detail["hint_clamped"] is True
    if agent == "researcher":
        assert gateway.requests[0].lane is None
        assert detail["tier"] == "unpinned"
    else:
        assert gateway.requests[0].lane is Lane.PRIVATE_VLLM
        assert detail["tier"] == "private"


def test_parent_settings_byte_identical_after_routed_dispatch() -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("adjudicated")])
    settings = RuntimeSettings()
    before = settings.model_dump_json()
    dispatcher = _dispatcher(
        sink, gateway=gateway, settings=settings, pdp_overrides=_ALLOW_FRONTIER_ROUTE
    )

    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="adjudicator", task="review")))

    assert settings.model_dump_json() == before
    assert dispatcher._settings is settings  # asserting no swap occurred


def test_subagent_route_audit_emitted_with_tier_and_trace_id() -> None:
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("adjudicated")])
    dispatcher = _dispatcher(sink, gateway=gateway, pdp_overrides=_ALLOW_FRONTIER_ROUTE)
    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="adjudicator", task="review")))

    routed = sink.with_action("subagent.route")
    assert len(routed) == 1
    detail = json.loads(routed[0].detail)
    assert detail["route_task"] == "ap.adjudicate"
    assert detail["route_lane"] == "frontier-api"
    assert detail["tier"] == "frontier"
    assert detail["hint_clamped"] is False
    assert detail["pdp_effect"] == "allow"

    (dispatched,) = sink.with_action("subagent.dispatch")
    assert routed[0].trace_id is not None
    assert routed[0].trace_id == dispatched.trace_id


def test_frontier_routing_blocked_by_default() -> None:
    """RUNTIME GATE (Codex P1 fix): a dispatcher that does NOT opt into
    ``allow_frontier_subagent_routing`` must refuse a definition-resolved
    'frontier' tier BEFORE the child ever calls the model — a comment alone
    is not enforcement.

    NE-169: this test grants a PDP allow via override so the ROUTE AUTHORITY
    consult passes — proving the RUNTIME GATE is INDEPENDENT defense-in-depth
    that still denies even when the PDP itself allowed the route.
    """
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("never")])
    dispatcher = _dispatcher(
        sink,
        gateway=gateway,
        allow_frontier_subagent_routing=False,
        pdp_overrides=_ALLOW_FRONTIER_ROUTE,
    )

    with pytest.raises(FrontierRoutingDisabledError):
        asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="adjudicator", task="review")))

    assert gateway.requests == []
    (route_event,) = sink.with_action("subagent.route")
    assert route_event.outcome == AuditOutcome.denied.value
    detail = json.loads(route_event.detail)
    assert detail["tier"] == "frontier"
    assert detail["reason"] == "frontier_routing_disabled"
    assert detail["pdp_effect"] == "allow"


def test_route_audit_append_failure_withholds_child_run() -> None:
    # NE-169: the PDP allow override keeps this test on the SUCCESS-emit path
    # (the route is not itself denied by policy) so the failure exercised here
    # is specifically the middleware's own audit append — C6 fail-closed still
    # holds with the ROUTE AUTHORITY consult in the path.
    sink = AppendCountFailingSink(fail_on_action="subagent.route")
    gateway = ScriptedGateway([text_completion("never")])
    dispatcher = _dispatcher(sink, gateway=gateway, pdp_overrides=_ALLOW_FRONTIER_ROUTE)

    with pytest.raises(RuntimeAuditError):
        asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="adjudicator", task="review")))

    assert gateway.requests == []


def test_private_route_pdp_permitted_and_effect_audited() -> None:
    """NE-169 ACCEPTANCE: a child routed to a PRIVATE lane is PDP-PERMITTED and
    the effect + policy_version are threaded into the ONE subagent.route audit
    event — no override needed, the ne.govern bundle (mirrored here) permits
    private/unpinned resolved lanes by default."""
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("extracted")])
    dispatcher = _dispatcher(sink, gateway=gateway)

    result = asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="extractor", task="pull")))

    assert result.succeeded is True
    (route_event,) = sink.with_action("subagent.route")
    assert route_event.outcome == AuditOutcome.success.value
    detail = json.loads(route_event.detail)
    assert detail["pdp_effect"] == "allow"
    assert detail["policy_version"] == "1.1.0"

    pdp_decisions = sink.with_action("pdp.decide")
    assert any(e.provenance.get("requested_action") == "subagent.route" for e in pdp_decisions)


def test_frontier_route_pdp_denied_by_default() -> None:
    """NE-169 ACCEPTANCE: a child routed to FRONTIER is PDP-CHECKED and DENIED
    BY DEFAULT (no grant) — even with the RUNTIME GATE open
    (allow_frontier_subagent_routing=True), the PDP consult withholds the
    child BEFORE the gate is reached and BEFORE any model call."""
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("never")])
    dispatcher = _dispatcher(sink, gateway=gateway, allow_frontier_subagent_routing=True)

    with pytest.raises(SubAgentRoutePolicyDeniedError):
        asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="adjudicator", task="review")))

    assert gateway.requests == []
    (route_event,) = sink.with_action("subagent.route")
    assert route_event.outcome == AuditOutcome.denied.value
    detail = json.loads(route_event.detail)
    assert detail["pdp_effect"] == "deny"
    assert detail["reason"] == "route_policy_denied"
    assert detail["tier"] == "frontier"


def test_route_pdp_require_approval_effect_withholds_child() -> None:
    """A require-approval effect ALSO withholds the child (strict is-not-ALLOW,
    fail closed) — routing has no human-in-the-loop leg."""
    sink = RecordingAuditSink()
    gateway = ScriptedGateway([text_completion("never")])
    overrides = {
        "subagent.route": {
            "effect": "require-approval",
            "tier": 2,
            "reason": "x",
            "obligations": {},
            "policy_version": "1.1.0",
        }
    }
    dispatcher = _dispatcher(sink, gateway=gateway, pdp_overrides=overrides)

    with pytest.raises(SubAgentRoutePolicyDeniedError):
        asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="extractor", task="pull")))

    assert gateway.requests == []


def test_invalid_route_lane_on_definition_rejected_at_registration() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentDefinition(name="x", instructions="i", route_lane="cloud")


class _RecordingCostSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


def test_unconfigured_route_task_fails_loud_over_real_gateway() -> None:
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
    cost_sink = _RecordingCostSink()
    audit_sink = RecordingAuditSink()
    gw = LiteLLMGateway(gateway_settings, cost_sink, audit_sink=audit_sink)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        raise AssertionError("no model call should occur for an unconfigured task")

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
    ghost_definitions = {
        "ghost": AgentDefinition(
            name="ghost",
            instructions="You are unreachable.",
            toolset=(),
            route_task="ghost.task",
        ),
    }
    dispatcher = GovernedSubAgentDispatcher(
        definitions=ghost_definitions,
        executor=executor,
        gateway=gw,
        registry=ProcessRegistry(),
        sink=audit_sink,
        settings=RuntimeSettings(),
        principal=make_principal(),
        pdp=pdp,
    )

    result = asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="ghost", task="t")))

    assert result.succeeded is False
    assert "UnknownTaskError" in result.output
    assert cost_sink.events == []
