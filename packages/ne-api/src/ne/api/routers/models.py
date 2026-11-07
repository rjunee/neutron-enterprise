# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The models read routes (NE-SERVING-STATUS) — ops-gated, server-authoritative.

``GET /v1/models/serving`` serves the LIVE serving observation for every
probeable private lane (:mod:`ne.api.serving` owns what "probeable", "observed"
and "redacted" mean — read its docstring before this one). The console renders
the server's verdict and NEVER probes a model endpoint itself (C6/C7): the
browser has no route to the serving endpoint, and every displayed fact below
arrived through this one governed read.

ACCESS DECISION (documented): the SAME ``_require_ops`` dependency as
``/v1/audit/*`` and ``/v1/trace/*`` (empty ``ApiSettings.ops_roles`` ⇒ 403 for
everyone, fail closed, audited ``api.audit.unauthorized``). Posture's
any-authenticated ruling does NOT extend here: this payload names endpoint
hosts/ports — deployment topology an operator may see and a reader-role
principal has no job needing (C12).

This module is also where §18 B3's ``GET /v1/models`` read router lands when it
is built; the serving observation deliberately lives beside it, not in posture
— §14 moved model routing OFF the posture page ("Model routing is on Models →").

NE-MODEL-ROUTING adds the routing pair below it:

``GET /v1/models/routing`` answers, per gateway task, which model the RUNNING
gateway routes to (read from the same register the enforcing route provider
reads — reported == enforced), what the deployment shipped, and EVERY target the
server would accept, each with the governed action and tier a repoint would
ride. ``selectable`` is computed on the server from the boot model set, the
gateway's budget arithmetic and the live egress posture; the console renders it
and computes none of it (C7).

``POST /v1/models/routing`` NEVER writes the register. It derives the governed
action from the TARGET MODEL'S LANE and drives it through ``state.executor``
— THE one governed executor, so the change takes the same PDP → guardrails →
typed validation → HITL → chain path every tool call takes (C6). There is no
store-writing code path reachable from any HTTP route. ``routers/network.py`` is
the template, including its three-outcome §2.5 contract (applied / pending /
denied) and its observation-order lesson.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from typing import Annotated, Literal

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException

from ne.api.agentic_workflows import AGENTIC_MODEL_TASK
from ne.api.authz import emit_surface_audit, require_role
from ne.api.deps import ApiState, api_state, current_principal
from ne.api.inference_lifecycle import (
    LIFECYCLE_ACTION,
    LifecycleConfirmationRefused,
    LifecycleConflict,
    LifecycleUnavailable,
    inference_lifecycle_request_principal,
)
from ne.api.model_routing import action_for_lane, refusal_for_target, routing_catalog
from ne.api.model_selection import (
    AuthorizedModelSelection,
    ModelSelectionEvidenceError,
    ModelSelectionPolicyRefused,
    ModelSelectionUnavailable,
    UnknownModelSelection,
)
from ne.api.routers.audit import _require_ops
from ne.api.wire import (
    FrontierReadinessView,
    GovernedChangeView,
    InferenceLifecycleTargetView,
    InferenceLifecycleView,
    InferenceStopConfirmationRequest,
    InferenceStopConfirmationView,
    InferenceTransitionRequest,
    ModelChoiceCatalogView,
    ModelChoiceView,
    ModelRoutingView,
    PendingInferenceLifecycleChange,
    PendingRoutingChange,
    RouteChangeRequest,
    RouteTargetView,
    ServingStatusView,
    TaskRoutingView,
)
from ne.contracts.inference import InferenceTargetState
from ne.contracts.types import Principal
from ne.govern.errors import GuardrailViolation
from ne.runtime.approvals import canonical_args_hash
from ne.runtime.correlation import action_scope
from ne.runtime.errors import (
    ActionValidationError,
    ApprovalNotGrantedError,
    ToolDeniedError,
    ToolExecutionError,
)
from ne.schema import AuditOutcome

router = APIRouter()

_LOG = logging.getLogger(__name__)

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]

_ROUTING_RESOURCE = "model:routing"

#: How long the route waits for EITHER the executor task to finish (applied) OR
#: the parked approval row to become durably visible (pending) before answering
#: 503 — the ``routers/network.py`` window verbatim.
_OUTCOME_VISIBLE_TIMEOUT_S = 15.0
_POLL_S = 0.05

#: Strong references to parked waiters (asyncio tasks are otherwise GC-eligible).
_PARKED_TASKS: set[asyncio.Task[str]] = set()

ModelChoiceSurface = Literal["ask", "agentic-workflow"]


def _watch(
    task: asyncio.Task[str],
    *,
    on_done: Callable[[BaseException | None], None] | None = None,
) -> None:
    _PARKED_TASKS.add(task)

    def _done(t: asyncio.Task[str]) -> None:
        _PARKED_TASKS.discard(t)
        if t.cancelled():
            if on_done is not None:
                on_done(asyncio.CancelledError())
            return
        exc = t.exception()
        if on_done is not None:
            on_done(exc)
        if exc is not None:
            # The deny/expiry legs land here — audited upstream by the executor;
            # log the class only (C12).
            _LOG.info("parked route change resolved without applying: %s", type(exc).__name__)

    task.add_done_callback(_done)


@router.get("/v1/models/serving")
async def models_serving(
    principal: _Principal,
    state: _State,
) -> ServingStatusView:
    await _require_ops(principal, state)
    if state.serving_status is None or state.gateway_settings is None:
        # No composed gateway ⇒ no lanes to observe. Honest empty, never a row.
        return ServingStatusView(lanes=[])
    lanes = await state.serving_status.observe(state.gateway_settings.models)
    return ServingStatusView(lanes=lanes)


@router.get("/v1/models/frontier-readiness")
async def frontier_readiness(
    principal: _Principal,
    state: _State,
) -> FrontierReadinessView:
    """Has the boot-time frontier warm landed yet? (NE-SELECT-LATENCY)

    The observable the deploy's acceptance gate polls after a container
    recreate, before spending its one canonical run admission: on a cold
    process the FIRST governed warm (eager since the cold-boot fix, but still
    a real provider call plus an O(chain) verified read on a 100k+-row chain)
    takes tens of seconds to land, and an admission that races it pays that
    whole cost inline — the measured 26-45s stall that blew the acceptance's
    20s budget and hit the owner's first request after every restart.

    This read issues no provider call, no chain read/replay, and no probe —
    but it is NOT free of I/O: the route and posture registers it consults are
    bounded synchronous row reads on store-backed compositions, so it runs off
    the event-loop thread here.  Polled every few seconds by the deploy gate,
    it cannot stall the loop.  It is an OPS SIGNAL, never an evidence source:
    admission still judges the real chain records.  ``unknown`` means the
    registers could not be read — a caller must treat it as not-ready, never
    as a green light.  Ops-gated like ``/v1/models/serving``: it is a
    deployment-operational fact with no reader-role use.
    """
    await _require_ops(principal, state)
    service = state.model_selection
    if service is None:
        # No selection authority composed ⇒ no admission can wait on evidence.
        return FrontierReadinessView(status="not-applicable")
    status = await anyio.to_thread.run_sync(service.frontier_evidence_readiness)
    return FrontierReadinessView(status=status)


def _task_for_surface(surface: ModelChoiceSurface, state: ApiState) -> str:
    """Map product surfaces to gateway tasks on the server, never in the client."""
    if surface == "ask":
        return state.runtime_settings.task
    return AGENTIC_MODEL_TASK


@router.get("/v1/models/choices/{surface}")
async def model_choices(
    surface: ModelChoiceSurface,
    principal: _Principal,
    state: _State,
) -> ModelChoiceCatalogView:
    """Return the CONFIGURED model choices for one console run surface.

    Unlike the topology-bearing serving screen this list is available to every
    authenticated caller who can Ask. It carries aliases/provider/lane only—
    never an endpoint or credential, and (because nothing was measured) no
    observation time.

    This read deliberately performs no liveness probe and no audit-chain
    verification: it is on the critical path of every Ask page load, and the
    evidence gather cost seconds. The liveness question has not been dropped,
    it has MOVED to submission: :func:`authorize_model_choice` re-runs PDP and
    demands current serving evidence, refuses the run by name when the chosen
    model is not available, and never substitutes another model for it.
    """
    del principal  # Authentication is the access decision for this names-only read.
    service = state.model_selection
    if service is None:
        raise HTTPException(status_code=503, detail="model selection is unavailable")
    if surface == "agentic-workflow" and not state.settings.workflows_enabled:
        raise HTTPException(status_code=404, detail="workflows not enabled")
    task = _task_for_surface(surface, state)
    try:
        catalog = await service.configured_choices(task)
    except ModelSelectionEvidenceError as exc:
        # Reachable only when the governed route authority itself is malformed;
        # an unreadable catalog is still an honest 503, never a guessed list.
        raise HTTPException(
            status_code=503, detail="model serving evidence is unavailable"
        ) from exc
    return ModelChoiceCatalogView(
        surface=surface,
        task=catalog.task,
        effective_default=catalog.effective_default,
        choices=[
            ModelChoiceView(
                model=choice.model,
                provider=choice.provider,
                lane=choice.lane.value,
                evidence=choice.evidence,
                observed_at=choice.observed_at,
            )
            for choice in catalog.choices
        ],
    )


async def authorize_model_choice(
    *,
    state: ApiState,
    principal: Principal,
    task: str,
    requested_model: str | None,
    trace_id: str,
    snapshot_tokens: int | None = None,
) -> AuthorizedModelSelection | None:
    """The shared HTTP adapter around the one governed selection authority.

    ``snapshot_tokens`` (NE-CTX-IN-FORCE round 7, SPEC PRE-DEMO-014): the
    CURRENT conversation snapshot's estimated size, passed by callers that hold
    a conversation so an explicit switch to a model whose window cannot safely
    accept it is refused BY NAME here — before any durable write — instead of
    being offered. ``None`` ⇒ no conversation in hand ⇒ no snapshot check.
    """
    service = state.model_selection
    if service is None:
        raise HTTPException(status_code=503, detail="model selection is unavailable")
    # An omitted model historically delegates to the deployment's gateway
    # default.  Resolve that default explicitly only when lifecycle governance
    # can make the configured default unavailable; installations without the
    # lifecycle seam keep their existing gateway-default behavior.
    lifecycle_manages_models = bool(
        state.inference_lifecycle is not None and state.inference_lifecycle.config.targets
    )
    managed_default_omitted = requested_model is None and lifecycle_manages_models
    if requested_model is None and not lifecycle_manages_models:
        return None
    if requested_model is None:
        # Lifecycle-managed omission means "the configured primary", never the
        # first currently available fallback. An unavailable primary therefore
        # refuses and requires an explicit operator/user selection.
        requested_model = service.configured_primary(task)
    try:
        return await service.authorize(
            task=task,
            requested_model=requested_model,
            principal=principal,
            trace_id=trace_id,
            snapshot_tokens=snapshot_tokens,
        )
    except ModelSelectionPolicyRefused as exc:
        # The service's messages are closed, names-free strings; in particular
        # AIR_GAP remains an explicit policy refusal, never a silent reroute.
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ModelSelectionUnavailable as exc:
        if managed_default_omitted:
            raise HTTPException(status_code=409, detail="no model is currently available") from exc
        # The picker lists configured models, so a caller can legitimately choose
        # one that is not currently serving. Name it: the alias comes from the
        # SERVER registry (never echoed client text, which raises
        # UnknownModelSelection below), so this is not a registry oracle, and a
        # silent or anonymous refusal would leave the user guessing which model
        # failed while the run went nowhere.
        detail = (
            f"requested model is unavailable: {exc.model}"
            if exc.model is not None
            else "requested model is unavailable"
        )
        raise HTTPException(status_code=409, detail=detail) from exc
    except UnknownModelSelection as exc:
        # Unknown client text is neither echoed nor distinguished from a known
        # but no-longer-live alias (no registry enumeration oracle).
        raise HTTPException(status_code=409, detail="requested model is unavailable") from exc
    except ModelSelectionEvidenceError as exc:
        raise HTTPException(
            status_code=503, detail="model selection evidence is unavailable"
        ) from exc


def _air_gap(state: ApiState) -> bool:
    """The LIVE air-gap posture, from the SAME provider the gateway's egress
    guard reads (reported == enforced). Absent provider ⇒ report NOT engaged:
    this is legibility, and the guard is the enforcement either way."""
    provider = state.egress_settings_provider
    if provider is None:
        return False
    try:
        return bool(provider().air_gap)
    except Exception:  # pragma: no cover - the provider is itself fail-closed
        return True


@router.get("/v1/models/routing")
async def models_routing(principal: _Principal, state: _State) -> ModelRoutingView:
    """Which model serves each gateway task, and every change the server allows."""
    await require_role(
        principal,
        state.settings.ops_roles,
        sink=state.sink,
        audit_action="api.models.routing.unauthorized",
        resource=_ROUTING_RESOURCE,
        http_detail="not authorized",
    )
    if state.route_store is None:
        # No composed routing register ⇒ nothing to report. Honest empty.
        return ModelRoutingView(tasks=[])
    rows = routing_catalog(state.route_store, air_gap=_air_gap(state))
    pending = {
        (row.action, row.args_hash): row
        for row in await state.gate.list_pending()
        if row.args_hash is not None
    }

    def _availability(alias: str) -> tuple[bool, str | None]:
        if state.inference_lifecycle is None:
            return True, None
        return state.inference_lifecycle.availability(alias)

    return ModelRoutingView(
        tasks=[
            TaskRoutingView(
                task=row.task,
                model=row.model,
                lane=row.lane,
                default_model=row.default_model,
                source="override" if row.source == "override" else "default",
                changed_by=row.changed_by,
                approved_by=row.approved_by,
                changed_at=row.changed_at,
                pending_change=next(
                    (
                        PendingRoutingChange(
                            request_id=item.request_id,
                            requested_by=item.principal_id,
                            model=target.model,
                        )
                        for target in row.targets
                        if (
                            item := pending.get(
                                (
                                    target.action,
                                    canonical_args_hash({"task": row.task, "model": target.model}),
                                )
                            )
                        )
                        is not None
                    ),
                    None,
                ),
                available=_availability(row.model)[0],
                unavailable_reason=_availability(row.model)[1],
                targets=[
                    RouteTargetView(
                        model=t.model,
                        lane=t.lane,
                        action=t.action,
                        tier=t.tier,
                        selectable=t.selectable and _availability(t.model)[0],
                        reason=t.reason or _availability(t.model)[1],
                    )
                    for t in row.targets
                ],
            )
            for row in rows
        ]
    )


@router.get("/v1/models/lifecycle")
async def models_lifecycle(principal: _Principal, state: _State) -> InferenceLifecycleView:
    """Report durable lifecycle state; never operator executable detail."""
    await require_role(
        principal,
        state.settings.ops_roles,
        sink=state.sink,
        audit_action="api.models.lifecycle.unauthorized",
        resource="model:lifecycle",
        http_detail="not authorized",
    )
    service = state.inference_lifecycle
    if service is None:
        return InferenceLifecycleView(registry_hash="0" * 64, transport_available=False, targets=[])
    rows = {row.target_id: row for row in service.states()}
    active_pending = {
        row.request_id for row in await state.gate.list_pending() if row.action == LIFECYCLE_ACTION
    }
    pending_by_target = service.pending_changes(active_pending)
    targets: list[InferenceLifecycleTargetView] = []
    for target in service.config.targets:
        row = rows[target.target_id]
        pending = pending_by_target.get(target.target_id)
        actions = [] if pending is not None else list(service.available_actions(target, row))
        targets.append(
            InferenceLifecycleTargetView(
                target_id=target.target_id,
                display_name=target.display_name,
                aliases=list(target.aliases),
                state=row.state.value,
                desired_state=row.desired_state.value,
                version=row.version,
                transition_id=row.transition_id,
                code=row.code.value,
                observed_at=row.observed_at,
                updated_at=row.updated_at,
                cooldown_until=row.cooldown_until,
                pending_change=(
                    PendingInferenceLifecycleChange.model_validate(pending)
                    if pending is not None
                    else None
                ),
                available_actions=actions,
            )
        )
    return InferenceLifecycleView(
        registry_hash=service.config.registry_hash,
        transport_available=service.transport_available,
        targets=targets,
    )


@router.post("/v1/models/lifecycle/{target_id}/stop-confirmation")
async def confirm_model_stop(
    target_id: str,
    body: InferenceStopConfirmationRequest,
    principal: _Principal,
    state: _State,
) -> InferenceStopConfirmationView:
    await require_role(
        principal,
        state.settings.ops_roles,
        sink=state.sink,
        audit_action="api.models.lifecycle.unauthorized",
        resource=f"inference-target:{target_id}",
        http_detail="not authorized",
    )
    service = state.inference_lifecycle
    if service is None:
        raise HTTPException(status_code=503, detail="inference lifecycle is unavailable")
    try:
        token, expires = service.issue_stop_confirmation(
            target_id=target_id,
            principal_id=principal.id,
            expected_version=body.expected_version,
        )
    except LifecycleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await emit_surface_audit(
        state.sink,
        action="api.models.lifecycle.stop-confirmed",
        resource=f"inference-target:{target_id}",
        outcome=AuditOutcome.success,
        actor=principal.id,
        detail={"target_id": target_id, "expected_version": str(body.expected_version)},
    )
    return InferenceStopConfirmationView(confirmation_token=token, expires_at=expires)


@router.post("/v1/models/lifecycle/{target_id}/transition")
async def transition_model_target(
    target_id: str,
    body: InferenceTransitionRequest,
    principal: _Principal,
    state: _State,
) -> GovernedChangeView:
    """Submit exactly one start/stop through the governed Tier-2 executor."""
    await require_role(
        principal,
        state.settings.ops_roles,
        sink=state.sink,
        audit_action="api.models.lifecycle.unauthorized",
        resource=f"inference-target:{target_id}",
        http_detail="not authorized",
    )
    service = state.inference_lifecycle
    if service is None:
        raise HTTPException(status_code=503, detail="inference lifecycle is unavailable")
    service.audit_requested(
        actor=principal.id,
        target_id=target_id,
        desired_state=body.desired_state,
    )
    try:
        retry = service.idempotent_retry_state(
            target_id=target_id,
            desired_state=body.desired_state,
            expected_version=body.expected_version,
            idempotency_key=body.idempotency_key,
        )
    except LifecycleConflict as exc:
        service.audit_refused(
            actor=principal.id,
            target_id=target_id,
            desired_state=body.desired_state,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if retry is not None:
        if retry.state in (InferenceTargetState.ERROR, InferenceTargetState.UNKNOWN):
            service.audit_refused(
                actor=principal.id,
                target_id=target_id,
                desired_state=body.desired_state,
                failed=True,
            )
            raise HTTPException(status_code=503, detail="the prior idempotent transition failed")
        return GovernedChangeView(
            status=(
                "pending"
                if retry.state in (InferenceTargetState.STARTING, InferenceTargetState.STOPPING)
                else "applied"
            ),
            request_id=None,
        )
    try:
        already_proven = service.already_proven_state(
            target_id=target_id,
            desired_state=body.desired_state,
            actor=principal.id,
        )
    except LifecycleUnavailable as exc:
        service.audit_refused(
            actor=principal.id,
            target_id=target_id,
            desired_state=body.desired_state,
            failed=True,
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if already_proven is not None:
        return GovernedChangeView(status="applied", request_id=None)
    args = {
        "target_id": target_id,
        "desired_state": body.desired_state,
        "expected_version": body.expected_version,
        "idempotency_key": body.idempotency_key,
    }
    if body.confirmation_token is not None:
        args["confirmation_token"] = body.confirmation_token
    args_hash = canonical_args_hash(args)
    try:
        reservation_id, pending_request_id, owns_reservation = service.reserve_pending_change(
            target_id=target_id,
            requested_by=principal.id,
            desired_state=body.desired_state,
            expected_version=body.expected_version,
            idempotency_key=body.idempotency_key,
            request_hash=args_hash,
        )
    except LifecycleConflict as exc:
        service.audit_refused(
            actor=principal.id,
            target_id=target_id,
            desired_state=body.desired_state,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not owns_reservation:
        deadline = asyncio.get_running_loop().time() + _OUTCOME_VISIBLE_TIMEOUT_S
        while pending_request_id is None:
            matching = await _pending_request_ids(state, LIFECYCLE_ACTION, principal.id, args_hash)
            if matching:
                pending_request_id = sorted(matching)[0]
                service.attach_pending_request(
                    reservation_id=reservation_id,
                    request_id=pending_request_id,
                )
                break
            retry = service.idempotent_retry_state(
                target_id=target_id,
                desired_state=body.desired_state,
                expected_version=body.expected_version,
                idempotency_key=body.idempotency_key,
            )
            if retry is not None:
                return GovernedChangeView(
                    status=(
                        "pending"
                        if retry.state
                        in (InferenceTargetState.STARTING, InferenceTargetState.STOPPING)
                        else "applied"
                    ),
                    request_id=None,
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise HTTPException(
                    status_code=503,
                    detail="pending lifecycle request is not yet visible",
                )
            await asyncio.sleep(_POLL_S)
            pending_request_id = service.pending_reservation_request(reservation_id)
        return GovernedChangeView(status="pending", request_id=pending_request_id)

    scope = hashlib.sha256(f"model-lifecycle:{principal.id}:{args_hash}".encode()).hexdigest()

    async def _execute() -> str:
        with inference_lifecycle_request_principal(principal), action_scope(scope):
            return await state.executor.execute(LIFECYCLE_ACTION, args, principal=principal)

    def _parked_done(exc: BaseException | None) -> None:
        cleared = service.clear_pending_reservation(reservation_id)
        if cleared and isinstance(
            exc, (ToolDeniedError, GuardrailViolation, ApprovalNotGrantedError)
        ):
            service.audit_refused(
                actor=principal.id,
                target_id=target_id,
                desired_state=body.desired_state,
            )

    task = asyncio.create_task(_execute())
    deadline = asyncio.get_running_loop().time() + _OUTCOME_VISIBLE_TIMEOUT_S
    while True:
        matching = await _pending_request_ids(state, LIFECYCLE_ACTION, principal.id, args_hash)
        if matching:
            request_id = sorted(matching)[0]
            service.attach_pending_request(
                reservation_id=reservation_id,
                request_id=request_id,
            )
            _watch(task, on_done=_parked_done)
            return GovernedChangeView(status="pending", request_id=request_id)
        if task.done():
            try:
                task.result()
            except (ToolDeniedError, GuardrailViolation) as exc:
                service.audit_refused(
                    actor=principal.id,
                    target_id=target_id,
                    desired_state=body.desired_state,
                )
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ApprovalNotGrantedError as exc:
                service.audit_refused(
                    actor=principal.id,
                    target_id=target_id,
                    desired_state=body.desired_state,
                )
                raise HTTPException(status_code=403, detail="approval not granted") from exc
            except LifecycleUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except (ActionValidationError, LifecycleConflict) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ToolExecutionError as exc:
                cause = exc.__cause__
                if isinstance(cause, LifecycleUnavailable):
                    raise HTTPException(status_code=503, detail=str(cause)) from exc
                if isinstance(cause, (LifecycleConflict, LifecycleConfirmationRefused)):
                    raise HTTPException(status_code=409, detail=str(cause)) from exc
                raise HTTPException(status_code=500, detail="lifecycle transition failed") from exc
            finally:
                service.clear_pending_reservation(reservation_id)
            row = service.state(target_id)
            return GovernedChangeView(
                status=(
                    "pending"
                    if row.state in (InferenceTargetState.STARTING, InferenceTargetState.STOPPING)
                    else "applied"
                ),
                request_id=None,
            )
        if asyncio.get_running_loop().time() >= deadline:
            _watch(task, on_done=_parked_done)
            raise HTTPException(
                status_code=503,
                detail="transition submitted; outcome is not yet visible",
            )
        await asyncio.sleep(_POLL_S)


async def _pending_request_ids(
    state: ApiState, action: str, principal_id: str, args_hash: str
) -> set[str]:
    """Rows belonging to this exact governed submission, never a sibling call."""
    return {
        row.request_id
        for row in await state.gate.list_pending()
        if row.action == action and row.principal_id == principal_id and row.args_hash == args_hash
    }


@router.post("/v1/models/routing")
async def change_model_route(
    body: RouteChangeRequest,
    principal: _Principal,
    state: _State,
) -> GovernedChangeView:
    """Repoint a gateway task at a different registered model (governed, live).

    This route NEVER writes the register. It resolves the target's LANE, derives
    the governed action from it, and drives THAT action through the one governed
    executor — so the tier a change gets derives from what the change does, and
    the PDP, the guardrails, the typed pre-gate validation, the human gate and
    the hash chain are all on the path (C6).
    """
    await require_role(
        principal,
        state.settings.ops_roles,
        sink=state.sink,
        audit_action="api.models.routing.unauthorized",
        resource=_ROUTING_RESOURCE,
        http_detail="not authorized",
    )
    store = state.route_store
    if store is None:
        raise HTTPException(status_code=503, detail="model routing is not configured")

    target = store.models_by_name().get(body.model)
    if target is None:
        # The honest 404: a name on no list has no lane, so there is no action to
        # derive and nothing to submit. Never mint a route for it.
        raise HTTPException(status_code=404, detail="no such model in this deployment's model set")
    # Pre-flight the SERVER's own rule so a change that cannot succeed never mints
    # an approval row. This is the SAME function the catalog marks `selectable`
    # with and the binding's validate_args refuses on — one rule, three readers.
    try:
        current = store.read_state().get(body.task)
    except Exception:  # pragma: no cover - defensive; validate_args re-checks
        current = None
    refusal = refusal_for_target(
        store.boot_settings,
        body.task,
        body.model,
        air_gap=_air_gap(state),
        current=current.model if current is not None else None,
        # NE-CTX-IN-FORCE round 9 (SPEC PRE-DEMO-014 on the repoint surface):
        # a repoint governs every conversation on the task, so the target is
        # measured against the LARGEST live conversation snapshot — the same
        # durable boundary-receipt counts the per-run selection gate reads.
        # None means no live conversation (no check); a failing read raises
        # out of this handler (fail closed), never "unknown => allowed".
        snapshot_tokens=store.snapshot_tokens_for(body.task),
    )
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)

    action = action_for_lane(target.lane)
    args = {"task": body.task, "model": body.model}
    args_hash = canonical_args_hash(args)

    # Give every identical API submission the same opaque scope. The durable
    # approval gate atomically claim-or-finds on scope + args hash, so concurrent
    # double-clicks converge on ONE row instead of creating siblings that cannot
    # be correlated back to their waiter.
    scope_seed = f"models-routing:{principal.id}:{action}:{args_hash}"
    scope = hashlib.sha256(scope_seed.encode()).hexdigest()

    async def _execute() -> str:
        with action_scope(scope):
            return await state.executor.execute(action, args, principal=principal)

    task = asyncio.create_task(_execute())

    deadline = asyncio.get_running_loop().time() + _OUTCOME_VISIBLE_TIMEOUT_S
    while True:
        # Row check FIRST (the NE-WORKFLOWS observation-order lesson): a waiter
        # that dies AFTER its row was durably inserted leaves a pending row a
        # human can already see — the honest answer is that row's id.
        matching = await _pending_request_ids(state, action, principal.id, args_hash)
        if matching:
            request_id = sorted(matching)[0]
            _watch(task)
            await emit_surface_audit(
                state.sink,
                action="api.models.routing.submitted",
                resource=_ROUTING_RESOURCE,
                outcome=AuditOutcome.success,
                actor=principal.id,
                detail={
                    "status": "pending",
                    "action": action,
                    "task": body.task,
                    "model": body.model,
                    "request_id": request_id,
                },
            )
            return GovernedChangeView(status="pending", request_id=request_id)

        if task.done():
            try:
                task.result()
            except (ToolDeniedError, GuardrailViolation) as exc:
                await emit_surface_audit(
                    state.sink,
                    action="api.models.routing.denied",
                    resource=_ROUTING_RESOURCE,
                    outcome=AuditOutcome.denied,
                    actor=principal.id,
                    detail={"error_class": type(exc).__name__},
                )
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ApprovalNotGrantedError as exc:
                raise HTTPException(status_code=403, detail=f"approval not granted: {exc}") from exc
            except ActionValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ToolExecutionError as exc:
                # The apply itself failed — the route is UNCHANGED (the state
                # write and its chain record are one transaction; fail closed).
                raise HTTPException(
                    status_code=500,
                    detail="the change could not be applied; the route is unchanged",
                ) from exc
            await emit_surface_audit(
                state.sink,
                action="api.models.routing.submitted",
                resource=_ROUTING_RESOURCE,
                outcome=AuditOutcome.success,
                actor=principal.id,
                detail={
                    "status": "applied",
                    "action": action,
                    "task": body.task,
                    "model": body.model,
                },
            )
            return GovernedChangeView(status="applied", request_id=None)

        if asyncio.get_running_loop().time() >= deadline:
            _watch(task)
            raise HTTPException(
                status_code=503,
                detail=(
                    "the change was submitted but its outcome is not yet visible; "
                    "re-read /v1/models/routing"
                ),
            )
        await asyncio.sleep(_POLL_S)
