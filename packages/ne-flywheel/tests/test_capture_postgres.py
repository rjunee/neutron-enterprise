# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKABLE Postgres acceptance for correction-signal capture (NE-064).

Env-gated on ``NE_TEST_POSTGRES_URL``, mirroring
``ne-events/tests/test_outbox_postgres.py``. The build host has no Docker
daemon, so this module auto-skips (pending-live-verification ledger). When a
real Postgres endpoint is configured it re-runs the capture-roundtrip,
idempotency, and atomic-dual-write subset whose semantics differ from SQLite
(JSONB payload round-trip, Identity primary keys, real SAVEPOINT behavior).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.contracts.types import Principal
from ne.events import ensure_outbox_schema, read_batch
from ne.events.outbox import metadata as outbox_metadata
from ne.flywheel.capture import CorrectionSignalCapture
from ne.flywheel.signals import SignalProvenance, SignalSurface, ThumbsSignal
from ne.flywheel.store import ensure_flywheel_schema
from ne.flywheel.store import flywheel_metadata as signal_metadata

_URL = os.environ.get("NE_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="live Postgres not configured (set NE_TEST_POSTGRES_URL); "
    "pending-live-verification ledger",
)


class _DemoSettings(NeSettings):
    demo: int = 7


@pytest.fixture
def pg_engine() -> Iterator[sa.Engine]:
    assert _URL is not None
    engine = sa.create_engine(_URL)
    outbox_metadata.drop_all(engine)
    signal_metadata.drop_all(engine)
    ensure_flywheel_schema(engine)
    ensure_outbox_schema(engine)
    try:
        yield engine
    finally:
        outbox_metadata.drop_all(engine)
        signal_metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def frozen_config() -> Iterator[str]:
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


def _prov() -> SignalProvenance:
    return SignalProvenance(
        principal=Principal(id="user:pg@acme.com"),
        surface=SignalSurface.API,
        session_id="sess-pg",
    )


def test_capture_roundtrip_under_real_postgres(pg_engine: sa.Engine, frozen_config: str) -> None:
    capture = CorrectionSignalCapture(pg_engine)

    async def go() -> str:
        return await capture.capture(_prov(), ThumbsSignal(up=True, target_ref="ans-1"))

    event_id = asyncio.run(go())

    rows = capture.store.read_signals()
    assert len(rows) == 1
    assert rows[0].event_id == event_id
    assert rows[0].payload["event_id"] == event_id


def test_capture_idempotency_under_real_postgres(pg_engine: sa.Engine, frozen_config: str) -> None:
    capture = CorrectionSignalCapture(pg_engine)

    async def go() -> tuple[str, str]:
        first = await capture.capture(
            _prov(), ThumbsSignal(up=True, target_ref="ans-1"), event_id="ev-pg-dup"
        )
        second = await capture.capture(
            _prov(), ThumbsSignal(up=True, target_ref="ans-1"), event_id="ev-pg-dup"
        )
        return first, second

    first, second = asyncio.run(go())
    assert first == second
    assert len(capture.store.read_signals()) == 1


def test_capture_atomic_dual_write_under_real_postgres(
    pg_engine: sa.Engine, frozen_config: str
) -> None:
    capture = CorrectionSignalCapture(pg_engine)
    consumer = f"ne065-pg-{uuid.uuid4().hex}"

    async def go() -> str:
        return await capture.capture(_prov(), ThumbsSignal(up=True, target_ref="ans-1"))

    event_id = asyncio.run(go())

    with pg_engine.begin() as conn:
        records, _ = read_batch(conn, consumer, event_types=["correction_signal"])
    assert len(records) == 1
    assert records[0].event_id == event_id
    assert records[0].payload["event_id"] == event_id
