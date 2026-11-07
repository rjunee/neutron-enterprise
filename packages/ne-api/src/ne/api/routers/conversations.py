# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal-owned Agent Conversation resources."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ne.api.conversation_turns_wire import (
    ConversationTurnPage,
    decode_turn_cursor,
    encode_turn_cursor,
)
from ne.api.deps import ApiState, api_state, current_principal
from ne.api.run_projection import (
    CursorUnavailable,
    decode_cursor,
    encode_cursor,
    projection_from_state,
    run_page,
)
from ne.api.runs_wire import ConversationPage, ConversationView, RunPage
from ne.contracts.types import Principal
from ne.runtime.store import PrincipalConversation

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]
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


def _visible_agent(agent_id: str, principal: Principal, state: ApiState) -> None:
    if state.agent_catalog.get_visible(agent_id, principal) is None:
        raise _not_found()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _title(row: PrincipalConversation) -> str:
    # Only server-authored time is used. No prompt/output/private context is read.
    return f"Conversation · {_utc(row.created_at):%Y-%m-%d %H:%M}"


def _view(row: PrincipalConversation, agent_id: str) -> ConversationView:
    return ConversationView(
        conversation_id=row.conversation_id,
        agent_id=agent_id,
        title=_title(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_sequence=row.last_sequence,
    )


@router.post(
    "/v1/agents/{agent_id}/conversations",
    response_model=ConversationView,
    status_code=201,
)
async def create_conversation(
    agent_id: str,
    principal: _Principal,
    state: _State,
    response: Response,
) -> ConversationView:
    """Mint a blank conversation from authenticated and path-owned identity only."""
    _visible_agent(agent_id, principal, state)
    department = _department(principal)
    row = await state.session_store.create_conversation(
        agent_id,
        department,
        principal.id,
    )
    response.headers["Cache-Control"] = _NO_STORE
    return _view(row, agent_id)


@router.get(
    "/v1/agents/{agent_id}/conversations",
    response_model=ConversationPage,
)
async def list_conversations(
    agent_id: str,
    principal: _Principal,
    state: _State,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> ConversationPage:
    _visible_agent(agent_id, principal, state)
    department = _department(principal)
    after: tuple[datetime, str] | None = None
    if cursor is not None:
        try:
            after = decode_cursor(cursor, kind=f"conversation:{agent_id}")
        except CursorUnavailable as exc:
            raise _not_found() from exc
    rows = [
        row
        for row in await state.session_store.list_conversations(principal.id, department)
        if row.agent_name == agent_id
    ]
    rows.sort(key=lambda row: (_utc(row.updated_at), row.conversation_id), reverse=True)
    if after is not None:
        rows = [row for row in rows if (_utc(row.updated_at), row.conversation_id) < after]
    selected = rows[:limit]
    next_cursor = None
    if len(rows) > limit and selected:
        last = selected[-1]
        next_cursor = encode_cursor(
            f"conversation:{agent_id}", _utc(last.updated_at), last.conversation_id
        )
    response.headers["Cache-Control"] = _NO_STORE
    return ConversationPage(
        items=tuple(_view(row, agent_id) for row in selected),
        next_cursor=next_cursor,
    )


@router.get(
    "/v1/agents/{agent_id}/conversations/{conversation_id}",
    response_model=ConversationView,
)
async def get_conversation(
    agent_id: str,
    conversation_id: str,
    principal: _Principal,
    state: _State,
    response: Response,
) -> ConversationView:
    _visible_agent(agent_id, principal, state)
    row = await state.session_store.get_conversation(
        conversation_id,
        principal.id,
        _department(principal),
    )
    if row is None or row.agent_name != agent_id:
        raise _not_found()
    response.headers["Cache-Control"] = _NO_STORE
    return _view(row, agent_id)


@router.get(
    "/v1/agents/{agent_id}/conversations/{conversation_id}/runs",
    response_model=RunPage,
)
async def list_conversation_runs(
    agent_id: str,
    conversation_id: str,
    principal: _Principal,
    state: _State,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> RunPage:
    _visible_agent(agent_id, principal, state)
    department = _department(principal)
    conversation = await state.session_store.get_conversation(
        conversation_id,
        principal.id,
        department,
    )
    if conversation is None or conversation.agent_name != agent_id:
        raise _not_found()
    kind = f"conversation-runs:{agent_id}:{conversation_id}"
    try:
        after = decode_cursor(cursor, kind=kind) if cursor is not None else None
        projection = projection_from_state(state)
    except (CursorUnavailable, RuntimeError) as exc:
        if isinstance(exc, RuntimeError):
            raise HTTPException(
                status_code=503,
                detail="agent run projection unavailable",
                headers={"Cache-Control": _NO_STORE},
            ) from exc
        raise _not_found() from exc
    rows, next_after = await projection.list_runs(
        owner_principal_id=principal.id,
        department=department,
        agent_id=agent_id,
        conversation_id=conversation_id,
        limit=limit,
        after=after,
    )
    response.headers["Cache-Control"] = _NO_STORE
    return run_page(rows, next_after, cursor_kind=kind)


@router.get(
    "/v1/agents/{agent_id}/conversations/{conversation_id}/turns",
    response_model=ConversationTurnPage,
)
async def list_conversation_turns(
    agent_id: str,
    conversation_id: str,
    principal: _Principal,
    state: _State,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> ConversationTurnPage:
    _visible_agent(agent_id, principal, state)
    department = _department(principal)
    conversation = await state.session_store.get_conversation(
        conversation_id, principal.id, department
    )
    if conversation is None or conversation.agent_name != agent_id:
        raise _not_found()
    try:
        after = decode_turn_cursor(cursor) if cursor is not None else None
        projection = projection_from_state(state)
    except (RuntimeError, ValueError) as exc:
        if isinstance(exc, RuntimeError):
            raise HTTPException(
                status_code=503,
                detail="agent run projection unavailable",
                headers={"Cache-Control": _NO_STORE},
            ) from exc
        raise _not_found() from exc
    items, next_sequence = await projection.conversation_turns(
        owner_principal_id=principal.id,
        department=department,
        agent_id=agent_id,
        conversation_id=conversation_id,
        limit=limit,
        after_sequence=after,
    )
    response.headers["Cache-Control"] = _NO_STORE
    return ConversationTurnPage(
        items=items,
        next_cursor=(None if next_sequence is None else encode_turn_cursor(next_sequence)),
    )
