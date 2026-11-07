# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Brain T1: ingest, retrieval, and knowledge-write API.

The Qdrant store adapter (dense+sparse hybrid ``ChunkStore``, TEI embedder,
FastEmbed BM25 sparse encoder, and the ``HybridRetriever``) ships with
NE-021. The production T1 retrieval pipeline (``RetrievalPipeline`` composing
hybrid retrieve -> TEI rerank -> citations, plus the ``TEIReranker`` adapter)
ships with NE-022. The ingest stages (Docling parse, structure-aware
chunking, optional contextual enrichment) ship with NE-020. The governed
KnowledgeWrite surface (upsert/curate/quarantine with provenance stamps,
JSON-Schema shape validation, an inspectable quarantine lane, and
ShapeViolation/AuditEvent emission) ships with NE-023.

Query-time C7 security trimming (``GroupExpander`` seam + ``PrincipalAclFilter``
fail-closed hook + real ``QdrantChunkStore.permission_filter``) ships with NE-043.

NE-CKC2 C2 hardening: TEIEmbedder/TEIReranker validate their config-supplied
endpoint at construction (``validate_in_vpc_endpoint``, SSRF + in-VPC/allowlist,
fail-closed via ``UnsafeEndpointError``), and the ingest paths fail closed under
AIR_GAP (``air_gap_engaged`` / ``AirGapViolationError``; parser-env failures
surface as the retryable ``ParserEnvironmentError``).
"""

from ne.brain.current_source_authority import (
    CONTINUATION_CODEC_VERSION,
    SOURCE_AUTHORITY_SCHEMA_VERSION,
    ContinuationUnavailableError,
    ForeignConstructionIdentityError,
    HmacAuthorizedContinuationTokenCodec,
    SourceAuthorityClaimConflictError,
    SourceAuthorityError,
    SourceAuthorityFenceError,
    SourceReconciliationScopeBusyError,
    SqlAlchemyAuthorizedContinuationStore,
    SqlAlchemyCurrentSourceAuthorityReader,
    SqlAlchemyCurrentSourceAuthorityWriter,
    bind_deployment_construction_id,
    claim_or_load_deployment_construction_id,
    ensure_source_authority_schema,
)
from ne.brain.embedder import TEIEmbedder
from ne.brain.evidence import VectorEvidenceDescriptor, VectorEvidenceSource
from ne.brain.ingest.chunker import StructuralChunker
from ne.brain.ingest.contracts import (
    BlockKind,
    DocBlock,
    ParsedDocument,
    ParserConversionError,
    ParserEnvironmentError,
    ParserOutputError,
    ParserResidueError,
)
from ne.brain.ingest.docling_parser import DoclingParser
from ne.brain.ingest.enricher import ContextualEnricher
from ne.brain.ingest.ledger import DocStage, IngestLedger, SqliteIngestLedger
from ne.brain.ingest.spine import (
    IngestionPipeline,
    IngestOutcome,
    IngestSource,
    IngestStatus,
)
from ne.brain.lineage import (
    DocumentArtifactKind,
    DocumentEvidenceState,
    DocumentJourney,
    DocumentLineageError,
    DocumentStage,
    DocumentStageArtifact,
    DocumentStageEvent,
    DocumentSummary,
    DocumentVersion,
    PreparedDocumentVersion,
    SqlAlchemyDocumentLineageStore,
    ensure_document_lineage_schema,
)
from ne.brain.pipeline import RetrievalPipeline
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.reranker import RerankResponseError, TEIReranker, VllmReranker
from ne.brain.retriever import HybridRetriever
from ne.brain.sovereignty import (
    AirGapViolationError,
    UnsafeEndpointError,
    air_gap_engaged,
    validate_in_vpc_endpoint,
)
from ne.brain.sparse import FastEmbedBM25Encoder
from ne.brain.trimming import GroupExpander, PrincipalAclFilter
from ne.brain.write import (
    AUTHORITY_ARTIFACT_PAYLOAD_KEY,
    CuratedKnowledgeItem,
    ImmutableArtifactContentMismatchError,
    ImmutableArtifactError,
    ImmutableArtifactVectorSeam,
    ImmutableKnowledgeArtifactStore,
    InvalidPatchError,
    KnowledgeItemNotFoundError,
    KnowledgeWriteError,
    KnowledgeWriter,
    OutboxShapeViolationSink,
    ProvenanceClass,
    ProvenanceStamp,
    QuarantinedWrite,
    ShapeContract,
    ShapeValidationError,
    ShapeViolationSink,
    SqliteWriteQuarantine,
    UnknownShapeError,
    WriteQuarantine,
    ensure_immutable_artifact_schema,
    provenance_from_chunk,
    stamped_metadata,
)

__version__ = "0.1.0"

__all__ = [
    "AUTHORITY_ARTIFACT_PAYLOAD_KEY",
    "CONTINUATION_CODEC_VERSION",
    "SOURCE_AUTHORITY_SCHEMA_VERSION",
    "AirGapViolationError",
    "BlockKind",
    "ContextualEnricher",
    "ContinuationUnavailableError",
    "CuratedKnowledgeItem",
    "DocBlock",
    "DocStage",
    "DoclingParser",
    "DocumentArtifactKind",
    "DocumentEvidenceState",
    "DocumentJourney",
    "DocumentLineageError",
    "DocumentStage",
    "DocumentStageArtifact",
    "DocumentStageEvent",
    "DocumentSummary",
    "DocumentVersion",
    "FastEmbedBM25Encoder",
    "ForeignConstructionIdentityError",
    "GroupExpander",
    "HmacAuthorizedContinuationTokenCodec",
    "HybridRetriever",
    "ImmutableArtifactContentMismatchError",
    "ImmutableArtifactError",
    "ImmutableArtifactVectorSeam",
    "ImmutableKnowledgeArtifactStore",
    "IngestLedger",
    "IngestOutcome",
    "IngestSource",
    "IngestStatus",
    "IngestionPipeline",
    "InvalidPatchError",
    "KnowledgeItemNotFoundError",
    "KnowledgeWriteError",
    "KnowledgeWriter",
    "OutboxShapeViolationSink",
    "ParsedDocument",
    "ParserConversionError",
    "ParserEnvironmentError",
    "ParserOutputError",
    "ParserResidueError",
    "PreparedDocumentVersion",
    "PrincipalAclFilter",
    "ProvenanceClass",
    "ProvenanceStamp",
    "QdrantChunkStore",
    "QuarantinedWrite",
    "RerankResponseError",
    "RetrievalPipeline",
    "ShapeContract",
    "ShapeValidationError",
    "ShapeViolationSink",
    "SourceAuthorityClaimConflictError",
    "SourceAuthorityError",
    "SourceAuthorityFenceError",
    "SourceReconciliationScopeBusyError",
    "SqlAlchemyAuthorizedContinuationStore",
    "SqlAlchemyCurrentSourceAuthorityReader",
    "SqlAlchemyCurrentSourceAuthorityWriter",
    "SqlAlchemyDocumentLineageStore",
    "SqliteIngestLedger",
    "SqliteWriteQuarantine",
    "StructuralChunker",
    "TEIEmbedder",
    "TEIReranker",
    "UnknownShapeError",
    "UnsafeEndpointError",
    "VectorEvidenceDescriptor",
    "VectorEvidenceSource",
    "VllmReranker",
    "WriteQuarantine",
    "air_gap_engaged",
    "bind_deployment_construction_id",
    "claim_or_load_deployment_construction_id",
    "ensure_document_lineage_schema",
    "ensure_immutable_artifact_schema",
    "ensure_source_authority_schema",
    "provenance_from_chunk",
    "stamped_metadata",
    "validate_in_vpc_endpoint",
]
