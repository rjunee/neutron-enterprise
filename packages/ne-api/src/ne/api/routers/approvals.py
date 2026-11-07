# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Approvals routes (HITL surface, spec §1.4) — the REAL gate, no parallel machine.

Every approve/reject delegates to :class:`ne.runtime.approvals.DurableApprovalGate`
(``approve``/``reject``); ne-api NEVER constructs an ``ApprovalOutcome``, NEVER
writes an approval row, NEVER re-implements tier logic. The approver is ALWAYS
``principal.id`` from the verified bearer — the request body is EMPTY with
``extra='forbid'`` so a smuggled ``{"approver": ...}`` is a loud 422 that never
reaches the gate.

The MANDATORY fail-closed role pre-check (``require_role``) runs FIRST on every
route (the ne-chat/approvals.py:112-119 pattern): empty ``approver_roles`` ⇒ 403
for EVERYONE, disjoint groups ⇒ 403, and the gate is NEVER called on failure —
because the gate applies no ROLE policy of its own for tier<3, so this role gate
IS the tier-2 authorization layer. ne-api layers its own redacted
surface-origin audit records (``api.approval.submitted``/``.unauthorized``/
``.stale``) on top of the gate's own events.

SEPARATION OF DUTIES is a SECOND, orthogonal check the role gate never made:
"is this principal AN approver" is not "is this principal THE REQUESTER". The
role gate only kept the two apart on the demo box because ``ops`` was never put
in the ``approver`` group — a role ASSIGNMENT, not a rule, while
``docs/ops/air-gap-recovery.md`` §2 promises the rule ("the requester cannot
approve their own lift"). The rule now lives in ONE predicate,
:func:`ne.runtime.approvals.refuse_self_approval`, applied at both decide paths:
``_refuse_requester_deciding`` here, INSIDE each branch, so the run-join path
(which records through the checkpoint coordinator and never reaches the gate)
and the standalone path refuse identically and audit the same reason; and
``DurableApprovalGate.approve`` itself, which is what every non-HTTP caller
(ne-chat, the MCP demo drivers, any direct library user) goes through. A
principal holding BOTH capabilities is refused 403, audited
``api.approval.unauthorized`` with ``reason="requester-may-not-decide"``.

⚠ INSIDE each branch, NOT hoisted above them, and that is load-bearing rather
than stylistic. On the run-join path the ONLY thing that terminalizes a run
whose owner authorization expired is a decide call reaching
``_refuse_expired_authorization``; a refusal that returns before it does not
reorder two refusals, it deletes the terminalization, and the run sits in
``waiting_approval`` forever with nothing left able to finish it. Expiry is a
fact about the RUN, separation of duties a fact about the DECIDER, and the run
is reconciled first. Nothing is granted in between: ``decider_refused`` turns
the standing-approval carryover off, so the refused decider's decision is not
preserved, and the 403 is still the answer they get.

``GET /v1/approvals/{request_id}/payload`` (owner ruling 2026-07-31): an
approver who cannot see what they are approving is not approving anything, so
this route discloses target resource / material parameters / reversibility for
ONE request. Authorization is TWO layers, narrowest wins: (1) the SAME
``_require_approver`` role pre-check as every other route — a non-approver
learns nothing, 403 audited; (2) THEN the gate's OWN named-approver policy
(``DurableApprovalGate.read_payload``) — for Tier-3, only the configured
``tier3_named_approvers`` may read (an approver-role holder outside that list
is refused, 403, with a body carrying NO payload fields — the gate's
``ApprovalPolicyError`` message names the policy, never the data). The LISTING
(``GET /v1/approvals/pending``) stays byte-identical payload-free — this route
is the ONLY payload-disclosure path, and it is per-request, named-approver-
gated, and audited (``api.approval.payload_read`` / ``.unauthorized``).

Errors import from ``ne.runtime.errors`` (the approval error family) — NEVER from
``ne.runtime.middleware`` (the guardian).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException

from ne.api.authz import emit_surface_audit, require_role
from ne.api.deps import (
    ApiState,
    api_state,
    bind_authenticated_request_context,
    current_principal,
)
from ne.api.wire import (
    ApprovalActionResultView,
    ApprovalPayloadView,
    ApprovalRunContextView,
    ApproveRequest,
    PendingApprovalsView,
    RejectRequest,
    approval_payload_view,
    pending_approval_view,
)
from ne.contracts.types import Principal
from ne.events import new_approval_event
from ne.runtime.approvals import (
    ApprovalPayloadDisclosure,
    refuse_self_approval,
    reversibility_label,
)
from ne.runtime.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalPolicyError,
    ApprovalRequestNotFoundError,
    ApprovalSourceWithheldFromApproverError,
    RuntimeAuditError,
)
from ne.runtime.run_checkpoint import StandingDecision
from ne.runtime.run_store import RunState
from ne.runtime.run_workflow import (
    AUTHORIZATION_EXPIRED_REASON,
    drain_run_approval_audit_intent,
)
from ne.schema import ApprovalDecision, AuditOutcome

router = APIRouter(dependencies=[Depends(bind_authenticated_request_context)])

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]

_RESOURCE = "api:approval"


async def _run_join(state: ApiState, request_id: str):
    checkpoints = getattr(state.gate, "_run_approval_checkpoints", None)
    if checkpoints is None:
        return None, None
    runtime_engine = getattr(state.agent_run_store, "_engine", None)
    try:
        joined = await checkpoints.get_join(request_id)
    except LookupError as exc:
        # A join row exists but cannot prove its park-time governance metadata
        # (legacy row predating the tier/action binding — deliberately never
        # backfilled from the mutable request row; Sol review, P1). Refuse HERE:
        # falling through to the standalone gate would let a run-bound row be
        # read or decided outside its run-bound governance.
        raise HTTPException(status_code=409, detail="approval context unavailable") from exc
    if (
        joined is not None
        and state.agent_run_store is not None
        and runtime_engine is not None
        and hasattr(checkpoints, "pending_audit_intent")
    ):
        # BACKFILL THE MANDATORY APPROVAL RECEIPT before this route decides
        # anything.  A park that predates the park-leg drain (pre-deploy rows)
        # has an unclaimed audit-outbox intent and NO `decision_kind='approval'`
        # Run receipt, so every decision path that composes a terminal —
        # expiry, source withholding, reject reconciliation — raises inside
        # `RunFinalizer._observed_approvals`, rolls back, and the approver sees
        # a 500 for a park the platform itself left undrainable.  The drain is
        # idempotent (epoch-stable deterministic audit address) and a no-op for
        # every post-deploy park.
        try:
            await drain_run_approval_audit_intent(
                checkpoints=checkpoints,
                sink=state.sink,
                audit_engine=state.audit_engine,
                runtime_engine=runtime_engine,
                run_store=state.agent_run_store,
                run_id=str(joined.run_id),
                attempt_id=str(joined.attempt_id),
                owner_principal_id=str(joined.principal_id),
                approval_id=str(joined.approval_id),
            )
        except LookupError as exc:
            # The drain reads the ACCEPTED run row for the pinned config hash
            # (Sol round 2, P1); a join whose run cannot be resolved is context
            # this route must not decide on. The duplicate-receipt refusal is a
            # RuntimeError and deliberately stays LOUD (operator intervention).
            raise HTTPException(status_code=409, detail="approval context unavailable") from exc
    return checkpoints, joined


