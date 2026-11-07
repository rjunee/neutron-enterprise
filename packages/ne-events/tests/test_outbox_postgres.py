# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKABLE Postgres acceptance for the outbox.

Env-gated on ``NE_TEST_POSTGRES_URL``. The build host has no Docker daemon, so
this module auto-skips (pending-live-verification ledger). When a real Postgres
endpoint is configured it re-runs the behaviors whose Postgres semantics differ
from SQLite: SAVEPOINT idempotency under real transaction-abort behavior,
crash-window redelivery, and JSONB payload round-trip.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from ne.events import append, drain_once, read_batch
from ne.events.outbox import OutboxRecord, ensure_outbox_schema, metadata, ne_outbox_events
from ne.schema import AuditEvent, AuditOutcome

_URL = os.environ.get("NE_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="live Postgres not configured (set NE_TEST_POSTGRES_URL); "
    "pending-live-verification ledger",
)

_HASH = "c" * 64


def _audit(event_id: str | None = None) -> AuditEvent:
    return AuditEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type="audit",
        config_hash=_HASH,
        actor="sys",
        provenance={"source": "pg-test"},
        occurred_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        action="a",
        resource="r",
        outcome=AuditOutcome.success,
    )


@pytest.fixture
def pg_engine() -> Iterator[sa.Engine]:
    assert _URL is not None
    engine = sa.create_engine(_URL)
    # Isolate: drop and recreate our tables for a clean slate.
    metadata.drop_all(engine)
    ensure_outbox_schema(engine)
    try:
        yield engine
    finally:
        metadata.drop_all(engine)
        engine.dispose()


def test_savepoint_idempotency_under_real_postgres(pg_engine: sa.Engine) -> None:
    dup = _audit(event_id="ev-dup")
    with pg_engine.begin() as conn:
        first = append(conn, dup)
        again = append(conn, dup)
        after = append(conn, _audit(event_id="ev-after"))
    assert first == again
    assert after != first
    with pg_engine.connect() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(ne_outbox_events)).scalar_one()
    assert count == 2


def test_crash_window_redelivery_under_real_postgres(pg_engine: sa.Engine) -> None:
    with pg_engine.begin() as conn:
        for _ in range(3):
            append(conn, _audit())

    seen: list[list[str]] = []

    def crashing(records: list[OutboxRecord]) -> None:
        seen.append([r.event_id for r in records])
        raise RuntimeError("crash")

    drain_once(pg_engine, "exp", crashing)
    drain_once(pg_engine, "exp", crashing)
    assert seen[0] == seen[1]


def test_jsonb_payload_round_trip(pg_engine: sa.Engine) -> None:
    ev = _audit(event_id="ev-jsonb")
    with pg_engine.begin() as conn:
        append(conn, ev)
        records, _ = read_batch(conn, "reader")
    payload = records[0].payload
    assert payload["provenance"] == {"source": "pg-test"}
    assert payload["event_id"] == "ev-jsonb"
