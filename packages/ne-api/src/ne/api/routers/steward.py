# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Steward routes (the flywheel review surface, spec §1.7) — the REAL review gate.

Every approve/reject/apply delegates to :class:`ne.flywheel.review.ProposalReviewService`
(``approve``/``reject``/``record_apply``); ne-api NEVER writes ``ne_proposal_reviews``,
NEVER folds-then-writes a status, NEVER runs a parallel state machine. The actor +
provenance are ALWAYS server-derived: :func:`_provenance` delegates to
:func:`ne.api.provenance.console_provenance` — the ONE grep-provable
``SignalProvenance`` construction in ne-api src (``principal`` = the verified bearer,
``surface`` = ``SignalSurface.CONSOLE``, ``session_id`` = the token ``sid`` claim when
present else a fresh ``uuid4``; NE-137 hoisted it so the chat signal routes reuse it).
The request bodies are ``extra='forbid'`` so a smuggled
``actor``/``surface``/``session_id`` is a LOUD 422 that never reaches the service.

The MANDATORY fail-closed role pre-check (:func:`_require_steward`) runs FIRST on
every route — as a route DEPENDENCY on the write routes (the 104b pattern) so an
unauthorized caller with a malformed body is 403+audited, never 422'd unaudited.
Empty ``steward_roles`` ⇒ 403 for EVERYONE (fail closed), disjoint groups ⇒ 403.
The 503 store-unconfigured check runs AFTER the role check (403 wins).

The proposals queue folds live status via a SINGLE aggregated query
(``steward_metrics.folded_status_map`` — never a per-row ``status_of``, R10) and
renders each kept proposal through ``ne.flywheel.diffs.render_proposal_diff`` so the
raw ``summary``/``payload`` NEVER cross the wire (only the sanitized artifact does).
``read_proposals(limit=500)`` is a documented pilot-scale cap (R10). The digest +
latency routes are ne-api-computed READ-ONLY SQL aggregations (``steward_metrics``)
— they modify no chokepoint. Every sync store/service/SQL call rides ONE
``anyio.to_thread.run_sync`` hop so the event loop never blocks on DB I/O.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Any

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException

from ne.api.authz import emit_surface_audit, require_role
from ne.api.deps import (
    ApiState,
    api_state,
    bind_authenticated_request_context,
    current_principal,
)
from ne.api.provenance import console_provenance
from ne.api.steward_disclosure import ProposalDisclosure, disclose_proposal
from ne.api.steward_metrics import (
    LatencySummary,
    WeeklyDigest,
    compute_digest,
    compute_latency,
    folded_status_map,
    iso_week_start,
)
from ne.api.verified_curation import (
    VerifiedCurationContext,
    apply_verified_template,
    propose_verified_template,
)
from ne.api.wire import (
    ReviewEventView,
    SkillApplyView,
    SkillCandidateView,
    SkillEditFieldsRequest,
    StewardApplyRequest,
    StewardApplySkillRequest,
    StewardApplyVerifiedTemplateRequest,
    StewardApproveRequest,
    StewardDigestView,
    StewardLatencyView,
    StewardProposalsView,
    StewardProposalView,
    StewardProposeVerifiedTemplateRequest,
    StewardRejectRequest,
    StewardSkillRotRequest,
    VerifiedTemplateApplyView,
    WithheldStewardProposalView,
    review_event_view,
    steward_digest_view,
    steward_latency_view,
    steward_proposal_view,
    verified_template_apply_view,
)
from ne.contracts import get_config_hash
from ne.contracts.types import Principal
from ne.flywheel.diffs import render_proposal_diff
from ne.flywheel.errors import (
    InvalidTransitionError,
    ReviewAuditError,
    ReviewError,
    UnknownProposalError,
)
from ne.flywheel.proposals import ProposalKind, SqlAlchemyProposalStore, StoredFixProposal
from ne.flywheel.review import ProposalReviewService, StoredReviewEvent
from ne.flywheel.signals import SignalProvenance
from ne.permissions.models import PUBLIC_LINK, TENANT_ALL
from ne.runtime.source_results import (
    MissingAuthenticatedContextError,
    require_authenticated_context,
)
from ne.schema import AuditOutcome
from ne.skills.apply import SkillEdits, apply_skill_candidate
from ne.skills.errors import (
    SkillApplyGateError,
    SkillApplyIntegrityError,
    SkillApplyLabelError,
    SkillApplyVersionError,
    SkillEvalGateError,
)
from ne.skills.evals import verify_skill_proposal_payload
from ne.skills.registry import SqlAlchemySkillRegistry
from ne.skills.retirement import restore_skill, retire_skill

