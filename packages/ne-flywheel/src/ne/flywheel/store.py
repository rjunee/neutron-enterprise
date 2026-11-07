# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SqlAlchemyCorrectionSignalStore — append-only correction-signal capture (NE-064).

Mirrors ``ne.runtime.memory_store.SqlAlchemyEpisodicStore`` in style: sync
SQLAlchemy-Core over a portable engine, so the async methods make DIRECT sync
calls on short-lived transactions. Portable Core column types only, so SQLite
(CI) and Postgres behave identically by construction.

APPEND-ONLY is a hard, audit-grade invariant: the entire public mutation
surface of :class:`SqlAlchemyCorrectionSignalStore` is a single idempotent
INSERT (:meth:`SqlAlchemyCorrectionSignalStore.append`). No update / delete /
edit method exists anywhere in this module. A correction signal is a durable
fact about what happened; it is never revised or removed in place.

Reads (:meth:`SqlAlchemyCorrectionSignalStore.read_signals`) are ordered by the
append id — the coordinate NE-065's clustering cursor advances over,
mirroring the ne-events outbox's ``id``-ordered cursor semantics.

SQLite/Postgres parity trap: SQLite returns NAIVE datetimes and compares them
lexicographically. Every timestamp is normalized to UTC before insert and
before binding a since/until query param, and coerced back to UTC-aware on
read (copied verbatim from ``memory_store.py``'s ``_require_aware``/``_as_utc``).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from ne.audit import ensure_transactional_sqlite
from ne.schema import CorrectionSignal

# Its OWN MetaData — independent of the ne-events outbox metadata and every
# other package's, so ``ensure_flywheel_schema`` stays independent.
flywheel_metadata = sa.MetaData()

ne_correction_signals = sa.Table(
    "ne_correction_signals",
    flywheel_metadata,
    # BigInteger on Postgres (Identity); Integer on SQLite so it aliases the
    # rowid and autoincrements (SQLite only autoincrements INTEGER PRIMARY KEY).
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    ),
    sa.Column("event_id", sa.String, nullable=False, unique=True),
    sa.Column("signal_kind", sa.String, nullable=False),
    sa.Column("surface", sa.String, nullable=False),
    sa.Column("principal_id", sa.String, nullable=False),
    sa.Column("session_id", sa.String, nullable=False),
    sa.Column("turn_id", sa.String, nullable=True),
    sa.Column("message_id", sa.String, nullable=True),
    sa.Column("subject_ref", sa.String, nullable=False),
    sa.Column("value", sa.Float, nullable=True),
    sa.Column("comment", sa.Text, nullable=True),
    sa.Column("actor", sa.String, nullable=False),
    sa.Column("config_hash", sa.String, nullable=False),
    sa.Column("kpi_tag", sa.String, nullable=True),
    sa.Column("trace_id", sa.String, nullable=True),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
    ),
    sa.Index("ix_ne_correction_signals_kind_occurred", "signal_kind", "occurred_at"),
    sa.Index("ix_ne_correction_signals_surface_occurred", "surface", "occurred_at"),
    sa.Index("ix_ne_correction_signals_principal", "principal_id"),
)


def ensure_flywheel_schema(engine: sa.Engine) -> None:
    """Create the correction-signal table if absent. Idempotent (``create_all``)."""
    flywheel_metadata.create_all(engine)


def _require_aware(dt: datetime, name: str) -> None:
    """Raise ``ValueError`` if ``dt`` is timezone-naive (parity guard)."""
    if dt.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


