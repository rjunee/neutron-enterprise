# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Read-only deterministic execution-profile route for visible Agents."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ne.api.agent_profile_projection import (
    AgentProfileProjectionState,
    AgentWorkflowProfileView,
    project_visible_agent_profile,
)
from ne.api.deps import current_principal
from ne.contracts.types import Principal

router = APIRouter()

AGENT_PROFILE_PROJECTION_STATE_KEY = "ne_agent_profile_projection"
_NO_STORE = "private, no-store"

_Principal = Annotated[Principal, Depends(current_principal)]


def agent_profile_projection_state(request: Request) -> AgentProfileProjectionState:
    """Resolve the typed boot projection; never construct a profile per request."""
    state = getattr(request.app.state, AGENT_PROFILE_PROJECTION_STATE_KEY, None)
    if not isinstance(state, AgentProfileProjectionState):
        raise HTTPException(
            status_code=503,
            detail="Agent profile projection is not configured",
            headers={"Cache-Control": _NO_STORE},
        )
    return state


_State = Annotated[AgentProfileProjectionState, Depends(agent_profile_projection_state)]


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail="Agent profile not found",
        headers={"Cache-Control": _NO_STORE},
    )


@router.get(
    "/v1/agents/{agent_id}/workflow-profile",
    response_model=AgentWorkflowProfileView,
)
async def get_agent_profile(
    agent_id: str,
    principal: _Principal,
    state: _State,
    response: Response,
) -> AgentWorkflowProfileView:
    profile = project_visible_agent_profile(state, agent_id=agent_id, principal=principal)
    if profile is None:
        raise _not_found()
    response.headers["Cache-Control"] = _NO_STORE
    return profile
