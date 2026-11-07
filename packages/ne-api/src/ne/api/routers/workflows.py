# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-WORKFLOWS — the governed multi-step workflow surface.

``GET  /v1/workflows``                  — what this deployment offers.
``POST /v1/workflows/{workflow_id}/run`` — run one, end to end, under ONE trace id.

The READS and the Tier-2 WRITE execute as the CALLING principal: every read is
ACL-trimmed to what that person may see, and every write is attributed to them in
the approval queue and the audit chain. The DRAFTING leg is the exception, stated
plainly: it executes and is audited as the service identity
``agent:workflow-drafter`` (see ``ne.api.workflows`` — "Who each leg executes as").
An ops operator running the sweep therefore gets the ops operator's view of the
data, which is the honest behaviour — a workflow that quietly widened its own read
scope would make the permission model a decoration.

Gated on ``ApiSettings.ops_roles`` (the ``routers/demo.py`` / ``routers/trace.py``
precedent): starting a run that parks Tier-2 writes across several accounts is an
operator action. The absence of the feature flag is an honest 404, never a silently
degraded endpoint.

Run-level failure mapping, stated exactly — each mapped class is pinned by a
test, and what is not mapped is declared rather than implied. A PDP deny on a
run-level read is 403 ``tool-denied``; a guardrail block is 422
``guardrail-violation``; a tool handler failing is 502 naming the cause class
(all three: ``test_run_level_refusals_map_to_honest_statuses``); a caller whose
labels give the verified read nothing to serve is 403 ``read-scope-denied``
(``test_operator_without_data_label_gets_403_not_500`` — the C7 fail-closed
scope refusal: for these templates the outcome is BINARY, the full table or a
refusal, because ``scoped_dataset`` intersects the caller's labels with the
single ``all-employees`` graph class; there is no per-row trimming gradient
here to soften it). DECLARED RESIDUAL: run-level classes with no branch below —
``ToolNotRegisteredError``, a bare ``ActionValidationError``,
``RuntimeAuditError``, and a ``ValueError`` from a run-level
``parse_verified_rows`` — surface as the framework's generic 500. All four are
composition or fail-closed-infrastructure faults on the run's READ legs, which
execute before any approval is parked, so such a 500 strands no human decision.
Failures INSIDE a subject's leg never reach this router: the engine degrades
them to per-proposal outcomes — ``refused`` for the same governance classes the
write leg names, ``failed`` otherwise — and returns 200 (``ne.api.workflows``).

Responses carry identifiers, counts, enums and bools — never note bodies, account
names or model output (C12). The approver reads the note itself through the approvals
disclosure route, which is the one audited surface for that material.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from ne.api.agentic_workflows import (
    AGENTIC_MODEL_TASK,
    AGENTIC_WORKFLOWS,
    AgenticRunStatusView,
    AgenticRunView,
    create_agentic_run,
    enable_agentic_mode,
    fail_agentic_run,
    read_agentic_run_status,
    run_agentic_workflow,
)
from ne.api.deps import (
    ApiState,
    api_state,
    bind_authenticated_request_context,
    current_principal,
)
from ne.api.routers.audit import _require_ops
from ne.api.routers.models import authorize_model_choice
from ne.api.tools import TURN_PRINCIPAL
from ne.api.wire import AgenticRunRequest
from ne.api.workflows import (
    MAX_SUBJECTS_CEILING,
    WORKFLOWS,
    WorkflowRunView,
    WorkflowView,
    run_workflow,
)
from ne.contracts.types import Lane, Principal
from ne.govern.errors import GuardrailViolation
from ne.runtime.correlation import run_trace_scope
from ne.runtime.errors import ToolDeniedError, ToolExecutionError

router = APIRouter(dependencies=[Depends(bind_authenticated_request_context)])

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]
_INFLIGHT_AGENTIC: set[asyncio.Task[None]] = set()


def _workflow_available(workflow: WorkflowView, state: ApiState) -> bool:
    """True only when this deployment's one executor contains every declared tool."""
    menu = {spec.name for spec in state.executor.tool_specs}
    return set(workflow.reads) | set(workflow.writes) <= menu


