# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Read-only principal-owned Agent Run and Event resources."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from typing import Annotated, Literal, Protocol, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

from ne.api.deps import (
    ApiState,
    api_state,
    bind_authenticated_request_context,
    current_authenticated_context,
    current_principal,
)
from ne.api.evidence_projection import DisclosedRunOutput
from ne.api.run_projection import (
    CursorUnavailable,
    decode_cursor,
    projection_from_state,
    run_page,
)
from ne.api.run_submission import (
    RUN_SUBMISSION_STATE_KEY,
    AcceptedRunSubmission,
    RunCredentialTooShort,
    RunSubmissionConflict,
    RunSubmissionNotFound,
    RunSubmissionService,
    RunSubmissionUnavailable,
)
from ne.api.runs_wire import (
    AcceptedRunSubmissionView,
    ControlRunRequest,
    RunDetailView,
    RunEventPage,
    RunPage,
    StartRunRequest,
)
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.types import Principal

SOURCE_BOUND_OUTPUT_READER_STATE_KEY = "ne_agent_source_bound_output_reader"

router = APIRouter(dependencies=[Depends(bind_authenticated_request_context)])

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]


def optional_authenticated_context(request: Request) -> AuthenticatedRequestContext | None:
    """The verifier-authored snapshot, or None -- never a 401 on this route.

    `bind_authenticated_request_context` already tolerates its absence, on the
    stated ground that "legacy verifier doubles can still exercise source-free
    routes" and any protected materialization stays fail-closed. Requiring it
    here would turn that tolerated case into a 401 on the whole Run detail read
    -- a page with plenty else to show -- to protect one field. Absent means the
    source-bound answer is not read; everything else is served exactly as before.
    """

    try:
        return current_authenticated_context(request)
    except HTTPException:
        return None


_Context = Annotated[AuthenticatedRequestContext | None, Depends(optional_authenticated_context)]


class SourceBoundOutputReader(Protocol):
    """The reauthorizing read of a source-derived terminal answer."""

    async def disclose(
        self,
        *,
        agent_id: str,
        run_id: str,
        principal: Principal,
        access_context_hash: str,
        projection_attempt_id: str,
    ) -> DisclosedRunOutput: ...


def attach_source_bound_output_reader(
    app_state: object, reader: SourceBoundOutputReader | None
) -> None:
    """Mount-tail hook, mirroring `evidence.attach_evidence_service`."""

    setattr(app_state, SOURCE_BOUND_OUTPUT_READER_STATE_KEY, reader)


def _source_bound_output_reader(request: Request) -> SourceBoundOutputReader | None:
    """Absent means UNMOUNTED, which discloses nothing -- never an error page.

    A deployment with no source-authority evaluator composes no reader; a
    source-free answer is still served by the projection, and a source-derived
    one reads `unavailable` rather than 503-ing a Run page that has plenty else
    to show.
    """

    reader = getattr(request.app.state, SOURCE_BOUND_OUTPUT_READER_STATE_KEY, None)
    return reader if callable(getattr(reader, "disclose", None)) else None


_IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[\x21-\x7e]+$",
    ),
]
_NO_STORE = "private, no-store"


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail="Resource not found",
        headers={"Cache-Control": _NO_STORE},
    )


def _department(principal: Principal) -> str:
    value = principal.claims.get("department")
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=422,
            detail="principal has no department claim",
            headers={"Cache-Control": _NO_STORE},
        )
    return value


def _projection(state: ApiState):
    try:
        return projection_from_state(state)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="agent run projection unavailable",
            headers={"Cache-Control": _NO_STORE},
        ) from exc


def _visible_agent(agent_id: str, principal: Principal, state: ApiState) -> None:
    if state.agent_catalog.get_visible(agent_id, principal) is None:
        raise _not_found()


def run_submission_service(request: Request) -> RunSubmissionService:
    """Return the atomic submission service, never a sequential fallback."""
    service = getattr(request.app.state, RUN_SUBMISSION_STATE_KEY, None)
    if not isinstance(service, RunSubmissionService):
        raise HTTPException(
            status_code=503,
            detail="agent run submission is unavailable",
            headers={"Cache-Control": _NO_STORE},
        )
    return service


_Submission = Annotated[RunSubmissionService, Depends(run_submission_service)]


def _accepted(result: AcceptedRunSubmission) -> AcceptedRunSubmissionView:
    accepted_statuses = {"submitted", "queued", "cancelling"}
    if result.status not in accepted_statuses:
        raise HTTPException(
            status_code=503,
            detail="agent run submission returned an invalid accepted state",
            headers={"Cache-Control": _NO_STORE},
        )
    return AcceptedRunSubmissionView(
        action=result.action,
        run_id=result.run_id,
        attempt_id=result.attempt_id,
        version=result.version,
        status=cast(Literal["submitted", "queued", "cancelling"], result.status),
        enqueue_id=result.enqueue_id,
        replayed=result.replayed,
    )