async def _standing_decision_for(
    state: ApiState,
    *,
    principal: Principal,
    checkpoints: Any,
    request_id: str,
    row: Any,
) -> StandingDecision | None:
    """The COMPLETE approver decision to preserve, or ``None`` to preserve nothing.

    Deciding is an APPROVER-authority act. This approver's own bearer is fresh,
    and their authority over the parked material is re-checked HERE, live,
    before anything is written — so recording the decision asserts exactly one
    thing (this named human approved this exact effect) and nothing at all
    about whether the run owner's groups are still current.

    ``None`` on every ambiguity, and the ambiguities are worth naming:

    * a Tier-3 FIRST check has not produced a decision at all — one named
      approver is half a ceremony, and half a ceremony must leave no carryover;
    * a second Tier-3 approver who is the first approver is not a second
      approver;
    * an approver whose own source authority does not admit the parked
      invocation may not decide on material they cannot see (403 territory —
      but the attempt is already dead, so the caller still gets the
      ``authorization_expired`` truth rather than a lesser one);
    * retryable-unknown authority proves nothing either way.
    """

    if checkpoints is None:
        return None
    try:
        await checkpoints.check_decision_authority(
            request_id, guard=state.source_guard, operation="approve"
        )
    except (
        ApprovalSourceWithheldFromApproverError,
        LookupError,
        RuntimeError,
    ):
        return None
    tier = int(row.tier)
    status = str(row.status)
    if tier < 3:
        return StandingDecision(approver=principal.id) if status == "pending" else None
    if principal.id not in state.gate._settings.approvals.tier3_named_approvers:
        return None
    if status != "pending_second":
        return None
    first_approver = str(row.first_approver or "")
    if not first_approver or first_approver == principal.id:
        return None
    return StandingDecision(approver=first_approver, second_approver=principal.id)


def _requester_is_deciding(*, principal: Principal, requester_id: str) -> bool:
    """The separation-of-duties rule asked as a QUESTION rather than a refusal.

    Same ONE predicate (:func:`ne.runtime.approvals.refuse_self_approval`) and
    therefore the same answer as :func:`_refuse_requester_deciding` — the rule
    is not restated here, only consulted. The run-join decide path has to know
    whether this decider is about to be refused BEFORE it reconciles the run,
    because a refused decider's decision may not be preserved for a successor.
    """

    try:
        refuse_self_approval(requester_id=requester_id, approver=principal.id)
    except ApprovalPolicyError:
        return True
    return False


