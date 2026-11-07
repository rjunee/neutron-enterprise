# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Stage-contract tests for the ingestion Protocols and value objects (NE-020)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import ValidationError

from ne.brain.ingest.chunker import StructuralChunker
from ne.brain.ingest.contracts import (
    BlockKind,
    Chunker,
    DocBlock,
    DocumentParser,
    Enricher,
    FileDescriptorDocumentParser,
    ParsedDocument,
)
from ne.brain.ingest.docling_parser import DoclingParser
from ne.brain.ingest.enricher import ContextualEnricher
from ne.contracts.models import Completion, CompletionChunk, CompletionRequest


def _sample_doc() -> ParsedDocument:
    return ParsedDocument(
        document_id="doc-1",
        source_name="doc-1.html",
        source_format="html",
        blocks=(
            DocBlock(kind=BlockKind.HEADING, text="Intro", level=1, page=1),
            DocBlock(kind=BlockKind.PARAGRAPH, text="Hello world.", page=1),
        ),
    )


def test_doc_block_is_frozen_and_hashable() -> None:
    block = DocBlock(kind=BlockKind.PARAGRAPH, text="hi")
    with pytest.raises(ValidationError):
        block.text = "changed"  # type: ignore[misc]
    assert hash(block) is not None


def test_parsed_document_is_frozen_and_hashable() -> None:
    doc = _sample_doc()
    with pytest.raises(ValidationError):
        doc.document_id = "other"  # type: ignore[misc]
    assert hash(doc) is not None


def test_parsed_document_schema_version_is_pinned() -> None:
    doc = _sample_doc()
    assert doc.schema_version == "ne.parsed_document/1"
    with pytest.raises(ValidationError):
        ParsedDocument(
            schema_version="ne.parsed_document/2",  # type: ignore[arg-type]
            document_id="doc-1",
            source_name="doc-1.html",
            source_format="html",
            blocks=(),
        )


def test_parsed_document_json_round_trip() -> None:
    doc = _sample_doc()
    restored = ParsedDocument.model_validate_json(doc.model_dump_json())
    assert restored == doc


def test_doc_block_rejects_bad_kind() -> None:
    with pytest.raises(ValidationError):
        DocBlock(kind="not-a-kind", text="hi")  # type: ignore[arg-type]


def test_doc_block_rejects_bad_level_type() -> None:
    with pytest.raises(ValidationError):
        DocBlock(kind=BlockKind.HEADING, text="hi", level="one")  # type: ignore[arg-type]


def test_structural_chunker_satisfies_chunker_protocol() -> None:
    chunker: Chunker = StructuralChunker()
    assert isinstance(chunker, Chunker)


def test_docling_parser_satisfies_document_parser_protocol() -> None:
    parser: DocumentParser = DoclingParser()
    assert isinstance(parser, DocumentParser)


def test_docling_parser_satisfies_file_descriptor_parser_protocol() -> None:
    parser: FileDescriptorDocumentParser = DoclingParser()
    assert isinstance(parser, FileDescriptorDocumentParser)


def test_contextual_enricher_satisfies_enricher_protocol() -> None:
    class _NullGateway:
        async def complete(self, request: CompletionRequest) -> Completion:  # pragma: no cover
            raise NotImplementedError

        def stream(  # pragma: no cover
            self, request: CompletionRequest
        ) -> AsyncIterator[CompletionChunk]:
            raise NotImplementedError

    enricher: Enricher = ContextualEnricher(_NullGateway())
    assert isinstance(enricher, Enricher)
