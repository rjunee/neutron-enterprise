# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""POST /v1/network/air-gap (NE-AIRGAP-TOGGLE) — the governed egress on/off.

The redesign-spec §18 B3 network write. This route NEVER mutates the posture
itself: it derives the direction (restrict = engage, expand = disengage) from
the CURRENT effective posture, then drives the corresponding governed action
through THE executor (C6 — the same PDP → guardrails → HITL → chain path every
tool call takes; flowise.py is the template). The §2.5 contract:

* **applied** — the Tier-1 restrict ran to completion (PDP allow, logged);
* **pending** — the Tier-2 expand parked at the approval gate; the response
  carries the durable ``request_id`` and the row is visible in /v1/approvals.
  The NE-WORKFLOWS park shape verbatim: the executor call runs as a background
  task that BLOCKS on the gate and falls through to the handler on grant, while
  this route watches the approval store for the fresh row and answers
  ``pending`` as soon as it is durably visible. The in-process waiter applies
  the change when a human approves; its loss on restart leaves the deployment
  in the MORE restrictive state (fail-closed — see ne.api.network_egress).
* **denied** — 403 with the server's reason verbatim (PDP deny / guardrail).

Ops-gated (``ApiSettings.ops_roles`` — the Models screen sits under Operations;
empty tuple refuses everyone, fail closed) and every refusal is audited by
``require_role``. A submission that matches the posture already in force is a
409 (nothing to change — a no-op must never mint an approval row).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from ne.api.authz import emit_surface_audit, require_role
from ne.api.deps import ApiState, api_state, current_principal
from ne.api.network_egress import (
    EGRESS_EXPAND_ACTION,
    EGRESS_RESTRICT_ACTION,
    FrontierRoutedTasksError,
)
from ne.api.wire import AirGapChangeRequest, GovernedChangeView
from ne.contracts.types import Principal
from ne.govern.errors import GuardrailViolation
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

_RESOURCE = "network:egress"


def _frontier_routes_block(exc: BaseException) -> FrontierRoutedTasksError | None:
    """The typed engage refusal inside an executor-wrapped failure, if present.

    The governed executor re-raises every handler exception as
    ``ToolExecutionError(...) from exc``, so the refusal is on the cause chain,
    one (or in principle more) hops down. Bounded walk; anything else -> None.
    """
    cur: BaseException | None = exc
    for _ in range(8):
        if cur is None:
            return None
        if isinstance(cur, FrontierRoutedTasksError):
            return cur
        cur = cur.__cause__ if cur.__cause__ is not None else cur.__context__
    return None


#: How long the route waits for EITHER the executor task to finish (Tier-1
#: applied) OR the parked approval row to become durably visible (Tier-2
#: pending) before answering 503. The submit is an INSERT and the restrict
#: apply is one small transaction — generous, like the workflows window.
_OUTCOME_VISIBLE_TIMEOUT_S = 15.0
_POLL_S = 0.05

#: Strong references to parked expand waiters (asyncio tasks are otherwise
#: GC-eligible). The done-callback logs terminal failures — a rejected/expired
#: approval resolves the waiter with ApprovalNotGrantedError, which is the
#: expected deny leg, already audited by the executor (tool.approval[denied]).
_PARKED_TASKS: set[asyncio.Task[str]] = set()


def _watch(task: asyncio.Task[str]) -> None:
    _PARKED_TASKS.add(task)

    def _done(t: asyncio.Task[str]) -> None:
        _PARKED_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            # The deny/expiry legs land here — audited upstream by the executor;
            # log the class only (C12).
            _LOG.info("parked air-gap change resolved without applying: %s", type(exc).__name__)

    task.add_done_callback(_done)


async def _pending_request_ids(state: ApiState, action: str) -> set[str]:
    return {row.request_id for row in await state.gate.list_pending() if row.action == action}


