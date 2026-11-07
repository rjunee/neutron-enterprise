# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the deterministic HashEmbedder / HashSparseEncoder test doubles."""

from __future__ import annotations

import asyncio
import math

from ne.brain.retriever import QuerySparseEncoder
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.contracts.knowledge import SparseEncoder


def test_determinism_same_text_same_vector() -> None:
    embedder = HashEmbedder(dimension=16)
    v1 = asyncio.run(embedder.embed(["hello world"]))[0]
    v2 = asyncio.run(embedder.embed(["hello world"]))[0]
    assert v1 == v2


def test_dimension_matches_ctor() -> None:
    embedder = HashEmbedder(dimension=24)
    vec = asyncio.run(embedder.embed(["some text"]))[0]
    assert len(vec) == 24
    assert embedder.dimension == 24


def test_vectors_are_unit_norm() -> None:
    embedder = HashEmbedder(dimension=32)
    vec = asyncio.run(embedder.embed(["norm check text"]))[0]
    norm = math.sqrt(sum(v * v for v in vec))
    assert math.isclose(norm, 1.0, rel_tol=1e-6, abs_tol=1e-9)


def test_distinct_texts_produce_distinct_vectors() -> None:
    embedder = HashEmbedder(dimension=32)
    v1, v2 = asyncio.run(embedder.embed(["text alpha", "text beta"]))
    assert v1 != v2


def test_model_id_is_stable() -> None:
    embedder = HashEmbedder()
    assert embedder.model_id == "ne-test-hash-v1"


def test_hash_sparse_encoder_deterministic() -> None:
    encoder = HashSparseEncoder()
    v1 = asyncio.run(encoder.encode(["alpha beta gamma"]))[0]
    v2 = asyncio.run(encoder.encode(["alpha beta gamma"]))[0]
    # Same text -> identical SparseVector.
    assert v1 == v2
    # Distinct tokens -> distinct indices; parallel index/value arrays.
    assert len(v1.indices) == len(v1.values) == 3
    assert len(set(v1.indices)) == 3
    # encode_queries mirrors encode (query/ingest symmetry for the test double).
    q = asyncio.run(encoder.encode_queries(["alpha beta gamma"]))[0]
    assert q == v1
    # Term-count weighting: a repeated token accumulates its count.
    repeated = asyncio.run(encoder.encode(["alpha alpha beta"]))[0]
    assert 2.0 in repeated.values
    assert encoder.model_id == "ne-test-hash-sparse-v1"


def _needs_sparse_encoder(encoder: SparseEncoder) -> str:
    return encoder.model_id


def _needs_query_sparse_encoder(encoder: QuerySparseEncoder) -> str:
    return encoder.model_id


def test_hash_sparse_encoder_satisfies_both_protocols() -> None:
    encoder = HashSparseEncoder()
    # Structurally satisfies the frozen SparseEncoder AND the query-side protocol
    # (verified statically by pyright via the typed parameters above; exercised
    # at runtime here). Neither Protocol is @runtime_checkable, so this is a
    # structural, not isinstance, conformance check.
    assert _needs_sparse_encoder(encoder) == "ne-test-hash-sparse-v1"
    assert _needs_query_sparse_encoder(encoder) == "ne-test-hash-sparse-v1"