@router.get("/v1/workflows")
async def list_workflows(principal: _Principal, state: _State) -> list[WorkflowView]:
    await _require_ops(principal, state)
    if not state.settings.workflows_enabled:
        raise HTTPException(status_code=404, detail="workflows not enabled")
    return [
        enable_agentic_mode(workflow)
        for workflow in WORKFLOWS.values()
        if _workflow_available(workflow, state)
    ]


@router.post("/v1/agentic-workflows/{workflow_id}/run")
async def run_agentic(
    workflow_id: str,
    principal: _Principal,
    state: _State,
    body: AgenticRunRequest | None = None,
) -> AgenticRunView:
    """Run one genuinely model-planned Meridian workflow under fixed server caps."""
    await _require_ops(principal, state)
    if not state.settings.workflows_enabled:
        raise HTTPException(status_code=404, detail="workflows not enabled")
    workflow = WORKFLOWS.get(workflow_id)
    if workflow is None or workflow_id not in AGENTIC_WORKFLOWS:
        raise HTTPException(status_code=404, detail="unknown agentic workflow")
    if not _workflow_available(workflow, state):
        raise HTTPException(status_code=404, detail="workflow not available in this deployment")

    with run_trace_scope() as trace_id:
        selection = await authorize_model_choice(
            state=state,
            principal=principal,
            task=AGENTIC_MODEL_TASK,
            requested_model=body.requested_model if body is not None else None,
            trace_id=trace_id,
        )
        # Verified-answer handlers consume the exact bearer principal from the same
        # carrier as chat/deterministic workflows. The model loop receives that
        # identical Principal and the authorized model pair under this same trace.
        token = TURN_PRINCIPAL.set(principal)
        try:
            return await run_agentic_workflow(
                workflow_id,
                executor=state.executor,
                gateway=state.gateway,
                settings=state.runtime_settings,
                sink=state.sink,
                engine=state.audit_engine,
                principal=principal,
                trace_id=trace_id,
                requested_model=selection.model if selection is not None else None,
                requested_model_lane=selection.lane if selection is not None else None,
                source_guard=state.source_guard,
            )
        finally:
            TURN_PRINCIPAL.reset(token)


async def _finish_agentic_run(
    workflow_id: str,
    *,
    state: ApiState,
    principal: Principal,
    trace_id: str,
    run_id: str,
    requested_model: str | None,
    requested_model_lane: Lane | None,
) -> None:
    """Own the request-independent context for one durable background run."""
    token = TURN_PRINCIPAL.set(principal)
    try:
        with run_trace_scope(trace_id):
            await run_agentic_workflow(
                workflow_id,
                executor=state.executor,
                gateway=state.gateway,
                settings=state.runtime_settings,
                sink=state.sink,
                engine=state.audit_engine,
                principal=principal,
                trace_id=trace_id,
                run_id=run_id,
                requested_model=requested_model,
                requested_model_lane=requested_model_lane,
                source_guard=state.source_guard,
            )
    except asyncio.CancelledError as exc:
        fail_agentic_run(state.audit_engine, run_id, exc)
        raise
    except Exception as exc:
        # Exception CLASS only: the status surface must never become a second
        # channel for model/provider/tool error text.
        fail_agentic_run(state.audit_engine, run_id, exc)
    finally:
        TURN_PRINCIPAL.reset(token)


@router.post(
    "/v1/agentic-workflows/{workflow_id}/start",
    response_model=AgenticRunStatusView,
    status_code=202,
)
async def start_agentic(
    workflow_id: str,
    principal: _Principal,
    state: _State,
    body: AgenticRunRequest | None = None,
) -> AgenticRunStatusView:
    """Return an opaque ticket immediately, then run on the same governed path."""
    await _require_ops(principal, state)
    if not state.settings.workflows_enabled:
        raise HTTPException(status_code=404, detail="workflows not enabled")
    workflow = WORKFLOWS.get(workflow_id)
    if workflow is None or workflow_id not in AGENTIC_WORKFLOWS:
        raise HTTPException(status_code=404, detail="unknown agentic workflow")
    if not _workflow_available(workflow, state):
        raise HTTPException(status_code=404, detail="workflow not available in this deployment")

    with run_trace_scope() as trace_id:
        # No ticket or task exists until the governed selection is authorized
        # and its model.selection evidence is durably appended.
        selection = await authorize_model_choice(
            state=state,
            principal=principal,
            task=AGENTIC_MODEL_TASK,
            requested_model=body.requested_model if body is not None else None,
            trace_id=trace_id,
        )
        ticket = create_agentic_run(
            state.audit_engine,
            workflow_id=workflow_id,
            trace_id=trace_id,
            principal=principal,
        )
        task = asyncio.create_task(
            _finish_agentic_run(
                workflow_id,
                state=state,
                principal=principal,
                trace_id=trace_id,
                run_id=ticket.run_id,
                requested_model=selection.model if selection is not None else None,
                requested_model_lane=selection.lane if selection is not None else None,
            )
        )
        _INFLIGHT_AGENTIC.add(task)
        task.add_done_callback(_INFLIGHT_AGENTIC.discard)
    return ticket


