# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Owner-visible Agent catalog and singular governed preflight reads."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ne.api.agent_catalog import decode_agent_cursor, encode_agent_cursor
from ne.api.agent_model_preflight import AgentModelPreflightService
from ne.api.agent_preflight import AgentCapabilityPreflightService
from ne.api.agents_wire import (
    AgentCatalogItem,
    AgentCatalogPage,
    AgentPreflightItem,
    agent_catalog_item,
    agent_preflight_item,
)
from ne.api.deps import ApiState, api_state, current_principal
from ne.api.model_selection import ModelSelectionEvidenceError
from ne.contracts.types import Principal

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]
_NO_STORE = "private, no-store"

#: ``app.state`` attribute the composition root will set once it builds the
#: service.  Namespaced so it cannot collide with a Starlette or middleware key.
CAPABILITY_PREFLIGHT_STATE_KEY = "ne_agent_capability_preflight"
#: The existing PRE-011D service, composed beside BD-15's selection authority.
MODEL_PREFLIGHT_STATE_KEY = "ne_agent_model_preflight"


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail="Agent not found",
        headers={"Cache-Control": _NO_STORE},
    )


def agent_capability_preflight_service(request: Request) -> AgentCapabilityPreflightService:
    """The composed service, or an honest 503 when nothing composed one.

    503 -- never 404 and never an empty surface: an unconfigured deployment has
    no opinion about whether this Agent exists or what it may use, and both of
    those alternatives would state one.
    """
    service = getattr(request.app.state, CAPABILITY_PREFLIGHT_STATE_KEY, None)
    if not isinstance(service, AgentCapabilityPreflightService):
        raise HTTPException(
            status_code=503,
            detail="Agent capability preflight is not configured",
            headers={"Cache-Control": _NO_STORE},
        )
    return service


_CapabilityPreflight = Annotated[
    AgentCapabilityPreflightService, Depends(agent_capability_preflight_service)
]


def agent_model_preflight_service(request: Request) -> AgentModelPreflightService:
    """Return the composed 011D service, never a model-free partial surface."""
    service = getattr(request.app.state, MODEL_PREFLIGHT_STATE_KEY, None)
    if not isinstance(service, AgentModelPreflightService):
        raise HTTPException(
            status_code=503,
            detail="Agent model preflight is not configured",
            headers={"Cache-Control": _NO_STORE},
        )
    return service


_ModelPreflight = Annotated[AgentModelPreflightService, Depends(agent_model_preflight_service)]


@router.get("/v1/agents", response_model=AgentCatalogPage)
async def list_agents(
    principal: _Principal,
    state: _State,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> AgentCatalogPage:
    after = decode_agent_cursor(cursor) if cursor is not None else None
    if cursor is not None and after is None:
        raise _not_found()
    rows, next_after = state.agent_catalog.list_visible(principal, after=after, limit=limit)
    response.headers["Cache-Control"] = _NO_STORE
    return AgentCatalogPage(
        items=tuple(agent_catalog_item(row) for row in rows),
        next_cursor=encode_agent_cursor(next_after) if next_after is not None else None,
    )


@router.get("/v1/agents/{agent_id}", response_model=AgentCatalogItem)
async def get_agent(
    agent_id: str,
    principal: _Principal,
    state: _State,
    response: Response,
) -> AgentCatalogItem:
    row = state.agent_catalog.get_visible(agent_id, principal)
    if row is None:
        raise _not_found()
    response.headers["Cache-Control"] = _NO_STORE
    return agent_catalog_item(row)


@router.get("/v1/agents/{agent_id}/preflight", response_model=AgentPreflightItem)
async def get_agent_preflight(
    agent_id: str,
    principal: _Principal,
    capability_service: _CapabilityPreflight,
    model_service: _ModelPreflight,
    response: Response,
    capability_hash: str | None = None,
) -> AgentPreflightItem:
    """Read this principal's complete capability, strategy and model surface.

    ``capability_hash`` is the submit-time join's read half: supply the hash a
    previous read returned and the surface is re-derived and compared HERE.  A
    stale hash is refused with the SAME 404 a hidden or unknown Agent gets, so
    the refusal reveals neither that the Agent exists nor that it changed.
    """
    capabilities = await capability_service.preflight(
        agent_id=agent_id,
        principal=principal,
        presented_capability_hash=capability_hash,
    )
    if capabilities is None:
        raise _not_found()

    try:
        model = await model_service.preflight(agent_id=agent_id, principal=principal)
    except ModelSelectionEvidenceError as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent preflight is unavailable",
            headers={"Cache-Control": _NO_STORE},
        ) from exc
    if model is None:
        # Both services are composed over the SAME catalog. A disagreement is a
        # broken composition, never permission to return only one half.
        raise HTTPException(
            status_code=503,
            detail="Agent preflight is unavailable",
            headers={"Cache-Control": _NO_STORE},
        )
    if (
        model.agent_id != capabilities.agent_id
        or model.agent_version != capabilities.agent_version
        or model.definition_hash != capabilities.definition_hash
        or model.available != capabilities.available
        or model.availability_reasons != capabilities.availability_reasons
    ):
        raise HTTPException(
            status_code=503,
            detail="Agent preflight is unavailable",
            headers={"Cache-Control": _NO_STORE},
        )
    response.headers["Cache-Control"] = _NO_STORE
    return agent_preflight_item(capabilities, model)
