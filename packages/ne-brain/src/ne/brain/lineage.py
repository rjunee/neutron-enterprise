# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Recoverable document-version lineage for governed connector writes.

The vector index, runtime database, and audit chain can use different stores.  A
version is therefore prepared in the runtime database *before* the vector write
and finalized only after ``knowledge.upsert`` is durable.  A crash in between
leaves one resumable ``indexing`` row; retrying the same content hash reuses it
instead of minting a phantom version.

Historical content and ACLs never cross a public seam from this module.  The API
must filter each version with the caller's expanded ACL labels before projecting
the C7-clean wire view.
"""

from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa

from ne.brain.evidence import VectorEvidenceDescriptor
from ne.brain.ingest.chunker import StructuralChunker
from ne.brain.ingest.contracts import BlockKind, DocBlock, ParsedDocument
from ne.brain.parsed_evidence import ParsedDocumentEvidenceDescriptor, carried_record_payload
from ne.contracts.source_authority import CurrentSourceAuthorityReceipt
from ne.contracts.types import Chunk

__all__ = [
    "MAX_DOCUMENT_PAGE",
    "DocumentArtifactKind",
    "DocumentComparison",
    "DocumentComparisonState",
    "DocumentCursorError",
    "DocumentEvidenceState",
    "DocumentJourney",
    "DocumentLineageError",
    "DocumentListPage",
    "DocumentStage",
    "DocumentStageArtifact",
    "DocumentStageEvent",
    "DocumentStageEvidence",
    "DocumentStageFact",
    "DocumentSummary",
    "DocumentType",
    "DocumentVersion",
    "GraphEvidenceBatch",
    "GraphEvidenceBindResult",
    "GraphEvidenceEntity",
    "GraphEvidenceRelationship",
    "GraphEvidenceView",
    "PreparedDocumentVersion",
    "RetainedSourceAuthority",
    "SqlAlchemyDocumentLineageStore",
    "ensure_document_lineage_schema",
]


class DocumentLineageError(RuntimeError):
    """A governed write was audited but its prepared lineage did not finalize."""


class DocumentCursorError(ValueError):
    """A document-page cursor is malformed or belongs to different filters."""


class DocumentType(StrEnum):
    PDF = "pdf"
    DOCUMENT = "document"
    PRESENTATION = "presentation"
    SPREADSHEET = "spreadsheet"
    TEXT = "text"
    OTHER = "other"


class DocumentStage(StrEnum):
    PARSE = "parse"
    CHUNK = "chunk"
    ACL = "acl"
    EMBED = "embed"
    GRAPH = "graph"
    SUPERSEDE = "supersede"


_STAGE_ORDER = tuple(DocumentStage)
_STATUS_INDEXING = "indexing"
_STATUS_CURRENT = "current"
_STATUS_SUPERSEDED = "superseded"
MAX_DOCUMENT_PAGE = 200
_CURSOR_VERSION = 1
_POSTGRES_SCHEMA_LOCK_KEY = 0x4E45_444F_4345

_DOCUMENT_SUFFIXES = frozenset({"doc", "docx", "odt", "rtf"})
_PRESENTATION_SUFFIXES = frozenset({"odp", "ppt", "pptx"})
_SPREADSHEET_SUFFIXES = frozenset({"csv", "ods", "tsv", "xls", "xlsx"})
_TEXT_SUFFIXES = frozenset({"htm", "html", "markdown", "md", "txt"})

_metadata = sa.MetaData()

_versions = sa.Table(
    "ne_document_versions",
    _metadata,
    sa.Column("document_id", sa.String, primary_key=True),
    sa.Column("version_seq", sa.Integer, primary_key=True),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("prev_content_hash", sa.String(64), nullable=True),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("display_name", sa.String, nullable=False),
    sa.Column("source_system", sa.String, nullable=False),
    sa.Column("source_ref", sa.String, nullable=False),
    sa.Column("acl", sa.JSON, nullable=False),
    sa.Column("chunk_count", sa.Integer, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("ingested_event_id", sa.String, nullable=True),
    sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("superseded_event_id", sa.String, nullable=True),
    sa.Index("ix_ne_document_versions_status", "status"),
    sa.Index("ix_ne_document_versions_hash", "document_id", "content_hash"),
)

_stages = sa.Table(
    "ne_document_stage_events",
    _metadata,
    sa.Column("document_id", sa.String, primary_key=True),
    sa.Column("version_seq", sa.Integer, primary_key=True),
    sa.Column("stage", sa.String, primary_key=True),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("audit_event_id", sa.String, nullable=True),
    sa.Column("detail", sa.String, nullable=False),
)

_artifacts = sa.Table(
    "ne_document_evidence_receipts",
    _metadata,
    sa.Column("document_id", sa.String, primary_key=True),
    sa.Column("version_seq", sa.Integer, primary_key=True),
    sa.Column("stage", sa.String, primary_key=True),
    sa.Column("audit_event_id", sa.String, primary_key=True),
    sa.Column("kind", sa.String, primary_key=True),
    sa.Column("ordinal", sa.Integer, primary_key=True),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("identifier", sa.String, nullable=True),
    sa.Column("size_bytes", sa.Integer, nullable=True),
    sa.Column("artifact_count", sa.Integer, nullable=True),
    sa.Column("dimensions", sa.Integer, nullable=True),
    sa.Column("model_id", sa.String, nullable=True),
    sa.Column("index_fingerprint", sa.String(64), nullable=True),
    sa.Column("numeric_preview", sa.JSON, nullable=True),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index(
        "ix_ne_document_evidence_receipts_stage_audit",
        "document_id",
        "version_seq",
        "stage",
        "audit_event_id",
    ),
)

_parse_receipts = sa.Table(
    "ne_document_parse_evidence_receipts",
    _metadata,
    sa.Column("document_id", sa.String, primary_key=True),
    sa.Column("version_seq", sa.Integer, primary_key=True),
    sa.Column("audit_event_id", sa.String, primary_key=True),
    sa.Column("parser_identity", sa.String, nullable=False),
    sa.Column("schema_version", sa.String, nullable=False),
    sa.Column("source_format", sa.String, nullable=False),
    sa.Column("block_count", sa.Integer, nullable=False),
    sa.Column("page_count", sa.Integer, nullable=False),
    sa.Column("table_count", sa.Integer, nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index(
        "ix_ne_document_parse_evidence_receipts_audit",
        "document_id",
        "version_seq",
        "audit_event_id",
    ),
)

_source_snapshots = sa.Table(
    "ne_document_source_snapshots",
    _metadata,
    sa.Column("source_system", sa.String, primary_key=True),
    sa.Column("source_ref", sa.String, primary_key=True),
    sa.Column("source_snapshot_hash", sa.String(64), primary_key=True),
    sa.Column("source_acl_hash", sa.String(64), primary_key=True),
    sa.Column("document_id", sa.String, nullable=False),
    sa.Column("version_seq", sa.Integer, nullable=False),
    sa.Column("acl", sa.JSON, nullable=False),
    sa.Column("audit_event_id", sa.String, nullable=False),
    sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index(
        "ix_ne_document_source_snapshots_version",
        "document_id",
        "version_seq",
    ),
)

#: 015A0-AUTHORITY-M365. T1 lineage retains the EXACT immutable artifact identity
#: and the admission receipt a supported-source version was admitted under.
#: Keyed by (document, version) — one admitted truth per version — and holding
#: only ids and hashes: no ACL label, no content and no credential.
_source_authority = sa.Table(
    "ne_document_source_authority_receipts",
    _metadata,
    sa.Column("document_id", sa.String, primary_key=True),
    sa.Column("version_seq", sa.Integer, primary_key=True),
    sa.Column("receipt_id", sa.String(512), nullable=False),
    sa.Column("receipt_hash", sa.String(64), nullable=False),
    sa.Column("source_identity_hash", sa.String(64), nullable=False),
    sa.Column("artifact_id", sa.String(64), nullable=True),
    sa.Column("contract_epoch", sa.Integer, nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_ne_document_source_authority_receipt", "receipt_id"),
)

_graph_runs = sa.Table(
    "ne_document_graph_evidence_runs",
    _metadata,
    sa.Column("document_id", sa.String, primary_key=True),
    sa.Column("version_seq", sa.Integer, primary_key=True),
    sa.Column("audit_event_id", sa.String, primary_key=True),
    sa.Column("source_snapshot_hash", sa.String(64), nullable=False),
    sa.Column("source_acl_hash", sa.String(64), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
)

_graph_artifacts = sa.Table(
    "ne_document_graph_evidence_artifacts",
    _metadata,
    sa.Column("document_id", sa.String, primary_key=True),
    sa.Column("version_seq", sa.Integer, primary_key=True),
    sa.Column("audit_event_id", sa.String, primary_key=True),
    sa.Column("kind", sa.String, primary_key=True),
    sa.Column("key", sa.String, primary_key=True),
    sa.Column("entity_class", sa.String, nullable=True),
    sa.Column("display_name", sa.String, nullable=True),
    sa.Column("edge_kind", sa.String, nullable=True),
    sa.Column("subject_key", sa.String, nullable=True),
    sa.Column("subject_class", sa.String, nullable=True),
    sa.Column("subject_display_name", sa.String, nullable=True),
    sa.Column("subject_source_system", sa.String, nullable=True),
    sa.Column("subject_source_ref", sa.String, nullable=True),
    sa.Column("subject_snapshot_hash", sa.String(64), nullable=True),
    sa.Column("subject_acl_hash", sa.String(64), nullable=True),
    sa.Column("subject_acl", sa.JSON, nullable=True),
    sa.Column("object_key", sa.String, nullable=True),
    sa.Column("object_class", sa.String, nullable=True),
    sa.Column("object_display_name", sa.String, nullable=True),
    sa.Column("object_source_system", sa.String, nullable=True),
    sa.Column("object_source_ref", sa.String, nullable=True),
    sa.Column("object_snapshot_hash", sa.String(64), nullable=True),
    sa.Column("object_acl_hash", sa.String(64), nullable=True),
    sa.Column("object_acl", sa.JSON, nullable=True),
)


def ensure_document_lineage_schema(engine: sa.Engine) -> None:
    # Multiple API/worker replicas may boot on the same runtime database.  Keep
    # package-owned create_all, but serialize its Postgres DDL in one transaction.
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _POSTGRES_SCHEMA_LOCK_KEY},
            )
        _metadata.create_all(conn)
        columns = {column["name"] for column in sa.inspect(conn).get_columns(_artifacts.name)}
        if "numeric_preview" not in columns:
            conn.execute(
                sa.text("ALTER TABLE ne_document_evidence_receipts ADD COLUMN numeric_preview JSON")
            )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _plain_json(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _normalized_document_type(display_name: str) -> DocumentType:
    _, separator, suffix = display_name.strip().lower().rpartition(".")
    if not separator:
        return DocumentType.OTHER
    if suffix == "pdf":
        return DocumentType.PDF
    if suffix in _DOCUMENT_SUFFIXES:
        return DocumentType.DOCUMENT
    if suffix in _PRESENTATION_SUFFIXES:
        return DocumentType.PRESENTATION
    if suffix in _SPREADSHEET_SUFFIXES:
        return DocumentType.SPREADSHEET
    if suffix in _TEXT_SUFFIXES:
        return DocumentType.TEXT
    return DocumentType.OTHER


def _record_payload(chunk: Chunk) -> dict[str, Any] | None:
    """The chunk's connector record payload, wherever it rides.

    A parsed document's chunk text is PROSE and its record payload is carried in
    metadata (``PARSED_RECORD_METADATA_KEY``); every other record still carries
    the payload as its canonical-JSON text. One accessor so ``_snapshot`` and
    ``_parsed_source_evidence`` can never disagree about where to look.
    """
    carried = carried_record_payload(chunk.metadata)
    if carried is not None:
        return carried
    return _plain_json(chunk.text)


def _snapshot(chunk: Chunk) -> tuple[str, str, str, str]:
    """Return ``(hash, body, display_name, source_system)`` for the indexed chunk."""
    payload = _record_payload(chunk)
    body = payload.get("body") if payload is not None else None
    source_hash = payload.get("body_content_sha256") if payload is not None else None
    content = body if isinstance(body, str) else chunk.text
    computed_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if (
        not isinstance(source_hash, str)
        or len(source_hash) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in source_hash)
        or source_hash.lower() != computed_hash
    ):
        source_hash = computed_hash
    display = payload.get("name") if payload is not None else None
    meta = chunk.metadata
    if not isinstance(display, str) or not display.strip():
        candidate = meta.get("record_id")
        display = (
            candidate if isinstance(candidate, str) and candidate.strip() else chunk.document_id
        )
    source = meta.get("connector")
    source_system = source if isinstance(source, str) else "knowledge"
    return source_hash.lower(), content, display, source_system


@dataclass(frozen=True)
class _PreparedSemanticChunk:
    identifier: str
    size_bytes: int


@dataclass(frozen=True)
class _PreparedParseEvidence:
    parser_identity: str
    schema_version: str
    source_format: str
    block_count: int
    page_count: int
    table_count: int
    semantic_chunks: tuple[_PreparedSemanticChunk, ...]


def _trusted_m365_parser_producer(chunk: Chunk) -> bool:
    """Require the writer-owned provenance stamp, not payload-authored identity alone."""
    if chunk.metadata.get("connector") != "m365-sharepoint":
        return False
    provenance = chunk.metadata.get("provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "provenance_class",
        "source",
        "actor",
        "written_at",
        "write_id",
        "shape",
    }:
        return False
    source = provenance.get("source")
    return (
        provenance.get("provenance_class") == "ingested"
        and provenance.get("shape") == "ne.connect.record@1"
        and isinstance(source, str)
        and source.startswith("connector:m365-sharepoint:")
        and all(
            isinstance(provenance.get(key), str) and bool(provenance.get(key))
            for key in ("actor", "written_at", "write_id")
        )
    )


def _parsed_source_evidence(
    chunk: Chunk,
    descriptor: ParsedDocumentEvidenceDescriptor,
) -> _PreparedParseEvidence | None:
    """Rebuild bounded semantic evidence only from an exact parsed-source payload."""
    if not _trusted_m365_parser_producer(chunk):
        return None
    payload = _record_payload(chunk)
    if payload is None:
        return None
    if not descriptor.matches_parser_payload(
        item_id=chunk.chunk_id,
        source_id=chunk.metadata.get("source_id"),
        record_id=chunk.metadata.get("record_id"),
        acl=chunk.acl,
        payload=payload,
    ):
        return None
    parser_identity = payload.get("body_parser_identity")
    schema_version = payload.get("body_schema_version")
    source_format = payload.get("body_source_format")
    body = payload.get("body")
    blocks_payload = payload.get("body_blocks")
    block_count = payload.get("body_block_count")
    source_name = payload.get("name")
    if (
        payload.get("body_ingestion_status") != "parsed"
        or parser_identity != "neutron-m365-document-parser/v1"
        or schema_version != "ne.parsed_document/1"
        or source_format not in {"pdf", "docx", "pptx", "html", "md"}
        or not isinstance(body, str)
        or not isinstance(blocks_payload, list)
        or isinstance(block_count, bool)
        or not isinstance(block_count, int)
        or block_count != len(blocks_payload)
        or not isinstance(source_name, str)
        or not source_name.strip()
    ):
        return None
    try:
        blocks = tuple(DocBlock.model_validate(value) for value in blocks_payload)
        if (
            not blocks
            or "\n\n".join(block.text.strip() for block in blocks if block.text.strip()) != body
        ):
            return None
        parsed = ParsedDocument(
            document_id=chunk.document_id,
            source_name=source_name,
            source_format=source_format,
            blocks=blocks,
        )
        semantic = StructuralChunker(
            max_chars=descriptor.max_chars,
            min_chars=descriptor.min_chars,
            include_headings_in_text=descriptor.include_headings_in_text,
        ).chunk(parsed, acl=chunk.acl)
    except (TypeError, ValueError):
        return None
    pages = {block.page for block in blocks if block.page is not None}
    return _PreparedParseEvidence(
        parser_identity=parser_identity,
        schema_version=schema_version,
        source_format=source_format,
        block_count=block_count,
        page_count=len(pages),
        table_count=sum(block.kind is BlockKind.TABLE for block in blocks),
        semantic_chunks=tuple(
            _PreparedSemanticChunk(row.chunk_id, len(row.text.encode("utf-8"))) for row in semantic
        ),
    )


def _source_ref(chunk: Chunk) -> str:
    canonical = chunk.metadata.get("source_ref")
    if isinstance(canonical, str) and canonical:
        return canonical
    provenance = chunk.metadata.get("provenance")
    if isinstance(provenance, dict):
        value = provenance.get("source")
    else:
        value = getattr(provenance, "get", lambda _key: None)("source")
    return value if isinstance(value, str) else ""


def _validated_hash(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    lowered = value.lower()
    if any(character not in "0123456789abcdef" for character in lowered):
        return None
    return lowered


def _source_snapshot_hash(chunk: Chunk) -> str | None:
    return _validated_hash(chunk.metadata.get("source_snapshot_hash"))


def _source_acl_hash(chunk: Chunk) -> str | None:
    return _validated_hash(chunk.metadata.get("source_acl_hash"))


def _normalized_filter_fingerprint(
    query: str,
    document_type: DocumentType | None,
    source_system: str,
    include_structured: bool,
) -> str:
    payload = json.dumps(
        [
            query,
            document_type.value if document_type is not None else "",
            source_system,
            include_structured,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _encode_cursor(row: _VisibleDocument, fingerprint: str) -> str:
    payload = json.dumps(
        {
            "at": row.prepared_at.isoformat(),
            "document_id": row.summary.document_id,
            "filters": fingerprint,
            "version": row.summary.version_seq,
            "wire": _CURSOR_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(value: str, fingerprint: str) -> _DocumentCursor:
    try:
        if not value or len(value) > 1024:
            raise ValueError
        encoded = value.encode("ascii")
        decoded = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        payload = json.loads(decoded)
        if not isinstance(payload, dict) or set(payload) != {
            "at",
            "document_id",
            "filters",
            "version",
            "wire",
        }:
            raise ValueError
        if payload["wire"] != _CURSOR_VERSION or payload["filters"] != fingerprint:
            raise ValueError
        document_id = payload["document_id"]
        version_seq = payload["version"]
        if (
            not isinstance(document_id, str)
            or not document_id
            or isinstance(version_seq, bool)
            or not isinstance(version_seq, int)
            or version_seq < 1
        ):
            raise ValueError
        prepared_at = datetime.fromisoformat(payload["at"])
        if not isinstance(payload["at"], str):
            raise ValueError
    except (
        UnicodeEncodeError,
        binascii.Error,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise DocumentCursorError("invalid document cursor") from None
    return _DocumentCursor(_utc(prepared_at), document_id, version_seq)


def _is_after_cursor(row: _VisibleDocument, cursor: _DocumentCursor) -> bool:
    if row.prepared_at != cursor.prepared_at:
        return row.prepared_at < cursor.prepared_at
    if row.summary.document_id != cursor.document_id:
        return row.summary.document_id > cursor.document_id
    return row.summary.version_seq < cursor.version_seq


@dataclass(frozen=True)
class PreparedDocumentVersion:
    document_id: str
    version_seq: int
    content_hash: str
    prev_content_hash: str | None
    creates_version: bool
    created_now: bool
    content: str
    display_name: str
    source_system: str
    source_ref: str
    source_snapshot_hash: str | None
    source_acl_hash: str | None
    acl: tuple[str, ...]
    chunk_id: str
    chunk_size_bytes: int
    vector_evidence: VectorEvidenceDescriptor | None
    parse_evidence: _PreparedParseEvidence | None


@dataclass(frozen=True)
class RetainedSourceAuthority:
    """The artifact identity and admission receipt one document version retains.

    Retained, not recomputed: an EvidenceLocator naming this version can be
    revalidated against the exact receipt it was admitted under even after the
    current head has moved on.
    """

    document_id: str
    version_seq: int
    receipt_id: str
    receipt_hash: str
    source_identity_hash: str
    artifact_id: str | None
    contract_epoch: int
    state: str


@dataclass(frozen=True)
class ArtifactBoundVersion:
    """The exact document version one retained contributor stamp was served from.

    The reverse of :class:`RetainedSourceAuthority`: given the immutable
    ``(source_system, source_snapshot_hash, source_acl_hash)`` triple a served
    result's contributor stamp carries in its artifact identity, name the
    ``(document_id, version_seq)`` that snapshot was admitted as, with the
    version's own content hash and display name.  This is how a historical
    answer's evidence names the exact version it rested on without consulting
    the corpus's current state.

    The lookup deliberately goes through ``ne_document_source_snapshots`` and
    NOT through the receipt id.  ``bind_source_authority`` REBINDS one row per
    ``(document_id, version_seq)``, so a receipt id stops resolving the moment
    the same version is re-admitted -- measured on the live box 2026-08-28,
    where a 21:00 re-sync rebound 248 of 252 rows and left every stamp retained
    by the 16:43 graph construction resolving to nothing.  The snapshot triple
    is content- and ACL-derived and never rebinds, so it still resolves.
    """

    document_id: str
    version_seq: int
    content_hash: str
    display_name: str


@dataclass(frozen=True)
class DocumentStageEvent:
    stage: DocumentStage
    status: str
    occurred_at: datetime
    audit_event_id: str | None
    detail: str


@dataclass(frozen=True)
class DocumentVersion:
    document_id: str
    version_seq: int
    content_hash: str
    content: str
    display_name: str
    source_system: str
    source_ref: str
    acl: tuple[str, ...]
    chunk_count: int
    status: str
    prepared_at: datetime
    ingested_at: datetime | None
    ingested_event_id: str | None
    superseded_at: datetime | None
    superseded_event_id: str | None
    stages: tuple[DocumentStageEvent, ...] = ()

    @property
    def document_type(self) -> DocumentType:
        return _normalized_document_type(self.display_name)


@dataclass(frozen=True)
class DocumentSummary:
    document_id: str
    display_name: str
    source_system: str
    document_type: DocumentType
    version_seq: int
    content_hash: str
    ingested_at: datetime | None
    indexing: bool


@dataclass(frozen=True)
class DocumentListPage:
    documents: tuple[DocumentSummary, ...]
    next_cursor: str | None
    document_types: tuple[DocumentType, ...]
    source_systems: tuple[str, ...]
    # INTERIM (PD-DOC-TITLES): honest count of ".negraph.json" rows in the
    # ACL-visible corpus (not the page). REMOVE alongside the rest of
    # PD-DOC-TITLES when the real `structured` flag lands on the document wire.
    operational_graph_count: int


class DocumentComparisonState(StrEnum):
    FIRST_VERSION = "first_version"
    AVAILABLE = "available"
    UNCHANGED = "unchanged"
    PREDECESSOR_HIDDEN = "predecessor_hidden"


@dataclass(frozen=True)
class DocumentComparison:
    state: DocumentComparisonState
    predecessor_version_seq: int | None


@dataclass(frozen=True)
class DocumentJourney:
    versions: tuple[DocumentVersion, ...]
    selected: DocumentVersion
    diff: str | None
    comparison: DocumentComparison


type DocumentStageFactValue = str | int | bool | None


class DocumentArtifactKind(StrEnum):
    CHUNK = "chunk"
    EMBEDDING = "embedding"
    SEMANTIC_CHUNK = "semantic_chunk"


class DocumentEvidenceState(StrEnum):
    AVAILABLE = "available"
    EMPTY = "empty"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    WITHHELD = "withheld"


@dataclass(frozen=True)
class DocumentStageArtifact:
    kind: DocumentArtifactKind
    state: DocumentEvidenceState
    ordinal: int
    identifier: str | None = None
    size_bytes: int | None = None
    artifact_count: int | None = None
    dimensions: int | None = None
    model_id: str | None = None
    index_reference: str | None = None
    fingerprint: str | None = None
    numeric_preview: tuple[float, ...] | None = None
    reason: str | None = None


@dataclass(frozen=True)
class GraphEvidenceEntity:
    """A source-snapshot-bound entity descriptor; never carries source content."""

    source_system: str
    source_ref: str
    source_snapshot_hash: str
    source_acl_hash: str
    acl: tuple[str, ...]
    key: str
    entity_class: str
    display_name: str

    @classmethod
    def from_payload(cls, payload: Any) -> GraphEvidenceEntity:
        if not isinstance(payload, dict) or set(payload) != {
            "source_system",
            "source_ref",
            "source_snapshot_hash",
            "source_acl_hash",
            "acl",
            "key",
            "entity_class",
            "display_name",
        }:
            raise ValueError("malformed graph entity evidence")
        scalar = {key: value for key, value in payload.items() if key != "acl"}
        if not all(isinstance(value, str) for value in scalar.values()):
            raise ValueError("malformed graph entity evidence")
        acl = payload["acl"]
        if (
            not isinstance(acl, (list, tuple))
            or not acl
            or not all(isinstance(value, str) and value for value in acl)
        ):
            raise ValueError("malformed graph entity evidence")
        entity = cls(**scalar, acl=tuple(acl))
        if not entity.source_system or not entity.source_ref or not entity.key:
            raise ValueError("malformed graph entity evidence")
        if _validated_hash(entity.source_snapshot_hash) is None:
            raise ValueError("malformed graph entity evidence")
        if _validated_hash(entity.source_acl_hash) is None:
            raise ValueError("malformed graph entity evidence")
        return entity


@dataclass(frozen=True)
class GraphEvidenceRelationship:
    """A relationship whose asserting and target snapshots are both explicit."""

    key: str
    edge_kind: str
    subject: GraphEvidenceEntity
    object: GraphEvidenceEntity

    @classmethod
    def from_payload(cls, payload: Any) -> GraphEvidenceRelationship:
        if not isinstance(payload, dict) or set(payload) != {
            "key",
            "edge_kind",
            "subject",
            "object",
        }:
            raise ValueError("malformed graph relationship evidence")
        if not isinstance(payload["key"], str) or not isinstance(payload["edge_kind"], str):
            raise ValueError("malformed graph relationship evidence")
        return cls(
            key=payload["key"],
            edge_kind=payload["edge_kind"],
            subject=GraphEvidenceEntity.from_payload(payload["subject"]),
            object=GraphEvidenceEntity.from_payload(payload["object"]),
        )


@dataclass(frozen=True)
class GraphEvidenceBatch:
    """One graph-sync audit event plus its content-free evidence descriptors."""

    audit_event_id: str
    occurred_at: datetime
    entities: tuple[GraphEvidenceEntity, ...]
    relationships: tuple[GraphEvidenceRelationship, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "audit_event_id": self.audit_event_id,
            "occurred_at": _utc(self.occurred_at).isoformat(),
            "entities": [asdict(entity) for entity in self.entities],
            "relationships": [asdict(relationship) for relationship in self.relationships],
        }

    @classmethod
    def from_payload(cls, payload: Any) -> GraphEvidenceBatch:
        if not isinstance(payload, dict) or set(payload) != {
            "audit_event_id",
            "occurred_at",
            "entities",
            "relationships",
        }:
            raise ValueError("malformed graph evidence batch")
        if not isinstance(payload["audit_event_id"], str) or not payload["audit_event_id"]:
            raise ValueError("malformed graph evidence batch")
        if not isinstance(payload["occurred_at"], str):
            raise ValueError("malformed graph evidence batch")
        try:
            occurred_at = _utc(datetime.fromisoformat(payload["occurred_at"]))
        except ValueError:
            raise ValueError("malformed graph evidence batch") from None
        if not isinstance(payload["entities"], list) or not isinstance(
            payload["relationships"], list
        ):
            raise ValueError("malformed graph evidence batch")
        return cls(
            audit_event_id=payload["audit_event_id"],
            occurred_at=occurred_at,
            entities=tuple(GraphEvidenceEntity.from_payload(row) for row in payload["entities"]),
            relationships=tuple(
                GraphEvidenceRelationship.from_payload(row) for row in payload["relationships"]
            ),
        )


@dataclass(frozen=True)
class GraphEvidenceBindResult:
    entities_bound: int
    relationships_bound: int
    entities_unbound: int
    relationships_unbound: int


@dataclass(frozen=True)
class GraphEvidenceView:
    """ACL-trimmed internal read model reserved for the later API projection."""

    audit_event_id: str
    occurred_at: datetime
    entities: tuple[GraphEvidenceEntity, ...]
    relationships: tuple[GraphEvidenceRelationship, ...]


@dataclass(frozen=True)
class DocumentStageFact:
    key: str
    label: str
    value: DocumentStageFactValue
    available: bool


@dataclass(frozen=True)
class DocumentStageEvidence:
    document_id: str
    version_seq: int
    content_hash: str
    document_type: DocumentType
    event: DocumentStageEvent
    facts: tuple[DocumentStageFact, ...]
    artifacts: tuple[DocumentStageArtifact, ...]
    semantic_chunks: tuple[DocumentStageArtifact, ...]
    acl_labels: tuple[str, ...]
    comparison: DocumentComparison


@dataclass(frozen=True)
class _DocumentCursor:
    prepared_at: datetime
    document_id: str
    version_seq: int


@dataclass(frozen=True)
class _VisibleDocument:
    summary: DocumentSummary
    prepared_at: datetime


class SqlAlchemyDocumentLineageStore:
    """Runtime-DB lineage store shared by ne-api and connector workers."""

    def __init__(self, engine: sa.Engine, *, clock: Callable[[], datetime] | None = None) -> None:
        self.engine = engine
        self._clock = clock or (lambda: datetime.now(UTC))
        ensure_document_lineage_schema(engine)

    @staticmethod
    def prepare_parsed_source_evidence(
        chunk: Chunk,
        descriptor: ParsedDocumentEvidenceDescriptor | None,
    ) -> _PreparedParseEvidence | None:
        """Validate a writer-bound descriptor before any lineage transaction."""
        if descriptor is None:
            return None
        parsed = _parsed_source_evidence(chunk, descriptor)
        if parsed is None:
            raise DocumentLineageError("parsed source evidence binding is invalid")
        return parsed

    def prepare(
        self,
        chunk: Chunk,
        *,
        vector_evidence: VectorEvidenceDescriptor | None = None,
        parse_evidence: _PreparedParseEvidence | None = None,
    ) -> PreparedDocumentVersion:
        """Prepare or resume a hash-gated version before the vector write."""
        now = _utc(self._clock())
        content_hash, content, display, source_system = _snapshot(chunk)
        acl = tuple(sorted(set(chunk.acl)))
        source_ref = _source_ref(chunk)
        source_snapshot_hash = _source_snapshot_hash(chunk)
        source_acl_hash = _source_acl_hash(chunk)
        with self.engine.begin() as conn:
            pending = (
                conn.execute(
                    sa.select(_versions).where(
                        _versions.c.document_id == chunk.document_id,
                        _versions.c.status == _STATUS_INDEXING,
                    )
                )
                .mappings()
                .first()
            )
            if pending is not None:
                if pending["content_hash"] != content_hash:
                    raise DocumentLineageError(
                        "document has an unresolved indexing version with a different hash"
                    )
                conn.execute(
                    _versions.update()
                    .where(
                        _versions.c.document_id == chunk.document_id,
                        _versions.c.version_seq == pending["version_seq"],
                    )
                    .values(
                        content=content,
                        display_name=display,
                        source_system=source_system,
                        source_ref=source_ref,
                        acl=acl,
                        chunk_count=1,
                    )
                )
                return PreparedDocumentVersion(
                    chunk.document_id,
                    int(pending["version_seq"]),
                    content_hash,
                    pending["prev_content_hash"],
                    True,
                    False,
                    content,
                    display,
                    source_system,
                    source_ref,
                    source_snapshot_hash,
                    source_acl_hash,
                    acl,
                    chunk.chunk_id,
                    len(chunk.text.encode("utf-8")),
                    vector_evidence,
                    parse_evidence,
                )

            current = (
                conn.execute(
                    sa.select(_versions).where(
                        _versions.c.document_id == chunk.document_id,
                        _versions.c.status == _STATUS_CURRENT,
                    )
                )
                .mappings()
                .first()
            )
            if current is not None and current["content_hash"] == content_hash:
                # Defer the security-envelope refresh until after store+audit
                # success.  A failed identical-content write must not mutate ACLs.
                return PreparedDocumentVersion(
                    chunk.document_id,
                    int(current["version_seq"]),
                    content_hash,
                    current["prev_content_hash"],
                    False,
                    False,
                    content,
                    display,
                    source_system,
                    source_ref,
                    source_snapshot_hash,
                    source_acl_hash,
                    acl,
                    chunk.chunk_id,
                    len(chunk.text.encode("utf-8")),
                    vector_evidence,
                    parse_evidence,
                )

            next_seq = (
                int(
                    conn.scalar(
                        sa.select(sa.func.coalesce(sa.func.max(_versions.c.version_seq), 0)).where(
                            _versions.c.document_id == chunk.document_id
                        )
                    )
                    or 0
                )
                + 1
            )
            conn.execute(
                _versions.insert().values(
                    document_id=chunk.document_id,
                    version_seq=next_seq,
                    content_hash=content_hash,
                    prev_content_hash=(current["content_hash"] if current is not None else None),
                    content=content,
                    display_name=display,
                    source_system=source_system,
                    source_ref=source_ref,
                    acl=acl,
                    chunk_count=1,
                    status=_STATUS_INDEXING,
                    prepared_at=now,
                )
            )
            initial = {
                DocumentStage.PARSE: ("complete", "source content normalized"),
                DocumentStage.CHUNK: ("complete", "one governed connector record"),
                DocumentStage.ACL: ("complete", "source ACL resolved"),
                DocumentStage.EMBED: ("active", "vector write in progress"),
                DocumentStage.GRAPH: ("waiting", "awaiting index result"),
                DocumentStage.SUPERSEDE: ("waiting", "awaiting version commit"),
            }
            conn.execute(
                _stages.insert(),
                [
                    {
                        "document_id": chunk.document_id,
                        "version_seq": next_seq,
                        "stage": stage.value,
                        "status": status,
                        "occurred_at": now,
                        "audit_event_id": None,
                        "detail": detail,
                    }
                    for stage, (status, detail) in initial.items()
                ],
            )
        return PreparedDocumentVersion(
            chunk.document_id,
            next_seq,
            content_hash,
            current["content_hash"] if current is not None else None,
            True,
            True,
            content,
            display,
            source_system,
            source_ref,
            source_snapshot_hash,
            source_acl_hash,
            acl,
            chunk.chunk_id,
            len(chunk.text.encode("utf-8")),
            vector_evidence,
            parse_evidence,
        )

    @staticmethod
    def _bind_evidence_receipts(
        conn: sa.Connection,
        prepared: PreparedDocumentVersion,
        *,
        audit_event_id: str,
        occurred_at: datetime,
    ) -> None:
        """Append receipts and bind their stage events in the same transaction."""
        common = {
            "document_id": prepared.document_id,
            "version_seq": prepared.version_seq,
            "audit_event_id": audit_event_id,
            "state": DocumentEvidenceState.AVAILABLE.value,
            "identifier": None,
            "size_bytes": None,
            "artifact_count": None,
            "dimensions": None,
            "model_id": None,
            "index_fingerprint": None,
            "numeric_preview": None,
            "occurred_at": occurred_at,
        }
        rows: list[dict[str, Any]] = [
            {
                **common,
                "stage": DocumentStage.CHUNK.value,
                "kind": DocumentArtifactKind.CHUNK.value,
                "ordinal": 0,
                "identifier": prepared.chunk_id,
                "size_bytes": prepared.chunk_size_bytes,
            }
        ]
        if prepared.parse_evidence is not None:
            rows.extend(
                {
                    **common,
                    "stage": DocumentStage.CHUNK.value,
                    "kind": DocumentArtifactKind.SEMANTIC_CHUNK.value,
                    "ordinal": ordinal,
                    "identifier": semantic.identifier,
                    "size_bytes": semantic.size_bytes,
                }
                for ordinal, semantic in enumerate(prepared.parse_evidence.semantic_chunks)
            )
        else:
            rows.append(
                {
                    **common,
                    "stage": DocumentStage.CHUNK.value,
                    "kind": DocumentArtifactKind.SEMANTIC_CHUNK.value,
                    "ordinal": 0,
                    "state": DocumentEvidenceState.UNAVAILABLE.value,
                }
            )
        if prepared.vector_evidence is not None:
            rows.append(
                {
                    **common,
                    "stage": DocumentStage.EMBED.value,
                    "kind": DocumentArtifactKind.EMBEDDING.value,
                    "ordinal": 0,
                    "artifact_count": 1,
                    "dimensions": prepared.vector_evidence.dimensions,
                    "model_id": prepared.vector_evidence.model_id,
                    "index_fingerprint": prepared.vector_evidence.index_fingerprint,
                    "numeric_preview": (
                        list(prepared.vector_evidence.numeric_preview)
                        if prepared.vector_evidence.numeric_preview is not None
                        else None
                    ),
                }
            )
        conn.execute(_artifacts.insert(), rows)

        if prepared.parse_evidence is not None:
            parsed = prepared.parse_evidence
            conn.execute(
                _parse_receipts.insert().values(
                    document_id=prepared.document_id,
                    version_seq=prepared.version_seq,
                    audit_event_id=audit_event_id,
                    parser_identity=parsed.parser_identity,
                    schema_version=parsed.schema_version,
                    source_format=parsed.source_format,
                    block_count=parsed.block_count,
                    page_count=parsed.page_count,
                    table_count=parsed.table_count,
                    occurred_at=occurred_at,
                )
            )

        details = {
            DocumentStage.PARSE: (
                "structured parser evidence receipt committed"
                if prepared.parse_evidence is not None
                else "normalized content committed; parser evidence unavailable"
            ),
            DocumentStage.CHUNK: (
                (
                    f"one governed indexed record artifact and "
                    f"{len(prepared.parse_evidence.semantic_chunks)} deterministic semantic "
                    "chunk receipts committed"
                )
                if prepared.parse_evidence is not None
                else (
                    "one governed indexed record artifact receipt committed; "
                    "semantic chunk reconstruction unavailable"
                )
            ),
            DocumentStage.EMBED: (
                "vector evidence receipt committed"
                if prepared.vector_evidence is not None
                else "vector write committed; evidence descriptor unavailable"
            ),
        }
        for stage, detail in details.items():
            conn.execute(
                _stages.update()
                .where(
                    _stages.c.document_id == prepared.document_id,
                    _stages.c.version_seq == prepared.version_seq,
                    _stages.c.stage == stage.value,
                )
                .values(
                    status="complete",
                    occurred_at=occurred_at,
                    audit_event_id=audit_event_id,
                    detail=detail,
                )
            )

    @staticmethod
    def _bind_source_snapshot(
        conn: sa.Connection,
        prepared: PreparedDocumentVersion,
        *,
        audit_event_id: str,
        occurred_at: datetime,
    ) -> None:
        """Bind the raw connector snapshot only after store+audit success."""
        snapshot_hash = prepared.source_snapshot_hash
        acl_hash = prepared.source_acl_hash
        if (
            snapshot_hash is None
            or acl_hash is None
            or not prepared.source_system
            or not prepared.source_ref
        ):
            return
        predicate = sa.and_(
            _source_snapshots.c.source_system == prepared.source_system,
            _source_snapshots.c.source_ref == prepared.source_ref,
            _source_snapshots.c.source_snapshot_hash == snapshot_hash,
            _source_snapshots.c.source_acl_hash == acl_hash,
        )
        existing = conn.execute(
            sa.select(
                _source_snapshots.c.document_id,
                _source_snapshots.c.version_seq,
            ).where(predicate)
        ).one_or_none()
        target = (prepared.document_id, prepared.version_seq)
        if existing is not None:
            if (existing.document_id, int(existing.version_seq)) != target:
                raise DocumentLineageError(
                    "source snapshot identity is already bound to a different document version"
                )
            return
        conn.execute(
            _source_snapshots.insert().values(
                source_system=prepared.source_system,
                source_ref=prepared.source_ref,
                source_snapshot_hash=snapshot_hash,
                source_acl_hash=acl_hash,
                document_id=prepared.document_id,
                version_seq=prepared.version_seq,
                acl=list(prepared.acl),
                audit_event_id=audit_event_id,
                bound_at=occurred_at,
            )
        )

    def finalize(
        self,
        prepared: PreparedDocumentVersion,
        *,
        audit_event_id: str,
        occurred_at: datetime,
    ) -> None:
        now = _utc(occurred_at)
        with self.engine.begin() as conn:
            if not prepared.creates_version:
                updated = conn.execute(
                    _versions.update()
                    .where(
                        _versions.c.document_id == prepared.document_id,
                        _versions.c.version_seq == prepared.version_seq,
                        _versions.c.content_hash == prepared.content_hash,
                        _versions.c.status == _STATUS_CURRENT,
                    )
                    .values(
                        content=prepared.content,
                        display_name=prepared.display_name,
                        source_system=prepared.source_system,
                        source_ref=prepared.source_ref,
                        acl=list(prepared.acl),
                    )
                )
                if updated.rowcount != 1:
                    raise RuntimeError("current document changed before metadata refresh")
                self._bind_evidence_receipts(
                    conn,
                    prepared,
                    audit_event_id=audit_event_id,
                    occurred_at=now,
                )
                self._bind_source_snapshot(
                    conn,
                    prepared,
                    audit_event_id=audit_event_id,
                    occurred_at=now,
                )
                return
            target = conn.execute(
                sa.select(_versions.c.status).where(
                    _versions.c.document_id == prepared.document_id,
                    _versions.c.version_seq == prepared.version_seq,
                )
            ).scalar_one_or_none()
            if target == _STATUS_CURRENT:
                return
            if target != _STATUS_INDEXING:
                raise RuntimeError("prepared document version is no longer recoverable")
            conn.execute(
                _versions.update()
                .where(
                    _versions.c.document_id == prepared.document_id,
                    _versions.c.status == _STATUS_CURRENT,
                )
                .values(
                    status=_STATUS_SUPERSEDED,
                    superseded_at=now,
                    superseded_event_id=audit_event_id,
                )
            )
            conn.execute(
                _versions.update()
                .where(
                    _versions.c.document_id == prepared.document_id,
                    _versions.c.version_seq == prepared.version_seq,
                )
                .values(
                    status=_STATUS_CURRENT,
                    ingested_at=now,
                    ingested_event_id=audit_event_id,
                )
            )
            had_prior = prepared.version_seq > 1
            terminal = {
                DocumentStage.EMBED: ("complete", "vector write committed"),
                DocumentStage.GRAPH: (
                    "skipped",
                    "SKIPPED — no version-bound entities/relationships claimed",
                ),
                DocumentStage.SUPERSEDE: (
                    "complete" if had_prior else "skipped",
                    "prior version superseded" if had_prior else "first version",
                ),
            }
            for stage in _STAGE_ORDER:
                values: dict[str, Any] = {"audit_event_id": audit_event_id}
                if stage in terminal:
                    values.update(
                        status=terminal[stage][0], occurred_at=now, detail=terminal[stage][1]
                    )
                conn.execute(
                    _stages.update()
                    .where(
                        _stages.c.document_id == prepared.document_id,
                        _stages.c.version_seq == prepared.version_seq,
                        _stages.c.stage == stage.value,
                    )
                    .values(**values)
                )
            self._bind_evidence_receipts(
                conn,
                prepared,
                audit_event_id=audit_event_id,
                occurred_at=now,
            )
            self._bind_source_snapshot(
                conn,
                prepared,
                audit_event_id=audit_event_id,
                occurred_at=now,
            )

    def abort(self, prepared: PreparedDocumentVersion) -> None:
        if not prepared.creates_version or not prepared.created_now:
            return
        with self.engine.begin() as conn:
            status = conn.execute(
                sa.select(_versions.c.status).where(
                    _versions.c.document_id == prepared.document_id,
                    _versions.c.version_seq == prepared.version_seq,
                )
            ).scalar_one_or_none()
            if status != _STATUS_INDEXING:
                return
            conn.execute(
                _stages.delete().where(
                    _stages.c.document_id == prepared.document_id,
                    _stages.c.version_seq == prepared.version_seq,
                )
            )
            conn.execute(
                _versions.delete().where(
                    _versions.c.document_id == prepared.document_id,
                    _versions.c.version_seq == prepared.version_seq,
                )
            )

    @staticmethod
    def _snapshot_binding(
        conn: sa.Connection, entity: GraphEvidenceEntity
    ) -> Mapping[str, Any] | None:
        row = (
            conn.execute(
                sa.select(_source_snapshots).where(
                    _source_snapshots.c.source_system == entity.source_system,
                    _source_snapshots.c.source_ref == entity.source_ref,
                    _source_snapshots.c.source_snapshot_hash == entity.source_snapshot_hash,
                    _source_snapshots.c.source_acl_hash == entity.source_acl_hash,
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else {str(key): value for key, value in row.items()}

    def bind_source_authority(
        self, document_id: str, *, receipt: CurrentSourceAuthorityReceipt
    ) -> bool:
        """Retain ``receipt`` and its artifact identity against a document version.

        Binds the version that is CURRENT or still indexing, so a supported-source
        admission lands on the version the same run just wrote. Returns ``False``
        when the document has no such version — an authority admission never
        invents a lineage row to attach itself to.
        """
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(_versions.c.version_seq)
                    .where(
                        _versions.c.document_id == document_id,
                        _versions.c.status.in_((_STATUS_INDEXING, _STATUS_CURRENT)),
                    )
                    .order_by(_versions.c.version_seq.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            version_seq = int(row["version_seq"])
            values = {
                "receipt_id": receipt.receipt_id,
                "receipt_hash": receipt.receipt_hash,
                "source_identity_hash": receipt.source_identity.identity_hash,
                "artifact_id": (
                    None
                    if receipt.artifact_identity is None
                    else receipt.artifact_identity.artifact_id
                ),
                "contract_epoch": receipt.contract_epoch,
                "state": receipt.state.value,
                "bound_at": _utc(self._clock()),
            }
            updated = conn.execute(
                _source_authority.update()
                .where(
                    _source_authority.c.document_id == document_id,
                    _source_authority.c.version_seq == version_seq,
                )
                .values(values)
            ).rowcount
            if updated == 0:
                conn.execute(
                    _source_authority.insert().values(
                        document_id=document_id, version_seq=version_seq, **values
                    )
                )
        return True

    def retained_source_authority(
        self, document_id: str, version_seq: int
    ) -> RetainedSourceAuthority | None:
        """The artifact/receipt this exact document version was admitted under."""
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(_source_authority).where(
                        _source_authority.c.document_id == document_id,
                        _source_authority.c.version_seq == version_seq,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return RetainedSourceAuthority(
            document_id=str(row["document_id"]),
            version_seq=int(row["version_seq"]),
            receipt_id=str(row["receipt_id"]),
            receipt_hash=str(row["receipt_hash"]),
            source_identity_hash=str(row["source_identity_hash"]),
            artifact_id=None if row["artifact_id"] is None else str(row["artifact_id"]),
            contract_epoch=int(row["contract_epoch"]),
            state=str(row["state"]),
        )

    def current_version_seq(self, document_id: str) -> int | None:
        """The ``current`` version's sequence for a document, ignoring ACLs.

        Exists for the API's historical-read authorization: a superseded
        version's own retained receipt can never match the current authority
        head again (its source revision moved on by definition), so the router
        evaluates document-level authority against the CURRENT version's
        receipt instead. This lookup discloses nothing itself — the caller
        still ACL-trims the historical content and still refuses when the
        current-authority evaluation refuses.
        """
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(_versions.c.version_seq).where(
                        _versions.c.document_id == document_id,
                        _versions.c.status == _STATUS_CURRENT,
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else int(row["version_seq"])

    def versions_for_artifacts(
        self, keys: Sequence[tuple[str, str, str]]
    ) -> dict[tuple[str, str, str], ArtifactBoundVersion]:
        """Resolve retained contributor-stamp artifact identities to their versions.

        ``keys`` are ``(source_system, source_snapshot_hash, source_acl_hash)``
        triples read off the artifact identity a served result's contributor
        stamp already carries.  Returns only the triples this store actually
        retained a snapshot row for; an unknown triple is simply absent from the
        mapping (absence renders as absence downstream -- never as a guessed
        version).  A triple that somehow matched more than one snapshot row is
        dropped rather than arbitrated: an ambiguous binding is not a binding.
        Bounded by the caller's input, which the evidence wire already caps at
        128 contributors.
        """
        unique = tuple(dict.fromkeys(keys))
        if not unique:
            return {}
        clause = sa.or_(
            *(
                sa.and_(
                    _source_snapshots.c.source_system == source_system,
                    _source_snapshots.c.source_snapshot_hash == snapshot_hash,
                    _source_snapshots.c.source_acl_hash == acl_hash,
                )
                for source_system, snapshot_hash, acl_hash in unique
            )
        )
        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    sa.select(
                        _source_snapshots.c.source_system,
                        _source_snapshots.c.source_snapshot_hash,
                        _source_snapshots.c.source_acl_hash,
                        _source_snapshots.c.document_id,
                        _source_snapshots.c.version_seq,
                        _versions.c.content_hash,
                        _versions.c.display_name,
                    )
                    .select_from(
                        _source_snapshots.join(
                            _versions,
                            sa.and_(
                                _versions.c.document_id == _source_snapshots.c.document_id,
                                _versions.c.version_seq == _source_snapshots.c.version_seq,
                            ),
                        )
                    )
                    .where(clause)
                ).mappings()
            )
        bound: dict[tuple[str, str, str], ArtifactBoundVersion] = {}
        ambiguous: set[tuple[str, str, str]] = set()
        for row in rows:
            key = (
                str(row["source_system"]),
                str(row["source_snapshot_hash"]),
                str(row["source_acl_hash"]),
            )
            version = ArtifactBoundVersion(
                document_id=str(row["document_id"]),
                version_seq=int(row["version_seq"]),
                content_hash=str(row["content_hash"]),
                display_name=str(row["display_name"]),
            )
            existing = bound.get(key)
            if existing is not None and existing != version:
                ambiguous.add(key)
                continue
            bound[key] = version
        for key in ambiguous:
            bound.pop(key, None)
        return bound

    def retained_source_authority_for_snapshot(
        self,
        *,
        source_system: str,
        source_ref: str,
        source_snapshot_hash: str,
        source_acl_hash: str,
    ) -> RetainedSourceAuthority | None:
        """Return authority retained by the exact T1 source snapshot.

        The four-part snapshot key is the same immutable identity used by graph
        evidence binding.  In particular, this never falls back to a current
        document selected only by ``source_ref``: an ACL or content revision
        without its own finalized T1 lineage row is unavailable to Tier-2.
        """
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(_source_authority)
                    .select_from(
                        _source_snapshots.join(
                            _source_authority,
                            sa.and_(
                                _source_authority.c.document_id == _source_snapshots.c.document_id,
                                _source_authority.c.version_seq == _source_snapshots.c.version_seq,
                            ),
                        )
                    )
                    .where(
                        _source_snapshots.c.source_system == source_system,
                        _source_snapshots.c.source_ref == source_ref,
                        _source_snapshots.c.source_snapshot_hash == source_snapshot_hash,
                        _source_snapshots.c.source_acl_hash == source_acl_hash,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return RetainedSourceAuthority(
            document_id=str(row["document_id"]),
            version_seq=int(row["version_seq"]),
            receipt_id=str(row["receipt_id"]),
            receipt_hash=str(row["receipt_hash"]),
            source_identity_hash=str(row["source_identity_hash"]),
            artifact_id=None if row["artifact_id"] is None else str(row["artifact_id"]),
            contract_epoch=int(row["contract_epoch"]),
            state=str(row["state"]),
        )

    def bind_graph_evidence(self, batch: GraphEvidenceBatch) -> GraphEvidenceBindResult:
        """Persist only evidence whose exact T1 source snapshot is bound.

        Missing/legacy snapshot bindings are omitted, never joined to a current
        document by ``source_ref``.  Relationship evidence additionally requires
        an exact binding for both endpoints; endpoint ACLs are copied from those
        immutable T1 bindings rather than trusted from the graph consumer.
        """
        batch = GraphEvidenceBatch.from_payload(batch.to_payload())
        grouped: dict[
            tuple[str, int],
            dict[str, Any],
        ] = {}
        entities_unbound = relationships_unbound = 0

        with self.engine.begin() as conn:
            for entity in sorted(batch.entities, key=lambda item: (item.key, item.source_ref)):
                binding = self._snapshot_binding(conn, entity)
                if binding is None:
                    entities_unbound += 1
                    continue
                group = grouped.setdefault(
                    (str(binding["document_id"]), int(binding["version_seq"])),
                    {
                        "snapshot_hash": entity.source_snapshot_hash,
                        "acl_hash": entity.source_acl_hash,
                        "entities": {},
                        "relationships": {},
                    },
                )
                if (
                    group["snapshot_hash"] != entity.source_snapshot_hash
                    or group["acl_hash"] != entity.source_acl_hash
                ):
                    raise DocumentLineageError(
                        "one document graph receipt cannot mix source security snapshots"
                    )
                group["entities"][entity.key] = entity

            for relationship in sorted(
                batch.relationships, key=lambda item: (item.key, item.subject.key, item.object.key)
            ):
                subject_binding = self._snapshot_binding(conn, relationship.subject)
                object_binding = self._snapshot_binding(conn, relationship.object)
                if subject_binding is None or object_binding is None:
                    relationships_unbound += 1
                    continue
                group = grouped.setdefault(
                    (
                        str(subject_binding["document_id"]),
                        int(subject_binding["version_seq"]),
                    ),
                    {
                        "snapshot_hash": relationship.subject.source_snapshot_hash,
                        "acl_hash": relationship.subject.source_acl_hash,
                        "entities": {},
                        "relationships": {},
                    },
                )
                if (
                    group["snapshot_hash"] != relationship.subject.source_snapshot_hash
                    or group["acl_hash"] != relationship.subject.source_acl_hash
                ):
                    raise DocumentLineageError(
                        "one document graph receipt cannot mix source security snapshots"
                    )
                group["relationships"][relationship.key] = (
                    relationship,
                    tuple(str(value) for value in subject_binding["acl"]),
                    tuple(str(value) for value in object_binding["acl"]),
                )

            entities_bound = relationships_bound = 0
            for (document_id, version_seq), group in sorted(grouped.items()):
                run_row = (
                    conn.execute(
                        sa.select(_graph_runs).where(
                            _graph_runs.c.document_id == document_id,
                            _graph_runs.c.version_seq == version_seq,
                            _graph_runs.c.audit_event_id == batch.audit_event_id,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if run_row is not None and (
                    str(run_row["source_snapshot_hash"]) != group["snapshot_hash"]
                    or str(run_row["source_acl_hash"]) != group["acl_hash"]
                ):
                    raise DocumentLineageError(
                        "graph audit receipt cannot rebind a document security snapshot"
                    )
                if run_row is None:
                    conn.execute(
                        _graph_runs.insert().values(
                            document_id=document_id,
                            version_seq=version_seq,
                            audit_event_id=batch.audit_event_id,
                            source_snapshot_hash=group["snapshot_hash"],
                            source_acl_hash=group["acl_hash"],
                            occurred_at=batch.occurred_at,
                        )
                    )

                existing_artifacts = {
                    (str(row.kind), str(row.key))
                    for row in conn.execute(
                        sa.select(_graph_artifacts.c.kind, _graph_artifacts.c.key).where(
                            _graph_artifacts.c.document_id == document_id,
                            _graph_artifacts.c.version_seq == version_seq,
                            _graph_artifacts.c.audit_event_id == batch.audit_event_id,
                        )
                    )
                }
                rows: list[dict[str, Any]] = []
                for entity in sorted(group["entities"].values(), key=lambda item: item.key):
                    if ("entity", entity.key) in existing_artifacts:
                        continue
                    rows.append(
                        {
                            "document_id": document_id,
                            "version_seq": version_seq,
                            "audit_event_id": batch.audit_event_id,
                            "kind": "entity",
                            "key": entity.key,
                            "entity_class": entity.entity_class,
                            "display_name": entity.display_name,
                        }
                    )
                for packed in sorted(group["relationships"].values(), key=lambda item: item[0].key):
                    relationship, subject_acl, object_acl = packed
                    if ("relationship", relationship.key) in existing_artifacts:
                        continue
                    rows.append(
                        {
                            "document_id": document_id,
                            "version_seq": version_seq,
                            "audit_event_id": batch.audit_event_id,
                            "kind": "relationship",
                            "key": relationship.key,
                            "edge_kind": relationship.edge_kind,
                            "subject_key": relationship.subject.key,
                            "subject_class": relationship.subject.entity_class,
                            "subject_display_name": relationship.subject.display_name,
                            "subject_source_system": relationship.subject.source_system,
                            "subject_source_ref": relationship.subject.source_ref,
                            "subject_snapshot_hash": relationship.subject.source_snapshot_hash,
                            "subject_acl_hash": relationship.subject.source_acl_hash,
                            "subject_acl": list(subject_acl),
                            "object_key": relationship.object.key,
                            "object_class": relationship.object.entity_class,
                            "object_display_name": relationship.object.display_name,
                            "object_source_system": relationship.object.source_system,
                            "object_source_ref": relationship.object.source_ref,
                            "object_snapshot_hash": relationship.object.source_snapshot_hash,
                            "object_acl_hash": relationship.object.source_acl_hash,
                            "object_acl": list(object_acl),
                        }
                    )
                if rows:
                    column_names = tuple(column.name for column in _graph_artifacts.columns)
                    conn.execute(
                        _graph_artifacts.insert(),
                        [{column: row.get(column) for column in column_names} for row in rows],
                    )
                conn.execute(
                    _stages.update()
                    .where(
                        _stages.c.document_id == document_id,
                        _stages.c.version_seq == version_seq,
                        _stages.c.stage == DocumentStage.GRAPH.value,
                        sa.or_(
                            _stages.c.occurred_at.is_(None),
                            _stages.c.occurred_at <= batch.occurred_at,
                        ),
                    )
                    .values(
                        status="complete",
                        occurred_at=batch.occurred_at,
                        audit_event_id=batch.audit_event_id,
                        detail=(
                            "version-bound graph evidence receipt committed "
                            f"(entities={len(group['entities'])} "
                            f"relationships={len(group['relationships'])})"
                        ),
                    )
                )
                entities_bound += len(group["entities"])
                relationships_bound += len(group["relationships"])

        return GraphEvidenceBindResult(
            entities_bound=entities_bound,
            relationships_bound=relationships_bound,
            entities_unbound=entities_unbound,
            relationships_unbound=relationships_unbound,
        )

    def graph_evidence(
        self,
        document_id: str,
        version_seq: int,
        acl_labels: Sequence[str],
    ) -> GraphEvidenceView | None:
        """Read one receipt with document ACL first and endpoint ACL trimming.

        This package-local seam is deliberately not an HTTP projection.  E2B may
        project it without re-implementing historical or relationship ACL truth.
        """
        labels = set(acl_labels)
        with self.engine.connect() as conn:
            version_row = (
                conn.execute(
                    sa.select(_versions).where(
                        _versions.c.document_id == document_id,
                        _versions.c.version_seq == version_seq,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if version_row is None:
                return None
            version = self._version(version_row, ())
            if not self._visible(version, labels):
                return None
            stage_row = (
                conn.execute(
                    sa.select(_stages).where(
                        _stages.c.document_id == document_id,
                        _stages.c.version_seq == version_seq,
                        _stages.c.stage == DocumentStage.GRAPH.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if stage_row is None or stage_row["audit_event_id"] is None:
                return None
            audit_event_id = str(stage_row["audit_event_id"])
            run_row = (
                conn.execute(
                    sa.select(_graph_runs).where(
                        _graph_runs.c.document_id == document_id,
                        _graph_runs.c.version_seq == version_seq,
                        _graph_runs.c.audit_event_id == audit_event_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if run_row is None:
                return None
            source_row = (
                conn.execute(
                    sa.select(_source_snapshots).where(
                        _source_snapshots.c.document_id == document_id,
                        _source_snapshots.c.version_seq == version_seq,
                        _source_snapshots.c.source_snapshot_hash == run_row["source_snapshot_hash"],
                        _source_snapshots.c.source_acl_hash == run_row["source_acl_hash"],
                    )
                )
                .mappings()
                .one_or_none()
            )
            if source_row is None:
                return None
            rows = [
                {str(key): value for key, value in row.items()}
                for row in conn.execute(
                    sa.select(_graph_artifacts)
                    .where(
                        _graph_artifacts.c.document_id == document_id,
                        _graph_artifacts.c.version_seq == version_seq,
                        _graph_artifacts.c.audit_event_id == audit_event_id,
                    )
                    .order_by(_graph_artifacts.c.kind, _graph_artifacts.c.key)
                ).mappings()
            ]
            current_relationship_visibility = {
                str(row["key"]): self._relationship_endpoints_currently_visible(conn, row, labels)
                for row in rows
                if row["kind"] == "relationship"
            }

        entities: list[GraphEvidenceEntity] = []
        relationships: list[GraphEvidenceRelationship] = []
        receipt_acl = {str(value) for value in source_row["acl"]}
        for row in rows:
            if row["kind"] == "entity":
                if not labels.intersection(receipt_acl):
                    continue
                entities.append(
                    GraphEvidenceEntity(
                        source_system=str(source_row["source_system"]),
                        source_ref=str(source_row["source_ref"]),
                        source_snapshot_hash=str(run_row["source_snapshot_hash"]),
                        source_acl_hash=str(run_row["source_acl_hash"]),
                        acl=tuple(str(value) for value in source_row["acl"]),
                        key=str(row["key"]),
                        entity_class=str(row["entity_class"]),
                        display_name=str(row["display_name"]),
                    )
                )
                continue
            if not current_relationship_visibility.get(str(row["key"]), False):
                continue
            subject_acl = {str(value) for value in (row["subject_acl"] or ())}
            object_acl = {str(value) for value in (row["object_acl"] or ())}
            if not labels.intersection(subject_acl) or not labels.intersection(object_acl):
                continue
            relationships.append(
                GraphEvidenceRelationship(
                    key=str(row["key"]),
                    edge_kind=str(row["edge_kind"]),
                    subject=GraphEvidenceEntity(
                        source_system=str(row["subject_source_system"]),
                        source_ref=str(row["subject_source_ref"]),
                        source_snapshot_hash=str(row["subject_snapshot_hash"]),
                        source_acl_hash=str(row["subject_acl_hash"]),
                        acl=tuple(str(value) for value in row["subject_acl"]),
                        key=str(row["subject_key"]),
                        entity_class=str(row["subject_class"]),
                        display_name=str(row["subject_display_name"]),
                    ),
                    object=GraphEvidenceEntity(
                        source_system=str(row["object_source_system"]),
                        source_ref=str(row["object_source_ref"]),
                        source_snapshot_hash=str(row["object_snapshot_hash"]),
                        source_acl_hash=str(row["object_acl_hash"]),
                        acl=tuple(str(value) for value in row["object_acl"]),
                        key=str(row["object_key"]),
                        entity_class=str(row["object_class"]),
                        display_name=str(row["object_display_name"]),
                    ),
                )
            )
        return GraphEvidenceView(
            audit_event_id=audit_event_id,
            occurred_at=_utc(run_row["occurred_at"]),
            entities=tuple(entities),
            relationships=tuple(relationships),
        )

    @staticmethod
    def _relationship_endpoints_currently_visible(
        conn: sa.Connection, row: Mapping[str, Any], labels: set[str]
    ) -> bool:
        """Require current ACL visibility for both exact immutable endpoints."""
        for endpoint in ("subject", "object"):
            bindings = list(
                conn.execute(
                    sa.select(
                        _source_snapshots.c.document_id, _source_snapshots.c.version_seq
                    ).where(
                        _source_snapshots.c.source_system == row[f"{endpoint}_source_system"],
                        _source_snapshots.c.source_ref == row[f"{endpoint}_source_ref"],
                        _source_snapshots.c.source_snapshot_hash
                        == row[f"{endpoint}_snapshot_hash"],
                        _source_snapshots.c.source_acl_hash == row[f"{endpoint}_acl_hash"],
                    )
                ).mappings()
            )
            if len(bindings) != 1:
                return False
            binding = bindings[0]
            current_acl = conn.execute(
                sa.select(_versions.c.acl).where(
                    _versions.c.document_id == binding["document_id"],
                    _versions.c.version_seq == binding["version_seq"],
                )
            ).scalar_one_or_none()
            if current_acl is None or not labels.intersection(str(value) for value in current_acl):
                return False
        return True

    @staticmethod
    def _visible(version: DocumentVersion, labels: set[str]) -> bool:
        return bool(set(version.acl) & labels)

    def list_documents(
        self, acl_labels: Sequence[str], *, limit: int = 200
    ) -> tuple[DocumentSummary, ...]:
        """Compatibility wrapper over the first unfiltered document page."""
        return self.list_document_page(acl_labels, limit=limit).documents

    def list_document_page(
        self,
        acl_labels: Sequence[str],
        *,
        query: str | None = None,
        document_type: DocumentType | None = None,
        source_system: str | None = None,
        include_structured: bool = False,
        cursor: str | None = None,
        limit: int = 100,
    ) -> DocumentListPage:
        """Return one stable, ACL-first page of the principal's document directory.

        The cursor is bound to the normalized filters.  ACL trimming happens
        before per-document collapse, facets, filtering, and pagination: a
        hidden newer indexing version cannot suppress an older visible current
        version, and hidden rows cannot influence a count or filter option.

        ``include_structured`` is an INTERIM (PD-DOC-TITLES) filter: structured-
        ness is inferred from the ".negraph.json" connector filename convention;
        REMOVE it, ``DocumentListPage.operational_graph_count`` below, and every
        other INTERIM (PD-DOC-TITLES) site when the real ``structured`` flag
        lands on the document wire.  Default ``False`` EXCLUDES operational-
        graph rows from the page — the directory's default is prose documents
        only.  ``True`` is the explicit opt-in that puts them back alongside
        everything else.
        """
        labels = set(acl_labels)
        page_limit = max(1, min(int(limit), MAX_DOCUMENT_PAGE))
        normalized_query = (query or "").strip().casefold()
        normalized_source = (source_system or "").strip().casefold()
        fingerprint = _normalized_filter_fingerprint(
            normalized_query, document_type, normalized_source, include_structured
        )
        decoded_cursor = _decode_cursor(cursor, fingerprint) if cursor is not None else None
        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    sa.select(_versions)
                    .where(_versions.c.status.in_([_STATUS_CURRENT, _STATUS_INDEXING]))
                    .order_by(
                        _versions.c.prepared_at.desc(),
                        _versions.c.document_id.asc(),
                        _versions.c.version_seq.desc(),
                    )
                ).mappings()
            )

        visible_rows: list[_VisibleDocument] = []
        seen: set[str] = set()
        for row in rows:
            version = self._version(row, ())
            if not self._visible(version, labels) or version.document_id in seen:
                continue
            seen.add(version.document_id)
            visible_rows.append(
                _VisibleDocument(
                    summary=DocumentSummary(
                        document_id=version.document_id,
                        display_name=version.display_name,
                        source_system=version.source_system,
                        document_type=version.document_type,
                        version_seq=version.version_seq,
                        content_hash=version.content_hash,
                        ingested_at=version.ingested_at,
                        indexing=version.status == _STATUS_INDEXING,
                    ),
                    prepared_at=version.prepared_at,
                )
            )

        document_types = tuple(
            kind
            for kind in DocumentType
            if any(row.summary.document_type is kind for row in visible_rows)
        )
        source_systems = tuple(
            sorted(
                {row.summary.source_system for row in visible_rows},
                key=lambda value: (value.casefold(), value),
            )
        )
        # INTERIM (PD-DOC-TITLES): honest count of ".negraph.json" rows in the
        # ACL-visible corpus, computed the same way as document_types and
        # source_systems above — over the full visible set, before pagination
        # narrows it — so it is never a page-local guess. REMOVE alongside the
        # rest of PD-DOC-TITLES.
        operational_graph_count = sum(
            1
            for row in visible_rows
            if row.summary.display_name.casefold().endswith(".negraph.json")
        )
        matches: list[_VisibleDocument] = []
        for row in visible_rows:
            if normalized_query and normalized_query not in row.summary.display_name.casefold():
                continue
            if document_type is not None and row.summary.document_type is not document_type:
                continue
            if normalized_source and row.summary.source_system.casefold() != normalized_source:
                continue
            # INTERIM (PD-DOC-TITLES): structured-ness inferred from the
            # ".negraph.json" connector filename convention; REMOVE when the
            # real `structured` flag lands on the document wire. Default
            # excludes graph rows; `include_structured=True` is the opt-in
            # that keeps them in the page.
            if not include_structured and row.summary.display_name.casefold().endswith(
                ".negraph.json"
            ):
                continue
            if decoded_cursor is not None and not _is_after_cursor(row, decoded_cursor):
                continue
            matches.append(row)
            if len(matches) > page_limit:
                break
        selected = matches[:page_limit]
        next_cursor = (
            _encode_cursor(selected[-1], fingerprint) if len(matches) > page_limit else None
        )
        return DocumentListPage(
            documents=tuple(row.summary for row in selected),
            next_cursor=next_cursor,
            document_types=document_types,
            source_systems=source_systems,
            operational_graph_count=operational_graph_count,
        )

    def stage_evidence(
        self,
        document_id: str,
        version_seq: int,
        stage: DocumentStage,
        acl_labels: Sequence[str],
    ) -> DocumentStageEvidence | None:
        """Return persisted/reconstructable evidence for one exact visible stage."""
        labels = set(acl_labels)
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(_versions).where(
                        _versions.c.document_id == document_id,
                        _versions.c.version_seq == version_seq,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            version = self._version(row, ())
            if not self._visible(version, labels):
                return None
            stage_row = (
                conn.execute(
                    sa.select(_stages).where(
                        _stages.c.document_id == document_id,
                        _stages.c.version_seq == version_seq,
                        _stages.c.stage == stage.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if stage_row is None:
                return None
            # LOAD-BEARING ORDER: the exact historical version ACL was checked
            # above before this audit-bound artifact table is touched.
            artifact_rows: list[Any] = []
            parse_row = None
            if (
                stage in (DocumentStage.CHUNK, DocumentStage.EMBED)
                and stage_row["audit_event_id"] is not None
            ):
                artifact_rows = list(
                    conn.execute(
                        sa.select(_artifacts)
                        .where(
                            _artifacts.c.document_id == document_id,
                            _artifacts.c.version_seq == version_seq,
                            _artifacts.c.stage == stage.value,
                            _artifacts.c.audit_event_id == stage_row["audit_event_id"],
                        )
                        .order_by(_artifacts.c.ordinal)
                    ).mappings()
                )
            if stage is DocumentStage.PARSE and stage_row["audit_event_id"] is not None:
                parse_row = (
                    conn.execute(
                        sa.select(_parse_receipts).where(
                            _parse_receipts.c.document_id == document_id,
                            _parse_receipts.c.version_seq == version_seq,
                            _parse_receipts.c.audit_event_id == stage_row["audit_event_id"],
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            predecessor_row = None
            if version_seq > 1:
                predecessor_row = (
                    conn.execute(
                        sa.select(_versions).where(
                            _versions.c.document_id == document_id,
                            _versions.c.version_seq == version_seq - 1,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )

        predecessor = self._version(predecessor_row, ()) if predecessor_row is not None else None
        comparison, _ = self._comparison(version, predecessor, labels)
        event = DocumentStageEvent(
            stage=DocumentStage(stage_row["stage"]),
            status=stage_row["status"],
            occurred_at=_utc(stage_row["occurred_at"]),
            audit_event_id=stage_row["audit_event_id"],
            detail=stage_row["detail"],
        )
        all_artifacts = self._stage_artifacts(event, artifact_rows)
        semantic_chunks = tuple(
            artifact
            for artifact in all_artifacts
            if artifact.kind is DocumentArtifactKind.SEMANTIC_CHUNK
        )
        artifacts = tuple(
            artifact
            for artifact in all_artifacts
            if artifact.kind is not DocumentArtifactKind.SEMANTIC_CHUNK
        )
        return DocumentStageEvidence(
            document_id=version.document_id,
            version_seq=version.version_seq,
            content_hash=version.content_hash,
            document_type=version.document_type,
            event=event,
            facts=self._stage_facts(
                version,
                event,
                comparison,
                artifacts,
                semantic_chunks,
                parse_row,
            ),
            artifacts=artifacts,
            semantic_chunks=semantic_chunks,
            acl_labels=tuple(version.acl),
            comparison=comparison,
        )

    def journey(
        self,
        document_id: str,
        acl_labels: Sequence[str],
        *,
        version_seq: int | None = None,
        as_of: datetime | None = None,
    ) -> DocumentJourney | None:
        labels = set(acl_labels)
        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    sa.select(_versions)
                    .where(_versions.c.document_id == document_id)
                    .order_by(_versions.c.version_seq)
                ).mappings()
            )
            stage_rows = list(
                conn.execute(
                    sa.select(_stages)
                    .where(_stages.c.document_id == document_id)
                    .order_by(_stages.c.version_seq, _stages.c.stage)
                ).mappings()
            )
        by_seq: dict[int, list[DocumentStageEvent]] = {}
        for row in stage_rows:
            by_seq.setdefault(int(row["version_seq"]), []).append(
                DocumentStageEvent(
                    stage=DocumentStage(row["stage"]),
                    status=row["status"],
                    occurred_at=_utc(row["occurred_at"]),
                    audit_event_id=row["audit_event_id"],
                    detail=row["detail"],
                )
            )
        stage_rank = {stage: index for index, stage in enumerate(_STAGE_ORDER)}
        for events in by_seq.values():
            events.sort(key=lambda event: stage_rank[event.stage])
        all_versions = tuple(
            self._version(row, tuple(by_seq.get(int(row["version_seq"]), ()))) for row in rows
        )
        visible = tuple(version for version in all_versions if self._visible(version, labels))
        if not visible:
            return None

        selected: DocumentVersion | None = None
        if version_seq is not None:
            selected = next((v for v in visible if v.version_seq == version_seq), None)
        elif as_of is not None:
            instant = _utc(as_of)
            actual = next(
                (
                    v
                    for v in all_versions
                    if v.ingested_at is not None
                    and v.ingested_at <= instant
                    and (v.superseded_at is None or instant < v.superseded_at)
                ),
                None,
            )
            selected = actual if actual is not None and self._visible(actual, labels) else None
        else:
            selected = next((v for v in reversed(visible) if v.status == _STATUS_INDEXING), None)
            if selected is None:
                selected = next((v for v in reversed(visible) if v.status == _STATUS_CURRENT), None)
        if selected is None:
            return None

        predecessor = next(
            (v for v in all_versions if v.version_seq == selected.version_seq - 1), None
        )
        comparison, diff = self._comparison(selected, predecessor, labels)
        return DocumentJourney(
            versions=visible,
            selected=selected,
            diff=diff,
            comparison=comparison,
        )

    @classmethod
    def _comparison(
        cls,
        selected: DocumentVersion,
        predecessor: DocumentVersion | None,
        labels: set[str],
    ) -> tuple[DocumentComparison, str | None]:
        if predecessor is None:
            return DocumentComparison(DocumentComparisonState.FIRST_VERSION, None), None
        if not cls._visible(predecessor, labels):
            return DocumentComparison(DocumentComparisonState.PREDECESSOR_HIDDEN, None), None
        diff = "\n".join(
            difflib.unified_diff(
                predecessor.content.splitlines(),
                selected.content.splitlines(),
                fromfile=f"v{predecessor.version_seq}",
                tofile=f"v{selected.version_seq}",
                lineterm="",
            )
        )
        state = DocumentComparisonState.UNCHANGED if not diff else DocumentComparisonState.AVAILABLE
        return DocumentComparison(state, predecessor.version_seq), diff

    @staticmethod
    def _stage_facts(
        version: DocumentVersion,
        event: DocumentStageEvent,
        comparison: DocumentComparison,
        artifacts: tuple[DocumentStageArtifact, ...],
        semantic_chunks: tuple[DocumentStageArtifact, ...],
        parse_receipt: Any | None,
    ) -> tuple[DocumentStageFact, ...]:
        def unavailable(key: str, label: str) -> DocumentStageFact:
            return DocumentStageFact(key, label, None, False)

        content_bytes = len(version.content.encode("utf-8"))
        if event.stage is DocumentStage.PARSE:
            parsed = parse_receipt is not None
            return (
                DocumentStageFact("input_name", "Input name", version.display_name, True),
                DocumentStageFact(
                    "document_type",
                    "Normalized document type",
                    version.document_type.value,
                    True,
                ),
                DocumentStageFact("content_bytes", "Extracted UTF-8 bytes", content_bytes, True),
                DocumentStageFact(
                    "parser",
                    "Parser identity",
                    parse_receipt["parser_identity"] if parsed else None,
                    parsed,
                ),
                DocumentStageFact(
                    "schema_version",
                    "Parsed schema",
                    parse_receipt["schema_version"] if parsed else None,
                    parsed,
                ),
                DocumentStageFact(
                    "source_format",
                    "Parsed source format",
                    parse_receipt["source_format"] if parsed else None,
                    parsed,
                ),
                DocumentStageFact(
                    "block_count",
                    "Structural block count",
                    int(parse_receipt["block_count"]) if parsed else None,
                    parsed,
                ),
                DocumentStageFact(
                    "page_count",
                    "Page count",
                    int(parse_receipt["page_count"]) if parsed else None,
                    parsed,
                ),
                DocumentStageFact(
                    "table_count",
                    "Table count",
                    int(parse_receipt["table_count"]) if parsed else None,
                    parsed,
                ),
            )
        if event.stage is DocumentStage.CHUNK:
            receipt = next(
                (
                    artifact
                    for artifact in artifacts
                    if artifact.kind is DocumentArtifactKind.CHUNK
                    and artifact.state is DocumentEvidenceState.AVAILABLE
                ),
                None,
            )
            available_semantic = tuple(
                artifact
                for artifact in semantic_chunks
                if artifact.state is DocumentEvidenceState.AVAILABLE
            )
            return (
                DocumentStageFact(
                    "chunk_count",
                    "Indexed record artifact count",
                    1 if receipt is not None else None,
                    receipt is not None,
                ),
                DocumentStageFact("content_bytes", "Visible content bytes", content_bytes, True),
                DocumentStageFact(
                    "semantic_chunk_count",
                    "Semantic chunk receipt count",
                    len(available_semantic) if available_semantic else None,
                    bool(available_semantic),
                ),
                DocumentStageFact(
                    "chunk_identifiers",
                    "Record chunk ID",
                    receipt.identifier if receipt is not None else None,
                    receipt is not None and receipt.identifier is not None,
                ),
                DocumentStageFact(
                    "chunk_sizes",
                    "UTF-8 payload size",
                    receipt.size_bytes if receipt is not None else None,
                    receipt is not None and receipt.size_bytes is not None,
                ),
                DocumentStageFact(
                    "segmentation",
                    "Segmentation",
                    (
                        "Deterministically reconstructed from exact parsed source blocks; "
                        "the connector record remains the indexed T1 artifact."
                        if available_semantic
                        else "Semantic chunk reconstruction is unavailable for this version."
                    ),
                    True,
                ),
            )
        if event.stage is DocumentStage.ACL:
            return (
                DocumentStageFact("acl_result", "Imported ACL result", event.status, True),
                unavailable("acl_labels", "Sensitive ACL labels"),
            )
        if event.stage is DocumentStage.EMBED:
            receipt = next(
                (
                    artifact
                    for artifact in artifacts
                    if artifact.kind is DocumentArtifactKind.EMBEDDING
                    and artifact.state is DocumentEvidenceState.AVAILABLE
                ),
                None,
            )
            artifact_count = receipt.artifact_count if receipt is not None else None
            return (
                DocumentStageFact(
                    "artifact_count",
                    "Vector artifact receipt",
                    artifact_count,
                    artifact_count is not None,
                ),
                DocumentStageFact(
                    "dimensions",
                    "Vector dimensions",
                    receipt.dimensions if receipt is not None else None,
                    receipt is not None and receipt.dimensions is not None,
                ),
                DocumentStageFact(
                    "model",
                    "Embedding model",
                    receipt.model_id if receipt is not None else None,
                    receipt is not None and receipt.model_id is not None,
                ),
                DocumentStageFact(
                    "index_reference",
                    "Index reference",
                    receipt.index_reference if receipt is not None else None,
                    receipt is not None and receipt.index_reference is not None,
                ),
                DocumentStageFact(
                    "fingerprint",
                    "Vector fingerprint",
                    receipt.fingerprint if receipt is not None else None,
                    receipt is not None and receipt.fingerprint is not None,
                ),
                DocumentStageFact(
                    "numeric_preview",
                    "Bounded numeric preview",
                    (
                        ", ".join(str(value) for value in receipt.numeric_preview)
                        if receipt is not None and receipt.numeric_preview is not None
                        else None
                    ),
                    receipt is not None and receipt.numeric_preview is not None,
                ),
            )
        if event.stage is DocumentStage.GRAPH:
            return (
                DocumentStageFact("graph_result", "Graph stage result", event.detail, True),
                unavailable("entities", "Created entities"),
                unavailable("relationships", "Created relationships"),
            )
        return (
            DocumentStageFact(
                "relationship",
                "Version relationship",
                comparison.state.value,
                True,
            ),
            DocumentStageFact(
                "predecessor_version",
                "Visible predecessor version",
                comparison.predecessor_version_seq,
                comparison.predecessor_version_seq is not None,
            ),
        )

    @staticmethod
    def _stage_artifacts(
        event: DocumentStageEvent,
        rows: Sequence[Any],
    ) -> tuple[DocumentStageArtifact, ...]:
        if event.stage is DocumentStage.CHUNK:
            missing_kind = DocumentArtifactKind.CHUNK
        elif event.stage is DocumentStage.EMBED:
            missing_kind = DocumentArtifactKind.EMBEDDING
        else:
            return ()
        if not rows:
            state = (
                DocumentEvidenceState.SKIPPED
                if event.status == "skipped"
                else DocumentEvidenceState.UNAVAILABLE
            )
            return (
                DocumentStageArtifact(
                    kind=missing_kind,
                    state=state,
                    ordinal=0,
                    reason=event.detail
                    if state is DocumentEvidenceState.SKIPPED
                    else "not_recorded",
                ),
            )
        return tuple(
            DocumentStageArtifact(
                kind=DocumentArtifactKind(row["kind"]),
                state=DocumentEvidenceState(row["state"]),
                ordinal=int(row["ordinal"]),
                identifier=row["identifier"],
                size_bytes=(int(row["size_bytes"]) if row["size_bytes"] is not None else None),
                artifact_count=(
                    int(row["artifact_count"]) if row["artifact_count"] is not None else None
                ),
                dimensions=(int(row["dimensions"]) if row["dimensions"] is not None else None),
                model_id=row["model_id"],
                index_reference=(
                    f"sha256:{row['index_fingerprint']}"
                    if row["index_fingerprint"] is not None
                    else None
                ),
                fingerprint=row["index_fingerprint"],
                numeric_preview=(
                    tuple(float(value) for value in row["numeric_preview"])
                    if row["numeric_preview"] is not None
                    else None
                ),
                reason=(
                    "semantic_chunks_not_recorded"
                    if row["kind"] == DocumentArtifactKind.SEMANTIC_CHUNK.value
                    and row["state"] == DocumentEvidenceState.UNAVAILABLE.value
                    else None
                ),
            )
            for row in rows
        )

    @staticmethod
    def _version(row: Any, stages: tuple[DocumentStageEvent, ...]) -> DocumentVersion:
        return DocumentVersion(
            document_id=row["document_id"],
            version_seq=int(row["version_seq"]),
            content_hash=row["content_hash"],
            content=row["content"],
            display_name=row["display_name"],
            source_system=row["source_system"],
            source_ref=row["source_ref"],
            acl=tuple(row["acl"] or ()),
            chunk_count=int(row["chunk_count"]),
            status=row["status"],
            prepared_at=_utc(row["prepared_at"]),
            ingested_at=_utc(row["ingested_at"]) if row["ingested_at"] is not None else None,
            ingested_event_id=row["ingested_event_id"],
            superseded_at=(
                _utc(row["superseded_at"]) if row["superseded_at"] is not None else None
            ),
            superseded_event_id=row["superseded_event_id"],
            stages=stages,
        )