async def _refuse_expired_authorization(
    state: ApiState,
    *,
    principal: Principal,
    checkpoints: Any,
    joined: Any,
    row: Any = None,
    preserve_decision: bool = False,
    decider_refused: bool = False,
) -> None:
    """Refuse a decision on a continuation whose OWNER authorization already ran out.

    The approver's own bearer is fresh — that is not the question. The question
    is whether the RUN OWNER's stored authorization snapshot
    (``ne_agent_run_authenticated_contexts``, INSERT-ONLY, carrying the access
    token's absolute ``exp``) is still unexpired, because that snapshot is the
    only honest bound on the claim that the owner's groups are still current:
    this platform has no revocation or entitlement service.

    Past that expiry THIS ATTEMPT can never resume — the guard refuses it with
    :class:`AuthorizationExpiredError`, and reviving, rebinding, or
    re-authorizing as the service identity are all RED. So the parked approval
    and its checkpoint are made non-actionable and the attempt fails terminally
    with safe reason ``authorization_expired``. That half is unchanged, and it
    is the security half: nothing executes, nothing is disclosed.

    F1 — WHAT SURVIVES. ``preserve_decision`` says this call is an APPROVE, so
    the human's decision is durably recorded as a STANDING APPROVAL in the same
    transaction that kills the attempt. The two facts are both true and neither
    softens the other: the decision was real, and the attempt is dead. The
    owner's next authenticated request admits a successor that re-parks at the
    same gate; on an exact binding-tuple match the gate consumes that standing
    approval and resumes under the SUCCESSOR's own fresh context, with live
    stamp re-checks and its own policy evaluation. Any drift parks for a fresh
    human decision.

    A REJECT deliberately preserves nothing (``preserve_decision`` stays
    ``False``). Denial needs no successor, so there is nothing for a carryover
    to carry; and recording a terminal "a human said no" over an attempt that
    was already dead would claim a weighing that did not decide anything. The
    SPEC-named truth for that attempt is ``authorization_expired``.

    ``decider_refused`` says the CALLER is going to be refused on separate,
    identity-based grounds (separation of duties) once this returns. The run is
    reconciled anyway — a dead attempt is dead whoever asks, and this call is
    the only thing that terminalizes it — but two things change: nothing is
    preserved (``preserve_decision`` is forced off, because a decider who may
    not decide has produced no approver-authority evidence), and the 409s below
    are not raised, because the caller's own 403 is the answer this request
    gets. The audit records still fire, so what happened to the run is on the
    record either way; only the HTTP answer defers to the stronger refusal.

    THE OBSERVABLE MATCHES REALITY. ``terminalize_authorization_expired``
    answers ``False`` when the (approval, checkpoint, run) tuple is outside its
    fail-closed ``actionable`` set, and that answer is BRANCHED ON here. The
    terminalized path is unchanged — ``api.approval.authorization_expired`` and
    the existing 409. The non-terminalized path emits the ordinary
    "refused, nothing changed" record (``api.approval.stale``) and a 409 that
    says the run was NOT terminalized by this request, because claiming a
    terminalization that did not happen makes the audit trail a liar about the
    one thing it exists to attest.
    """

    store = state.agent_run_store
    if store is None or checkpoints is None or joined is None:
        return
    try:
        context = await store.load_authenticated_context(
            run_id=str(joined.run_id), owner_principal_id=str(joined.principal_id)
        )
    except LookupError as exc:
        raise HTTPException(status_code=409, detail="approval context unavailable") from exc
    # DB-authoritative UTC, never this process's clock: the decision, the
    # snapshot and the terminalization all live in one database.
    now = await checkpoints.database_now()
    if now.timestamp() < context.expires_at:
        return
    decision: StandingDecision | None = None
    if preserve_decision and not decider_refused and row is not None:
        decision = await _standing_decision_for(
            state,
            principal=principal,
            checkpoints=checkpoints,
            request_id=str(joined.approval_id),
            row=row,
        )
    terminalized = await checkpoints.terminalize_authorization_expired(
        str(joined.approval_id),
        decided_at=now,
        decision=decision,
        standing_event=(
            None
            if decision is None or row is None
            else new_approval_event(
                approval_id=str(joined.approval_id),
                tier=int(row.tier),
                decision=ApprovalDecision.approved,
                subject_ref=f"tool:{row.action}",
                actor=principal.id,
            )
        ),
    )
    if not terminalized:
        # THE TERMINALIZATION DID NOT HAPPEN, SO NOTHING MAY CLAIM IT DID.
        # ``terminalize_authorization_expired`` is fail-closed: it answers
        # ``False`` when the (approval, checkpoint, run) tuple is not in its
        # ``actionable`` set — e.g. a sweep already flipped the approval row to
        # ``expired`` while the checkpoint stayed ``pending`` and the run stayed
        # ``waiting_approval``. That guard is correct and is NOT widened here.
        #
        # What was wrong was this caller: it discarded the answer and emitted
        # ``api.approval.authorization_expired`` + the "nothing was sent or
        # written" 409 unconditionally, so the audit trail asserted a
        # terminalization that never occurred and the operator was told a run
        # had been handled while it sat in ``waiting_approval`` forever. In a
        # product whose whole claim is that its audit trail is true, an audit
        # row that overstates is worse than no row at all.
        #
        # So: the codebase's existing "refused, nothing changed" record
        # (``api.approval.stale``, the same action ``_audit_stale`` writes for
        # every other drifted-row refusal), with a reason that names THIS
        # refusal, and a 409 that says what actually happened. NO standing
        # lookup: nothing was minted by this call, so nothing may be reported
        # as preserved.
        await emit_surface_audit(
            state.sink,
            action="api.approval.stale",
            resource=_RESOURCE,
            outcome=AuditOutcome.denied,
            actor=principal.id,
            detail={
                "reason": "authorization-expired-not-terminalizable",
                "request_id": str(joined.approval_id),
            },
        )
        if decider_refused:
            # The refusal below belongs to the caller, not to this helper: it
            # is about WHO asked, and it is a 403 that grants nothing. The
            # ``stale`` record above already says the run was NOT terminalized.
            return
        raise HTTPException(
            status_code=409,
            detail=(
                f"{AUTHORIZATION_EXPIRED_REASON}: the run owner's authorization snapshot "
                "expired before this decision, so this attempt can no longer execute -- and "
                "the parked approval and its checkpoint were NOT in a state this request "
                "could terminalize, so THIS REQUEST CHANGED NOTHING and the run was not "
                "terminalized here. Nothing was sent or written and your decision was not "
                "recorded. The run needs an operator: it is not safely finished."
            ),
        )
    standing = (
        None
        if decision is None
        else await checkpoints.standing_approval_for(str(joined.approval_id))
    )
    detail: dict[str, Any] = {
        "reason": AUTHORIZATION_EXPIRED_REASON,
        "request_id": str(joined.approval_id),
    }
    if standing is not None:
        # The audit linkage starts here: the record that says a decision was
        # preserved names the standing row a later successor will consume.
        detail["standing_id"] = standing.standing_id
        detail["standing_lapses_at"] = standing.lapses_at.isoformat()
    await emit_surface_audit(
        state.sink,
        action="api.approval.authorization_expired",
        resource=_RESOURCE,
        outcome=AuditOutcome.denied,
        actor=principal.id,
        detail=detail,
    )
    # The approver is a HUMAN who just pressed approve on something that then
    # refused. A bare reason code told them nothing, and the one thing they most
    # need to know -- that they did NOT just send anything -- was left implied.
    # (docs/architecture/approval-horizon.md: say the right thing on 409.)
    #
    # The code stays the PREFIX so an existing consumer matching on it still
    # matches, and the audit `reason` above is untouched: that is the
    # machine-readable SPEC-named contract and it does not become prose.
    #
    # ⚠ ONE CASE WHERE THE "NOT recorded" BRANCH UNDERSTATES WHAT HAPPENS.
    # `standing_approval_for` looks the row up by THIS approval's id, and
    # `_mint_standing_approval` short-circuits when a live standing approval
    # already exists for the same binding tuple — so if another approver
    # already carried a bit-identical effect, this lookup answers None and this
    # approver is told nothing was recorded, while that earlier carryover will
    # in fact fire on the owner's next run. Their OWN decision really was not
    # recorded; the effect is not dead. Documented at the mint seam
    # (`_mint_standing_approval`) and deliberately not changed here.
    if decider_refused:
        # The run IS terminalized and the ``authorization_expired`` record above
        # says so truthfully. The caller still gets the separation-of-duties
        # 403: nothing was preserved (``decision`` is None by construction
        # above), so there is no carryover to report and none to spend.
        return
    if standing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{AUTHORIZATION_EXPIRED_REASON}: the run owner's authorization snapshot "
                "expired before this decision, so THIS attempt can no longer execute and "
                "nothing was sent or written. Your decision WAS recorded and stands until "
                f"{standing.lapses_at.isoformat()}: when the owner next signs in and "
                "retries this exact action, it runs without asking you again."
            ),
        )
    raise HTTPException(
        status_code=409,
        detail=(
            f"{AUTHORIZATION_EXPIRED_REASON}: the run owner's authorization snapshot "
            "expired before this decision, so the parked effect can no longer execute. "
            "Your decision was NOT recorded and nothing was sent or written. Approving "
            "again cannot revive this run -- the owner has to start a new one."
        ),
    )


async def _canonical_run_context(
    state: ApiState,
    *,
    request_id: str,
    joined: Any,
    row: Any,
) -> ApprovalRunContextView:
    """Resolve a content-free approval-scoped Run shell, or fail uniformly."""

    unavailable = HTTPException(status_code=409, detail="approval context unavailable")
    store = state.agent_run_store
    if not _canonical_run_binding_is_valid(
        state,
        request_id=request_id,
        joined=joined,
        row=row,
    ):
        raise unavailable
    assert store is not None
    run_id = str(getattr(joined, "run_id", ""))
    owner_principal_id = str(getattr(joined, "principal_id", ""))
    run = await store.get_run(run_id=run_id, owner_principal_id=owner_principal_id)
    if (
        run is None
        or run.run_id != run_id
        or run.owner_principal_id != owner_principal_id
        or run.owner_principal_id != str(row.principal_id)
        or run.status is not RunState.WAITING_APPROVAL
        or not run.agent_id
    ):
        raise unavailable
    return ApprovalRunContextView(
        approval_id=request_id,
        agent_id=run.agent_id,
        run_id=run_id,
    )


