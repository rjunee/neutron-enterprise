# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GovernedSubAgentDispatcher — typed, principal-bound sub-agent fan-out (NE-034).

Implements the frozen ``ne.contracts.runtime.SubAgentDispatcher`` protocol. Every
dispatched sub-agent runs as an ISOLATED governed agent (empty history, no session
store — memory isolation by construction) over a
:class:`~ne.runtime.toolset.ToolsetScopedExecutor` gated to the agent definition's
toolset, so its every tool call still routes through the ONE
:class:`GovernedToolExecutor` (C6). Out-of-set calls are audited + PDP-consulted +
terminally refused; the handler never runs.

Caps are SERVER-SIDE (orchestrator hard constraint):

* **Depth** — the dispatcher carries a ``_budget`` seeded from
  ``settings.dispatch.max_depth`` and MIN-composed with the spec's ``max_depth`` in
  :meth:`child_for`, so a lying ``spec.max_depth`` can only NARROW the budget, never
  widen it. A dispatch with an exhausted budget is rejected (``subagent.cap_rejected``,
  ``reason='depth'``, ``scope='server'``).
* **Global concurrency** — the :class:`~ne.runtime.processes.ProcessRegistry` counts
  live sub-agents and rejects registration atomically at
  ``settings.dispatch.max_concurrent_subagents`` (``scope='server'``); the count is
  the registry's, never trusted from the spec.
* **Local fan-out** — a per-dispatcher ``_active_local`` bounds concurrent children
  to ``max(1, spec.max_concurrency)`` (``scope='spec'`` — a spec may only narrow it).

Redaction discipline: dispatch/complete/cap-rejection audit ``detail`` and the
failed-run ``output`` carry ONLY agent names, depths, reasons, and error CLASSES —
never raw task text, context values, or exception messages.

Per-child routing (NE-161): a governed sub-agent may route to a DIFFERENT
gateway task/lane than its parent, but the binding is keyed ONLY on the
TRUSTED, server-registered :class:`AgentDefinition` (``route_task`` /
``route_lane``) — never on the model-emitted, untrusted ``SubAgentSpec``.
``SubAgentSpec.route_lane_hint`` (ne-contracts 1.5.0, ADR-0008) may only
NARROW a child toward ``Lane.PRIVATE_VLLM``; any attempt to widen toward
``Lane.FRONTIER_API`` is deterministically CLAMPED back to the
``AgentDefinition``-derived lane in :meth:`GovernedSubAgentDispatcher._resolve_route`
— mirroring the existing ``max_depth``/``max_concurrency`` MIN-compose
discipline (§10.2, C12). Every dispatch ALWAYS emits a ``subagent.route``
audit event (agent, resolved task/lane, tier, source enums, and the
``hint_clamped`` bool — identifiers only, never prompt text) before the child
run starts; an append failure raises ``RuntimeAuditError`` and withholds the
child (C6 fail-closed). A per-child ``RuntimeSettings`` is built via
``settings.model_copy(update={...})`` and handed to ``run_isolated_agent`` —
the shared ``self._settings`` is NEVER mutated. The child already runs inside
the parent's ``run_trace_scope`` (NE-166), so its model calls and its
``subagent.route`` event inherit the run's ``trace_id`` automatically — no
extra correlation plumbing is needed here.

RUNTIME GATE (CODE-ENFORCED, not merely documented): resolving a child's
route to ``Lane.FRONTIER_API`` raises :class:`FrontierRoutingDisabledError`
and is audited as a ``subagent.route`` DENIAL unless the dispatcher was
constructed with ``allow_frontier_subagent_routing=True``. The default is
``False``, so a composition root that registers a ``route_lane='frontier-api'``
``AgentDefinition`` WITHOUT deliberately opting in gets an inert child, not a
live frontier egress — closing the exfiltration path a comment alone cannot
close. Flip the flag on only once NE-162's pre-egress redaction is wired in;
until then this unit ships private/open-weight routing live, with frontier
routing code-complete but opt-in-gated.

