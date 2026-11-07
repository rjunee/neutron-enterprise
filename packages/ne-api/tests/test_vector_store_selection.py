# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ALWAYS-ON: ``_default_chunk_store`` vector-store selection (NE-091).

* default settings (``vector_store='qdrant'``) construct ``QdrantChunkStore`` on
  the BYTE-IDENTICAL path (client is lazy — no network);
* ``vector_store='pgvector'`` with an empty DSN fails LOUD at boot;
* ``vector_store='pgvector'`` resolves the alternate via the real
  ``PluginRegistry`` seam and constructs it with
  ``(dsn, table, embedder, sparse_encoder, group_expander=expander)``.
"""

from __future__ import annotations

from typing import Any

import pytest

from ne.api.platform import _default_chunk_store
from ne.api.settings import ApiSettings
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.contracts.plugins import PluginRegistry


def _settings(**overrides: Any) -> ApiSettings:
    base: dict[str, Any] = {
        "qdrant_url": "http://qdrant.test",
        "qdrant_collection": "t",
        "tei_url": "http://tei.test",
    }
    base.update(overrides)
    return ApiSettings(**base)


class _RecordingFakeStore:
    def __init__(
        self,
        dsn: str,
        table: str,
        embedder: Any,
        sparse_encoder: Any,
        *,
        group_expander: Any = None,
    ) -> None:
        self.dsn = dsn
        self.table = table
        self.embedder = embedder
        self.sparse_encoder = sparse_encoder
        self.group_expander = group_expander


class _Expander:
    def expand(self, principal: Any) -> frozenset[str]:
        return frozenset()


def test_default_is_qdrant_chunk_store() -> None:
    embedder = HashEmbedder(32)
    sparse = HashSparseEncoder()
    expander = _Expander()
    store = _default_chunk_store(_settings(), embedder, sparse, expander, PluginRegistry())
    assert isinstance(store, QdrantChunkStore)


def test_pgvector_without_dsn_fails_loud() -> None:
    with pytest.raises(ValueError, match="requires NE_API_PGVECTOR_DSN"):
        _default_chunk_store(
            _settings(vector_store="pgvector"),
            HashEmbedder(32),
            HashSparseEncoder(),
            _Expander(),
            PluginRegistry(),
        )


def test_pgvector_resolves_via_registry_and_constructs_with_expected_args() -> None:
    embedder = HashEmbedder(32)
    sparse = HashSparseEncoder()
    expander = _Expander()
    registry = PluginRegistry()
    registry.register("vector_store", "pgvector", _RecordingFakeStore)
    store = _default_chunk_store(
        _settings(
            vector_store="pgvector",
            pgvector_dsn="postgresql://x/y",
            pgvector_table="custom_chunks",
        ),
        embedder,
        sparse,
        expander,
        registry,
    )
    assert isinstance(store, _RecordingFakeStore)
    assert store.dsn == "postgresql://x/y"
    assert store.table == "custom_chunks"
    assert store.embedder is embedder
    assert store.sparse_encoder is sparse
    assert store.group_expander is expander


def test_pgvector_real_class_resolves_from_entry_point() -> None:
    # Without a programmatic registration the real ne.plugins.vector_store entry
    # point must resolve to the real PgvectorChunkStore.
    from ne.brain.pgvector.store import PgvectorChunkStore

    store = _default_chunk_store(
        _settings(vector_store="pgvector", pgvector_dsn="postgresql://x/y"),
        HashEmbedder(32),
        HashSparseEncoder(),
        _Expander(),
        PluginRegistry(),
    )
    assert isinstance(store, PgvectorChunkStore)
