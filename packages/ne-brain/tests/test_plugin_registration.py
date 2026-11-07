# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end proof that ne-brain's adapters resolve via REAL entry points.

Mirrors packages/ne-contracts/tests/test_secret_store_conformance.py's
registry-resolution pattern.
"""

from __future__ import annotations

from ne.contracts import PluginRegistry


def test_resolve_vector_store_qdrant() -> None:
    from ne.brain.qdrant import QdrantChunkStore

    resolved = PluginRegistry().resolve("vector_store", "qdrant")
    assert resolved is QdrantChunkStore


def test_resolve_vector_store_pgvector() -> None:
    from ne.brain.pgvector.store import PgvectorChunkStore

    resolved = PluginRegistry().resolve("vector_store", "pgvector")
    assert resolved is PgvectorChunkStore


def test_resolve_sparse_encoder_fastembed_bm25() -> None:
    from ne.brain.sparse import FastEmbedBM25Encoder

    resolved = PluginRegistry().resolve("sparse_encoder", "fastembed-bm25")
    assert resolved is FastEmbedBM25Encoder


def test_resolve_embedder_tei() -> None:
    from ne.brain.embedder import TEIEmbedder

    resolved = PluginRegistry().resolve("embedder", "tei")
    assert resolved is TEIEmbedder


def test_resolve_reranker_tei() -> None:
    from ne.brain.reranker import TEIReranker

    resolved = PluginRegistry().resolve("reranker", "tei")
    assert resolved is TEIReranker


def test_resolve_document_parser_docling() -> None:
    from ne.brain.ingest.docling_parser import DoclingParser

    resolved = PluginRegistry().resolve("document_parser", "docling")
    assert resolved is DoclingParser


def test_resolve_chunker_structural() -> None:
    from ne.brain.ingest.chunker import StructuralChunker

    resolved = PluginRegistry().resolve("chunker", "structural")
    assert resolved is StructuralChunker


def test_resolve_enricher_contextual() -> None:
    from ne.brain.ingest.enricher import ContextualEnricher

    resolved = PluginRegistry().resolve("enricher", "contextual")
    assert resolved is ContextualEnricher
