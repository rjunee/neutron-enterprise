# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Ingestion stages (NE-020) plus the NE-026 pipeline spine.

The three NE-020 stages (PARSE -> CHUNK -> optional ENRICH) compose into
NE-026's idempotent/resumable/doc-versioned :class:`IngestionPipeline`, backed
by the durable :class:`IngestLedger` seam (a local ``sqlite3`` implementation
ships now; a Postgres ledger swaps in at the same seam later). The spine is a
package-local orchestrator, not a frozen-contract slot adapter, so it has no
plugin entry-point.
"""

from __future__ import annotations

from ne.brain.ingest.chunker import StructuralChunker
from ne.brain.ingest.contracts import (
    BlockKind,
    Chunker,
    DocBlock,
    DocumentParser,
    Enricher,
    ParsedDocument,
)
from ne.brain.ingest.docling_parser import DoclingParser, parsed_document_from_json
from ne.brain.ingest.enricher import ContextualEnricher
from ne.brain.ingest.ledger import (
    DocStage,
    IngestLedger,
    LedgerState,
    QuarantineRecord,
    SqliteIngestLedger,
)
from ne.brain.ingest.spine import (
    IngestionPipeline,
    IngestOutcome,
    IngestSource,
    IngestStatus,
    PayloadUpdatable,
    content_hash_for,
)

__all__ = [
    "BlockKind",
    "Chunker",
    "ContextualEnricher",
    "DocBlock",
    "DocStage",
    "DoclingParser",
    "DocumentParser",
    "Enricher",
    "IngestLedger",
    "IngestOutcome",
    "IngestSource",
    "IngestStatus",
    "IngestionPipeline",
    "LedgerState",
    "ParsedDocument",
    "PayloadUpdatable",
    "QuarantineRecord",
    "SqliteIngestLedger",
    "StructuralChunker",
    "content_hash_for",
    "parsed_document_from_json",
]