router = APIRouter(dependencies=[Depends(bind_authenticated_request_context)])

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]

_RESOURCE = "api:steward"

#: Documented pilot-scale cap on the steward queue read (R10). The review log is
#: tiny at pilot scale; the single aggregated fold makes status folding O(1) query.
_QUEUE_LIMIT = 500

#: The non-terminal statuses the steward queue surfaces (client.ts:88-90).
_NON_TERMINAL = frozenset({"proposed", "approved"})


async def _require_steward(principal: Principal, state: ApiState, proposal_id: str | None) -> None:
    await require_role(
        principal,
        state.settings.steward_roles,
        sink=state.sink,
        audit_action="api.steward.unauthorized",
        resource=_RESOURCE,
        http_detail="not an authorized steward",
        request_id=proposal_id,
    )


async def _require_steward_dep(proposal_id: str, principal: _Principal, state: _State) -> None:
    """Route dependency: run the fail-closed steward role check (403 + audited
    ``api.steward.unauthorized``) BEFORE FastAPI validates the request body.

    FastAPI solves a route's ``dependencies`` before validating the endpoint's own
    body field, so an authenticated-but-unauthorized caller sending a malformed
    body is still rejected-and-audited (403), never 422'd unaudited (the 104b
    regression pattern).
    """
    await _require_steward(principal, state, proposal_id)


async def _require_steward_no_proposal_dep(principal: _Principal, state: _State) -> None:
    """Route dependency for the body-only propose route (no ``proposal_id`` path param).

    Mirrors :func:`_require_steward_dep` but with ``request_id=None`` — runs the
    fail-closed steward role check (403 + audited) BEFORE FastAPI validates the
    request body (the 104b pattern).
    """
    await _require_steward(principal, state, None)


async def _require_steward_skill_dep(name: str, principal: _Principal, state: _State) -> None:
    """Route dependency for the retire/restore routes (path param ``name``).

    Mirrors :func:`_require_steward_dep` but reads the ``{name}`` path param — so
    an authenticated-but-unauthorized caller with a malformed body is 403+audited
    BEFORE FastAPI validates the body (the 104b pattern). ``request_id`` carries
    the skill name for the audit trail."""
    await _require_steward(principal, state, name)


def _flywheel(state: ApiState) -> tuple[ProposalReviewService, SqlAlchemyProposalStore]:
    """Return the review service + proposal store, or 503 when unconfigured.

    Called AFTER the role check on every route, so a 403 for an unauthorized
    caller always wins over the 503 (never a store-existence oracle).
    """
    if state.review_service is None or state.proposal_store is None:
        raise HTTPException(status_code=503, detail="steward store not configured")
    return state.review_service, state.proposal_store


def _require_verified(state: ApiState) -> VerifiedCurationContext:
    """Return the governed-curation seam, or 409 when verified answers are disabled.

    Called AFTER the role check + the 503 store check, so a 403/503 always wins
    (the guard ladder: 403 > 503 > 409 — never a feature-flag oracle to a
    non-steward or a flywheel-less deployment).
    """
    if state.verified_curation is None:
        raise HTTPException(
            status_code=409, detail="verified answers are not enabled on this deployment"
        )
    return state.verified_curation


def _provenance(principal: Principal) -> SignalProvenance:
    """Build the server-side-ONLY steward signal provenance (spec §7-R4).

    Delegates to :func:`ne.api.provenance.console_provenance` — the ONE grep-provable
    ``SignalProvenance`` construction in ne-api src (NE-137 hoisted it out of this
    module so the chat signal routes reuse the identical server-side-only builder;
    the single-construction invariant is preserved-and-hoisted, guarded by
    ``test_no_bypass``). Identity is NEVER read from the request body/query/header.
    """
    return console_provenance(principal)


def _note(
    body: StewardApproveRequest
    | StewardRejectRequest
    | StewardApplyRequest
    | StewardApplyVerifiedTemplateRequest,
) -> str | None:
    """Blank/whitespace ``note`` ⇒ ``None`` (spec §1.7 #14)."""
    return body.note if body.note.strip() else None


