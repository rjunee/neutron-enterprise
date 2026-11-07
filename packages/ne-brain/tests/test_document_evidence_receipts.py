# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Audit-bound CHUNK/EMBED receipt persistence and ACL-order proofs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy import event

from ne.brain.evidence import submitted_embedding_evidence, vector_evidence_descriptor
from ne.brain.ingest.chunker import StructuralChunker
from ne.brain.ingest.contracts import DocBlock, ParsedDocument
from ne.brain.lineage import (
    DocumentEvidenceState,
    DocumentLineageError,
    DocumentStage,
    GraphEvidenceBatch,
    GraphEvidenceEntity,
    GraphEvidenceRelationship,
    SqlAlchemyDocumentLineageStore,
)
from ne.brain.parsed_evidence import (
    ParsedDocumentEvidenceDescriptor,
    _consume_parsed_document_evidence,
)
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.connector import M365GraphConnector
from ne.contracts.types import Chunk


def _chunk(
    body: str,
    acl: tuple[str, ...] = ("ops",),
    *,
    document_id: str = "document-1",
    source_ref: str | None = None,
    source_snapshot_hash: str | None = None,
) -> Chunk:
    metadata: dict[str, object] = {
        "connector": "m365-sharepoint",
        "source_id": document_id,
        "record_id": document_id,
        "provenance": {"source": f"connector:m365-sharepoint:{document_id}"},
    }
    if source_ref is not None:
        metadata["source_ref"] = source_ref
    if source_snapshot_hash is not None:
        metadata["source_snapshot_hash"] = source_snapshot_hash
        metadata["source_acl_hash"] = _acl_hash(acl)
    return Chunk(
        chunk_id=f"{document_id}:record",
        document_id=document_id,
        text=json.dumps(
            {
                "body": body,
                "body_content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "name": "Procedure.docx",
            },
            sort_keys=True,
        ),
        metadata=metadata,
        acl=acl,
    )


def _parsed_chunk(
    *,
    body: str,
    blocks: list[dict[str, object]],
    acl: tuple[str, ...] = ("ops",),
    document_id: str = "document-1",
) -> Chunk:
    chunk = _chunk(body, acl, document_id=document_id)
    payload = json.loads(chunk.text)
    payload.update(
        body_blocks=blocks,
        body_block_count=len(blocks),
        body_ingestion_status="parsed",
        body_parser_identity="neutron-m365-document-parser/v1",
        body_schema_version="ne.parsed_document/1",
        body_source_format="docx",
    )
    metadata = dict(chunk.metadata)
    metadata["provenance"] = {
        "provenance_class": "ingested",
        "source": f"connector:m365-sharepoint:{document_id}",
        "actor": "connector:test",
        "written_at": "2026-08-21T12:00:00+00:00",
        "write_id": "trusted-m365-write",
        "shape": "ne.connect.record@1",
    }
    return chunk.model_copy(
        update={"text": json.dumps(payload, sort_keys=True), "metadata": metadata}
    )


class _EvidenceParser:
    def __init__(self, blocks: list[dict[str, object]]) -> None:
        self._blocks = tuple(DocBlock.model_validate(block) for block in blocks)

    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument:
        return self._parsed(document_id=document_id, source_name=path.name)

    async def parse_fd(self, file_fd: int, *, document_id: str, source_name: str) -> ParsedDocument:
        del file_fd
        return self._parsed(document_id=document_id, source_name=source_name)

    def _parsed(self, *, document_id: str, source_name: str) -> ParsedDocument:
        return ParsedDocument(
            document_id=document_id,
            source_name=source_name,
            source_format="docx",
            blocks=self._blocks,
        )


def _producer_evidence(chunk: Chunk) -> ParsedDocumentEvidenceDescriptor:
    payload = json.loads(chunk.text)
    source_id = str(chunk.metadata["source_id"])
    record_id = str(chunk.metadata["record_id"])
    connector = M365GraphConnector(
        M365ConnectorSettings(
            tenant_id="test-tenant",
            client_id="test-client",
            auth_method="secret",
            client_secret=SecretStr("test-secret"),
            drive_ids=("test-drive",),
            owner="test-owner",
        ),
        parser=_EvidenceParser(payload["body_blocks"]),
    )
    parsed_payload = asyncio.run(
        connector._body_parser.parse(
            str(payload["body"]).encode(),
            document_id=source_id,
            record_id=record_id,
            source_format="docx",
            acl=chunk.acl,
        )
    )
    descriptor = connector.take_parsed_evidence(
        source_id,
        record_id,
        parsed_payload,
        chunk.acl,
        chunk.chunk_id,
    )
    assert descriptor is not None
    _consume_parsed_document_evidence(
        [
            (
                descriptor,
                chunk.chunk_id,
                source_id,
                record_id,
                chunk.acl,
                payload,
            )
        ]
    )
    return descriptor


