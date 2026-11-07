# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SqlAlchemyEpisodicStore — append-only, per-agent-isolated agent memory (NE-037).

Agent memory v1: an APPEND-ONLY episodic event store for agent experience
(decisions, outcomes, steward/HITL verdicts, learned facts) plus the promotion
bookkeeping events. Mirrors ``store.py`` (NE-033 ``SqlAlchemySessionStore``):
sync SQLAlchemy-Core over a portable engine, so the async methods make DIRECT
sync calls on short-lived transactions. Portable Core column types only, so
SQLite (CI) and Postgres behave identically by construction.

APPEND-ONLY is a hard, audit-grade invariant: the entire public mutation surface
is a single-row INSERT (:meth:`SqlAlchemyEpisodicStore.append`). No update /
delete / edit method exists anywhere in this module. Promotion state changes are
themselves appended events (``promotion-proposed`` / ``promotion-approved``),
never row mutations.

Per-agent isolation is by construction: every query is ``agent_id``-scoped, and
:meth:`SqlAlchemyEpisodicStore.get` raises :class:`MemoryEventNotFoundError` on a
cross-agent id so an agent can never even confirm the existence of another
agent's event. The only cross-agent visibility is the explicit append-time
``shared_with`` allowlist, read back via
:meth:`SqlAlchemyEpisodicStore.recall_shared`.

SQLite/Postgres parity trap: SQLite returns NAIVE datetimes and compares them
lexicographically. Every timestamp is normalized to UTC before insert and before
binding a since/until query param, and coerced back to UTC-aware on read.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.audit import ensure_transactional_sqlite
from ne.audit.engine import create_engine_with_timeouts
from ne.contracts.runtime import MemoryEvent
from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import MemoryEventNotFoundError


class MemoryEventKind(enum.StrEnum):
    """Canonical episodic-event kinds.

    The store accepts ANY non-empty kind string (the frozen ``MemoryEvent.kind``
    is a free ``str``); these are the canonical values the runtime writes.
    """

    DECISION = "decision"
    OUTCOME = "outcome"
    STEWARD_VERDICT = "steward-verdict"
    LEARNED_FACT = "learned-fact"
    PROMOTION_PROPOSED = "promotion-proposed"
    PROMOTION_APPROVED = "promotion-approved"


# Its OWN MetaData — do NOT reuse store.py's, so ensure_runtime_schema and
# ensure_memory_schema stay independent.
memory_metadata = sa.MetaData()

ne_agent_memory_events = sa.Table(
    "ne_agent_memory_events",
    memory_metadata,
    sa.Column("event_id", sa.String, primary_key=True),
    sa.Column("agent_id", sa.String, nullable=False),
    sa.Column("session_id", sa.String, nullable=True),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("provenance", sa.String, nullable=False, default=""),
    sa.Column("shared_with", sa.Text, nullable=False, default="[]"),
    sa.Index("ix_ne_memory_agent_occurred", "agent_id", "occurred_at"),
    sa.Index("ix_ne_memory_agent_kind", "agent_id", "kind"),
)


def ensure_memory_schema(engine: sa.Engine) -> None:
    """Create the agent-memory table. Idempotent (``create_all`` skips existing)."""
    memory_metadata.create_all(engine)


def _require_aware(dt: datetime, name: str) -> None:
    """Raise ``ValueError`` if ``dt`` is timezone-naive (parity guard)."""
    if dt.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