ROUTE AUTHORITY (NE-169): the PDP is consulted at the routing decision —
right after the child's lane is resolved and BEFORE any child dispatch — via
``PolicyRequest(action="subagent.route", context={route_task, route_lane,
route_tier})``. The decision's ``effect`` and ``policy_version`` are threaded
into the SAME ``subagent.route`` audit event emitted below (never a second
event). Any effect OTHER than ``DecisionEffect.ALLOW`` — a ``deny``, or a
``require-approval`` (routing has no human-in-the-loop leg) — withholds the
child: the denial is audited and :class:`FrontierRoutingDisabledError`'s
sibling, :class:`~ne.runtime.errors.SubAgentRoutePolicyDeniedError`, is
raised before any model call. The ``ne.govern`` bundle (1.1.0) classifies
``subagent.route``: a private or unpinned resolved lane is tier-0 ALLOW; a
frontier-resolved lane is a total DENY — an explicit grant mechanism
(a principal attribute or a policy tier) is a deliberate, named FOLLOW-UP
PARAMETER, not invented in this unit. The NE-161 RUNTIME GATE below remains
as independent defense-in-depth AFTER the PDP consult — a PDP allow does not
by itself open frontier egress; ``allow_frontier_subagent_routing=True`` is
still required, and is never flipped by this unit.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from ne.contracts.governance import AuditSink, DecisionEffect, PolicyDecisionPoint, PolicyRequest
from ne.contracts.models import ModelGateway
from ne.contracts.runtime import SubAgentResult, SubAgentSpec
from ne.contracts.types import Lane, Principal
from ne.events import new_audit_event
from ne.govern.errors import AuditEmissionError
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import current_run_trace_id, run_trace_scope
from ne.runtime.errors import (
    DispatchCapExceededError,
    NeRuntimeError,
    RuntimeAuditError,
    SubAgentRoutePolicyDeniedError,
    UnknownAgentTypeError,
)
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.processes import ProcessRegistry
from ne.runtime.runner import run_isolated_agent
from ne.runtime.source_results import ProtectedSourceResultGuard
from ne.runtime.toolset import ToolsetScopedExecutor
from ne.schema import AuditOutcome

_PROVENANCE_SOURCE = {"source": "ne-runtime", "component": "dispatch"}


class FrontierRoutingDisabledError(NeRuntimeError):
    """A child resolved to ``Lane.FRONTIER_API`` but frontier sub-agent routing
    is disabled on this dispatcher (NE-161 RUNTIME GATE, code-enforced).

    Raised BEFORE the child ever runs (no model call is issued) whenever
    :meth:`GovernedSubAgentDispatcher._resolve_route` resolves a ``'frontier'``
    tier and the dispatcher was not built with
    ``allow_frontier_subagent_routing=True``. The rejection is audited as a
    ``subagent.route`` DENIAL first (fail-closed discipline mirrors the
    depth/concurrency caps). Flip the flag on only once NE-162's pre-egress
    redaction is wired in for a live composition.
    """