def _acl_hash(acl: tuple[str, ...]) -> str:
    payload = json.dumps(
        {"acl": sorted(set(acl))},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _descriptor(*, dimensions: int = 12, logical_index: str = "tenant-index"):
    return vector_evidence_descriptor(
        backend="test-vector",
        logical_index=logical_index,
        model_id=f"test/embed-{dimensions}",
        dimensions=dimensions,
    )


def test_finalize_persists_exact_record_and_safe_vector_receipts() -> None:
    engine = sa.create_engine("sqlite://")
    store = SqlAlchemyDocumentLineageStore(engine)
    chunk = _chunk("governed body")
    descriptor = _descriptor()
    prepared = store.prepare(chunk, vector_evidence=descriptor)

    # Prepare captures evidence but does not claim a durable receipt.
    active = store.stage_evidence("document-1", 1, DocumentStage.EMBED, ("ops",))
    assert active is not None
    assert active.artifacts[0].state is DocumentEvidenceState.UNAVAILABLE
    assert active.artifacts[0].reason == "not_recorded"
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT count(*) FROM ne_document_evidence_receipts")) == 0

    store.finalize(
        prepared,
        audit_event_id="audit-vector-1",
        occurred_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )

    chunk_evidence = store.stage_evidence("document-1", 1, DocumentStage.CHUNK, ("ops",))
    assert chunk_evidence is not None
    assert chunk_evidence.event.audit_event_id == "audit-vector-1"
    assert len(chunk_evidence.artifacts) == 1
    record = chunk_evidence.artifacts[0]
    assert record.state is DocumentEvidenceState.AVAILABLE
    assert record.identifier == chunk.chunk_id
    assert record.size_bytes == len(chunk.text.encode("utf-8"))
    facts = {fact.key: fact for fact in chunk_evidence.facts}
    assert facts["chunk_identifiers"].label == "Record chunk ID"
    assert facts["chunk_identifiers"].value == chunk.chunk_id
    assert facts["chunk_sizes"].label == "UTF-8 payload size"
    assert facts["segmentation"].value == (
        "Semantic chunk reconstruction is unavailable for this version."
    )

    embed = store.stage_evidence("document-1", 1, DocumentStage.EMBED, ("ops",))
    assert embed is not None
    vector = embed.artifacts[0]
    assert vector.state is DocumentEvidenceState.AVAILABLE
    assert vector.artifact_count == 1
    assert vector.dimensions == descriptor.dimensions
    assert vector.model_id == descriptor.model_id
    assert vector.index_reference == descriptor.index_reference
    assert vector.fingerprint == descriptor.index_fingerprint
    assert "tenant-index" not in repr(vector)

    graph = store.stage_evidence("document-1", 1, DocumentStage.GRAPH, ("ops",))
    assert graph is not None
    assert graph.event.status == "skipped"
    assert graph.event.detail == "SKIPPED — no version-bound entities/relationships claimed"
    assert graph.artifacts == ()


def test_finalize_persists_audit_bound_bounded_numeric_preview() -> None:
    engine = sa.create_engine("sqlite://")
    store = SqlAlchemyDocumentLineageStore(engine)
    descriptor = submitted_embedding_evidence(
        _descriptor(dimensions=12),
        [float(index) / 10 for index in range(12)],
    )
    prepared = store.prepare(_chunk("preview body"), vector_evidence=descriptor)
    store.finalize(
        prepared,
        audit_event_id="audit-preview-1",
        occurred_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )

    evidence = store.stage_evidence("document-1", 1, DocumentStage.EMBED, ("ops",))
    assert evidence is not None
    assert evidence.event.audit_event_id == "audit-preview-1"
    assert evidence.artifacts[0].numeric_preview == tuple(float(index) / 10 for index in range(8))
    facts = {fact.key: fact for fact in evidence.facts}
    assert facts["numeric_preview"].available is True


def test_finalize_binds_real_structure_aware_semantic_chunks_to_exact_audit() -> None:
    engine = sa.create_engine("sqlite://")
    store = SqlAlchemyDocumentLineageStore(engine)
    body = "Disputes\n\nInvestigate the claim.\n\nThresholds\n\nAmount | Review\n500 | fast"
    chunk = _parsed_chunk(
        body=body,
        blocks=[
            {"kind": "heading", "text": "Disputes", "level": 1, "page": 1},
            {
                "kind": "paragraph",
                "text": "Investigate the claim.",
                "level": None,
                "page": 1,
            },
            {"kind": "heading", "text": "Thresholds", "level": 1, "page": 2},
            {
                "kind": "table",
                "text": "Amount | Review\n500 | fast",
                "level": None,
                "page": 2,
            },
        ],
    )
    prepared = store.prepare(
        chunk,
        vector_evidence=_descriptor(),
        parse_evidence=store.prepare_parsed_source_evidence(chunk, _producer_evidence(chunk)),
    )

    before = store.stage_evidence("document-1", 1, DocumentStage.CHUNK, ("ops",))
    assert before is not None
    assert before.semantic_chunks == ()

    store.finalize(
        prepared,
        audit_event_id="audit-semantic-1",
        occurred_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )

    evidence = store.stage_evidence("document-1", 1, DocumentStage.CHUNK, ("ops",))
    assert evidence is not None
    assert evidence.event.audit_event_id == "audit-semantic-1"
    assert [row.identifier for row in evidence.semantic_chunks] == [
        "document-1:0000",
        "document-1:0001",
    ]
    assert [row.size_bytes for row in evidence.semantic_chunks] == [
        len(b"Disputes\n\nInvestigate the claim."),
        len(b"Thresholds\n\nAmount | Review\n500 | fast"),
    ]
    facts = {fact.key: fact for fact in evidence.facts}
    assert facts["semantic_chunk_count"].value == 2
    assert facts["segmentation"].value == (
        "Deterministically reconstructed from exact parsed source blocks; "
        "the connector record remains the indexed T1 artifact."
    )

    parse = store.stage_evidence("document-1", 1, DocumentStage.PARSE, ("ops",))
    assert parse is not None
    parse_facts = {fact.key: fact for fact in parse.facts}
    assert parse_facts["parser"].value == "neutron-m365-document-parser/v1"
    assert parse_facts["block_count"].value == 4
    assert parse_facts["page_count"].value == 2
    assert parse_facts["table_count"].value == 1


def test_non_m365_lookalike_payload_cannot_claim_parser_or_semantic_evidence() -> None:
    engine = sa.create_engine("sqlite://")
    store = SqlAlchemyDocumentLineageStore(engine)
    source = _parsed_chunk(
        body="Heading\n\nBody",
        blocks=[
            {"kind": "heading", "text": "Heading", "level": 1, "page": 1},
            {"kind": "paragraph", "text": "Body", "level": None, "page": 1},
        ],
    )
    metadata = dict(source.metadata)
    metadata["connector"] = "non-m365"
    metadata["provenance"] = {
        **metadata["provenance"],
        "source": "connector:non-m365:document-1",
    }
    lookalike = source.model_copy(update={"metadata": metadata})

    prepared = store.prepare(lookalike)
    store.finalize(
        prepared,
        audit_event_id="audit-lookalike",
        occurred_at=datetime(2026, 8, 21, 12, 30, tzinfo=UTC),
    )

    semantic = store.stage_evidence("document-1", 1, DocumentStage.CHUNK, ("ops",))
    assert semantic is not None
    assert semantic.semantic_chunks[0].state is DocumentEvidenceState.UNAVAILABLE
    parsed = store.stage_evidence("document-1", 1, DocumentStage.PARSE, ("ops",))
    assert parsed is not None
    facts = {fact.key: fact for fact in parsed.facts}
    assert facts["parser"].available is False
    assert facts["block_count"].available is False


def test_non_m365_lookalike_cannot_attach_a_forged_m365_producer_descriptor() -> None:
    store = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"))
    source = _parsed_chunk(
        body="Heading\n\nBody",
        blocks=[
            {"kind": "heading", "text": "Heading", "level": 1, "page": 1},
            {"kind": "paragraph", "text": "Body", "level": None, "page": 1},
        ],
    )
    metadata = dict(source.metadata)
    metadata["connector"] = "non-m365"
    metadata["provenance"] = {
        **metadata["provenance"],
        "source": "connector:non-m365:document-1",
    }
    lookalike = source.model_copy(update={"metadata": metadata})

    with pytest.raises(DocumentLineageError, match="binding is invalid"):
        store.prepare_parsed_source_evidence(lookalike, _producer_evidence(lookalike))
    assert store.list_documents(("ops",)) == ()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda chunk: chunk.model_copy(
            update={"metadata": {**chunk.metadata, "source_id": "different-source"}}
        ),
        lambda chunk: chunk.model_copy(
            update={"metadata": {**chunk.metadata, "record_id": "different-record"}}
        ),
        lambda chunk: chunk.model_copy(update={"acl": ("different-acl",)}),
        lambda chunk: _mutate_parsed_payload(chunk, "body", "different body"),
        lambda chunk: _mutate_parsed_payload(chunk, "body_blocks", []),
        lambda chunk: _mutate_parsed_payload(chunk, "body_content_sha256", "0" * 64),
        lambda chunk: _mutate_parsed_payload(chunk, "body_schema_version", "unreviewed"),
        lambda chunk: _mutate_parsed_payload(chunk, "body_source_format", "pdf"),
        lambda chunk: _mutate_parsed_payload(chunk, "body_parser_identity", "lookalike"),
    ],
)
def test_parser_capability_binds_source_record_acl_and_exact_payload(mutation) -> None:
    store = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"))
    chunk = _parsed_chunk(
        body="Heading\n\nBody",
        blocks=[
            {"kind": "heading", "text": "Heading", "level": 1, "page": 1},
            {"kind": "paragraph", "text": "Body", "level": None, "page": 1},
        ],
    )
    descriptor = _producer_evidence(chunk)
    mutated = mutation(chunk)

    with pytest.raises(DocumentLineageError, match="binding is invalid"):
        store.prepare_parsed_source_evidence(mutated, descriptor)
    assert store.list_documents(("ops",)) == ()


