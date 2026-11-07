# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PostgresAuditSink — the frozen ``AuditSink`` Protocol over the hash chain.

Structurally implements ``ne.contracts.governance.AuditSink`` (``append`` +
``verify``). The Protocol is async (the platform's ports are async), but the
chain writes are sync SQLAlchemy-Core — so the async methods make DIRECT sync
calls on a short-lived transaction, matching the repo's ObjectStore/SecretStore
adapter style. No async DB driver and no pytest-asyncio are introduced (SPEC C4:
zero new deps).

``append_record`` is re-exported as the same-transaction seam: an emitter that
must commit a business write + an ne-events outbox append + an audit append
atomically calls :func:`ne.audit.append_record` on its own connection.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa

from ne.audit.config import AuditSettings
from ne.audit.engine import (
    call_db,
    create_engine_with_timeouts,
    default_offload,
    ensure_transactional_sqlite,
)
from ne.audit.store import append_record, ensure_audit_schema
from ne.audit.verify import to_audit_verification, verify_chain
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditVerification

__all__ = ["PostgresAuditSink"]


class PostgresAuditSink:
    """Durable, tamper-evident audit sink backed by the hash chain."""

    def __init__(self, engine: sa.Engine, *, offload_db_io: bool | None = None) -> None:
        ensure_transactional_sqlite(engine)
        self._engine = engine
        # Offload the blocking chain writes to a worker thread on non-sqlite
        # dialects so a slow/blocked Postgres append never pins the ASGI event
        # loop (P2-16). sqlite stays on-loop (thread-bound pysqlite connections).
        self._offload = default_offload(engine) if offload_db_io is None else offload_db_io

    @classmethod
    def from_settings(cls, settings: AuditSettings) -> PostgresAuditSink:
        if not settings.database_url:
            raise ValueError(
                "AuditSettings.database_url is empty; set NE_AUDIT_DATABASE_URL or "
                "construct PostgresAuditSink(engine) directly"
            )
        engine = create_engine_with_timeouts(
            settings.database_url, timeouts=settings.engine_timeouts
        )
        ensure_audit_schema(engine)
        return cls(engine)

    @property
    def engine(self) -> sa.Engine:
        return self._engine

    def _append_sync(self, event: AuditEventLike) -> None:
        with self._engine.begin() as conn:
            append_record(conn, event)

    async def append(self, event: AuditEventLike) -> None:
        await call_db(self._offload, self._append_sync, event)

    def _verify_sync(self, start: datetime | None, end: datetime | None) -> AuditVerification:
        return to_audit_verification(verify_chain(self._engine, start=start, end=end))

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return await call_db(self._offload, self._verify_sync, start, end)