class AgentDefinition(BaseModel):
    """A registered sub-agent type: its instructions and its GRANTED toolset.

    ``toolset`` empty ⇒ ZERO tools (fail closed): a sub-agent with no granted tools
    can call nothing. Frozen + ``extra='forbid'`` so a typo fails loudly.

    ``route_task`` / ``route_lane`` (NE-161, design §10.2) are the TRUSTED,
    server-side routing binding for a child dispatched under this definition.
    ``route_task`` is the GATEWAY routing task (``CompletionRequest.task``) — a
    DIFFERENT namespace from ``SubAgentSpec.task``, which is prompt work-text
    rendered into the child's instructions. ``None`` on either field means the
    child INHERITS the parent ``RuntimeSettings`` value for that field. RUNTIME
    GATE: a ``route_lane='frontier-api'`` definition is CODE-ENFORCED inert
    (see :class:`FrontierRoutingDisabledError`) unless the dispatcher is built
    with ``allow_frontier_subagent_routing=True`` — flip that on only once
    NE-162's pre-egress redaction is wired in.

    ``kpi_tag`` (C8) is the NAMED BUSINESS OUTCOME this sub-agent's model spend
    is attributed to. It sits here, beside ``route_task``/``route_lane``, for the
    same reason those do: this object is server-registered, frozen and
    ``extra='forbid'``, and it is the single declaration point shared by the agent
    turn path and the workflow path (``ne.api.workflows``' drafting leg dispatches
    through an ``AgentDefinition`` too), both of which converge on ``GatewayModel``'s
    one ``CompletionRequest``. It is NOT the only model call in the platform:
    ne-brain's ingest enricher, ne-brain-graph's extraction job, ne-eval's judge,
    ne-memory-graph's facade and ne-api's grounding-assist each build their own
    request. Of those, grounding-assist stamps ``RuntimeSettings.kpi_tag`` (it is
    part of a governed agent run); the other four are platform-internal pipelines
    that carry no declared outcome today and therefore report as unattributed. A
    ``SubAgentSpec`` — model-emitted, untrusted — carries no tag and
    :meth:`GovernedSubAgentDispatcher._resolve_route` never reads one from it,
    exactly as it never reads a lane widen. ``None`` INHERITS
    ``RuntimeSettings.kpi_tag``; ``None`` on both emits an UNTAGGED call, which
    the reporting path shows as unattributed rather than guessing an owner.

    ``scheduled_execution`` is a server-owned scheduler binding. ``"agent"``
    preserves the ordinary isolated model loop. ``"sole-tool"`` is an explicit
    opt-in for deterministic maintenance jobs: the scheduler invokes the
    definition's exact singleton governed tool once without consulting a model.
    It never widens the tool grant or bypasses PDP, guardrail, or audit execution.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    instructions: str
    toolset: tuple[str, ...] = ()
    route_task: str | None = None
    route_lane: str | None = None
    kpi_tag: str | None = None
    scheduled_execution: Literal["agent", "sole-tool"] = "agent"

    @field_validator("route_lane")
    @classmethod
    def _validate_route_lane(cls, value: str | None) -> str | None:
        # Fails loudly at registration (boot time) rather than at first dispatch.
        return None if value is None else Lane(value).value


def _route_tier(lane: str | None) -> str:
    """Classify a resolved lane string into an audit-friendly tier label."""
    if lane == Lane.FRONTIER_API.value:
        return "frontier"
    if lane == Lane.PRIVATE_VLLM.value:
        return "private"
    return "unpinned"


class GovernedSubAgentDispatcher:
    """Typed, principal-bound, server-capped sub-agent dispatcher (frozen protocol impl)."""

    def __init__(
        self,
        *,
        definitions: Mapping[str, AgentDefinition],
        executor: GovernedToolExecutor,
        gateway: ModelGateway,
        registry: ProcessRegistry,
        sink: AuditSink,
        settings: RuntimeSettings,
        principal: Principal,
        pdp: PolicyDecisionPoint,
        source_guard: ProtectedSourceResultGuard | None = None,
        allow_frontier_subagent_routing: bool = False,
    ) -> None:
        if sink is None:
            raise TypeError("GovernedSubAgentDispatcher requires a non-None audit sink (C6)")
        if pdp is None:
            raise TypeError("GovernedSubAgentDispatcher requires a non-None pdp (C6)")
        self._definitions = dict(definitions)
        self._executor = executor
        self._gateway = gateway
        self._registry = registry
        self._sink = sink
        self._settings = settings
        self._principal = principal
        self._pdp = pdp
        self._source_guard = source_guard
        self._depth = 0
        self._budget = settings.dispatch.max_depth
        self._parent_id: str | None = None
        self._active_local = 0
        # RUNTIME GATE (NE-161, code-enforced): a resolved 'frontier' tier is
        # refused unless explicitly opted into — see FrontierRoutingDisabledError.
        # Defaults False so an unmodified composition root never gets live
        # frontier sub-agent egress just because a definition declares one.
        self._allow_frontier_subagent_routing = allow_frontier_subagent_routing

    async def dispatch(self, spec: SubAgentSpec) -> SubAgentResult:
        """Run ``spec`` as an isolated, toolset-gated governed sub-agent (server-capped).

        The whole body runs inside a run-scoped correlation :func:`run_trace_scope`
        (NE-166): a dispatch nested inside an enclosing scope INHERITS the ambient
        trace_id unchanged; a standalone dispatch mints one, so its own
        dispatch/cap/complete audit events and every child-run event
        (model requests, tool.execute) share ONE non-null id.
        """
        with run_trace_scope():
            settings = self._settings
            principal = self._principal

            # (1) Unknown agent type → audited denial, terminal refusal (fail closed).
            definition = self._definitions.get(spec.agent)
            if definition is None:
                await self._emit(
                    action="subagent.dispatch",
                    outcome=AuditOutcome.denied,
                    actor=principal.id,
                    resource=f"subagent:{spec.agent}",
                    detail={"reason": "unknown agent type", "agent": spec.agent},
                )
                raise UnknownAgentTypeError(
                    f"no AgentDefinition registered for sub-agent type {spec.agent!r}"
                )

            # (2) DEPTH cap — server-side budget (a lying spec can only narrow it).
            if self._budget < 1:
                await self._emit(
                    action="subagent.cap_rejected",
                    outcome=AuditOutcome.denied,
                    actor=principal.id,
                    resource=f"subagent:{spec.agent}",
                    detail={
                        "reason": "depth",
                        "scope": "server",
                        "limit": settings.dispatch.max_depth,
                    },
                )
                raise DispatchCapExceededError(
                    f"sub-agent depth budget exhausted (server max_depth "
                    f"{settings.dispatch.max_depth})",
                    reason="depth",
                    scope="server",
                )

            # (3) LOCAL fan-out cap — spec may only NARROW (min 1). RESERVE the slot in
            # the SAME synchronous step as the check (no intervening await), so two
            # concurrent dispatch() coroutines on ONE event loop can't both pass the
            # check before either reserves (NE-034). The slot is released in the finally
            # below, which dominates EVERY subsequent exit (registry-cap rejection,
            # dispatch-audit failure, run, completion-audit).
            if self._active_local >= max(1, spec.max_concurrency):
                await self._emit(
                    action="subagent.cap_rejected",
                    outcome=AuditOutcome.denied,
                    actor=principal.id,
                    resource=f"subagent:{spec.agent}",
                    detail={"reason": "concurrency", "scope": "spec"},
                )
                raise DispatchCapExceededError(
                    f"local sub-agent fan-out cap {max(1, spec.max_concurrency)} reached",
                    reason="concurrency",
                    scope="spec",
                )
            self._active_local += 1
            try:
                return await self._dispatch_reserved(spec, definition)
            finally:
                self._active_local -= 1

    async def _dispatch_reserved(
        self, spec: SubAgentSpec, definition: AgentDefinition
    ) -> SubAgentResult:
        """Run the dispatch with the local fan-out slot already reserved (NE-034)."""
        settings = self._settings
        principal = self._principal

        # (4) GLOBAL concurrency — the registry counts live sub-agents and rejects
        # atomically at the server cap (the count is the registry's, not the spec's).
        cap = settings.dispatch.max_concurrent_subagents
        try:
            record = self._registry.register(
                kind="subagent",
                name=spec.agent,
                principal_id=principal.id,
                parent_id=self._parent_id,
                depth=self._depth + 1,
                concurrency_cap=cap,
            )
        except DispatchCapExceededError:
            await self._emit(
                action="subagent.cap_rejected",
                outcome=AuditOutcome.denied,
                actor=principal.id,
                resource=f"subagent:{spec.agent}",
                detail={"reason": "concurrency", "scope": "server", "limit": cap},
            )
            raise

        succeeded = False
        try:
            # (5) Dispatch audit BEFORE the run (fail closed: a RuntimeAuditError here
            # aborts the dispatch — the outer finally still finishes the registry record).
            await self._emit(
                action="subagent.dispatch",
                outcome=AuditOutcome.success,
                actor=principal.id,
                resource=f"subagent:{spec.agent}",
                detail={"agent": spec.agent, "depth": self._depth + 1},
            )

            # (5b) Resolve per-child routing off the TRUSTED AgentDefinition (NE-161,
            # §10.2) and audit the decision BEFORE the child run starts (fail closed:
            # a RuntimeAuditError here aborts the dispatch, same discipline as (5)).
            route_task, route_lane, route_kpi_tag, route_detail = self._resolve_route(
                spec, definition
            )

            # (5c) ROUTE AUTHORITY (NE-169, §10.2): consult the PDP at the exact
            # point the child's lane is resolved, BEFORE any child dispatch. Context
            # carries ONLY identifiers/enums (redaction discipline) — never prompt
            # text or context values. GovernedPdp audits its OWN pdp.decide event by
            # construction; a PolicyBackendError / AuditEmissionError from decide()
            # propagates UNWRAPPED here (fail closed — already audited by the PDP).
            decision = await self._pdp.decide(
                PolicyRequest(
                    principal=principal,
                    action="subagent.route",
                    resource=f"subagent:{spec.agent}",
                    context={
                        "route_task": route_task,
                        "route_lane": route_lane,
                        "route_tier": route_detail["tier"],
                    },
                )
            )
            route_detail["pdp_effect"] = decision.effect.value
            route_detail["policy_version"] = decision.policy_version

            if decision.effect is not DecisionEffect.ALLOW:
                # Fail closed on ANY non-ALLOW effect (deny OR require-approval —
                # routing has no HITL leg): audit the ONE subagent.route event as a
                # denial, carrying the PDP's effect, then withhold the child.
                await self._emit(
                    action="subagent.route",
                    outcome=AuditOutcome.denied,
                    actor=principal.id,
                    resource=f"subagent:{spec.agent}",
                    detail={
                        **route_detail,
                        "reason": "route_policy_denied",
                        "pdp_reason": decision.reason,
                    },
                )
                raise SubAgentRoutePolicyDeniedError(
                    f"sub-agent {spec.agent!r} route to lane {route_lane!r} "
                    f"(tier {route_detail['tier']!r}) was not allowed by policy "
                    f"(effect: {decision.effect.value}); the denial was audited and "
                    "the child was withheld"
                )

            # RUNTIME GATE (code-enforced): a 'frontier' tier is refused unless this
            # dispatcher was explicitly opted in — never a live frontier egress just
            # because a definition declares one (pending NE-162 redaction).
            if route_detail["tier"] == "frontier" and not self._allow_frontier_subagent_routing:
                await self._emit(
                    action="subagent.route",
                    outcome=AuditOutcome.denied,
                    actor=principal.id,
                    resource=f"subagent:{spec.agent}",
                    detail={**route_detail, "reason": "frontier_routing_disabled"},
                )
                raise FrontierRoutingDisabledError(
                    f"sub-agent {spec.agent!r} resolved to the frontier-api lane but "
                    "frontier sub-agent routing is disabled on this dispatcher "
                    "(allow_frontier_subagent_routing=False) pending NE-162 redaction"
                )

            await self._emit(
                action="subagent.route",
                outcome=AuditOutcome.success,
                actor=principal.id,
                resource=f"subagent:{spec.agent}",
                detail=route_detail,
            )

            # (6) ALWAYS gate to the definition's toolset (empty ⇒ zero tools).
            scoped = ToolsetScopedExecutor(
                self._executor,
                granted=definition.toolset,
                sink=self._sink,
                scope=f"subagent:{spec.agent}",
                pdp=self._pdp,
            )

            # (7) Render the prompt — spec.context is the ONLY sanctioned parent→child
            # channel, and only when non-empty.
            prompt = f"{definition.instructions}\n\nTask: {spec.task}"
            if spec.context:
                prompt += "\n\nContext (JSON):\n" + json.dumps(
                    dict(spec.context), sort_keys=True, default=str
                )

            # Per-child settings via model_copy — self._settings (the shared parent
            # object) is NEVER mutated; only this local child_settings carries the
            # resolved route_task/route_lane/kpi_tag.
            child_settings = settings.model_copy(
                update={"task": route_task, "lane": route_lane, "kpi_tag": route_kpi_tag}
            )

            try:
                output = await run_isolated_agent(
                    task=prompt,
                    principal=principal,
                    executor=scoped,
                    gateway=self._gateway,
                    settings=child_settings,
                    source_guard=self._source_guard,
                )
                succeeded = True
            except (RuntimeAuditError, AuditEmissionError):
                # An un-recordable call must never be swallowed — propagate (fail closed).
                raise
            except Exception as exc:
                output = f"ERROR: {type(exc).__name__}"

            # (8) Completion audit + typed result.
            await self._emit(
                action="subagent.complete",
                outcome=AuditOutcome.success if succeeded else AuditOutcome.error,
                actor=principal.id,
                resource=f"subagent:{spec.agent}",
                detail={"agent": spec.agent},
            )
            return SubAgentResult(agent=spec.agent, output=output, succeeded=succeeded)
        finally:
            self._registry.finish(record.process_id, succeeded=succeeded)

    def _resolve_route(
        self, spec: SubAgentSpec, definition: AgentDefinition
    ) -> tuple[str, str | None, str | None, dict[str, object]]:
        """Resolve the child's routing task/lane/outcome off the TRUSTED ``AgentDefinition``.

        ``spec.route_lane_hint`` (model-emitted, untrusted) may only NARROW toward
        ``Lane.PRIVATE_VLLM``; any attempt to widen toward ``Lane.FRONTIER_API`` is
        clamped back to the ``AgentDefinition``-derived lane (§10.2, C12) — this is
        the ONLY place a lane decision is made, and it never reads a widen from the
        spec. Returns ``(route_task, route_lane, kpi_tag, detail)`` where ``detail``
        is the redaction-compliant ``subagent.route`` audit payload (identifiers/
        enums/bools only — never prompt text, context values, or exception messages).

        ``kpi_tag`` (C8) resolves by the SAME definition-then-settings rule and is
        read from ``spec`` NOWHERE: the business outcome a call's spend is billed
        to is declared configuration, so an agent can never label its own spend.
        """
        settings = self._settings

        route_task = definition.route_task if definition.route_task is not None else settings.task
        task_source = "definition" if definition.route_task is not None else "settings"

        base_lane = definition.route_lane if definition.route_lane is not None else settings.lane
        lane_source = "definition" if definition.route_lane is not None else "settings"

        kpi_tag = definition.kpi_tag if definition.kpi_tag is not None else settings.kpi_tag
        # "unset" — NOT a tag. Naming the source when there is no tag would imply
        # a config declared one.
        kpi_tag_source = (
            "definition"
            if definition.kpi_tag is not None
            else ("settings" if settings.kpi_tag is not None else "unset")
        )

        hint_clamped = False
        lane = base_lane
        if spec.route_lane_hint is Lane.PRIVATE_VLLM:
            lane = Lane.PRIVATE_VLLM.value
            lane_source = "spec-hint" if base_lane != Lane.PRIVATE_VLLM.value else lane_source
        elif spec.route_lane_hint is Lane.FRONTIER_API and base_lane != Lane.FRONTIER_API.value:
            hint_clamped = True  # lane stays base_lane — NEVER widen (frontier-exfil path).

        detail: dict[str, object] = {
            "agent": spec.agent,
            "route_task": route_task,
            "route_lane": lane,
            "tier": _route_tier(lane),
            "task_source": task_source,
            "lane_source": lane_source,
            "hint_clamped": hint_clamped,
            "kpi_tag": kpi_tag,
            "kpi_tag_source": kpi_tag_source,
        }
        return route_task, lane, kpi_tag, detail

    def child_for(
        self, spec: SubAgentSpec, *, parent_id: str | None = None
    ) -> GovernedSubAgentDispatcher:
        """The ONLY dispatcher a nested integration may hand to a sub-agent.

        Shares the registry/sink/executor/pdp/gateway/settings/definitions and steps
        the lineage down one level: ``_depth+1`` and a budget MIN-composed with
        ``spec.max_depth`` (a lying spec can only narrow, never widen). ``parent_id``
        should be the dispatched sub-agent's process id when the caller knows it (a
        future dispatch-as-tool unit passes it); it defaults to this dispatcher's own
        parent marker. v1 has no in-product recursion, so this seam is honor-system —
        global concurrency stays registry-enforced regardless (see README).
        """
        child = GovernedSubAgentDispatcher(
            definitions=self._definitions,
            executor=self._executor,
            gateway=self._gateway,
            registry=self._registry,
            sink=self._sink,
            settings=self._settings,
            principal=self._principal,
            pdp=self._pdp,
            source_guard=self._source_guard,
            allow_frontier_subagent_routing=self._allow_frontier_subagent_routing,
        )
        child._depth = self._depth + 1
        child._budget = max(0, min(self._budget - 1, spec.max_depth - 1))
        child._parent_id = parent_id if parent_id is not None else self._parent_id
        return child

    async def _emit(
        self,
        *,
        action: str,
        outcome: AuditOutcome,
        actor: str,
        resource: str,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        # Copies middleware._emit exactly: event construction (frozen-hash resolution)
        # is OUTSIDE the try so ConfigHashNotFrozenError propagates; the append failure
        # is wrapped as RuntimeAuditError (fail closed).
        event = new_audit_event(
            action=action,
            resource=resource,
            outcome=outcome,
            actor=actor,
            detail=json.dumps(detail) if detail is not None else None,
            provenance=dict(_PROVENANCE_SOURCE),
            trace_id=current_run_trace_id(),
        )
        try:
            await self._sink.append(event)
        except Exception as exc:
            raise RuntimeAuditError(
                f"audit append failed for {action!r} on {resource!r}; "
                "operation withheld (C6 fail closed)"
            ) from exc