def _mutate_parsed_payload(chunk: Chunk, key: str, value: object) -> Chunk:
    payload = json.loads(chunk.text)
    payload[key] = value
    return chunk.model_copy(update={"text": json.dumps(payload, sort_keys=True)})


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("EVIDENCE_REVISION", "unreviewed-chunker"),
        ("DEFAULT_MAX_CHARS", StructuralChunker.DEFAULT_MAX_CHARS + 1),
        ("DEFAULT_MIN_CHARS", StructuralChunker.DEFAULT_MIN_CHARS + 1),
        (
            "DEFAULT_INCLUDE_HEADINGS_IN_TEXT",
            not StructuralChunker.DEFAULT_INCLUDE_HEADINGS_IN_TEXT,
        ),
    ],
)
def test_parser_capability_binds_chunker_revision_and_parameters(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    value: object,
) -> None:
    store = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"))
    chunk = _parsed_chunk(
        body="Heading\n\nBody",
        blocks=[
            {"kind": "heading", "text": "Heading", "level": 1, "page": 1},
            {"kind": "paragraph", "text": "Body", "level": None, "page": 1},
        ],
    )
    descriptor = _producer_evidence(chunk)
    monkeypatch.setattr(StructuralChunker, attribute, value)

    with pytest.raises(DocumentLineageError, match="binding is invalid"):
        store.prepare_parsed_source_evidence(chunk, descriptor)