async def _submit(
    call: Awaitable[AcceptedRunSubmission],
    response: Response,
    *,
    state: ApiState,
    dispatch: bool,
    owner_principal_id: str,
) -> AcceptedRunSubmissionView:
    try:
        result = await call
    except RunSubmissionNotFound as exc:
        raise _not_found() from exc
    except RunCredentialTooShort as exc:
        # BEFORE the generic conflict arm, which this subclasses. A credential
        # that cannot outlast the Run is an AUTHENTICATION problem with a remedy
        # the caller can act on -- renew and resubmit -- so it answers 401
        # carrying that remedy rather than the opaque 409 that told a scripted
        # client nothing and left it retrying the same dead token.
        raise HTTPException(
            status_code=401,
            detail=exc.detail,
            headers={"Cache-Control": _NO_STORE},
        ) from exc
    except RunSubmissionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="request conflicts with current Run state",
            headers={"Cache-Control": _NO_STORE},
        ) from exc
    except RunSubmissionUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="agent run submission is unavailable",
            headers={"Cache-Control": _NO_STORE},
        ) from exc
    if dispatch:
        assert state.agent_run_store is not None
        assert state.agent_run_enqueuer is not None
        try:
            request = await state.agent_run_store.load_workflow_request(
                enqueue_id=result.enqueue_id,
                run_id=result.run_id,
                attempt_id=result.attempt_id,
                owner_principal_id=owner_principal_id,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=503,
                detail="agent run durable dispatch target is unavailable",
                headers={"Cache-Control": _NO_STORE},
            ) from exc
        try:
            await state.agent_run_enqueuer.enqueue(request)
        except Exception as exc:
            # Admission and target are durable. An exact idempotency replay
            # reloads and re-enqueues the same DBOS workflow id.
            raise HTTPException(
                status_code=503,
                detail="agent run durable dispatch failed",
                headers={"Cache-Control": _NO_STORE},
            ) from exc
    response.headers["Cache-Control"] = _NO_STORE
    return _accepted(result)


def _require_durable_dispatch(state: ApiState) -> None:
    if state.agent_run_store is None or state.agent_run_enqueuer is None:
        raise HTTPException(
            status_code=503,
            detail="agent run durable dispatch is unavailable",
            headers={"Cache-Control": _NO_STORE},
        )


@router.post(
    "/v1/agents/{agent_id}/runs",
    response_model=AcceptedRunSubmissionView,
    status_code=202,
)
async def start_run(
    agent_id: str,
    body: StartRunRequest,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    service: _Submission,
    state: _State,
    response: Response,
) -> AcceptedRunSubmissionView:
    _require_durable_dispatch(state)
    return await _submit(
        service.start(
            principal=principal,
            agent_id=agent_id,
            conversation_id=body.conversation_id,
            expected_version=body.expected_version,
            agent_version=body.agent_version,
            strategy=body.strategy,
            config_hash=body.config_hash,
            capability_hash=body.capability_snapshot_hash,
            input_text=body.input,
            idempotency_key=idempotency_key,
            requested_model=body.requested_model,
            requested_model_lane=body.requested_model_lane,
        ),
        response,
        state=state,
        dispatch=True,
        owner_principal_id=principal.id,
    )


@router.post(
    "/v1/agents/{agent_id}/runs/{run_id}/cancel",
    response_model=AcceptedRunSubmissionView,
    status_code=202,
)
async def cancel_run(
    agent_id: str,
    run_id: str,
    body: ControlRunRequest,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    service: _Submission,
    state: _State,
    response: Response,
) -> AcceptedRunSubmissionView:
    return await _submit(
        service.cancel(
            principal=principal,
            agent_id=agent_id,
            run_id=run_id,
            expected_version=body.expected_version,
            agent_version=body.agent_version,
            strategy=body.strategy,
            config_hash=body.config_hash,
            capability_hash=body.capability_snapshot_hash,
            idempotency_key=idempotency_key,
        ),
        response,
        state=state,
        dispatch=False,
        owner_principal_id=principal.id,
    )


@router.post(
    "/v1/agents/{agent_id}/runs/{run_id}/retry",
    response_model=AcceptedRunSubmissionView,
    status_code=202,
)
async def retry_run(
    agent_id: str,
    run_id: str,
    body: ControlRunRequest,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    service: _Submission,
    state: _State,
    response: Response,
) -> AcceptedRunSubmissionView:
    _require_durable_dispatch(state)
    return await _submit(
        service.retry(
            principal=principal,
            agent_id=agent_id,
            run_id=run_id,
            expected_version=body.expected_version,
            agent_version=body.agent_version,
            strategy=body.strategy,
            config_hash=body.config_hash,
            capability_hash=body.capability_snapshot_hash,
            idempotency_key=idempotency_key,
        ),
        response,
        state=state,
        dispatch=True,
        owner_principal_id=principal.id,
    )


