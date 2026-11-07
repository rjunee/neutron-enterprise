# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-168 — the governed multi-lane agent-to-model delegation demo trigger.

``POST /v1/demo/delegate`` is the deterministic, ops-gated entrypoint that makes
``ne demo`` VISIBLY exercise governed delegation: it dispatches the 3 registered
sub-agents (``triage``/``reason`` -> private-vllm, ``adjudicate`` -> frontier-api)
inside ONE :func:`~ne.runtime.correlation.run_trace_scope`, so every
``subagent.route`` audit event they emit shares a single ``trace_id`` — readable
back via ``GET /v1/trace/{trace_id}`` (NE-167a).

Gated identically to the trace-read route it feeds (the ``routers/trace.py``
``_require_ops`` precedent) — the delegation trigger is an ops surface. Absence of
the demo flag is the honest 404 (the ``routers/graph.py`` precedent): the demo
seam is default-off, never a silently-degraded endpoint.

The trigger is DETERMINISTIC — fixed agent order, fixed task texts — so it never
depends on a weak CPU model choosing to call a tool; the delegation ITSELF (the
dispatch through the C6-governed ``GovernedSubAgentDispatcher``, the per-child
routing resolution, the PDP ROUTE AUTHORITY consult (NE-169), and the frontier
RUNTIME GATE) is REAL and governed. The ``adjudicate`` leg's frontier route is
now PDP-CHECKED and policy-DENIED by the ``ne.govern`` ``subagent.route`` rule
(NE-169: no grant, default deny) BEFORE the RUNTIME GATE is even reached — the
gate remains a second, independent defense-in-depth layer that also denies it.

The response carries IDENTIFIERS/ENUMS/BOOLS ONLY — never model output text or
task prompts (the ``dispatch.py`` redaction discipline, C12).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ne.api.demo_seams import demo_agent_definitions
from ne.api.deps import ApiState, api_state, current_principal
from ne.api.routers.audit import _require_ops
from ne.contracts.runtime import SubAgentSpec
from ne.contracts.types import Principal
from ne.runtime.correlation import run_trace_scope
from ne.runtime.dispatch import FrontierRoutingDisabledError
from ne.runtime.errors import SubAgentRoutePolicyDeniedError

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]

#: Fixed dispatch order + fixed task texts (DETERMINISTIC — never model-chosen).
_LEGS: tuple[tuple[str, str], ...] = (
    ("triage", "Classify the priority of the demo incident report."),
    ("reason", "Draft a one-sentence action plan for the demo incident."),
    ("adjudicate", "Adjudicate whether the action plan may be released externally."),
)


class DelegateLegView(BaseModel):
    """One dispatched sub-agent leg — identifiers/enums/bools only (C12)."""

    agent: str
    route_task: str | None
    route_lane: str | None
    tier: str
    outcome: str
    succeeded: bool


class DelegateRunView(BaseModel):
    """``POST /v1/demo/delegate`` response: the shared run trace_id + per-leg outcomes."""

    trace_id: str
    legs: list[DelegateLegView]


def _tier(route_lane: str | None) -> str:
    return "frontier" if route_lane == "frontier-api" else "private"


@router.post("/v1/demo/delegate")
async def delegate(
    principal: _Principal,
    state: _State,
) -> DelegateRunView:
    await _require_ops(principal, state)
    if state.demo_dispatcher is None:
        raise HTTPException(status_code=404, detail="delegation demo not enabled")

    definitions = demo_agent_definitions()
    legs: list[DelegateLegView] = []
    with run_trace_scope() as trace_id:
        for agent, task in _LEGS:
            definition = definitions[agent]
            tier = _tier(definition.route_lane)
            try:
                result = await state.demo_dispatcher.dispatch(SubAgentSpec(agent=agent, task=task))
                outcome = "completed" if result.succeeded else "error"
                succeeded = result.succeeded
            except SubAgentRoutePolicyDeniedError:
                outcome = "denied-route-policy"
                succeeded = False
            except FrontierRoutingDisabledError:
                outcome = "denied-frontier-routing"
                succeeded = False
            legs.append(
                DelegateLegView(
                    agent=agent,
                    route_task=definition.route_task,
                    route_lane=definition.route_lane,
                    tier=tier,
                    outcome=outcome,
                    succeeded=succeeded,
                )
            )
    return DelegateRunView(trace_id=trace_id, legs=legs)