def test_generic_m365_lookalike_without_producer_descriptor_cannot_claim_evidence() -> None:
    store = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"))
    forged = _parsed_chunk(
        body="Heading\n\nBody",
        blocks=[
            {"kind": "heading", "text": "Heading", "level": 1, "page": 1},
            {"kind": "paragraph", "text": "Body", "level": None, "page": 1},
        ],
    )
    prepared = store.prepare(forged)
    store.finalize(
        prepared,
        audit_event_id="audit-forged-m365",
        occurred_at=datetime(2026, 8, 21, 12, 45, tzinfo=UTC),
    )
    parsed = store.stage_evidence("document-1", 1, DocumentStage.PARSE, ("ops",))
    semantic = store.stage_evidence("document-1", 1, DocumentStage.CHUNK, ("ops",))
    assert parsed is not None
    assert all(not fact.available for fact in parsed.facts if fact.key == "parser")
    assert semantic is not None
    assert semantic.semantic_chunks[0].state is DocumentEvidenceState.UNAVAILABLE


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("body_parser_identity"),
        lambda payload: payload.__setitem__("body", "mismatched rendered body"),
        lambda payload: payload.__setitem__("body_block_count", 999),
    ],
)
def test_malformed_parsed_source_never_claims_semantic_or_parser_evidence(mutation) -> None:
    engine = sa.create_engine("sqlite://")
    store = SqlAlchemyDocumentLineageStore(engine)
    chunk = _parsed_chunk(
        body="Heading\n\nBody",
        blocks=[
            {"kind": "heading", "text": "Heading", "level": 1, "page": 1},
            {"kind": "paragraph", "text": "Body", "level": None, "page": 1},
        ],
    )
    payload = json.loads(chunk.text)
    mutation(payload)
    malformed = chunk.model_copy(update={"text": json.dumps(payload, sort_keys=True)})

    prepared = store.prepare(malformed)
    store.finalize(
        prepared,
        audit_event_id="audit-malformed",
        occurred_at=datetime(2026, 8, 21, 13, tzinfo=UTC),
    )

    semantic = store.stage_evidence("document-1", 1, DocumentStage.CHUNK, ("ops",))
    assert semantic is not None
    assert len(semantic.semantic_chunks) == 1
    assert semantic.semantic_chunks[0].state is DocumentEvidenceState.UNAVAILABLE
    assert semantic.semantic_chunks[0].reason == "semantic_chunks_not_recorded"
    parsed = store.stage_evidence("document-1", 1, DocumentStage.PARSE, ("ops",))
    assert parsed is not None
    facts = {fact.key: fact for fact in parsed.facts}
    assert facts["parser"].available is False
    assert facts["block_count"].available is False