@router.get("/v1/agents/{agent_id}/runs", response_model=RunPage)
async def list_runs(
    agent_id: str,
    principal: _Principal,
    state: _State,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> RunPage:
    _visible_agent(agent_id, principal, state)
    kind = f"runs:{agent_id}"
    try:
        after = decode_cursor(cursor, kind=kind) if cursor is not None else None
    except CursorUnavailable as exc:
        raise _not_found() from exc
    rows, next_after = await _projection(state).list_runs(
        owner_principal_id=principal.id,
        department=_department(principal),
        agent_id=agent_id,
        limit=limit,
        after=after,
    )
    response.headers["Cache-Control"] = _NO_STORE
    return run_page(rows, next_after, cursor_kind=kind)


@router.get("/v1/agents/{agent_id}/runs/{run_id}", response_model=RunDetailView)
async def get_run(
    agent_id: str,
    run_id: str,
    principal: _Principal,
    context: _Context,
    state: _State,
    request: Request,
    response: Response,
) -> RunDetailView:
    _visible_agent(agent_id, principal, state)
    department = _department(principal)
    projection = _projection(state)
    row = await projection.get_run(
        run_id=run_id,
        owner_principal_id=principal.id,
        department=department,
        agent_id=agent_id,
    )
    if row is None:
        raise _not_found()
    response.headers["Cache-Control"] = _NO_STORE
    detail = await projection.run_detail(
        run=row,
        owner_principal_id=principal.id,
        department=department,
        agent_id=agent_id,
    )
    return await _with_terminal_output(
        detail,
        agent_id=agent_id,
        run_id=run_id,
        principal=principal,
        context=context,
        request=request,
    )


async def _with_terminal_output(
    detail: RunDetailView,
    *,
    agent_id: str,
    run_id: str,
    principal: Principal,
    context: AuthenticatedRequestContext | None,
    request: Request,
) -> RunDetailView:
    """ONE READ, WHICHEVER STORE THE FINALIZER CHOSE.

    `run_finalizer._protect_output` writes the terminal answer to one of two
    tables depending on whether the Run served any source read. The projection
    reads the source-free one; this fills in the source-derived one through the
    Evidence drawer's own reauthorizing resolver. Which table the bytes landed
    in is an implementation detail and must not decide which screen the reader
    has to go find their answer on.

    Order matters and is fail-closed: an answer the projection already
    disclosed is served as-is and no second read is attempted.
    """

    if detail.output_preview is not None:
        return detail.model_copy(update={"output_state": "available"})
    # Only a COMPLETED Run has a terminal to disclose. A paused or running Run
    # has no answer yet, and asking for one would be a wasted reauthorization.
    if detail.status != "completed":
        return detail
    # No verifier-authored context, no source reauthorization, no disclosure.
    if context is None:
        return detail
    if principal.id != context.principal_id:
        raise _not_found()
    reader = _source_bound_output_reader(request)
    if reader is None:
        return detail
    disclosed = await reader.disclose(
        agent_id=agent_id,
        run_id=run_id,
        principal=principal,
        access_context_hash=context.context_hash,
        projection_attempt_id=str(uuid.uuid4()),
    )
    if disclosed.state == "available" and disclosed.text is not None:
        return detail.model_copy(
            update={"output_preview": disclosed.text, "output_state": "available"}
        )
    if disclosed.state == "withheld":
        return detail.model_copy(update={"output_state": "withheld"})
    return detail


@router.get(
    "/v1/agents/{agent_id}/runs/{run_id}/events",
    response_model=RunEventPage,
)
async def list_run_events(
    agent_id: str,
    run_id: str,
    principal: _Principal,
    state: _State,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> RunEventPage:
    _visible_agent(agent_id, principal, state)
    department = _department(principal)
    projection = _projection(state)
    row = await projection.get_run(
        run_id=run_id,
        owner_principal_id=principal.id,
        department=department,
        agent_id=agent_id,
    )
    if row is None:
        raise _not_found()
    try:
        if cursor is None:
            after_sequence = 0
        else:
            _at, encoded_sequence = decode_cursor(cursor, kind=f"event:{run_id}")
            after_sequence = int(encoded_sequence)
            if after_sequence < 0:
                raise ValueError
    except (CursorUnavailable, ValueError) as exc:
        raise _not_found() from exc
    response.headers["Cache-Control"] = _NO_STORE
    return await projection.list_events(
        run=row,
        owner_principal_id=principal.id,
        after_sequence=after_sequence,
        limit=limit,
    )
