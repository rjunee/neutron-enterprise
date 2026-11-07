# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Ingestion stage contracts (NE-020).

Three stages compose a document into retrievable chunks:

    PARSE (DocumentParser)  -> ParsedDocument
    CHUNK (Chunker)         -> tuple[Chunk, ...]           (frozen ne-contracts Chunk)
    ENRICH (Enricher, opt.) -> tuple[Chunk, ...]           (contextual, off by default)

``ParsedDocument``/``DocBlock`` are package-local stage contracts (NOT a
ne-contracts addition — ne-contracts stays FROZEN); ``Chunk`` is imported as-is
from :mod:`ne.contracts.types` and never redefined. NE-026 orchestrates these
three Protocols into an idempotent/resumable pipeline spine; this module only
defines the clean seams they compose over.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ne.contracts.frozen import AnyMap, FrozenDict
from ne.contracts.types import Chunk


class ParserEnvironmentError(RuntimeError):
    """Infrastructure-caused parser failure (isolated-env provisioning/locking/
    network), NOT a property of the document.

    The ingestion spine treats this as FAILED/retryable — never QUARANTINE — so a
    transient infra outage (a failed ``uv pip install``, a lock timeout, an
    AIR_GAP-blocked cold env) cannot become a permanent silent index gap. A
    genuine document-caused parse failure (the parse subprocess rejecting the
    file) remains a plain exception the spine quarantines.
    """


class ParserResidueError(ParserEnvironmentError):
    """Security-critical parser cleanup left temporary document bytes behind."""


class ParserConversionError(RuntimeError):
    """Document bytes were rejected while the parser performed conversion."""


class ParserOutputError(RuntimeError):
    """Parser output violated the admitted :class:`ParsedDocument` contract."""


class _Frozen(BaseModel):
    """Base for immutable stage-contract value objects."""

    model_config = ConfigDict(frozen=True)


class BlockKind(StrEnum):
    """The structural kind of one parsed document block."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    LIST_ITEM = "list_item"
    CODE = "code"
    CAPTION = "caption"
    OTHER = "other"


class DocBlock(_Frozen):
    """One structural unit of a parsed document, in document order."""

    kind: BlockKind
    text: str
    level: int | None = None
    """Heading level (1..n). ``None`` for non-heading blocks."""
    page: int | None = None
    """1-based page number from parser provenance. ``None`` when the source
    format has no pages (e.g. HTML)."""


class ParsedDocument(_Frozen):
    """The output of the PARSE stage: a parser-agnostic structured document."""

    schema_version: Literal["ne.parsed_document/1"] = "ne.parsed_document/1"
    document_id: str
    source_name: str
    source_format: str
    """One of ``'pdf' | 'docx' | 'pptx' | 'html' | 'md'``."""
    blocks: tuple[DocBlock, ...]
    metadata: AnyMap = FrozenDict()


@runtime_checkable
class DocumentParser(Protocol):
    """PARSE stage: a source document -> a :class:`ParsedDocument`."""

    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument: ...


@runtime_checkable
class FileDescriptorDocumentParser(Protocol):
    """PARSE stage over an already-open regular file without a pathname reopen."""

    async def parse_fd(
        self,
        file_fd: int,
        *,
        document_id: str,
        source_name: str,
    ) -> ParsedDocument: ...


@runtime_checkable
class Chunker(Protocol):
    """CHUNK stage: a :class:`ParsedDocument` -> structure-aware frozen chunks."""

    def chunk(self, doc: ParsedDocument, *, acl: Sequence[str] = ()) -> tuple[Chunk, ...]: ...


@runtime_checkable
class Enricher(Protocol):
    """ENRICH stage (optional): contextual enrichment of already-chunked text."""

    async def enrich(self, doc: ParsedDocument, chunks: Sequence[Chunk]) -> tuple[Chunk, ...]: ...