def _as_utc(dt: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC (SQLite reads back naive)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class StoredMemoryEvent(BaseModel):
    """A frozen, fully-materialized episodic event as stored + read back."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    agent_id: str
    session_id: str | None
    kind: str
    content: str
    occurred_at: datetime
    recorded_at: datetime
    provenance: str
    shared_with: tuple[str, ...]

    def as_memory_event(self) -> MemoryEvent:
        """Project to the frozen ``ne.contracts.runtime.MemoryEvent`` seam."""
        return MemoryEvent(
            kind=self.kind,
            content=self.content,
            occurred_at=self.occurred_at,
            provenance=self.provenance,
        )


def _row_to_stored(row: sa.Row[Any]) -> StoredMemoryEvent:
    return StoredMemoryEvent(
        event_id=str(row.event_id),
        agent_id=str(row.agent_id),
        session_id=str(row.session_id) if row.session_id is not None else None,
        kind=str(row.kind),
        content=str(row.content),
        occurred_at=_as_utc(row.occurred_at),
        recorded_at=_as_utc(row.recorded_at),
        provenance=str(row.provenance),
        shared_with=tuple(json.loads(row.shared_with)),
    )


class SqlAlchemyEpisodicStore:
    """Append-only per-agent episodic memory over sync SQLAlchemy-Core.

    The COMPLETE public mutation surface is a single INSERT (:meth:`append`) —
    append-only by construction. No update / delete / edit method exists.
    """

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> SqlAlchemyEpisodicStore:
        """Build over ``settings.session_database_url`` (memory rides the same URL).

        No new settings field is introduced (keeping ``config.py`` untouched for
        the NE-034 rebase); episodic memory shares the platform-Postgres session
        URL. Pass ``SqlAlchemyEpisodicStore(engine)`` directly to override.
        """
        if not settings.session_database_url:
            raise ValueError(
                "RuntimeSettings.session_database_url is empty; set "
                "NE_RUNTIME_SESSION_DATABASE_URL or construct "
                "SqlAlchemyEpisodicStore(engine) directly"
            )
        engine = create_engine_with_timeouts(
            settings.session_database_url, timeouts=settings.engine_timeouts
        )
        ensure_transactional_sqlite(engine)
        ensure_memory_schema(engine)
        return cls(engine)

    @property
    def engine(self) -> sa.Engine:
        return self._engine

    async def append(
        self,
        *,
        agent_id: str,
        kind: str,
        content: str,
        occurred_at: datetime,
        provenance: str = "",
        session_id: str | None = None,
        shared_with: Sequence[str] = (),
    ) -> str:
        """Append ONE episodic event; returns its ``event_id``. Append-only.

        ``event_id`` is a client-generated ``uuid4().hex`` (a single INSERT, so
        no sequence TOCTOU). ``occurred_at`` must be tz-aware; ``shared_with`` is
        stored as a sorted JSON array with the owner and empties removed.
        """
        if not agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")
        if not kind.strip():
            raise ValueError("kind must be a non-empty string")
        if not content.strip():
            raise ValueError("content must be a non-empty string")
        _require_aware(occurred_at, "occurred_at")
        for other in shared_with:
            if not other.strip():
                raise ValueError("shared_with entries must be non-empty strings")
        shared = sorted(set(shared_with) - {agent_id})
        event_id = uuid4().hex
        with self._engine.begin() as conn:
            conn.execute(
                ne_agent_memory_events.insert().values(
                    event_id=event_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    kind=str(kind),
                    content=content,
                    occurred_at=_as_utc(occurred_at),
                    recorded_at=datetime.now(UTC),
                    provenance=provenance,
                    shared_with=json.dumps(shared),
                )
            )
        return event_id

    def _select_for_agent(
        self,
        agent_id: str,
        *,
        kinds: Sequence[str] | None,
        since: datetime | None,
        until: datetime | None,
    ) -> sa.Select[Any]:
        stmt = sa.select(ne_agent_memory_events).where(
            ne_agent_memory_events.c.agent_id == agent_id
        )
        if kinds is not None:
            stmt = stmt.where(ne_agent_memory_events.c.kind.in_([str(k) for k in kinds]))
        if since is not None:
            _require_aware(since, "since")
            stmt = stmt.where(ne_agent_memory_events.c.occurred_at >= _as_utc(since))
        if until is not None:
            _require_aware(until, "until")
            stmt = stmt.where(ne_agent_memory_events.c.occurred_at <= _as_utc(until))
        return stmt.order_by(
            ne_agent_memory_events.c.occurred_at.desc(),
            ne_agent_memory_events.c.recorded_at.desc(),
            ne_agent_memory_events.c.event_id.desc(),
        )

    async def recall(
        self,
        *,
        agent_id: str,
        kinds: Sequence[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = 100,
    ) -> list[StoredMemoryEvent]:
        """Time-scoped recall for ONE agent (isolation is unconditional).

        Filters: ``agent_id`` always; ``kind IN kinds`` when given; an INCLUSIVE
        ``since <= occurred_at <= until`` window. Newest first; ``limit`` caps the
        result when not ``None``.
        """
        stmt = self._select_for_agent(agent_id, kinds=kinds, since=since, until=until)
        if limit is not None:
            stmt = stmt.limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_row_to_stored(row) for row in rows]

    async def recall_shared(
        self,
        *,
        owner_agent_id: str,
        requesting_agent_id: str,
        kinds: Sequence[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = 100,
    ) -> list[StoredMemoryEvent]:
        """Recall ``owner``'s events that were explicitly shared with ``requesting``.

        Portable v1: fetch the owner's matching rows (same filters, no limit),
        Python-filter to rows whose ``shared_with`` contains the requester, then
        truncate to ``limit``. JSON-membership pushdown + pagination is an NE-070
        note.
        """
        stmt = self._select_for_agent(owner_agent_id, kinds=kinds, since=since, until=until)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        shared = [
            stored
            for row in rows
            if requesting_agent_id in (stored := _row_to_stored(row)).shared_with
        ]
        if limit is not None:
            shared = shared[:limit]
        return shared

    async def get(self, *, agent_id: str, event_id: str) -> StoredMemoryEvent:
        """Fetch one of ``agent_id``'s own events by id.

        Raises :class:`MemoryEventNotFoundError` when no row exists OR the row
        belongs to another agent — existence is never leaked cross-agent.
        """
        stmt = sa.select(ne_agent_memory_events).where(
            ne_agent_memory_events.c.event_id == event_id
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None or str(row.agent_id) != agent_id:
            raise MemoryEventNotFoundError(
                f"no memory event {event_id!r} visible to agent {agent_id!r}"
            )
        return _row_to_stored(row)


class AgentMemory:
    """Per-agent facade implementing the FROZEN ``MemoryProvider`` Protocol.

    Binds a single ``agent_id`` (and optional ``session_id``) to a shared
    :class:`SqlAlchemyEpisodicStore`; every append is stamped with the bound
    agent + session, and every recall is hard-scoped to the bound agent.

    v1 recall is a keyword/substring scan over the bound agent's episodic events;
    semantic + bi-temporal recall (NE-083) swaps in behind this SAME frozen
    Protocol with no interface change.
    """

    def __init__(
        self,
        store: SqlAlchemyEpisodicStore,
        agent_id: str,
        *,
        session_id: str | None = None,
    ) -> None:
        if not agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")
        self._store = store
        self._agent_id = agent_id
        self._session_id = session_id

    async def append(self, event: MemoryEvent) -> None:
        """Append via the frozen Protocol, stamping the bound agent + session."""
        await self._store.append(
            agent_id=self._agent_id,
            kind=event.kind,
            content=event.content,
            occurred_at=event.occurred_at,
            provenance=event.provenance,
            session_id=self._session_id,
        )

    async def recall(
        self,
        query: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 20,
    ) -> Sequence[MemoryEvent]:
        """Keyword/substring recall over the bound agent's events, newest-first.

        An empty ``query`` matches all; otherwise a case-insensitive match when
        the query is a content substring OR equals a kind. ``limit`` caps the
        result.
        """
        rows = await self._store.recall(
            agent_id=self._agent_id, since=since, until=until, limit=None
        )
        q = query.strip().lower()
        matched: list[MemoryEvent] = []
        for row in rows:
            if not q or q in row.content.lower() or q == row.kind.lower():
                matched.append(row.as_memory_event())
            if len(matched) >= limit:
                break
        return matched
