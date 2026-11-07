# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Contextual enrichment stage tests (NE-020): optional, off by default, C3-clean."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from ne.brain.ingest.contracts import BlockKind, DocBlock, ParsedDocument
from ne.brain.ingest.enricher import ContextualEnricher
from ne.contracts.models import Completion as CompletionModel
from ne.contracts.models import CompletionChunk, CompletionRequest, Usage
from ne.contracts.types import Chunk


class _StubGateway:
    """Records every request; returns a fixed completion."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.requests: list[CompletionRequest] = []
        self._delay = delay
        self.in_flight = 0
        self.peak_in_flight = 0

    async def complete(self, request: CompletionRequest) -> CompletionModel:
        self.requests.append(request)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        if self._delay:
            await asyncio.sleep(self._delay)
        self.in_flight -= 1
        return CompletionModel(
            text="Context for testing.",
            model_id="stub-model",
            usage=Usage(prompt_tokens=1, completion_tokens=1),
        )

    def stream(  # pragma: no cover - unused
        self, request: CompletionRequest
    ) -> AsyncIterator[CompletionChunk]:
        raise NotImplementedError


def _doc() -> ParsedDocument:
    return ParsedDocument(
        document_id="doc-1",
        source_name="doc-1.md",
        source_format="md",
        blocks=(
            DocBlock(kind=BlockKind.HEADING, text="Intro", level=1),
            DocBlock(kind=BlockKind.PARAGRAPH, text="First paragraph of the document."),
            DocBlock(kind=BlockKind.PARAGRAPH, text="Second paragraph of the document."),
        ),
    )


def _chunks() -> tuple[Chunk, ...]:
    return (
        Chunk(chunk_id="doc-1:0000", document_id="doc-1", text="chunk one text", acl=("grp:a",)),
        Chunk(chunk_id="doc-1:0001", document_id="doc-1", text="chunk two text", acl=("grp:a",)),
        Chunk(chunk_id="doc-1:0002", document_id="doc-1", text="chunk three text", acl=("grp:a",)),
    )


def test_disabled_by_default_returns_identity_and_never_touches_gateway() -> None:
    gateway = _StubGateway()
    enricher = ContextualEnricher(gateway)  # enabled defaults to False
    chunks = _chunks()

    result = asyncio.run(enricher.enrich(_doc(), chunks))

    assert result == chunks
    assert all(a is b for a, b in zip(result, chunks, strict=True))
    assert gateway.requests == []


def test_disabled_explicit_returns_identity() -> None:
    gateway = _StubGateway()
    enricher = ContextualEnricher(gateway, enabled=False)
    chunks = _chunks()

    result = asyncio.run(enricher.enrich(_doc(), chunks))

    assert result == chunks
    assert gateway.requests == []


def test_enabled_issues_one_request_per_chunk_with_expected_task_and_context() -> None:
    gateway = _StubGateway()
    enricher = ContextualEnricher(gateway, enabled=True)
    doc = _doc()
    chunks = _chunks()

    result = asyncio.run(enricher.enrich(doc, chunks))

    assert len(gateway.requests) == len(chunks)
    for request in gateway.requests:
        assert request.task == "ingest.enrich"
        assert len(request.messages) == 2
        system, user = request.messages
        assert system.role == "system"
        assert user.role == "user"
        assert "First paragraph of the document." in user.content
        assert "Second paragraph of the document." in user.content

    assert len(result) == len(chunks)
    for original, enriched in zip(chunks, result, strict=True):
        assert enriched.chunk_id == original.chunk_id
        assert enriched.document_id == original.document_id
        assert enriched.acl == original.acl
        assert enriched.text == f"Context for testing.\n\n{original.text}"
        assert enriched.metadata["enriched"] is True
        assert enriched.metadata["enrichment_model"] == "stub-model"
        assert enriched.metadata["enrichment_context"] == "Context for testing."


def test_enabled_preserves_input_order() -> None:
    gateway = _StubGateway()
    enricher = ContextualEnricher(gateway, enabled=True)
    chunks = _chunks()

    result = asyncio.run(enricher.enrich(_doc(), chunks))

    assert [c.chunk_id for c in result] == [c.chunk_id for c in chunks]


def test_max_concurrency_bounds_in_flight_requests() -> None:
    gateway = _StubGateway(delay=0.02)
    enricher = ContextualEnricher(gateway, enabled=True, max_concurrency=2)
    chunks = tuple(
        Chunk(chunk_id=f"doc-1:{i:04d}", document_id="doc-1", text=f"chunk {i}") for i in range(6)
    )

    asyncio.run(enricher.enrich(_doc(), chunks))

    assert gateway.peak_in_flight <= 2


def test_gateway_exception_propagates() -> None:
    class _FailingGateway:
        async def complete(self, request: CompletionRequest) -> CompletionModel:
            raise RuntimeError("gateway boom")

        def stream(  # pragma: no cover - unused
            self, request: CompletionRequest
        ) -> AsyncIterator[CompletionChunk]:
            raise NotImplementedError

    enricher = ContextualEnricher(_FailingGateway(), enabled=True)

    with pytest.raises(RuntimeError, match="gateway boom"):
        asyncio.run(enricher.enrich(_doc(), _chunks()))


def test_no_provider_sdk_or_gateway_import() -> None:
    import ast
    from pathlib import Path

    src_dir = Path(__file__).parent.parent / "src" / "ne" / "brain"
    banned = ("litellm", "openai", "anthropic", "ne.gateway")
    for path in src_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not any(name == b or name.startswith(f"{b}.") for b in banned), (
                    f"{path}: banned import {name!r}"
                )
