# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FastEmbed BM25 sparse encoder tests (real model; skips if assets unavailable)."""

from __future__ import annotations

import asyncio

from ne.brain.sparse import FastEmbedBM25Encoder
from ne.contracts.types import SparseVector


def test_model_id_default(bm25_encoder: FastEmbedBM25Encoder) -> None:
    assert bm25_encoder.model_id == "Qdrant/bm25"


def test_encode_returns_sparse_vectors(bm25_encoder: FastEmbedBM25Encoder) -> None:
    vectors = asyncio.run(bm25_encoder.encode(["the cat sat on the mat", "a dog ran fast"]))
    assert len(vectors) == 2
    for v in vectors:
        assert isinstance(v, SparseVector)
        assert len(v.indices) == len(v.values)
        assert all(isinstance(i, int) for i in v.indices)
        assert all(isinstance(x, float) for x in v.values)


def test_query_values_are_all_one(bm25_encoder: FastEmbedBM25Encoder) -> None:
    queries = asyncio.run(bm25_encoder.encode_queries(["cat mat"]))
    assert len(queries) == 1
    assert queries[0].values
    assert all(value == 1.0 for value in queries[0].values)


def test_document_values_are_bm25_weighted(bm25_encoder: FastEmbedBM25Encoder) -> None:
    docs = asyncio.run(bm25_encoder.encode(["cat cat cat mat"]))
    assert docs[0].values
    # BM25 document weights are not flat 1.0 like query vectors.
    assert any(value != 1.0 for value in docs[0].values)


def test_shared_token_yields_common_index(bm25_encoder: FastEmbedBM25Encoder) -> None:
    docs = asyncio.run(bm25_encoder.encode(["the cat sat on the mat"]))
    queries = asyncio.run(bm25_encoder.encode_queries(["cat"]))
    doc_indices = set(docs[0].indices)
    query_indices = set(queries[0].indices)
    assert doc_indices & query_indices