def _read_queue(store: SqlAlchemyProposalStore) -> list[tuple[StoredFixProposal, str]]:
    """Read the non-terminal steward queue with a SINGLE aggregated status fold.

    One threadpool hop: ``read_proposals`` (pilot cap R10) + ``folded_status_map``
    (never per-row ``status_of``) + a server-rendered sanitized artifact per kept
    row. A proposal absent from the fold map is ``proposed`` (the fold default).
    Ordered ``created_at`` ASC with an ``id`` tie-break (oldest-first, deterministic).
    """
    proposals = store.read_proposals(limit=_QUEUE_LIMIT)
    status_map = folded_status_map(store.engine)
    kept = [(proposal, status_map.get(proposal.proposal_id, "proposed")) for proposal in proposals]
    kept = [(p, status) for p, status in kept if status in _NON_TERMINAL]
    kept.sort(key=lambda item: (item[0].created_at, item[0].id))
    return kept


def _access_context_hash() -> str | None:
    try:
        return require_authenticated_context().context_hash
    except MissingAuthenticatedContextError:
        return None


async def _disclosure(
    proposal: StoredFixProposal, principal: Principal, state: ApiState
) -> ProposalDisclosure:
    return await disclose_proposal(
        proposal,
        evaluator=state.source_authority_evaluator,
        principal=principal,
        access_context_hash=_access_context_hash(),
    )