def test_same_content_reembed_appends_audit_receipt_without_new_version() -> None:
    engine = sa.create_engine("sqlite://")
    store = SqlAlchemyDocumentLineageStore(engine)
    at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    chunk = _chunk("same bytes")

    first = store.prepare(chunk, vector_evidence=_descriptor(dimensions=8))
    store.finalize(first, audit_event_id="audit-1", occurred_at=at)
    repeated = store.prepare(chunk, vector_evidence=_descriptor(dimensions=16))
    assert repeated.creates_version is False
    assert repeated.version_seq == 1
    store.finalize(
        repeated,
        audit_event_id="audit-2",
        occurred_at=at + timedelta(minutes=1),
    )

    journey = store.journey("document-1", ("ops",))
    assert journey is not None
    assert [version.version_seq for version in journey.versions] == [1]
    embed = store.stage_evidence("document-1", 1, DocumentStage.EMBED, ("ops",))
    assert embed is not None
    assert embed.event.audit_event_id == "audit-2"
    assert embed.artifacts[0].dimensions == 16
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT audit_event_id FROM ne_document_evidence_receipts "
                "WHERE stage = 'embed' ORDER BY occurred_at"
            )
        ).scalars()
        assert list(rows) == ["audit-1", "audit-2"]


def test_abort_and_hidden_historical_read_never_expose_or_query_receipts() -> None:
    engine = sa.create_engine("sqlite://")
    store = SqlAlchemyDocumentLineageStore(engine)
    pending = store.prepare(_chunk("aborted"), vector_evidence=_descriptor())
    store.abort(pending)
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT count(*) FROM ne_document_evidence_receipts")) == 0

    committed = store.prepare(_chunk("restricted", ("secret",)), vector_evidence=_descriptor())
    store.finalize(
        committed,
        audit_event_id="audit-secret",
        occurred_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )
    statements: list[str] = []

    def _observe(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _observe)
    try:
        assert store.stage_evidence("document-1", 1, DocumentStage.EMBED, ("ops",)) is None
        assert store.stage_evidence("document-1", 1, DocumentStage.PARSE, ("ops",)) is None
    finally:
        event.remove(engine, "before_cursor_execute", _observe)
    assert not any("ne_document_evidence_receipts" in statement for statement in statements)
    assert not any("ne_document_parse_evidence_receipts" in statement for statement in statements)


def _entity(
    *,
    source_ref: str,
    snapshot_hash: str,
    key: str,
    display_name: str,
    acl: tuple[str, ...] = ("ops",),
) -> GraphEvidenceEntity:
    return GraphEvidenceEntity(
        source_system="m365-sharepoint",
        source_ref=source_ref,
        source_snapshot_hash=snapshot_hash,
        source_acl_hash=_acl_hash(acl),
        acl=acl,
        key=key,
        entity_class="Document",
        display_name=display_name,
    )