@router.post("/v1/network/air-gap")
async def change_air_gap(
    body: AirGapChangeRequest,
    principal: _Principal,
    state: _State,
) -> GovernedChangeView:
    await require_role(
        principal,
        state.settings.ops_roles,
        sink=state.sink,
        audit_action="api.network.airgap.unauthorized",
        resource=_RESOURCE,
        http_detail="not authorized",
    )
    provider = state.egress_settings_provider
    if provider is None:
        raise RuntimeError(
            "ApiState.egress_settings_provider was not threaded from build_platform — "
            "the air-gap toggle requires the governed posture register"
        )
    current = provider()
    if current.air_gap == body.air_gap:
        raise HTTPException(
            status_code=409,
            detail=(
                f"air gap is already {'engaged' if current.air_gap else 'disengaged'}; "
                "nothing to change"
            ),
        )
    action = EGRESS_RESTRICT_ACTION if body.air_gap else EGRESS_EXPAND_ACTION

    # Idempotent pending read-back (§2.5): a change already parked for this
    # posture answers with the EXISTING durable row rather than minting a second
    # approval for the same flip.
    already = await _pending_request_ids(state, action)
    if already:
        return GovernedChangeView(status="pending", request_id=sorted(already)[0])

    args = {"air_gap": body.air_gap}
    task = asyncio.create_task(state.executor.execute(action, args, principal=principal))

    deadline = asyncio.get_running_loop().time() + _OUTCOME_VISIBLE_TIMEOUT_S
    while True:
        # Row check FIRST (the NE-WORKFLOWS observation-order lesson): a waiter
        # that dies AFTER its row was durably inserted leaves a pending row a
        # human can already see — the honest answer is that row's id, not the
        # dead task's error. A true refusal inserts no row and is reported from
        # the task branch below.
        fresh = await _pending_request_ids(state, action) - already
        if fresh:
            request_id = sorted(fresh)[0]
            _watch(task)
            await emit_surface_audit(
                state.sink,
                action="api.network.airgap.submitted",
                resource=_RESOURCE,
                outcome=AuditOutcome.success,
                actor=principal.id,
                detail={"status": "pending", "action": action, "request_id": request_id},
            )
            return GovernedChangeView(status="pending", request_id=request_id)

        if task.done():
            try:
                task.result()
            except (ToolDeniedError, GuardrailViolation) as exc:
                await emit_surface_audit(
                    state.sink,
                    action="api.network.airgap.denied",
                    resource=_RESOURCE,
                    outcome=AuditOutcome.denied,
                    actor=principal.id,
                    detail={"error_class": type(exc).__name__},
                )
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ApprovalNotGrantedError as exc:
                # A same-request-cycle rejection/expiry (rare — the row would have
                # to be decided within the visibility window).
                raise HTTPException(status_code=403, detail=f"approval not granted: {exc}") from exc
            except ActionValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ToolExecutionError as exc:
                # NE-AIRGAP-NO-APPROVAL: the ONE fixable refusal gets a legible
                # answer. The executor wraps every handler failure uniformly, so
                # walk the cause chain for the typed frontier-routes refusal and
                # answer 409 NAMING the offending routes — the operator's next
                # step is a repoint, not a stack trace. Everything else keeps
                # the generic 500 (the posture is unchanged either way).
                blocked = _frontier_routes_block(exc)
                if blocked is not None:
                    await emit_surface_audit(
                        state.sink,
                        action="api.network.airgap.denied",
                        resource=_RESOURCE,
                        outcome=AuditOutcome.denied,
                        actor=principal.id,
                        detail={
                            "error_class": type(blocked).__name__,
                            # Task/model ALIASES only (C12): deployment-chosen
                            # names, never an endpoint and never a credential.
                            "frontier_routes": ", ".join(blocked.routes),
                        },
                    )
                    raise HTTPException(status_code=409, detail=str(blocked)) from exc
                # The apply itself failed — the posture is UNCHANGED (the state
                # write and its chain record are one transaction; fail closed).
                raise HTTPException(
                    status_code=500,
                    detail="the change could not be applied; the posture is unchanged",
                ) from exc
            await emit_surface_audit(
                state.sink,
                action="api.network.airgap.submitted",
                resource=_RESOURCE,
                outcome=AuditOutcome.success,
                actor=principal.id,
                detail={"status": "applied", "action": action},
            )
            return GovernedChangeView(status="applied", request_id=None)

        if asyncio.get_running_loop().time() >= deadline:
            _watch(task)
            raise HTTPException(
                status_code=503,
                detail=(
                    "the change was submitted but its outcome is not yet visible; "
                    "re-read /v1/posture"
                ),
            )
        await asyncio.sleep(_POLL_S)
