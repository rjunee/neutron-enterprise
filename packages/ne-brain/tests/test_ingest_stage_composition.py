# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end stage composition (NE-020) WITHOUT the docling runtime.

Proves the three stage contracts compose exactly as NE-026 will wire them:
fixture JSON -> parse contract -> chunk -> enrich -> frozen Chunk tuple.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from ne.brain.ingest.chunker import StructuralChunker
from ne.brain.ingest.docling_parser import parsed_document_from_json
from ne.brain.ingest.enricher import ContextualEnricher
from ne.contracts.models import Completion, CompletionChunk, CompletionRequest, Usage
from ne.contracts.types import Chunk

_FIXTURE = Path(__file__).parent / "fixtures" / "ingest" / "sample_report.parsed.json"


class _StubGateway:
    async def complete(self, request: CompletionRequest) -> Completion:
        return Completion(
            text="Context for testing.",
            model_id="stub-model",
            usage=Usage(prompt_tokens=1, completion_tokens=1),
        )

    def stream(  # pragma: no cover - unused
        self, request: CompletionRequest
    ) -> AsyncIterator[CompletionChunk]:
        raise NotImplementedError


def test_parse_chunk_enrich_compose_without_docling_runtime() -> None:
    doc = parsed_document_from_json(_FIXTURE.read_bytes())

    chunks = StructuralChunker().chunk(doc, acl=("grp:eng",))
    assert chunks
    for c in chunks:
        assert isinstance(c, Chunk)

    enricher = ContextualEnricher(_StubGateway(), enabled=True)
    enriched = asyncio.run(enricher.enrich(doc, chunks))

    assert len(enriched) == len(chunks)
    for original, result in zip(chunks, enriched, strict=True):
        assert isinstance(result, Chunk)
        assert result.chunk_id == original.chunk_id
        assert result.document_id == original.document_id
        assert result.acl == ("grp:eng",)
        assert result.text.startswith("Context for testing.\n\n")
        assert result.metadata["enriched"] is True


def test_stage_composition_disabled_enrichment_is_identity() -> None:
    doc = parsed_document_from_json(_FIXTURE.read_bytes())
    chunks = StructuralChunker().chunk(doc)

    enricher = ContextualEnricher(_StubGateway())  # enabled=False
    result = asyncio.run(enricher.enrich(doc, chunks))

    assert result == chunks