def test_graph_receipts_require_exact_snapshot_and_both_endpoint_acls() -> None:
    engine = sa.create_engine("sqlite://")
    store = SqlAlchemyDocumentLineageStore(engine)
    at = datetime(2026, 8, 21, 12, tzinfo=UTC)
    snapshot_a = "a" * 64
    snapshot_b = "b" * 64
    ref_a = "drive-a:item-a:item-a"
    ref_b = "drive-a:item-b:item-b"

    for index, (document_id, source_ref, snapshot_hash, acl) in enumerate(
        (
            ("document-a", ref_a, snapshot_a, ("ops",)),
            ("document-b", ref_b, snapshot_b, ("finance",)),
        )
    ):
        prepared = store.prepare(
            _chunk(
                f"body-{index}",
                acl,
                document_id=document_id,
                source_ref=source_ref,
                source_snapshot_hash=snapshot_hash,
            )
        )
        store.finalize(
            prepared,
            audit_event_id=f"knowledge-{index}",
            occurred_at=at + timedelta(minutes=index),
        )

    subject = _entity(
        source_ref=ref_a,
        snapshot_hash=snapshot_a,
        key="entity-a",
        display_name="Procedure A",
    )
    obj = _entity(
        source_ref=ref_b,
        snapshot_hash=snapshot_b,
        key="entity-b",
        display_name="Procedure B",
        acl=("finance",),
    )
    batch = GraphEvidenceBatch(
        audit_event_id="graph-audit-1",
        occurred_at=at + timedelta(minutes=2),
        entities=(subject, obj),
        relationships=(
            GraphEvidenceRelationship(
                key="relationship-1",
                edge_kind="REFERENCES",
                subject=subject,
                object=obj,
            ),
        ),
    )
    bound = store.bind_graph_evidence(batch)
    assert bound.entities_bound == 2
    assert bound.relationships_bound == 1
    assert bound.entities_unbound == 0

    ops_only = store.graph_evidence("document-a", 1, ("ops",))
    assert ops_only is not None
    assert [entity.key for entity in ops_only.entities] == ["entity-a"]
    assert ops_only.relationships == ()

    object_current_ops = store.prepare(
        _chunk(
            "body-1",
            ("ops",),
            document_id="document-b",
            source_ref="drive-a:item-b-current:item-b-current",
            source_snapshot_hash="c" * 64,
        )
    )
    assert object_current_ops.version_seq == 1
    store.finalize(
        object_current_ops,
        audit_event_id="knowledge-object-current-ops",
        occurred_at=at + timedelta(minutes=3),
    )
    immutable_acl_still_hides = store.graph_evidence("document-a", 1, ("ops",))
    assert immutable_acl_still_hides is not None
    assert immutable_acl_still_hides.relationships == ()

    both = store.graph_evidence("document-a", 1, ("ops", "finance"))
    assert both is not None
    assert [relationship.key for relationship in both.relationships] == ["relationship-1"]
    assert both.relationships[0].object.source_snapshot_hash == snapshot_b
    assert store.graph_evidence("document-a", 1, ("unrelated",)) is None

    store.bind_graph_evidence(
        GraphEvidenceBatch(
            audit_event_id="graph-older-delayed",
            occurred_at=at + timedelta(minutes=1),
            entities=(subject,),
            relationships=(),
        )
    )
    current = store.graph_evidence("document-a", 1, ("ops", "finance"))
    assert current is not None
    assert current.audit_event_id == "graph-audit-1"
    assert [relationship.key for relationship in current.relationships] == ["relationship-1"]


