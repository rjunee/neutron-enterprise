# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""KnowledgeWriteContractKit against a REAL Qdrant server (dev-stack v1.18.2).

THIS is the actual conformance claim against real Qdrant; local mode
(test_knowledge_write_conformance_local.py) is supplementary always-on coverage.

Live-parked unless the operator NAMES a Qdrant with ``NE_QDRANT_URL``. There is
no default: on a box running the demo stack the default WAS the deployed store,
and this module wrote 985 orphan collections into it before 2026-09-10. The
collections it makes now are dropped in fixture teardown
(``owned_qdrant_collections``), pass, fail or error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from ne.brain.qdrant import QdrantChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.testing.kits.knowledge_write import KnowledgeWriteContractKit
from ne.brain.testing.qdrant_live import (
    OwnedQdrantCollections,
    qdrant_server_url,
    server_reachable,
)
from ne.brain.write import ShapeContract
from ne.contracts.knowledge import ChunkStore, KnowledgeWrite

from .test_knowledge_write_conformance_local import _make_writer

_URL = qdrant_server_url()

if not server_reachable(_URL):
    pytest.skip(
        "LIVE-PARKED: real-Qdrant KnowledgeWrite conformance needs a Qdrant YOU own — "
        "name it in NE_QDRANT_URL (never the deployed stack's; it is not a scratch store)",
        allow_module_level=True,
    )


@pytest.mark.usefixtures("frozen_config")
class TestKnowledgeWriteServerMode(KnowledgeWriteContractKit):
    @pytest.fixture(autouse=True)
    def _live_store(self, owned_qdrant_collections: OwnedQdrantCollections) -> None:
        self._owned = owned_qdrant_collections

    def create_writer(
        self, contracts: Mapping[str, ShapeContract], *, default_shape: str
    ) -> tuple[KnowledgeWrite, ChunkStore]:
        store = QdrantChunkStore(
            self._owned.client(),
            self._owned.collection("kw"),
            HashEmbedder(32),
            HashSparseEncoder(),
        )
        asyncio.run(store.ensure_collection())
        return _make_writer(contracts, default_shape=default_shape, store=store)
