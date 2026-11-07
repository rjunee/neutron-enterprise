# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Read-only report of one Agent's declared facts and allowed model choices.

This module is deliberately free of HTTP and composition-root imports.  A caller
injects the boot Agent catalog, the existing :class:`ModelSelectionService`, the
already-governed PDP, and the route provider built by
:func:`ne.api.model_routing.build_route_provider`.  The console's Agent panel and
the later submit path therefore read one mechanism instead of re-deriving
strategy, budget or availability facts independently.

This is a REPORT, not a launch gate.  It answers "what does this Agent declare,
and which models is this principal currently allowed to pick for it" — it does
NOT answer "may this Agent run".  The catalog's own ``available`` verdict and
``availability_reasons`` are copied through verbatim precisely because this
service must not adjudicate them.  The boot composition ships both seeded Agents
unavailable, and it does so for reasons that move independently: a verified Skill
pin CAN now be satisfied at composition, while ``build_platform`` still hands the
catalog empty Knowledge and workflow-profile sets.  So the reason set an Agent
carries is a live fact about this deployment, not a fixed one — and refusing
every unavailable Agent here would make the report inert while telling the caller
nothing.  Deciding that a run may start belongs to the launch path.

Five properties this report is built around:

* it is not an existence oracle.  ``AgentCatalog.get_visible`` already answers
  hidden and unknown identically, and this service returns that one ``None``
  before it reads any route, probes any serving row or asks the PDP anything;
* it is not a second availability authority.  Candidates are exactly the
  ``ModelSelectionService`` catalog, filtered by the same ``read.model.select``
  PDP request ``authorize`` sends, under the SAME effective AIR_GAP value the
  catalog was computed from;
* it fails whole, never partial.  A missing availability verdict or an
  unreachable PDP raises; a shortened candidate list would read as "these are
  the models you may use" and would be a lie;
* it decides nothing.  No ``model.selection`` evidence is appended and
  ``authorize`` is never called, so the submit-time gate re-runs unchanged.
  (The production PDP records each ``decide`` call on the chain; a preflight over
  N candidates is therefore N governed reads, not zero audit activity.)
* it never reports model facts without the dependency verdict beside them.
  ``available`` and the exact ``availability_reasons`` travel with the model
  facts, so a caller has what it needs to avoid presenting an Agent whose
  dependencies are missing as runnable.  Whether it does so is the caller's,
  and this module cannot constrain that.

The default is the route in force, never a substitute: when the primary is not
in the allowed candidate set the projection reports no default rather than
promoting a fallback the operator did not route.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from ne.api.agent_catalog import AgentCatalog
from ne.api.model_selection import (
    MODEL_SELECTION_ACTION,
    ModelChoice,
    ModelSelectionEvidenceError,
    ModelSelectionService,
    UnknownModelSelection,
)
from ne.contracts.governance import DecisionEffect, PolicyDecisionPoint, PolicyRequest
from ne.contracts.types import Principal
from ne.gateway.config import TaskRoute

__all__ = [
    "AgentModelCandidate",
    "AgentModelPreflight",
    "AgentModelPreflightService",
]


@dataclass(frozen=True)
class AgentModelCandidate:
    """One currently-evidenced model this principal is allowed to pick."""

    model: str
    provider: str
    lane: str
    evidence: str
    observed_at: datetime
    #: This model's OWN input budget --
    #: ``context_window - max_output_tokens - overhead_margin`` -- which is the
    #: exact number ``ne.gateway.gateway`` compares an estimated prompt against
    #: before it calls the provider.  ``None`` only where the boot registry does
    #: not carry the model.
    #:
    #: WHAT THIS IS NOT, STATED BECAUSE THE OBVIOUS READING IS WRONG.  It is NOT
    #: joined here against the Agent's declared budgets, and in particular not
    #: against ``max_input_tokens``, which is a WHOLE-TURN total spread over
    #: every request of the turn -- no serving window is ever asked to hold it,
    #: so comparing the two answers nothing.  The comparison that WOULD be
    #: meaningful is against one final-answer request at the Agent's ceiling,
    #: and this deployment cannot compute that honestly: the manifest's budget
    #: derivations are written in MODEL tokens (chars / 2.93) while every
    #: enforcement site -- the gateway pre-flight and the runtime trim -- uses
    #: ``ByteTokenEstimator`` (bytes / 1.5 + 8) over the SERIALIZED request.
    #: Measured on this tree, those two units differ by ~1.96x on the Meridian
    #: catalog, so a fit verdict derived from the manifest figures would be
    #: wrong by roughly half AND wrong in the unsafe direction.  See
    #: ``packages/ne-api/tests/test_agent_budget_units.py``, which pins the
    #: discrepancy so it cannot be "fixed" by quietly reusing the wrong divisor.
    input_budget: int | None = None


