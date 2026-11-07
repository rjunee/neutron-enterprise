# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for ne-connect tests.

A real write stack over ``:memory:`` Qdrant + real sqlite audit/quarantine lanes
(no mock past the seam), plus the ne-brain ``frozen_config`` pattern.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from qdrant_client import AsyncQdrantClient

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.write import (
    KnowledgeWriter,
    OutboxShapeViolationSink,
    ProvenanceClass,
    SqliteWriteQuarantine,
)
from ne.connect import CONNECTOR_RECORD_SHAPE
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.events import ensure_outbox_schema

FIXTURE_SOURCE_PATH = Path(__file__).parent / "fixtures" / "fixture_source.json"


class _DemoSettings(NeSettings):
    demo: int = 7


@pytest.fixture
def frozen_config() -> Iterator[str]:
    """Freeze a deterministic config_hash for the duration of a test."""
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


@dataclass
class WriterStack:
    """A fully-wired KnowledgeWrite stack over REAL components."""

    writer: KnowledgeWriter
    store: QdrantChunkStore
    quarantine: SqliteWriteQuarantine
    sink: PostgresAuditSink
    engine: sa.Engine


@pytest.fixture
def writer_stack(tmp_path: Path) -> WriterStack:
    """Build a :class:`WriterStack` over :memory: Qdrant + real sqlite lanes."""
    client = AsyncQdrantClient(":memory:")
    store = QdrantChunkStore(
        client, f"nc_{uuid.uuid4().hex}", HashEmbedder(32), HashSparseEncoder()
    )
    asyncio.run(store.ensure_collection())
    quarantine = SqliteWriteQuarantine(tmp_path / f"q_{uuid.uuid4().hex}.db")
    engine = sa.create_engine("sqlite://")
    ensure_outbox_schema(engine)
    ensure_audit_schema(engine)
    sink = PostgresAuditSink(engine)
    writer = KnowledgeWriter(
        store,
        quarantine,
        {CONNECTOR_RECORD_SHAPE.name: CONNECTOR_RECORD_SHAPE},
        default_shape="ne.connect.record",
        actor="connector:test",
        default_provenance_class=ProvenanceClass.INGESTED,
        audit_sink=sink,
        violation_sink=OutboxShapeViolationSink(engine),
    )
    return WriterStack(writer=writer, store=store, quarantine=quarantine, sink=sink, engine=engine)