async def _require_proposal_disclosure(
    proposal_id: str, principal: Principal, state: ApiState
) -> StoredFixProposal:
    _service, store = _flywheel(state)
    proposal = await anyio.to_thread.run_sync(store.read_proposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if await _disclosure(proposal, principal, state) is not ProposalDisclosure.AVAILABLE:
        raise HTTPException(status_code=409, detail="proposal content is not currently available")
    return proposal


_SKILL_KINDS = frozenset({"skill_candidate", "skill_update_candidate"})


def _skill_view(proposal: StoredFixProposal) -> SkillCandidateView | None:
    """Build a field-by-field :class:`SkillCandidateView` for a skill proposal.

    The raw ``payload`` NEVER crosses the wire — every field is copied explicitly
    from ``payload['pack']`` + ``payload['suggested_acl_labels']``. Returns
    ``None`` for a non-skill kind (the view's ``skill`` stays absent).
    """
    if proposal.kind not in _SKILL_KINDS:
        return None
    payload = proposal.payload or {}
    pack = payload.get("pack") if isinstance(payload, dict) else None
    if not isinstance(pack, dict):
        return None
    provenance = pack.get("provenance") if isinstance(pack.get("provenance"), dict) else {}
    session_id = str(provenance.get("session_id", "")) if isinstance(provenance, dict) else ""
    forged_at = str(provenance.get("forged_at", "")) if isinstance(provenance, dict) else ""
    date_str = forged_at.split("T", 1)[0] if forged_at else ""
    provenance_line = f"forged from session {session_id or 'unknown'} on {date_str or 'unknown'}"
    return SkillCandidateView(
        name=str(pack.get("name", "")),
        version=int(pack.get("version", 1)),
        description=str(pack.get("description", "")),
        triggers=[str(t) for t in (pack.get("triggers") or [])],
        suggested_acl_labels=[str(x) for x in (payload.get("suggested_acl_labels") or [])],
        provenance_line=provenance_line,
        body=str(pack.get("body", "")),
    )


@router.get("/v1/steward/proposals")
async def steward_proposals(principal: _Principal, state: _State) -> StewardProposalsView:
    await _require_steward(principal, state, None)
    _service, store = _flywheel(state)
    proposals = await anyio.to_thread.run_sync(functools.partial(_read_queue, store))
    served: list[StewardProposalView | WithheldStewardProposalView] = []
    for proposal, status in proposals:
        disclosure = await _disclosure(proposal, principal, state)
        if disclosure is ProposalDisclosure.AVAILABLE:
            served.append(
                steward_proposal_view(
                    proposal, status, render_proposal_diff(proposal), skill=_skill_view(proposal)
                )
            )
        else:
            served.append(
                WithheldStewardProposalView(
                    disclosure_status=disclosure.value,
                    proposal_id=proposal.proposal_id,
                    status=status,
                    created_at=proposal.created_at,
                )
            )
    return StewardProposalsView(proposals=served)


@router.post(
    "/v1/steward/proposals/{proposal_id}/approve", dependencies=[Depends(_require_steward_dep)]
)
async def steward_approve(
    proposal_id: str,
    body: StewardApproveRequest,
    principal: _Principal,
    state: _State,
) -> ReviewEventView:
    service, store = _flywheel(state)
    await _require_proposal_disclosure(proposal_id, principal, state)
    # NE-SKILL-2 birth gate (kind-guarded): a skill(_update)_candidate cannot be
    # approved unless its payload carries a PASSING, store-anchored eval ref bound
    # to the pack routing_hash. Generic kinds take ZERO new code paths; an unknown
    # id falls through to service.approve's UnknownProposalError (→ 404).
    stored = await anyio.to_thread.run_sync(functools.partial(store.read_proposal, proposal_id))
    if stored is not None and stored.kind in _SKILL_KINDS:
        if state.skills_engine is None:
            raise HTTPException(status_code=503, detail="skills not configured")
        skills_engine = state.skills_engine
        try:
            await anyio.to_thread.run_sync(
                functools.partial(
                    verify_skill_proposal_payload, stored.payload, engine=skills_engine
                )
            )
        except SkillEvalGateError as exc:
            await _audit_stale(state, principal, proposal_id, exc)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    event = await _run_transition(
        state,
        principal,
        decision="approve",
        proposal_id=proposal_id,
        call=functools.partial(
            service.approve, proposal_id, _provenance(principal), note=_note(body)
        ),
    )
    return review_event_view(event)


@router.post(
    "/v1/steward/proposals/{proposal_id}/reject", dependencies=[Depends(_require_steward_dep)]
)
async def steward_reject(
    proposal_id: str,
    body: StewardRejectRequest,
    principal: _Principal,
    state: _State,
) -> ReviewEventView:
    service, _store = _flywheel(state)
    await _require_proposal_disclosure(proposal_id, principal, state)
    event = await _run_transition(
        state,
        principal,
        decision="reject",
        proposal_id=proposal_id,
        call=functools.partial(
            service.reject, proposal_id, _provenance(principal), code=body.code, note=_note(body)
        ),
    )
    return review_event_view(event)


@router.post(
    "/v1/steward/proposals/{proposal_id}/apply", dependencies=[Depends(_require_steward_dep)]
)
async def steward_apply(
    proposal_id: str,
    body: StewardApplyRequest,
    principal: _Principal,
    state: _State,
) -> ReviewEventView:
    service, _store = _flywheel(state)
    proposal = await _require_proposal_disclosure(proposal_id, principal, state)
    if proposal.kind == ProposalKind.GRAPH_EXTRACTION_CANDIDATE.value:
        raise HTTPException(
            status_code=409,
            detail="graph extraction proposals require the guarded extraction apply route",
        )
    event = await _run_transition(
        state,
        principal,
        decision="apply",
        proposal_id=proposal_id,
        call=functools.partial(
            service.record_apply,
            proposal_id,
            _provenance(principal),
            applied_ref=body.applied_ref,
            note=_note(body),
        ),
    )
    return review_event_view(event)


def _translate_edits(edits: SkillEditFieldsRequest | None) -> SkillEdits | None:
    """Map the wire edit-fields to ``ne.skills.apply.SkillEdits`` (None passthrough).

    ``triggers`` list → tuple; a body edit is structurally impossible upstream
    (``SkillEditFieldsRequest`` is ``extra='forbid'``), so nothing here can carry a
    body edit into the seam.
    """
    if edits is None:
        return None
    return SkillEdits(
        name=edits.name,
        description=edits.description,
        triggers=tuple(edits.triggers) if edits.triggers is not None else None,
    )


@router.post(
    "/v1/steward/proposals/{proposal_id}/apply-skill",
    dependencies=[Depends(_require_steward_dep)],
)
async def steward_apply_skill(
    proposal_id: str,
    body: StewardApplySkillRequest,
    principal: _Principal,
    state: _State,
) -> SkillApplyView:
    """Apply an APPROVED skill(_update)_candidate — register in ONE transaction.

    Guard ladder: 403 (role) > 503 (no flywheel) > 503 (no skills engine). Error
    map mirrors the verified-template seam: audit-chain failure → 500,
    unknown/gate → 404/409, integrity/label/version → 422. Success emits
    ``api.steward.submitted`` (decision ``skill_apply``).
    """
    service, store = _flywheel(state)
    await _require_proposal_disclosure(proposal_id, principal, state)
    if state.skills_engine is None:
        raise HTTPException(status_code=503, detail="skills not configured")
    skills_engine = state.skills_engine
    registry = SqlAlchemySkillRegistry(skills_engine)
    edits = _translate_edits(body.edits)

    def _label_validator(label: str) -> bool:
        return label in {TENANT_ALL, PUBLIC_LINK} or state.perm_store.is_known(label)

    try:
        applied = await anyio.to_thread.run_sync(
            functools.partial(
                apply_skill_candidate,
                review_service=service,
                skills_engine=skills_engine,
                registry=registry,
                proposal_store=store,
                proposal_id=proposal_id,
                provenance=_provenance(principal),
                acl_labels=tuple(body.acl_labels),
                global_visibility=body.global_visibility,
                label_validator=_label_validator,
                note=body.note if body.note.strip() else None,
                edits=edits,
                config_hash=get_config_hash(),
            )
        )
    except ReviewAuditError:
        raise
    except UnknownProposalError as exc:
        await _audit_stale(state, principal, proposal_id, exc)
        raise HTTPException(status_code=404, detail="proposal not found") from exc
    except (InvalidTransitionError, SkillApplyGateError) as exc:
        await _audit_stale(state, principal, proposal_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (SkillApplyIntegrityError, SkillApplyLabelError, SkillApplyVersionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await emit_surface_audit(
        state.sink,
        action="api.steward.submitted",
        resource=_RESOURCE,
        outcome=AuditOutcome.success,
        actor=principal.id,
        detail={
            "decision": "skill_apply",
            "proposal_id": proposal_id,
            "review_id": applied.review.review_id,
            "name": applied.name,
            "version": str(applied.version),
            "edited": str(edits is not None and edits.any_set()),
        },
    )
    return SkillApplyView(
        review=review_event_view(applied.review),
        name=applied.name,
        version=applied.version,
        content_hash=applied.content_hash,
        acl_labels=list(applied.acl_labels),
    )


async def _run_transition(
    state: ApiState,
    principal: Principal,
    *,
    decision: str,
    proposal_id: str,
    call: Callable[[], StoredReviewEvent],
) -> StoredReviewEvent:
    """Run ONE review transition on the threadpool and map its errors.

    Exception ORDER MATTERS: ``UnknownProposalError``/``InvalidTransitionError``/
    ``ReviewAuditError`` are ALL ``ReviewError`` subclasses, so the most specific
    (and the fail-closed audit error) are caught first — a naive ``except
    ReviewError`` first would turn 404/409/500 into 422.
    """
    try:
        event = await anyio.to_thread.run_sync(call)
    except ReviewAuditError:
        # Fail closed: the dual-appended audit chain failed and the whole
        # transition rolled back. Propagate as an opaque 500 (no reason oracle).
        raise
    except UnknownProposalError as exc:
        await _audit_stale(state, principal, proposal_id, exc)
        raise HTTPException(status_code=404, detail="proposal not found") from exc
    except InvalidTransitionError as exc:
        await _audit_stale(state, principal, proposal_id, exc)
        # str(exc) is a library-authored safe message; the console renders it
        # verbatim and keeps the row actionable.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReviewError as exc:
        # Belt-and-braces (e.g. an empty applied_ref that slipped the wire check).
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await emit_surface_audit(
        state.sink,
        action="api.steward.submitted",
        resource=_RESOURCE,
        outcome=AuditOutcome.success,
        actor=principal.id,
        detail={
            "decision": decision,
            "proposal_id": proposal_id,
            "review_id": event.review_id,
            "to_status": event.to_status,
        },
    )
    return event


async def _audit_stale(
    state: ApiState, principal: Principal, proposal_id: str, exc: Exception
) -> None:
    await emit_surface_audit(
        state.sink,
        action="api.steward.stale",
        resource=_RESOURCE,
        outcome=AuditOutcome.denied,
        actor=principal.id,
        detail={"reason": "stale", "proposal_id": proposal_id, "error_class": type(exc).__name__},
    )


def _compute_digest(
    service: ProposalReviewService,
    skills_engine: Any | None,
    week_start: date,
    visible_proposal_ids: frozenset[str],
) -> WeeklyDigest:
    return compute_digest(
        service.engine,
        week_start=week_start,
        now=datetime.now(UTC),
        skills_engine=skills_engine,
        visible_proposal_ids=visible_proposal_ids,
    )


def _compute_latency(
    service: ProposalReviewService, visible_proposal_ids: frozenset[str]
) -> LatencySummary:
    return compute_latency(
        service.engine,
        now=datetime.now(UTC),
        visible_proposal_ids=visible_proposal_ids,
    )


async def _visible_proposal_ids(
    store: SqlAlchemyProposalStore, principal: Principal, state: ApiState
) -> frozenset[str]:
    proposals = await anyio.to_thread.run_sync(
        functools.partial(store.read_proposals, limit=100_000)
    )
    visible: set[str] = set()
    for proposal in proposals:
        if await _disclosure(proposal, principal, state) is ProposalDisclosure.AVAILABLE:
            visible.add(proposal.proposal_id)
    return frozenset(visible)


@router.get("/v1/steward/digest")
async def steward_digest(
    principal: _Principal,
    state: _State,
    week_start: str | None = None,
) -> StewardDigestView:
    await _require_steward(principal, state, None)
    service, store = _flywheel(state)
    if week_start is not None:
        try:
            ws = date.fromisoformat(week_start)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="week_start: invalid ISO date") from exc
    else:
        ws = iso_week_start(datetime.now(UTC))
    visible = await _visible_proposal_ids(store, principal, state)
    digest = await anyio.to_thread.run_sync(
        functools.partial(_compute_digest, service, state.skills_engine, ws, visible)
    )
    return steward_digest_view(digest)


@router.get("/v1/steward/latency")
async def steward_latency(principal: _Principal, state: _State) -> StewardLatencyView:
    await _require_steward(principal, state, None)
    service, store = _flywheel(state)
    visible = await _visible_proposal_ids(store, principal, state)
    summary = await anyio.to_thread.run_sync(functools.partial(_compute_latency, service, visible))
    return steward_latency_view(summary)


# --- NE-SKILL-4: HUMAN retire / restore (never an agent actor) ----------------


async def _run_skill_rot(
    state: ApiState,
    principal: Principal,
    *,
    name: str,
    decision: str,
    call: Callable[[], None],
) -> dict[str, str]:
    """Run ONE retire/restore transition on the threadpool + map its errors.

    Guard ladder (the role check ran in the route dependency): 503 skills-engine
    None > 404 LookupError (unknown name) > 409 ValueError (wrong current status).
    Success emits ``api.steward.submitted`` (decision ``skill_retire``/
    ``skill_restore``) with the name in the audit detail (retire loudly)."""
    if state.skills_engine is None:
        raise HTTPException(status_code=503, detail="skills not configured")
    try:
        await anyio.to_thread.run_sync(call)
    except LookupError as exc:
        await _audit_stale(state, principal, name, exc)
        raise HTTPException(status_code=404, detail="skill not found") from exc
    except ValueError as exc:
        await _audit_stale(state, principal, name, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await emit_surface_audit(
        state.sink,
        action="api.steward.submitted",
        resource=_RESOURCE,
        outcome=AuditOutcome.success,
        actor=principal.id,
        detail={"decision": decision, "name": name},
    )
    return {"name": name, "decision": decision}


@router.post("/v1/steward/skills/{name}/retire", dependencies=[Depends(_require_steward_skill_dep)])
async def steward_retire_skill(
    name: str,
    body: StewardSkillRotRequest,
    principal: _Principal,
    state: _State,
) -> dict[str, str]:
    """Retire an ACTIVE skill (HUMAN transition). 403 > 503 > 404 > 409."""
    return await _run_skill_rot(
        state,
        principal,
        name=name,
        decision="skill_retire",
        call=functools.partial(
            retire_skill,
            state.skills_engine,  # type: ignore[arg-type]
            name,
            actor=principal.id,
            reason=body.reason,
        ),
    )


@router.post(
    "/v1/steward/skills/{name}/restore", dependencies=[Depends(_require_steward_skill_dep)]
)
async def steward_restore_skill(
    name: str,
    body: StewardSkillRotRequest,
    principal: _Principal,
    state: _State,
) -> dict[str, str]:
    """Restore a RETIRED skill in one lifecycle transition. 403 > 503 > 404 > 409."""
    return await _run_skill_rot(
        state,
        principal,
        name=name,
        decision="skill_restore",
        call=functools.partial(
            restore_skill,
            state.skills_engine,  # type: ignore[arg-type]
            name,
            actor=principal.id,
            reason=body.reason,
        ),
    )


# --- NE-ONTOLOGY-FLYWHEEL: governed verified-answer-template curation ---------


@router.post(
    "/v1/steward/proposals/verified-template",
    dependencies=[Depends(_require_steward_no_proposal_dep)],
)
async def steward_propose_verified_template(
    body: StewardProposeVerifiedTemplateRequest,
    principal: _Principal,
    state: _State,
) -> StewardProposalView:
    """Validate + propose a steward-authored verified-answer template (fail-closed).

    Guard ladder: 403 (role, via the route dependency) > 503 (flywheel store) >
    409 (verified answers disabled). A template that fails the load gate is a 422
    with a DENIED ``api.steward.verified_propose_invalid`` audit (error class +
    message ONLY — never the TOML) and NO proposal row (fail-closed propose).
    """
    from ne.brain_ontology.verified.errors import TemplateRegistryError

    _service, store = _flywheel(state)
    ctx = _require_verified(state)
    try:
        stored = await anyio.to_thread.run_sync(
            functools.partial(
                propose_verified_template,
                ctx,
                store.engine,
                store,
                _provenance(principal),
                template_toml=body.template_toml,
                summary=body.summary,
            )
        )
    except TemplateRegistryError as exc:
        # C12: the audit detail carries the error CLASS ONLY — never the message,
        # which can embed steward-authored TOML fragments (a pydantic validation
        # error echoes ``input_value``). The 422 body still returns str(exc) so the
        # steward sees why THEIR OWN submission failed.
        await emit_surface_audit(
            state.sink,
            action="api.steward.verified_propose_invalid",
            resource=_RESOURCE,
            outcome=AuditOutcome.denied,
            actor=principal.id,
            detail={"error_class": type(exc).__name__},
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await emit_surface_audit(
        state.sink,
        action="api.steward.submitted",
        resource=_RESOURCE,
        outcome=AuditOutcome.success,
        actor=principal.id,
        detail={
            "decision": "verified_propose",
            "proposal_id": stored.proposal_id,
            "template_id": str(stored.payload.get("template_id", "")),
        },
    )
    return steward_proposal_view(stored, "proposed", render_proposal_diff(stored))


@router.post(
    "/v1/steward/proposals/{proposal_id}/apply-verified-template",
    dependencies=[Depends(_require_steward_dep)],
)
async def steward_apply_verified_template(
    proposal_id: str,
    body: StewardApplyVerifiedTemplateRequest,
    principal: _Principal,
    state: _State,
) -> VerifiedTemplateApplyView:
    """Apply an APPROVED verified-answer-template proposal (governed + audited).

    Guard ladder mirrors propose. Error mapping: gate/transition refusals → 409,
    unknown proposal → 404, template/sha/conflict validation → 422, a fail-closed
    audit-chain failure → 500. On success the template is PERSISTED + the
    config_hash delta AUDITED; ``restart_required=True`` (activation is
    restart-to-activate).
    """
    from ne.brain_ontology.verified.errors import (
        TemplateRegistryError,
        VerifiedTemplateConflictError,
        VerifiedTemplateGateError,
    )

    service, store = _flywheel(state)
    await _require_proposal_disclosure(proposal_id, principal, state)
    ctx = _require_verified(state)
    try:
        applied = await anyio.to_thread.run_sync(
            functools.partial(
                apply_verified_template,
                ctx,
                service,
                store.engine,
                store,
                proposal_id,
                _provenance(principal),
                note=_note(body),
            )
        )
    except ReviewAuditError:
        # Fail closed: the dual-appended audit chain failed and the whole apply
        # rolled back. Propagate as an opaque 500 (no reason oracle).
        raise
    except UnknownProposalError as exc:
        await _audit_stale(state, principal, proposal_id, exc)
        raise HTTPException(status_code=404, detail="proposal not found") from exc
    except (VerifiedTemplateGateError, InvalidTransitionError) as exc:
        await _audit_stale(state, principal, proposal_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (TemplateRegistryError, VerifiedTemplateConflictError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await emit_surface_audit(
        state.sink,
        action="api.steward.submitted",
        resource=_RESOURCE,
        outcome=AuditOutcome.success,
        actor=principal.id,
        detail={
            "decision": "verified_apply",
            "proposal_id": proposal_id,
            "review_id": applied.review.review_id,
            "new_config_hash": applied.new_config_hash,
        },
    )
    return verified_template_apply_view(applied)