@router.get(
    "/v1/agentic-workflows/runs/{run_id}",
    response_model=AgenticRunStatusView,
)
async def get_agentic_run_status(
    run_id: str,
    principal: _Principal,
    state: _State,
) -> AgenticRunStatusView:
    """Poll one server-owned ticket; the trace itself stays on GET /v1/trace."""
    await _require_ops(principal, state)
    if not state.settings.workflows_enabled:
        raise HTTPException(status_code=404, detail="workflows not enabled")
    view = read_agentic_run_status(state.audit_engine, run_id)
    if view is None:
        raise HTTPException(status_code=404, detail="agentic run not found")
    return view


@router.post("/v1/workflows/{workflow_id}/run")
async def run(
    workflow_id: str,
    principal: _Principal,
    state: _State,
    # ``le=`` mirrors the ENGINE-owned ceiling (run_workflow clamps regardless) so an
    # over-asking caller gets an honest 422 before a run starts, not a silent narrow.
    max_subjects: Annotated[int, Query(ge=1, le=MAX_SUBJECTS_CEILING)] = 2,
) -> WorkflowRunView:
    await _require_ops(principal, state)
    if not state.settings.workflows_enabled:
        raise HTTPException(status_code=404, detail="workflows not enabled")
    workflow = WORKFLOWS.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="unknown workflow")
    if not _workflow_available(workflow, state):
        raise HTTPException(status_code=404, detail="workflow not available in this deployment")

    # The knowledge + verified-answer handlers read their acting principal from the
    # ne-api turn carrier (fail-closed when unbound) — the SAME carrier the chat route
    # sets. Setting it here, from the VERIFIED bearer, is what makes a workflow read
    # ACL-trimmed to the caller; it is reset in `finally` so it can never leak into a
    # later request on this worker.
    token = TURN_PRINCIPAL.set(principal)
    try:
        with run_trace_scope() as trace_id:
            return await run_workflow(
                workflow_id,
                executor=state.executor,
                dispatcher=state.workflow_dispatcher,
                gate=state.gate,
                principal=principal,
                trace_id=trace_id,
                max_subjects=max_subjects,
                source_guard=state.source_guard,
                protected_output_writer=state.protected_source_output_writer,
                protected_execution_writer=state.protected_source_execution_writer,
            )
    except ToolDeniedError as exc:
        raise HTTPException(status_code=403, detail="tool-denied") from exc
    except GuardrailViolation as exc:
        raise HTTPException(status_code=422, detail="guardrail-violation") from exc
    except ToolExecutionError as exc:
        # Cause-class name-compare is the routers/ontology.py precedent (ne-api does
        # not import brain-ontology error types). C12: class NAME only, never the
        # message. Per-subject failures never reach here (the engine degrades them);
        # what does is a run-level read failing before any approval was parked.
        cause = exc.__cause__
        if cause is not None and type(cause).__name__ == "VerifiedAnswerScopeError":
            # C7 fail-closed: the caller's labels intersect none of the template's
            # graph classes. An authorization outcome, not a server fault — 403.
            raise HTTPException(status_code=403, detail="read-scope-denied") from exc
        detail = type(cause).__name__ if cause is not None else "ToolExecutionError"
        raise HTTPException(status_code=502, detail=detail) from exc
    finally:
        TURN_PRINCIPAL.reset(token)