def test_graph_receipts_never_join_wrong_or_legacy_snapshot_to_current_state() -> None:
    engine = sa.create_engine("sqlite://")
    store = SqlAlchemyDocumentLineageStore(engine)
    at = datetime(2026, 8, 21, 12, tzinfo=UTC)
    source_ref = "drive-a:item-a:item-a"
    snapshot_hash = "c" * 64
    prepared = store.prepare(
        _chunk(
            "bound",
            document_id="bound-document",
            source_ref=source_ref,
            source_snapshot_hash=snapshot_hash,
        )
    )
    store.finalize(prepared, audit_event_id="knowledge-bound", occurred_at=at)
    legacy = store.prepare(_chunk("legacy", document_id="legacy-document"))
    store.finalize(legacy, audit_event_id="knowledge-legacy", occurred_at=at)
    aborted_ref = "drive-a:aborted:aborted"
    aborted_hash = "f" * 64
    aborted = store.prepare(
        _chunk(
            "aborted",
            document_id="aborted-document",
            source_ref=aborted_ref,
            source_snapshot_hash=aborted_hash,
        )
    )
    store.abort(aborted)

    wrong = _entity(
        source_ref=source_ref,
        snapshot_hash="d" * 64,
        key="wrong",
        display_name="Wrong",
    )
    wrong_acl = _entity(
        source_ref=source_ref,
        snapshot_hash=snapshot_hash,
        key="wrong-acl",
        display_name="Wrong ACL",
        acl=("finance",),
    )
    legacy_entity = _entity(
        source_ref="connector:m365-sharepoint:legacy-document",
        snapshot_hash="e" * 64,
        key="legacy",
        display_name="Legacy",
    )
    aborted_entity = _entity(
        source_ref=aborted_ref,
        snapshot_hash=aborted_hash,
        key="aborted",
        display_name="Aborted",
    )
    result = store.bind_graph_evidence(
        GraphEvidenceBatch(
            audit_event_id="graph-unbound",
            occurred_at=at,
            entities=(wrong, wrong_acl, legacy_entity, aborted_entity),
            relationships=(),
        )
    )
    assert result.entities_bound == 0
    assert result.entities_unbound == 4
    assert store.graph_evidence("bound-document", 1, ("ops",)) is None
    assert store.graph_evidence("legacy-document", 1, ("ops",)) is None


def test_graph_entity_requires_current_and_immutable_receipt_acl_and_exact_source_tuple() -> None:
    engine = sa.create_engine("sqlite://")
    store = SqlAlchemyDocumentLineageStore(engine)
    at = datetime(2026, 8, 21, 12, tzinfo=UTC)
    old_ref = "drive-a:item-old:item-old"
    old_snapshot = "1" * 64
    first = store.prepare(
        _chunk(
            "same content",
            ("ops",),
            document_id="acl-refresh-document",
            source_ref=old_ref,
            source_snapshot_hash=old_snapshot,
        )
    )
    store.finalize(first, audit_event_id="knowledge-old", occurred_at=at)
    old_entity = _entity(
        source_ref=old_ref,
        snapshot_hash=old_snapshot,
        key="old-entity",
        display_name="Old Entity",
        acl=("ops",),
    )
    store.bind_graph_evidence(
        GraphEvidenceBatch(
            audit_event_id="graph-old",
            occurred_at=at + timedelta(minutes=1),
            entities=(old_entity,),
            relationships=(),
        )
    )

    expanded = store.prepare(
        _chunk(
            "same content",
            ("ops", "finance"),
            document_id="acl-refresh-document",
            source_ref="drive-a:item-new:item-new",
            source_snapshot_hash="2" * 64,
        )
    )
    assert expanded.version_seq == 1
    assert expanded.creates_version is False
    store.finalize(
        expanded,
        audit_event_id="knowledge-expanded",
        occurred_at=at + timedelta(minutes=2),
    )

    ops = store.graph_evidence("acl-refresh-document", 1, ("ops",))
    assert ops is not None
    assert len(ops.entities) == 1
    assert ops.entities[0].source_ref == old_ref
    assert ops.entities[0].source_snapshot_hash == old_snapshot
    assert ops.entities[0].acl == ("ops",)
    finance = store.graph_evidence("acl-refresh-document", 1, ("finance",))
    assert finance is not None
    assert finance.entities == ()

    narrowed = store.prepare(
        _chunk(
            "same content",
            ("finance",),
            document_id="acl-refresh-document",
            source_ref="drive-a:item-finance:item-finance",
            source_snapshot_hash="3" * 64,
        )
    )
    store.finalize(
        narrowed,
        audit_event_id="knowledge-finance",
        occurred_at=at + timedelta(minutes=3),
    )
    assert store.graph_evidence("acl-refresh-document", 1, ("ops",)) is None
    finance_only = store.graph_evidence("acl-refresh-document", 1, ("finance",))
    assert finance_only is not None
    assert finance_only.entities == ()


