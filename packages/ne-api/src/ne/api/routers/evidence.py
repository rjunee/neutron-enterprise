# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Owner-scoped Agent evidence read and bounded export routes."""

from __future__ import annotations

import uuid
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ne.api.deps import (
    bind_authenticated_request_context,
    current_authenticated_context,
    current_principal,
)
from ne.api.evidence_projection import EvidenceResourceNotFound
from ne.api.evidence_wire import EvidenceEnvelope
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.types import Principal

EVIDENCE_SERVICE_STATE_KEY = "ne_agent_evidence_service"
NO_STORE = "private, no-store, max-age=0"
PRAGMA_NO_CACHE = "no-cache"

router = APIRouter(dependencies=[Depends(bind_authenticated_request_context)])

_Principal = Annotated[Principal, Depends(current_principal)]
_Context = Annotated[AuthenticatedRequestContext, Depends(current_authenticated_context)]


class EvidenceService(Protocol):
    async def project(
        self,
        *,
        agent_id: str,
        run_id: str,
        principal: Principal,
        access_context_hash: str,
        projection_attempt_id: str,
    ) -> EvidenceEnvelope: ...


def attach_evidence_service(app_state: object, service: EvidenceService | None) -> None:
    """Mount-tail hook; this core never mutates ``ApiState`` or app composition."""

    setattr(app_state, EVIDENCE_SERVICE_STATE_KEY, service)


def evidence_service(request: Request) -> EvidenceService:
    service = getattr(request.app.state, EVIDENCE_SERVICE_STATE_KEY, None)
    if service is None or not callable(getattr(service, "project", None)):
        raise HTTPException(
            status_code=503,
            detail="agent evidence projection unavailable",
            headers={"Cache-Control": NO_STORE, "Pragma": PRAGMA_NO_CACHE},
        )
    return service


_Service = Annotated[EvidenceService, Depends(evidence_service)]


def _headers(response: Response, *, export: bool) -> None:
    response.headers["Cache-Control"] = NO_STORE
    response.headers["Pragma"] = PRAGMA_NO_CACHE
    if export:
        response.headers["Content-Disposition"] = 'attachment; filename="run-evidence.json"'


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail="Resource not found",
        headers={"Cache-Control": NO_STORE, "Pragma": PRAGMA_NO_CACHE},
    )


async def _project(
    *,
    agent_id: str,
    run_id: str,
    principal: Principal,
    context: AuthenticatedRequestContext,
    service: EvidenceService,
    response: Response,
    export: bool,
) -> EvidenceEnvelope:
    if principal.id != context.principal_id:
        raise _not_found()
    try:
        result = await service.project(
            agent_id=agent_id,
            run_id=run_id,
            principal=principal,
            access_context_hash=context.context_hash,
            projection_attempt_id=str(uuid.uuid4()),
        )
    except EvidenceResourceNotFound as exc:
        raise _not_found() from exc
    _headers(response, export=export)
    return result


@router.get(
    "/v1/agents/{agent_id}/runs/{run_id}/evidence",
    response_model=EvidenceEnvelope,
)
async def get_evidence(
    agent_id: str,
    run_id: str,
    principal: _Principal,
    context: _Context,
    service: _Service,
    response: Response,
) -> EvidenceEnvelope:
    return await _project(
        agent_id=agent_id,
        run_id=run_id,
        principal=principal,
        context=context,
        service=service,
        response=response,
        export=False,
    )


@router.get(
    "/v1/agents/{agent_id}/runs/{run_id}/evidence/export",
    response_model=EvidenceEnvelope,
)
async def export_evidence(
    agent_id: str,
    run_id: str,
    principal: _Principal,
    context: _Context,
    service: _Service,
    response: Response,
) -> EvidenceEnvelope:
    return await _project(
        agent_id=agent_id,
        run_id=run_id,
        principal=principal,
        context=context,
        service=service,
        response=response,
        export=True,
    )
