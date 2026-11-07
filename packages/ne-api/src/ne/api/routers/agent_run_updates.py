# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""HTTP surface for principal-scoped Attention and Recent Run updates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ne.api.agent_run_updates import (
    AgentRunUpdatePage,
    RunUpdateView,
    decode_update_cursor,
    encode_update_cursor,
    projection_from_state,
)
from ne.api.deps import ApiState, api_state, current_principal
from ne.api.run_projection import CursorUnavailable
from ne.contracts.types import Principal

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


def _utc_bound(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise HTTPException(
            status_code=422,
            detail="Run update window bounds must be explicit UTC timestamps",
            headers={"Cache-Control": _NO_STORE},
        )
    return value.astimezone(UTC)


@router.get("/v1/agents/updates", response_model=AgentRunUpdatePage)
async def list_agent_run_updates(
    principal: _Principal,
    state: _State,
    response: Response,
    view: RunUpdateView,
    window_start: datetime,
    window_end: datetime,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> AgentRunUpdatePage:
    start = _utc_bound(window_start)
    end = _utc_bound(window_end)
    if start >= end:
        raise HTTPException(
            status_code=422,
            detail="Run update window must be non-empty and half-open",
            headers={"Cache-Control": _NO_STORE},
        )
    try:
        after = (
            decode_update_cursor(cursor, view=view, window_start=start, window_end=end)
            if cursor is not None
            else None
        )
        projection = projection_from_state(state)
    except CursorUnavailable as exc:
        raise _not_found() from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="agent run update projection unavailable",
            headers={"Cache-Control": _NO_STORE},
        ) from exc
    items, next_after = await projection.list_updates(
        principal=principal,
        department=_department(principal),
        view=view,
        window_start=start,
        window_end=end,
        limit=limit,
        after=after,
    )
    next_cursor = None
    if next_after is not None:
        next_cursor = encode_update_cursor(view, start, end, *next_after)
    response.headers["Cache-Control"] = _NO_STORE
    return AgentRunUpdatePage(
        view=view,
        window_start=start,
        window_end=end,
        items=items,
        next_cursor=next_cursor,
    )