def _as_utc(dt: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC (SQLite reads back naive)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class StoredCorrectionSignal(BaseModel):
    """A frozen, fully-materialized correction signal as stored + read back."""

    model_config = ConfigDict(frozen=True)

    id: int
    event_id: str
    signal_kind: str
    surface: str
    principal_id: str
    session_id: str
    turn_id: str | None
    message_id: str | None
    subject_ref: str
    value: float | None
    comment: str | None
    actor: str
    config_hash: str
    kpi_tag: str | None
    trace_id: str | None
    occurred_at: datetime
    recorded_at: datetime
    payload: dict[str, Any]


def _row_to_stored(row: sa.Row[Any]) -> StoredCorrectionSignal:
    return StoredCorrectionSignal(
        id=int(row.id),
        event_id=str(row.event_id),
        signal_kind=str(row.signal_kind),
        surface=str(row.surface),
        principal_id=str(row.principal_id),
        session_id=str(row.session_id),
        turn_id=str(row.turn_id) if row.turn_id is not None else None,
        message_id=str(row.message_id) if row.message_id is not None else None,
        subject_ref=str(row.subject_ref),
        value=float(row.value) if row.value is not None else None,
        comment=str(row.comment) if row.comment is not None else None,
        actor=str(row.actor),
        config_hash=str(row.config_hash),
        kpi_tag=str(row.kpi_tag) if row.kpi_tag is not None else None,
        trace_id=str(row.trace_id) if row.trace_id is not None else None,
        occurred_at=_as_utc(row.occurred_at),
        recorded_at=_as_utc(row.recorded_at),
        payload=dict(row.payload),
    )


class SqlAlchemyCorrectionSignalStore:
    """Append-only correction-signal store over sync SQLAlchemy-Core.

    The COMPLETE public mutation surface is :meth:`append` — one idempotent
    INSERT. No update / delete / edit method exists anywhere in this class.
    """

    def __init__(self, engine: sa.Engine) -> None:
        ensure_transactional_sqlite(engine)
        self._engine = engine

    @property
    def engine(self) -> sa.Engine:
        return self._engine

    def append(
        self,
        conn: sa.Connection,
        signal: CorrectionSignal,
        *,
        surface: str,
        principal_id: str,
        session_id: str,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> int:
        """Append ONE correction signal on the CALLER's connection; return its id.

        ``signal`` is the FROZEN ``ne.schema.CorrectionSignal`` event (built via
        ``ne.events.new_correction_signal`` / ``build_correction_signal`` —
        never hand-rolled); every column BESIDES the provenance-projected ones
        (``surface``/``principal_id``/``session_id``/``turn_id``/``message_id``,
        which are not first-class fields on the event's flat schema) is derived
        directly from it, so the stored row and the outbox payload can never
        diverge from a single source of truth. ``payload`` is the signal's own
        ``model_dump(mode="json")`` — the SAME post-scrub JSON the outbox stores.

        Runs on ``conn`` (not its own engine) so a caller (``capture.py``) can
        compose this INSERT atomically with the ne-events outbox append in ONE
        transaction.

        Idempotent on a duplicate ``event_id``: the INSERT runs in a SAVEPOINT
        (mirroring the ne-events outbox ``append`` verbatim); a unique-violation
        rolls back just the SAVEPOINT and the EXISTING row's id is returned,
        leaving the caller's enclosing transaction usable.
        """
        _require_aware(signal.occurred_at, "signal.occurred_at")
        payload = signal.model_dump(mode="json")
        stmt = ne_correction_signals.insert().values(
            event_id=signal.event_id,
            signal_kind=str(signal.signal_kind),
            surface=surface,
            principal_id=principal_id,
            session_id=session_id,
            turn_id=turn_id,
            message_id=message_id,
            subject_ref=signal.subject_ref,
            value=signal.value,
            comment=signal.comment,
            actor=signal.actor,
            config_hash=signal.config_hash,
            kpi_tag=signal.kpi_tag,
            trace_id=signal.trace_id,
            occurred_at=_as_utc(signal.occurred_at),
            recorded_at=datetime.now(UTC),
            payload=payload,
        )
        try:
            with conn.begin_nested():
                result = conn.execute(stmt)
            pk = result.inserted_primary_key
            if pk is None:  # pragma: no cover - a single-row insert always yields a pk
                raise RuntimeError("correction-signal insert returned no primary key")
            return int(pk[0])
        except IntegrityError:
            existing = conn.execute(
                sa.select(ne_correction_signals.c.id).where(
                    ne_correction_signals.c.event_id == signal.event_id
                )
            ).scalar_one()
            return int(existing)

    def read_signals(
        self,
        *,
        after_id: int = 0,
        limit: int = 100,
        kinds: Sequence[str] | None = None,
        surface: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[StoredCorrectionSignal]:
        """Cursor-ordered read for NE-065 clustering: ``id > after_id``, ASC, capped.

        Filters (all optional, ANDed): ``kinds`` (signal_kind IN), ``surface``
        (exact match), an INCLUSIVE ``since <= occurred_at <= until`` window.
        """
        stmt = sa.select(ne_correction_signals).where(ne_correction_signals.c.id > after_id)
        if kinds is not None:
            stmt = stmt.where(ne_correction_signals.c.signal_kind.in_([str(k) for k in kinds]))
        if surface is not None:
            stmt = stmt.where(ne_correction_signals.c.surface == surface)
        if since is not None:
            _require_aware(since, "since")
            stmt = stmt.where(ne_correction_signals.c.occurred_at >= _as_utc(since))
        if until is not None:
            _require_aware(until, "until")
            stmt = stmt.where(ne_correction_signals.c.occurred_at <= _as_utc(until))
        stmt = stmt.order_by(ne_correction_signals.c.id.asc()).limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_row_to_stored(row) for row in rows]

    def count_by_kind(self) -> dict[str, int]:
        """Convenience: a ``{signal_kind: count}`` map over the whole store."""
        stmt = sa.select(ne_correction_signals.c.signal_kind, sa.func.count()).group_by(
            ne_correction_signals.c.signal_kind
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return {str(kind): int(count) for kind, count in rows}
