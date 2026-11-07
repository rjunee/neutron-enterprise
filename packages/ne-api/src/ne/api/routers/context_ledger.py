# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Authenticated, department-scoped Context Ledger route (BD-14)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ne.api.context_ledger import assemble_agent_context_ledger, assemble_context_ledger
from ne.api.deps import ApiState, api_state, current_principal
from ne.api.routers.chat import _require_department, resolve_session
from ne.api.routers.conversations import _department, _not_found, _visible_agent
from ne.api.wire import AgentContextLedgerView, ContextLedgerView
from ne.contracts.types import Principal

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]


@router.get("/v1/chat/sessions/{session_id}/context-ledger")
async def context_ledger(
    session_id: str,
    response: Response,
    principal: _Principal,
    state: _State,
) -> ContextLedgerView:
    """Return content-free identities for an R3-authorized chat session."""
    response.headers["Cache-Control"] = "private, no-store"
    department = _require_department(principal)
    matched = await resolve_session(session_id, principal.id, department, state)
    if matched is None:
        # Preserve chat's foreign/unknown-session indistinguishability.
        raise HTTPException(status_code=404, detail="unknown session")
    try:
        return await assemble_context_ledger(state, matched.session_id)
    except LookupError as exc:
        # A session reset/deletion racing the R3 lookup remains indistinguishable
        # from an ordinary miss; never disclose another department's identifier.
        raise HTTPException(status_code=404, detail="unknown session") from exc


@router.get(
    "/v1/agents/{agent_id}/conversations/{conversation_id}/context-ledger",
    response_model=AgentContextLedgerView,
)
async def agent_context_ledger(
    agent_id: str,
    conversation_id: str,
    response: Response,
    principal: _Principal,
    state: _State,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> AgentContextLedgerView:
    """Return the bounded canonical context identity for one visible Conversation."""

    _visible_agent(agent_id, principal, state)
    department = _department(principal)
    conversation = await state.session_store.get_conversation(
        conversation_id,
        principal.id,
        department,
    )
    if conversation is None or conversation.agent_name != agent_id:
        raise _not_found()
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await assemble_agent_context_ledger(
            state,
            owner_principal_id=principal.id,
            department=department,
            agent_id=agent_id,
            conversation_id=conversation_id,
            limit=limit,
        )
    except LookupError as exc:
        raise _not_found() from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="agent context ledger unavailable",
            headers={"Cache-Control": "private, no-store"},
        ) from exc