def test_graph_relationship_requires_current_endpoint_acl_and_restores_after_regrant() -> None:
    engine = sa.create_engine("sqlite://")
    store = SqlAlchemyDocumentLineageStore(engine)
    at = datetime(2026, 8, 21, 12, tzinfo=UTC)
    subject = _entity(
        source_ref="drive-a:subject:subject",
        snapshot_hash="4" * 64,
        key="subject",
        display_name="Subject",
    )
    obj = _entity(
        source_ref="drive-a:object:object",
        snapshot_hash="5" * 64,
        key="object",
        display_name="Object",
    )
    for document_id, entity in (("subject-document", subject), ("object-document", obj)):
        prepared = store.prepare(
            _chunk(
                "same content",
                document_id=document_id,
                source_ref=entity.source_ref,
                source_snapshot_hash=entity.source_snapshot_hash,
            )
        )
        store.finalize(prepared, audit_event_id=f"t1-{document_id}", occurred_at=at)
    store.bind_graph_evidence(
        GraphEvidenceBatch(
            audit_event_id="graph-edge",
            occurred_at=at + timedelta(minutes=1),
            entities=(subject, obj),
            relationships=(
                GraphEvidenceRelationship(
                    key="subject-object",
                    edge_kind="REFERENCES",
                    subject=subject,
                    object=obj,
                ),
            ),
        )
    )
    initial = store.graph_evidence("subject-document", 1, ("ops",))
    assert initial is not None
    assert [relationship.key for relationship in initial.relationships] == ["subject-object"]

    revoked = store.prepare(
        _chunk(
            "same content",
            ("finance",),
            document_id="object-document",
            source_ref="drive-a:object-finance:object-finance",
            source_snapshot_hash="6" * 64,
        )
    )
    assert revoked.version_seq == 1
    store.finalize(
        revoked, audit_event_id="t1-object-revoked", occurred_at=at + timedelta(minutes=2)
    )
    hidden = store.graph_evidence("subject-document", 1, ("ops",))
    assert hidden is not None
    assert hidden.relationships == ()

    regranted = store.prepare(
        _chunk(
            "same content",
            document_id="object-document",
            source_ref="drive-a:object-regranted:object-regranted",
            source_snapshot_hash="7" * 64,
        )
    )
    assert regranted.version_seq == 1
    store.finalize(
        regranted,
        audit_event_id="t1-object-regranted",
        occurred_at=at + timedelta(minutes=3),
    )
    restored = store.graph_evidence("subject-document", 1, ("ops",))
    assert restored is not None
    assert [relationship.key for relationship in restored.relationships] == ["subject-object"]


def test_graph_receipt_retry_adds_relationship_after_target_t1_binding() -> None:
    engine = sa.create_engine("sqlite://")
    store = SqlAlchemyDocumentLineageStore(engine)
    at = datetime(2026, 8, 21, 12, tzinfo=UTC)
    subject = _entity(
        source_ref="drive-a:subject:subject",
        snapshot_hash="1" * 64,
        key="subject",
        display_name="Subject",
    )
    obj = _entity(
        source_ref="drive-a:object:object",
        snapshot_hash="2" * 64,
        key="object",
        display_name="Object",
        acl=("finance",),
    )
    subject_prepared = store.prepare(
        _chunk(
            "subject",
            document_id="subject-document",
            source_ref=subject.source_ref,
            source_snapshot_hash=subject.source_snapshot_hash,
        )
    )
    store.finalize(subject_prepared, audit_event_id="t1-subject", occurred_at=at)
    batch = GraphEvidenceBatch(
        audit_event_id="graph-partial",
        occurred_at=at + timedelta(minutes=1),
        entities=(subject,),
        relationships=(
            GraphEvidenceRelationship(
                key="late-target",
                edge_kind="REFERENCES",
                subject=subject,
                object=obj,
            ),
        ),
    )

    first = store.bind_graph_evidence(batch)
    assert first.entities_bound == 1
    assert first.relationships_unbound == 1
    first_view = store.graph_evidence("subject-document", 1, ("ops", "finance"))
    assert first_view is not None
    assert first_view.relationships == ()

    object_prepared = store.prepare(
        _chunk(
            "object",
            ("finance",),
            document_id="object-document",
            source_ref=obj.source_ref,
            source_snapshot_hash=obj.source_snapshot_hash,
        )
    )
    store.finalize(object_prepared, audit_event_id="t1-object", occurred_at=at)
    second = store.bind_graph_evidence(batch)
    assert second.relationships_bound == 1
    assert second.relationships_unbound == 0
    second_view = store.graph_evidence("subject-document", 1, ("ops", "finance"))
    assert second_view is not None
    assert [relationship.key for relationship in second_view.relationships] == ["late-target"]