def _parked_governance_matches(joined: Any, row: Any) -> bool:
    """True iff decision-time tier/action equal the park-time join record (F2).

    ``tier`` selects which approval policy applies (Tier-3 dual-approval), so
    the mutable ``ne_approval_requests`` re-load must match the insert-only
    ``ne_agent_run_approval_joins`` row written in the park transaction. A join
    without the parked metadata fails closed.
    """
    parked_tier = getattr(joined, "tier", None)
    parked_action = getattr(joined, "action", None)
    try:
        return (
            parked_tier is not None
            and parked_action is not None
            and int(row.tier) == int(parked_tier)
            and str(row.action) == str(parked_action)
        )
    except (AttributeError, TypeError, ValueError):
        return False


async def _post_gate_failure_response(
    state: ApiState, *, request_id: str, audit_failed: bool
) -> HTTPException:
    """Report a standalone-gate runtime failure TRUTHFULLY (Sol rounds 2-3, P1).

    The gate commits its decision CAS, then the mandatory audit append, then
    metrics — so an exception on this surface can arrive with the row ALREADY
    TERMINAL (a failing sink raises :class:`RuntimeAuditError` post-CAS; a
    failing ``record_latency`` raises a plain ``RuntimeError`` post-CAS AND
    post-audit). A fixed "the request remains pending" answer is then a false
    statement about committed governance state — the same category as the false
    ``source_withheld`` reasons this unit corrected. So: durable state is
    RELOADED for EVERY such failure rather than enumerating which exception
    types can appear after the commit point, and the response says what
    actually happened. Never a 200 when the audit is missing: an audit-less
    success receipt would violate the fail-closed audit floor.
    """
    try:
        row = await state.gate._store.get(request_id)
        status = None if row is None else str(row.status)
    except Exception:  # the reporting seam must not mask the original failure
        status = None
    if status in {"approved", "rejected", "expired", "source_withheld"}:
        if audit_failed:
            return HTTPException(
                status_code=500,
                detail=(
                    f"the approval decision ({status}) is durably recorded but its audit "
                    "record failed to append; do NOT retry the decision — re-read the "
                    "approval and escalate the audit failure"
                ),
            )
        return HTTPException(
            status_code=500,
            detail=(
                f"the approval decision ({status}) is durably recorded and audited; a "
                "post-decision step failed — do NOT retry the decision, re-read the "
                "approval"
            ),
        )
    if status in {"pending", "pending_second"}:
        return HTTPException(
            status_code=503,
            detail=(
                "approval audit unavailable; the request remains pending"
                if audit_failed
                else "approval authority unavailable; the request remains pending"
            ),
        )
    return HTTPException(
        status_code=500,
        detail=(
            "approval state could not be determined after a decision-surface failure; "
            "re-read the approval before retrying"
        ),
    )


async def _refuse_source_withheld_for_principal(
    state: ApiState,
    *,
    principal: Principal,
    request_id: str,
    exc: ApprovalSourceWithheldFromApproverError,
) -> NoReturn:
    """Audit-then-403 for a principal whose OWN authority does not admit the payload.

    The request stays PENDING for another approver and nothing terminal was
    written (U2 adversarial review, F1): this refusal is a fact about THIS
    principal's ACL standing, never a source revocation, so it must never
    destroy the Run or mint a ``source_withheld`` record.
    """
    await emit_surface_audit(
        state.sink,
        action="api.approval.unauthorized",
        resource=_RESOURCE,
        outcome=AuditOutcome.denied,
        actor=principal.id,
        detail={
            "reason": "source-authority-withheld-for-principal",
            "request_id": request_id,
            "error_class": type(exc).__name__,
        },
    )
    raise HTTPException(status_code=403, detail=str(exc)) from exc


async def _check_decision_source_authority(
    state: ApiState,
    *,
    principal: Principal,
    checkpoints: Any,
    request_id: str,
    operation: str,
) -> None:
    """The decide-path authority floor: stamp re-evaluation, zero disclosure.

    Neither decide path consumes the decrypted arguments (the sole consumer is
    the payload GET), so this calls the coordinator's non-materializing check —
    protected customer content is no longer parsed into API memory just to
    obtain an authority answer.
    """
    try:
        await checkpoints.check_decision_authority(
            request_id, guard=state.source_guard, operation=operation
        )
    except ApprovalSourceWithheldFromApproverError as exc:
        await _refuse_source_withheld_for_principal(
            state, principal=principal, request_id=request_id, exc=exc
        )
    except LookupError as exc:
        raise HTTPException(status_code=409, detail="approval context unavailable") from exc
    except RuntimeError as exc:
        # Retryable-unknown authority: the request is STILL PENDING and the
        # decision may simply be retried. 503, never 409 — the console reads
        # 409 as "already decided", which would be false here.
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _canonical_run_binding_is_valid(
    state: ApiState,
    *,
    request_id: str,
    joined: Any,
    row: Any,
) -> bool:
    """Validate content-free request/join/row identity before disclosure policy."""

    return bool(
        state.agent_run_store is not None
        and str(getattr(joined, "approval_id", "")) == request_id
        and str(getattr(row, "request_id", "")) == request_id
        and str(getattr(joined, "principal_id", "")) == str(getattr(row, "principal_id", ""))
        and str(getattr(row, "status", "")) in {"pending", "pending_second"}
        and str(getattr(joined, "run_id", ""))
        and str(getattr(joined, "principal_id", ""))
    )


async def _require_canonical_payload_reader(
    state: ApiState,
    *,
    principal: Principal,
    request_id: str,
    row: Any,
) -> None:
    """Apply the gate's Tier-3 named-reader policy to a canonical Run row."""

    named = state.gate._settings.approvals.tier3_named_approvers
    if int(row.tier) < 3 or principal.id in named:
        return
    error = ApprovalPolicyError(
        "payload withheld: not a named approver for this request (Tier-3 "
        "payload is disclosed only to the configured named-approver list, "
        f"size {len(named)}; other approvers may see the entry exists but not read it)"
    )
    await emit_surface_audit(
        state.sink,
        action="api.approval.unauthorized",
        resource=_RESOURCE,
        outcome=AuditOutcome.denied,
        actor=principal.id,
        detail={
            "reason": "payload-withheld",
            "request_id": request_id,
            "error_class": type(error).__name__,
        },
    )
    raise HTTPException(status_code=403, detail=str(error))


