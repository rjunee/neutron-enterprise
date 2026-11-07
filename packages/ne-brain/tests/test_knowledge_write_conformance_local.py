# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""KnowledgeWriteContractKit against qdrant-client local (in-process) mode.

Always runs in CI — vendor in-process ("local mode") Qdrant, not a real server,
with the real SQLite quarantine lane (no mock past the seam). The real-server
run is test_knowledge_write_conformance_server.py.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping

import pytest
import sqlalchemy as sa
from qdrant_client import AsyncQdrantClient

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.testing.kits.knowledge_write import KnowledgeWriteContractKit
from ne.brain.write import (
    KnowledgeWriter,
    OutboxShapeViolationSink,
    ShapeContract,
    SqliteWriteQuarantine,
)
from ne.contracts.knowledge import ChunkStore, KnowledgeWrite
from ne.events import ensure_outbox_schema


def _make_writer(
    contracts: Mapping[str, ShapeContract],
    *,
    default_shape: str,
    store: ChunkStore | None = None,
) -> tuple[KnowledgeWrite, ChunkStore]:
    """Wire the kit's writer over ``store``, defaulting to in-process local mode.

    ``store`` is an INJECTED parameter rather than a ``url`` this function knows
    how to dial: the server-mode subclass owns its collection through
    ``owned_qdrant_collections`` and hands the built store in, so nothing here can
    address a Qdrant that nobody is going to clean up.
    """
    if store is None:
        store = QdrantChunkStore(
            AsyncQdrantClient(":memory:"),
            f"kw_{uuid.uuid4().hex}",
            HashEmbedder(32),
            HashSparseEncoder(),
        )
        asyncio.run(store.ensure_collection())
    engine = sa.create_engine("sqlite://")
    ensure_outbox_schema(engine)
    ensure_audit_schema(engine)
    writer = KnowledgeWriter(
        store,
        SqliteWriteQuarantine(":memory:"),
        contracts,
        default_shape=default_shape,
        actor="kit",
        audit_sink=PostgresAuditSink(engine),
        violation_sink=OutboxShapeViolationSink(engine),
    )
    # Static (pyright-checked) proof KnowledgeWriter satisfies the frozen Protocol.
    conforms: KnowledgeWrite = writer
    return conforms, store


@pytest.mark.usefixtures("frozen_config")
class TestKnowledgeWriteLocalMode(KnowledgeWriteContractKit):
    def create_writer(
        self, contracts: Mapping[str, ShapeContract], *, default_shape: str
    ) -> tuple[KnowledgeWrite, ChunkStore]:
        return _make_writer(contracts, default_shape=default_shape)
