# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal-scoped, content-free Attention and Recent Run updates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, cast

import sqlalchemy as sa
from anyio import to_thread
from pydantic import BaseModel, ConfigDict

from ne.api.agent_catalog import AgentCatalog
from ne.api.run_projection import decode_cursor, encode_cursor, locate_run_engine
from ne.contracts.types import Principal
from ne.runtime.run_store import ne_agent_runs
from ne.runtime.store import ne_agent_conversations

RunUpdateView = Literal["attention", "recent"]
RunUpdateStatus = Literal[
    "waiting_approval",
    "failed",
    "context_exhausted",
    "budget_exhausted",
    "completed",
]
RunUpdateReason = Literal[
    "Approval required",
    "Run failed",
    "Context limit exhausted",
    "Budget limit exhausted",
    "Run completed",
]

_ATTENTION_STATUSES: Final[tuple[str, ...]] = (
    "waiting_approval",
    "failed",
    "context_exhausted",
    "budget_exhausted",
)
_RECENT_STATUSES: Final[tuple[str, ...]] = ("completed",)
_SAFE_REASONS: Final[dict[str, RunUpdateReason]] = {
    "waiting_approval": "Approval required",
    "failed": "Run failed",
    "context_exhausted": "Context limit exhausted",
    "budget_exhausted": "Budget limit exhausted",
    "completed": "Run completed",
}
_SCAN_BATCH: Final[int] = 200


class _WireModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AgentRunUpdate(_WireModel):
    """A linkable update shell containing no Run content or arbitrary detail."""

    run_id: str
    agent_id: str
    conversation_id: str
    agent_label: str
    conversation_title: str
    status: RunUpdateStatus
    reason: RunUpdateReason
    updated_at: datetime


class AgentRunUpdatePage(_WireModel):
    view: RunUpdateView
    window_start: datetime
    window_end: datetime
    items: tuple[AgentRunUpdate, ...]
    next_cursor: str | None


class AgentRunUpdateState(Protocol):
    agent_catalog: AgentCatalog
    engines: list[sa.Engine]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _cursor_kind(view: RunUpdateView, window_start: datetime, window_end: datetime) -> str:
    return (
        f"agent-run-updates:{view}:updated_at-desc:run_id-desc:"
        f"{_utc(window_start).isoformat()}:{_utc(window_end).isoformat()}"
    )


def encode_update_cursor(
    view: RunUpdateView,
    window_start: datetime,
    window_end: datetime,
    updated_at: datetime,
    run_id: str,
) -> str:
    """Encode an opaque position bound to the selected view and exact window."""

    return encode_cursor(_cursor_kind(view, window_start, window_end), updated_at, run_id)


def decode_update_cursor(
    token: str,
    *,
    view: RunUpdateView,
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, str]:
    """Decode only a cursor minted for this selected view and exact window."""

    return decode_cursor(token, kind=_cursor_kind(view, window_start, window_end))


class AgentRunUpdateProjection:
    """Read canonical Runs through owner, department, and current Agent visibility."""

    def __init__(self, engine: sa.Engine, catalog: AgentCatalog) -> None:
        self._engine = engine
        self._catalog = catalog

    async def list_updates(
        self,
        *,
        principal: Principal,
        department: str,
        view: RunUpdateView,
        window_start: datetime,
        window_end: datetime,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> tuple[tuple[AgentRunUpdate, ...], tuple[datetime, str] | None]:
        """Apply every visibility predicate before page selection."""

        if limit < 1 or limit > 100:
            raise ValueError("Run update page limit must be between 1 and 100")
        visible: list[AgentRunUpdate] = []
        scan_after = after
        while len(visible) <= limit:
            rows = await to_thread.run_sync(
                self._candidate_rows,
                principal.id,
                department,
                view,
                _utc(window_start),
                _utc(window_end),
                scan_after,
            )
            for row in rows:
                agent_label = self._visible_agent_label(str(row.agent_id), principal)
                if agent_label is None:
                    continue
                status = str(row.status)
                visible.append(
                    AgentRunUpdate(
                        run_id=str(row.run_id),
                        agent_id=str(row.agent_id),
                        conversation_id=str(row.conversation_id),
                        agent_label=agent_label,
                        conversation_title=(
                            f"Conversation · {_utc(row.conversation_created_at):%Y-%m-%d %H:%M}"
                        ),
                        status=cast(RunUpdateStatus, status),
                        reason=_SAFE_REASONS[status],
                        updated_at=_utc(row.updated_at),
                    )
                )
                if len(visible) > limit:
                    break
            if len(visible) > limit or len(rows) < _SCAN_BATCH:
                break
            last_scanned = rows[-1]
            scan_after = (_utc(last_scanned.updated_at), str(last_scanned.run_id))

        selected = tuple(visible[:limit])
        next_after = None
        if len(visible) > limit and selected:
            last = selected[-1]
            next_after = (_utc(last.updated_at), last.run_id)
        return selected, next_after

    def _visible_agent_label(self, agent_id: str, principal: Principal) -> str | None:
        row = self._catalog.get_visible(agent_id, principal)
        return None if row is None else row.definition.label

    def _candidate_rows(
        self,
        owner_principal_id: str,
        department: str,
        view: RunUpdateView,
        window_start: datetime,
        window_end: datetime,
        after: tuple[datetime, str] | None,
    ) -> list[sa.Row[Any]]:
        statuses = _ATTENTION_STATUSES if view == "attention" else _RECENT_STATUSES
        predicate: sa.ColumnElement[bool] = sa.and_(
            ne_agent_runs.c.owner_principal_id == owner_principal_id,
            ne_agent_runs.c.department == department,
            ne_agent_runs.c.status.in_(statuses),
            ne_agent_runs.c.updated_at >= window_start,
            ne_agent_runs.c.updated_at < window_end,
        )
        if after is not None:
            after_at, after_run_id = after
            predicate = sa.and_(
                predicate,
                sa.or_(
                    ne_agent_runs.c.updated_at < _utc(after_at),
                    sa.and_(
                        ne_agent_runs.c.updated_at == _utc(after_at),
                        ne_agent_runs.c.run_id < after_run_id,
                    ),
                ),
            )
        statement = (
            sa.select(
                ne_agent_runs.c.run_id,
                ne_agent_runs.c.agent_id,
                ne_agent_runs.c.conversation_id,
                ne_agent_runs.c.status,
                ne_agent_runs.c.updated_at,
                ne_agent_conversations.c.created_at.label("conversation_created_at"),
            )
            .select_from(
                ne_agent_runs.join(
                    ne_agent_conversations,
                    sa.and_(
                        ne_agent_conversations.c.conversation_id == ne_agent_runs.c.conversation_id,
                        ne_agent_conversations.c.owner_principal_id
                        == ne_agent_runs.c.owner_principal_id,
                        ne_agent_conversations.c.department == ne_agent_runs.c.department,
                        ne_agent_conversations.c.agent_name
                        == ne_agent_runs.c.conversation_agent_name,
                    ),
                )
            )
            .where(predicate)
            .order_by(ne_agent_runs.c.updated_at.desc(), ne_agent_runs.c.run_id.desc())
            .limit(_SCAN_BATCH)
        )
        with self._engine.connect() as connection:
            return list(connection.execute(statement).all())


def projection_from_state(state: AgentRunUpdateState) -> AgentRunUpdateProjection:
    """Resolve the one canonical Run engine; ambiguity remains unavailable."""

    engine = locate_run_engine(state.engines)
    if engine is None:
        raise RuntimeError("agent run update projection unavailable")
    return AgentRunUpdateProjection(engine, state.agent_catalog)