@dataclass(frozen=True)
class AgentModelPreflight:
    """The exact strategy, budget and model facts one principal may see."""

    agent_id: str
    agent_version: int
    definition_hash: str
    available: bool
    availability_reasons: tuple[str, ...]
    task: str
    allowed_strategies: tuple[str, ...]
    default_strategy: str
    max_steps: int
    max_tool_calls: int
    max_input_tokens: int
    max_output_tokens: int
    candidates: tuple[AgentModelCandidate, ...]
    default_model: str | None


class AgentModelPreflightService:
    """Projects one Agent definition against live, governed model availability."""

    def __init__(
        self,
        *,
        agent_catalog: AgentCatalog,
        selection: ModelSelectionService,
        pdp: PolicyDecisionPoint,
        route_provider: Callable[[], Mapping[str, TaskRoute]],
    ) -> None:
        self._agent_catalog = agent_catalog
        self._selection = selection
        self._pdp = pdp
        self._route_provider = route_provider

    async def preflight(
        self,
        *,
        agent_id: str,
        principal: Principal,
    ) -> AgentModelPreflight | None:
        """Return the projection, or ``None`` for hidden AND unknown alike."""
        agent = self._agent_catalog.get_visible(agent_id, principal)
        if agent is None:
            # Deliberately the FIRST statement with an exit: no probe, no audit
            # read, no policy question distinguishes "not yours" from "no such
            # Agent" by cost, side effect or shape.
            return None

        definition = agent.definition
        # The task is the Agent's declared model policy.  A literal here would
        # silently project a second Agent's availability onto this one.
        task = definition.model_policy_id
        catalog, air_gap = await self._selection.choices_with_air_gap(task)

        candidates: list[AgentModelCandidate] = []
        for choice in catalog.choices:
            if choice.evidence is None or choice.observed_at is None:
                # This projection reports MEASURED candidates, and the measured
                # catalog always carries both fields.  An unmeasured choice is
                # dropped rather than reported with an invented observation.
                continue
            if await self._is_allowed(choice, task=task, principal=principal, air_gap=air_gap):
                try:
                    input_budget: int | None = self._selection.input_budget(choice.model)
                except UnknownModelSelection:
                    # A candidate the boot registry does not carry: report the
                    # model, withhold the budget.  Dropping the candidate would
                    # understate what this principal may actually pick.
                    input_budget = None
                candidates.append(
                    AgentModelCandidate(
                        model=choice.model,
                        provider=choice.provider,
                        # A plain ``str``, not the ``Lane`` member: a consumer
                        # comparing this field must not have to import the enum.
                        # (``Lane`` is a ``StrEnum``, so this is NOT about JSON
                        # bytes -- those are identical either way.)
                        lane=choice.lane.value,
                        evidence=choice.evidence,
                        observed_at=choice.observed_at,
                        input_budget=input_budget,
                    )
                )
        ordered = tuple(candidates)

        # ``build_route_provider`` already fails CLOSED to the boot routes and
        # never raises, so no second fallback layer is written on top of it.  The
        # constructor accepts any ``Callable`` though, and a provider wider than
        # that guarantee must not escape as a raw exception: an unreadable route
        # register fails the WHOLE report, exactly like unreadable availability.
        try:
            routes = self._route_provider()
        except Exception as exc:
            raise ModelSelectionEvidenceError(
                "agent model preflight route register is unavailable"
            ) from exc
        route = routes.get(task)
        primary = route.model if route is not None else None
        default_model = (
            primary
            if primary is not None and any(item.model == primary for item in ordered)
            else None
        )

        budgets = definition.budgets
        return AgentModelPreflight(
            agent_id=definition.agent_id,
            agent_version=definition.version,
            definition_hash=definition.definition_hash,
            # Copied, never re-derived and never acted on here: this service
            # reports the catalog's dependency verdict, it does not gate on it.
            available=agent.available,
            availability_reasons=agent.availability_reasons,
            task=task,
            allowed_strategies=tuple(definition.allowed_strategies),
            default_strategy=definition.default_strategy,
            max_steps=budgets.max_steps,
            max_tool_calls=budgets.max_tool_calls,
            max_input_tokens=budgets.max_input_tokens,
            max_output_tokens=budgets.max_output_tokens,
            candidates=ordered,
            default_model=default_model,
        )

    async def _is_allowed(
        self,
        choice: ModelChoice,
        *,
        task: str,
        principal: Principal,
        air_gap: bool,
    ) -> bool:
        """The identical request ``ModelSelectionService.authorize`` sends."""
        try:
            decision = await self._pdp.decide(
                PolicyRequest(
                    principal=principal,
                    action=MODEL_SELECTION_ACTION,
                    resource=f"model:{choice.model}",
                    context={
                        "task": task,
                        "lane": choice.lane.value,
                        "air_gap": air_gap,
                    },
                )
            )
        except Exception as exc:
            # One unreachable verdict invalidates the WHOLE list: dropping just
            # this candidate would present the remainder as a complete answer.
            raise ModelSelectionEvidenceError(
                "agent model preflight policy is unavailable"
            ) from exc
        # DENY and REQUIRE_APPROVAL are absent, not annotated: a reason string
        # would tell the caller which models exist but are withheld from them.
        return decision.effect is DecisionEffect.ALLOW