async def _enqueue_run_resume(state: ApiState, checkpoints: Any, request_id: str) -> object:
    enqueuer = state.agent_run_enqueuer
    if enqueuer is None or not hasattr(enqueuer, "enqueue_approval_resume"):
        raise HTTPException(status_code=503, detail="Run approval resume queue unavailable")
    request = await checkpoints.load_resume_request(request_id)
    try:
        await enqueuer.enqueue_approval_resume(request)
    except Exception as exc:
        # The decision and exact resume intent are already durable. Repeating
        # the same decision reloads this intent and retries the same DBOS id.
        raise HTTPException(status_code=503, detail="Run approval resume enqueue failed") from exc
    return request


async def _require_approver(principal: Principal, state: ApiState, request_id: str | None) -> None:
    await require_role(
        principal,
        state.settings.approver_roles,
        sink=state.sink,
        audit_action="api.approval.unauthorized",
        resource=_RESOURCE,
        http_detail="not an authorized approver",
        request_id=request_id,
    )


async def _refuse_requester_deciding(
    state: ApiState, *, principal: Principal, request_id: str, requester_id: str
) -> None:
    """Separation of duties at the API: the requester may never decide.

    The role pre-check above answers "is this principal AN approver"; it never
    answered "is this principal THE REQUESTER". On the demo box those are
    different people only because ``ops`` was never put in the ``approver``
    group — a role ASSIGNMENT, not a rule, and
    ``docs/ops/air-gap-recovery.md`` §2 promises the rule. This is that rule at
    the API, applied on BOTH decide branches — the run-join branch records
    through the checkpoint coordinator and never reaches
    :meth:`DurableApprovalGate.approve`, and the standalone branch's own gate
    refusal would be audited as the generic ``gate-policy``. The gate keeps the
    same :func:`refuse_self_approval` for every non-HTTP caller.

    It is called INSIDE each branch and never hoisted above them: the run-join
    branch must reconcile an expired authorization first, or this refusal
    silently deletes the terminalization of a run only a decide call can
    finish. See the call site and :func:`_refuse_expired_authorization`.
    """

    try:
        refuse_self_approval(requester_id=requester_id, approver=principal.id)
    except ApprovalPolicyError as exc:
        await emit_surface_audit(
            state.sink,
            action="api.approval.unauthorized",
            resource=_RESOURCE,
            outcome=AuditOutcome.denied,
            actor=principal.id,
            detail={"reason": "requester-may-not-decide", "request_id": request_id},
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc


async def _require_approver_dep(request_id: str, principal: _Principal, state: _State) -> None:
    """Route dependency: run the fail-closed approver role check (403 + audited
    ``api.approval.unauthorized``) BEFORE FastAPI validates the request body.

    FastAPI solves a route's ``dependencies`` before validating the endpoint's own
    body field, so an authenticated-but-unauthorized caller sending a malformed
    body is still rejected-and-audited (403), never 422'd unaudited (Codex P2).
    """
    await _require_approver(principal, state, request_id)


@router.get("/v1/approvals/pending")
async def list_pending(principal: _Principal, state: _State) -> PendingApprovalsView:
    await _require_approver(principal, state, None)
    rows = await state.gate.list_pending()
    return PendingApprovalsView(
        approvals=[
            pending_approval_view(
                row, authorization_expires_at=await _authorization_horizon(state, row.request_id)
            )
            for row in rows
        ]
    )


async def _authorization_horizon(state: ApiState, request_id: str) -> datetime | None:
    """When this approval's RUN OWNER authorization runs out, or ``None``.

    The pending queue's own ``expires_at`` is the approval REQUEST's deadline —
    how long the human has to answer, resolved by policy per action (ADR-0030).
    This is a DIFFERENT and shorter clock: the owner's frozen authorization
    snapshot, which on the demo box dies ~15 minutes into the run.

    ⭐ Since F1 it is no longer the decision deadline either, and the difference
    matters to whoever renders it. Crossing this horizon still terminalizes THIS
    attempt — nothing executes, nothing is disclosed — but an APPROVE past it is
    now recorded as a standing approval and honoured by a successor the owner's
    next sign-in admits. So this value answers "after this moment, this attempt
    is dead and the effect will need the owner to sign in again", not "after
    this moment your decision is worthless". Surfacing it remains the "show the
    horizon" mitigation from docs/architecture/approval-horizon.md; it is now an
    honesty aid rather than a countdown to a lost decision.

    Returns ``None`` — meaning "this approval has no owner-snapshot horizon",
    never "unknown" — for a standalone approval, and also when the join or the
    snapshot cannot be read. A queue read must never fail because one row's
    horizon is unresolvable: the decision routes still enforce the expiry
    themselves (``_refuse_expired_authorization``), so this is a display aid,
    not the guard.

    One join per pending row. The pending queue is small by construction (it is
    the human work queue, not a log), so this is deliberately simple rather
    than batched; if it ever grows, batch it here.
    """

    store = state.agent_run_store
    if store is None:
        return None
    try:
        _checkpoints, joined = await _run_join(state, request_id)
        if joined is None:
            return None
        context = await store.load_authenticated_context(
            run_id=str(joined.run_id), owner_principal_id=str(joined.principal_id)
        )
    except (LookupError, HTTPException):
        return None
    return datetime.fromtimestamp(context.expires_at, tz=UTC)


@router.get("/v1/approvals/{request_id}/payload")
async def read_payload(
    request_id: str,
    principal: _Principal,
    state: _State,
) -> ApprovalPayloadView:
    """The governed, audited, per-request disclosure read (owner ruling 2026-07-31).

    Layer 1 (role pre-check, same as every other approvals route): a caller
    outside ``approver_roles`` learns NOTHING — 403, audited, gate never
    reached. Layer 2 (the gate's own named-approver policy): an approver-role
    holder who is NOT the named approver for THIS request is refused (403) with
    a body carrying no payload fields — proven by
    ``test_role_holder_not_named_is_refused_but_sees_entry`` — while
    ``GET /v1/approvals/pending`` still shows them the entry EXISTS (id, tier,
    action) — that route is unaffected by this one.
    """
    await _require_approver(principal, state, request_id)
    checkpoints, joined = await _run_join(state, request_id)
    if joined is not None and checkpoints is not None:
        if state.source_guard is None:
            raise HTTPException(status_code=503, detail="approval authority unavailable")
        row = await state.gate._store.get(request_id)
        if (
            row is None
            or not _canonical_run_binding_is_valid(
                state,
                request_id=request_id,
                joined=joined,
                row=row,
            )
            or not _parked_governance_matches(joined, row)
        ):
            raise HTTPException(status_code=409, detail="approval context unavailable")
        await _require_canonical_payload_reader(
            state,
            principal=principal,
            request_id=request_id,
            row=row,
        )
        try:
            arguments = await checkpoints.authorize(
                request_id,
                guard=state.source_guard,
                operation="detail",
                decided_at=datetime.now(UTC),
            )
        except ApprovalSourceWithheldFromApproverError as exc:
            # A READ by a principal whose own authority does not admit the
            # material is a 403 for THAT reader and nothing else (F1): the
            # request stays pending, the Run stays parked, and no revocation
            # is asserted anywhere.
            await _refuse_source_withheld_for_principal(
                state, principal=principal, request_id=request_id, exc=exc
            )
        except (LookupError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail="approval context unavailable") from exc
        if arguments is None:
            raise HTTPException(status_code=409, detail="approval content withheld")
        run_context = await _canonical_run_context(
            state,
            request_id=request_id,
            joined=joined,
            row=row,
        )
        payload = approval_payload_view(
            ApprovalPayloadDisclosure(
                request_id=request_id,
                action=str(row.action),
                tier=int(row.tier),
                principal_id=str(row.principal_id),
                target_resource="protected Run action",
                description="Protected source-derived Run action",
                material_parameters=arguments,
                reversibility=reversibility_label(int(row.tier)),
            )
        )
        # The canonical branch materializes the protected invocation through
        # the checkpoint rather than DurableApprovalGate.read_payload().  Emit
        # the same library audit contract explicitly, then the API-surface
        # audit, before returning any disclosure (C6 fail closed).
        await state.gate._audit(
            action="approval.payload_read",
            resource=f"tool:{row.action}",
            outcome=AuditOutcome.success,
            actor=principal.id,
            detail={"tier": int(row.tier), "action": str(row.action)},
            provenance={"approval_id": request_id, "approver": principal.id},
        )
        await emit_surface_audit(
            state.sink,
            action="api.approval.payload_read",
            resource=_RESOURCE,
            outcome=AuditOutcome.success,
            actor=principal.id,
            detail={"request_id": request_id},
        )
        return payload.model_copy(
            update={
                "disclosure_status": "available",
                "run_context": run_context,
            }
        )
    try:
        disclosure = await state.gate.read_payload(request_id, approver=principal.id)
    except ApprovalSourceWithheldFromApproverError as exc:
        # Standalone-gate mirror of the canonical-branch rule (U2 ruling): the
        # reader's own authority does not admit the material — 403 for THAT
        # reader, the request stays pending, nothing terminal was written.
        await _refuse_source_withheld_for_principal(
            state, principal=principal, request_id=request_id, exc=exc
        )
    except ApprovalPolicyError as exc:
        await emit_surface_audit(
            state.sink,
            action="api.approval.unauthorized",
            resource=_RESOURCE,
            outcome=AuditOutcome.denied,
            actor=principal.id,
            detail={
                "reason": "payload-withheld",
                "request_id": request_id,
                "error_class": type(exc).__name__,
            },
        )
        # str(exc) is a library-authored safe message carrying NO material
        # values (only ids/tier/list-size) — the console renders it verbatim.
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequestNotFoundError as exc:
        await _audit_stale(state, principal, request_id, exc)
        raise HTTPException(status_code=404, detail="approval request not found") from exc
    except ApprovalAlreadyDecidedError as exc:
        await _audit_stale(state, principal, request_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeAuditError as exc:
        # Post-CAS audit failure: the decision may already be COMMITTED — the
        # response must report the row's actual state, never claim "pending"
        # over a terminal row (Sol round 2, P1).
        raise await _post_gate_failure_response(
            state, request_id=request_id, audit_failed=True
        ) from exc
    except RuntimeError as exc:
        # Typed gate refusals (e.g. SourceAuthorityRetryableError, row still
        # PENDING) must not escape as 500s (Sol round 1) — but a plain
        # RuntimeError can ALSO arrive after the decision AND audit committed
        # (a failing metrics step), so the answer is chosen from RELOADED
        # durable state, never a fixed "remains pending" claim (Sol slice 2,
        # P1). Fixed safe details either way — an arbitrary RuntimeError's
        # text is not proven safe to serve.
        raise await _post_gate_failure_response(
            state, request_id=request_id, audit_failed=False
        ) from exc

    await emit_surface_audit(
        state.sink,
        action="api.approval.payload_read",
        resource=_RESOURCE,
        outcome=AuditOutcome.success,
        actor=principal.id,
        detail={"request_id": request_id},
    )
    return approval_payload_view(disclosure)


@router.post("/v1/approvals/{request_id}/approve", dependencies=[Depends(_require_approver_dep)])
async def approve(
    request_id: str,
    body: ApproveRequest,
    principal: _Principal,
    state: _State,
) -> ApprovalActionResultView:
    # Authorization already enforced-and-audited by _require_approver_dep (runs
    # before body validation). body: ApproveRequest is the empty extra=forbid model.
    #
    # Separation of duties is applied on BOTH decide branches, with the SAME
    # predicate and the SAME audit reason — but INSIDE each branch, not hoisted
    # above them. It cannot be hoisted: the run-join branch has a reconciliation
    # of its own to run first (see there), and running the decider refusal ahead
    # of it strands the very runs that reconciliation exists to finish.
    checkpoints, joined = await _run_join(state, request_id)
    if joined is not None and checkpoints is not None:
        if state.source_guard is None:
            raise HTTPException(status_code=503, detail="approval authority unavailable")
        if state.agent_run_enqueuer is None or not hasattr(
            state.agent_run_enqueuer, "enqueue_approval_resume"
        ):
            raise HTTPException(status_code=503, detail="Run approval resume queue unavailable")
        row = await state.gate._store.get(request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="approval request not found")
        # The decide path applies AT LEAST the read path's consistency floor
        # (F2): the content-free binding check while the row is still
        # actionable, plus the park-time tier/action equality always — the
        # policy branch below must never run on drifted governance metadata.
        if not _parked_governance_matches(joined, row):
            raise HTTPException(status_code=409, detail="approval context unavailable")
        if str(row.status) in {"pending", "pending_second"} and not _canonical_run_binding_is_valid(
            state,
            request_id=request_id,
            joined=joined,
            row=row,
        ):
            raise HTTPException(status_code=409, detail="approval context unavailable")
        # THE RUN IS RECONCILED BEFORE THE DECIDER IS JUDGED.
        #
        # Whether the owner's authorization ran out is a fact about the RUN, not
        # about who is asking: past that horizon the attempt can never resume no
        # matter who presses the button, and the ONLY thing that terminalizes it
        # is a decide call reaching `_refuse_expired_authorization`. Refusing the
        # decider first therefore does not merely reorder two refusals — it
        # deletes the terminalization outright, and a dual-role requester's
        # expired run is left parked in `waiting_approval` with nothing left able
        # to finish it. So: reconcile the run, THEN refuse the decider.
        #
        # Nothing is disclosed or granted in between. `decider_refused` is the
        # separation-of-duties predicate asked as a QUESTION, and it turns the
        # F1 carryover OFF: a decider who may not decide has not produced
        # approver-authority evidence, so there is nothing to preserve for a
        # successor to consume. It also tells the helper to reconcile-and-audit
        # WITHOUT raising, because the 403 below is the answer this caller gets.
        decider_refused = _requester_is_deciding(
            principal=principal, requester_id=str(row.principal_id)
        )
        # BEFORE anything is recorded or disclosed: an approval whose run-owner
        # authorization has already run out can never be resumed, so it must be
        # refused here rather than answered with a 200 APPROVED receipt for a
        # continuation that is already dead.
        await _refuse_expired_authorization(
            state,
            principal=principal,
            checkpoints=checkpoints,
            joined=joined,
            row=row,
            # F1: an APPROVE past the horizon still refuses THIS attempt, but the
            # human's decision is durable approver-authority evidence and is
            # recorded as a standing approval in the same transaction.
            preserve_decision=True,
            decider_refused=decider_refused,
        )
        # Separation of duties: same predicate, same reason, same 403 — the
        # run-join branch records through the checkpoint coordinator and never
        # reaches the gate, so this is the only place it can be enforced here.
        await _refuse_requester_deciding(
            state,
            principal=principal,
            request_id=request_id,
            requester_id=str(row.principal_id),
        )
        now = datetime.now(UTC)
        # Authority floor WITHOUT disclosure (F1): the decrypted arguments are
        # consumed by no decide path, so nothing is materialized here; a
        # non-authorized approver is a 403 and the request stays pending for
        # another approver — never a terminal ``source_withheld``.
        await _check_decision_source_authority(
            state,
            principal=principal,
            checkpoints=checkpoints,
            request_id=request_id,
            operation="approve",
        )
        first_approver = principal.id
        second_approver: str | None = None
        if int(row.tier) >= 3:
            named = state.gate._settings.approvals.tier3_named_approvers
            if principal.id not in named:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "approver is not in the Tier-3 named-approver list "
                        f"(list size {len(named)}; empty ⇒ never approvable)"
                    ),
                )
            if str(row.status) == "pending":
                landed_first = await checkpoints.record_first_approval(
                    request_id,
                    expected_action_hash=joined.action_hash,
                    principal_id=joined.principal_id,
                    policy_hash=joined.policy_hash,
                    approver=principal.id,
                    decided_at=now,
                    event=new_approval_event(
                        approval_id=request_id,
                        tier=int(row.tier),
                        decision=ApprovalDecision.requested,
                        subject_ref=f"tool:{row.action}",
                        actor=principal.id,
                    ),
                )
                if not landed_first:
                    raise HTTPException(status_code=409, detail="approval already decided")
                return ApprovalActionResultView(status="pending_second")
            first_approver = str(row.first_approver or "")
            if not first_approver or first_approver == principal.id:
                raise HTTPException(
                    status_code=403,
                    detail="the second Tier-3 approver must be distinct from the first",
                )
            second_approver = principal.id
        landed = await checkpoints.decide(
            request_id,
            status="approved",
            expected_action_hash=joined.action_hash,
            principal_id=joined.principal_id,
            policy_hash=joined.policy_hash,
            approver=first_approver,
            second_approver=second_approver,
            reason=None,
            decided_at=now,
            event=new_approval_event(
                approval_id=request_id,
                tier=int(row.tier),
                decision=ApprovalDecision.approved,
                subject_ref=f"tool:{row.action}",
                actor=principal.id,
            ),
        )
        decided_approver = first_approver
        decided_at = now
        if not landed:
            try:
                resume = await checkpoints.load_resume_request(request_id)
            except LookupError as exc:
                raise HTTPException(status_code=409, detail="approval already decided") from exc
            decided = await state.gate._store.get(request_id)
            if (
                resume.decision != "approved"
                or decided is None
                or (decided.second_approver if int(decided.tier) >= 3 else decided.first_approver)
                != principal.id
            ):
                raise HTTPException(status_code=409, detail="approval already decided")
            decided_approver = decided.first_approver
            decided_at = decided.decided_at
        await _enqueue_run_resume(state, checkpoints, request_id)
        return ApprovalActionResultView(
            status="approved",
            approver=decided_approver,
            decided_at=decided_at,
        )
    # Standalone branch (no run join): the gate below applies the same
    # `refuse_self_approval`, but its `ApprovalPolicyError` would be audited as
    # the generic "gate-policy". Ask here so this refusal names itself. A
    # missing row is left to the gate's own 404.
    submitted = await state.gate._store.get(request_id)
    if submitted is not None:
        await _refuse_requester_deciding(
            state,
            principal=principal,
            request_id=request_id,
            requester_id=str(submitted.principal_id),
        )
    try:
        outcome = await state.gate.approve(request_id, approver=principal.id)
    except ApprovalSourceWithheldFromApproverError as exc:
        await _refuse_source_withheld_for_principal(
            state, principal=principal, request_id=request_id, exc=exc
        )
    except ApprovalPolicyError as exc:
        await emit_surface_audit(
            state.sink,
            action="api.approval.unauthorized",
            resource=_RESOURCE,
            outcome=AuditOutcome.denied,
            actor=principal.id,
            detail={
                "reason": "gate-policy",
                "request_id": request_id,
                "error_class": type(exc).__name__,
            },
        )
        # str(exc) is a library-authored safe message; the console renders the
        # Tier-3 named-approver-list message VERBATIM (do not rewrite it).
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequestNotFoundError as exc:
        await _audit_stale(state, principal, request_id, exc)
        raise HTTPException(status_code=404, detail="approval request not found") from exc
    except ApprovalAlreadyDecidedError as exc:
        await _audit_stale(state, principal, request_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeAuditError as exc:
        # Post-CAS audit failure: the decision may already be COMMITTED — the
        # response must report the row's actual state, never claim "pending"
        # over a terminal row (Sol round 2, P1).
        raise await _post_gate_failure_response(
            state, request_id=request_id, audit_failed=True
        ) from exc
    except RuntimeError as exc:
        # Typed gate refusals (e.g. SourceAuthorityRetryableError, row still
        # PENDING) must not escape as 500s (Sol round 1) — but a plain
        # RuntimeError can ALSO arrive after the decision AND audit committed
        # (a failing metrics step), so the answer is chosen from RELOADED
        # durable state, never a fixed "remains pending" claim (Sol slice 2,
        # P1). Fixed safe details either way — an arbitrary RuntimeError's
        # text is not proven safe to serve.
        raise await _post_gate_failure_response(
            state, request_id=request_id, audit_failed=False
        ) from exc

    if outcome is None:
        status = "pending_second"
        result = ApprovalActionResultView(status=status)
    else:
        status = outcome.status.value
        result = ApprovalActionResultView(
            status=status, approver=outcome.approver, decided_at=outcome.decided_at
        )
    await emit_surface_audit(
        state.sink,
        action="api.approval.submitted",
        resource=_RESOURCE,
        outcome=AuditOutcome.success,
        actor=principal.id,
        detail={"decision": "approve", "request_id": request_id, "status": status},
    )
    return result


@router.post("/v1/approvals/{request_id}/reject", dependencies=[Depends(_require_approver_dep)])
async def reject(
    request_id: str,
    body: RejectRequest,
    principal: _Principal,
    state: _State,
) -> ApprovalActionResultView:
    # Authorization already enforced-and-audited by _require_approver_dep (runs
    # before body validation), so an unauthorized malformed-body call is 403+audited.
    checkpoints, joined = await _run_join(state, request_id)
    if joined is not None and checkpoints is not None:
        if state.source_guard is None:
            raise HTTPException(status_code=503, detail="approval authority unavailable")
        if state.agent_run_enqueuer is None or not hasattr(
            state.agent_run_enqueuer, "enqueue_approval_resume"
        ):
            raise HTTPException(status_code=503, detail="Run approval resume queue unavailable")
        row = await state.gate._store.get(request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="approval request not found")
        # Same consistency floor as approve (F2): parked tier/action equality
        # always, the content-free binding check while still actionable.
        if not _parked_governance_matches(joined, row):
            raise HTTPException(status_code=409, detail="approval context unavailable")
        if str(row.status) in {"pending", "pending_second"} and not _canonical_run_binding_is_valid(
            state,
            request_id=request_id,
            joined=joined,
            row=row,
        ):
            raise HTTPException(status_code=409, detail="approval context unavailable")
        # Same as approve (F9): a continuation whose run-owner authorization
        # already ran out records the SPEC-named ``authorization_expired``
        # terminal, never a human ``rejected`` that no human decided.
        #
        # F1 does NOT add a carryover here, and that is deliberate rather than
        # unfinished: "Denial requires no successor and is never
        # horizon-bounded." There is nothing for a standing DENIAL to authorize
        # — a successor that finds no standing approval already parks for a
        # fresh human decision, which is exactly what a denial should produce.
        await _refuse_expired_authorization(
            state, principal=principal, checkpoints=checkpoints, joined=joined, row=row
        )
        now = datetime.now(UTC)
        # Authority floor WITHOUT disclosure (F1) — same seam as approve.
        await _check_decision_source_authority(
            state,
            principal=principal,
            checkpoints=checkpoints,
            request_id=request_id,
            operation="reject",
        )
        landed = await checkpoints.decide(
            request_id,
            status="rejected",
            expected_action_hash=joined.action_hash,
            principal_id=joined.principal_id,
            policy_hash=joined.policy_hash,
            approver=principal.id,
            reason=body.reason,
            decided_at=now,
            event=new_approval_event(
                approval_id=request_id,
                tier=int(row.tier),
                decision=ApprovalDecision.rejected,
                subject_ref=f"tool:{row.action}",
                actor=principal.id,
            ),
        )
        decided_approver = principal.id
        decided_at = now
        if not landed:
            try:
                resume = await checkpoints.load_resume_request(request_id)
            except LookupError as exc:
                raise HTTPException(status_code=409, detail="approval already decided") from exc
            decided = await state.gate._store.get(request_id)
            if (
                resume.decision != "rejected"
                or decided is None
                or decided.first_approver != principal.id
                or decided.decided_reason != body.reason
            ):
                raise HTTPException(status_code=409, detail="approval already decided")
            decided_approver = decided.first_approver
            decided_at = decided.decided_at
        await _enqueue_run_resume(state, checkpoints, request_id)
        return ApprovalActionResultView(
            status="rejected",
            approver=decided_approver,
            decided_at=decided_at,
        )
    try:
        outcome = await state.gate.reject(request_id, approver=principal.id, reason=body.reason)
    except ApprovalSourceWithheldFromApproverError as exc:
        await _refuse_source_withheld_for_principal(
            state, principal=principal, request_id=request_id, exc=exc
        )
    except ApprovalPolicyError as exc:
        await emit_surface_audit(
            state.sink,
            action="api.approval.unauthorized",
            resource=_RESOURCE,
            outcome=AuditOutcome.denied,
            actor=principal.id,
            detail={
                "reason": "gate-policy",
                "request_id": request_id,
                "error_class": type(exc).__name__,
            },
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequestNotFoundError as exc:
        await _audit_stale(state, principal, request_id, exc)
        raise HTTPException(status_code=404, detail="approval request not found") from exc
    except ApprovalAlreadyDecidedError as exc:
        await _audit_stale(state, principal, request_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeAuditError as exc:
        # Post-CAS audit failure: the decision may already be COMMITTED — the
        # response must report the row's actual state, never claim "pending"
        # over a terminal row (Sol round 2, P1).
        raise await _post_gate_failure_response(
            state, request_id=request_id, audit_failed=True
        ) from exc
    except RuntimeError as exc:
        # Typed gate refusals (e.g. SourceAuthorityRetryableError, row still
        # PENDING) must not escape as 500s (Sol round 1) — but a plain
        # RuntimeError can ALSO arrive after the decision AND audit committed
        # (a failing metrics step), so the answer is chosen from RELOADED
        # durable state, never a fixed "remains pending" claim (Sol slice 2,
        # P1). Fixed safe details either way — an arbitrary RuntimeError's
        # text is not proven safe to serve.
        raise await _post_gate_failure_response(
            state, request_id=request_id, audit_failed=False
        ) from exc

    await emit_surface_audit(
        state.sink,
        action="api.approval.submitted",
        resource=_RESOURCE,
        outcome=AuditOutcome.success,
        actor=principal.id,
        detail={"decision": "reject", "request_id": request_id, "status": outcome.status.value},
    )
    if state.inference_lifecycle is not None:
        state.inference_lifecycle.terminalize_pending_approval(request_id)
    return ApprovalActionResultView(
        status=outcome.status.value, approver=outcome.approver, decided_at=outcome.decided_at
    )


async def _audit_stale(
    state: ApiState, principal: Principal, request_id: str, exc: Exception
) -> None:
    await emit_surface_audit(
        state.sink,
        action="api.approval.stale",
        resource=_RESOURCE,
        outcome=AuditOutcome.denied,
        actor=principal.id,
        detail={"reason": "stale", "request_id": request_id, "error_class": type(exc).__name__},
    )
