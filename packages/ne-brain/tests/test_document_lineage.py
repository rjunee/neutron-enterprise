# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""BD-3 hash gating, as-of, diff, active-stage, and historical ACL proof."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from ne.audit.store import ne_audit_log
from ne.brain.ingest.contracts import BlockKind, DocBlock, ParsedDocument
from ne.brain.lineage import (
    DocumentComparisonState,
    DocumentCursorError,
    DocumentLineageError,
    DocumentStage,
    DocumentType,
    SqlAlchemyDocumentLineageStore,
    ensure_document_lineage_schema,
)
from ne.brain.write import CuratedKnowledgeItem, ProvenanceClass, ShapeContract
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.connector import M365GraphConnector
from ne.contracts.types import Chunk

from .conftest import WriteStack


def test_schema_upgrade_adds_nullable_numeric_preview_to_legacy_receipts() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE ne_document_evidence_receipts ("
                "document_id VARCHAR NOT NULL, version_seq INTEGER NOT NULL, "
                "stage VARCHAR NOT NULL, audit_event_id VARCHAR NOT NULL, "
                "kind VARCHAR NOT NULL, ordinal INTEGER NOT NULL, state VARCHAR NOT NULL, "
                "identifier VARCHAR, size_bytes INTEGER, artifact_count INTEGER, "
                "dimensions INTEGER, model_id VARCHAR, index_fingerprint VARCHAR(64), "
                "occurred_at DATETIME NOT NULL, "
                "PRIMARY KEY (document_id, version_seq, stage, audit_event_id, kind, ordinal))"
            )
        )

    ensure_document_lineage_schema(engine)

    columns = {
        column["name"] for column in sa.inspect(engine).get_columns("ne_document_evidence_receipts")
    }
    assert "numeric_preview" in columns


def test_malformed_optional_receipts_suppress_whole_batch_preview_without_losing_data(
    write_stack: Callable[..., WriteStack],
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lineage = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"))
    stack = write_stack(lineage=lineage)
    original = stack.store.upsert_chunks_with_settlement

    async def malformed(chunks):  # type: ignore[no-untyped-def]
        outcome = await original(chunks)
        return replace(outcome, receipts=())

    monkeypatch.setattr(stack.store, "upsert_chunks_with_settlement", malformed)
    receipt = asyncio.run(
        stack.writer.upsert(
            [
                CuratedKnowledgeItem(
                    item_id="safe-preview",
                    content=json.dumps({"body": "body", "name": "Safe.docx"}),
                    provenance="source",
                    acl=("ops",),
                )
            ]
        )
    )

    assert receipt.accepted == ("safe-preview",)
    assert json.loads((asyncio.run(stack.store.get(["safe-preview"])))[0].text)["body"] == "body"
    evidence = lineage.stage_evidence("safe-preview", 1, DocumentStage.EMBED, ("ops",))
    assert evidence is not None
    assert evidence.artifacts[0].state.value == "available"
    assert evidence.artifacts[0].numeric_preview is None


def _chunk(
    body: str,
    acl: tuple[str, ...],
    *,
    name: str = "Servicing SOP.docx",
    document_id: str = "doc-1",
    source_system: str = "m365-sharepoint",
) -> Chunk:
    payload = {
        "body": body,
        "body_content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "name": name,
    }
    return Chunk(
        chunk_id=document_id,
        document_id=document_id,
        text=json.dumps(payload, sort_keys=True),
        metadata={
            "connector": source_system,
            "record_id": document_id,
            "provenance": {"source": f"connector:{source_system}:{document_id}"},
        },
        acl=acl,
    )


class _EchoEvidenceParser:
    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument:
        return self._parsed(path.read_text(), document_id=document_id, source_name=path.name)

    async def parse_fd(self, file_fd: int, *, document_id: str, source_name: str) -> ParsedDocument:
        import os

        os.lseek(file_fd, 0, os.SEEK_SET)
        body = os.read(file_fd, 1024 * 1024).decode()
        return self._parsed(body, document_id=document_id, source_name=source_name)

    @staticmethod
    def _parsed(body: str, *, document_id: str, source_name: str) -> ParsedDocument:
        return ParsedDocument(
            document_id=document_id,
            source_name=source_name,
            source_format="docx",
            blocks=(DocBlock(kind=BlockKind.PARAGRAPH, text=body),),
        )


def _evidence_connector() -> M365GraphConnector:
    return M365GraphConnector(
        M365ConnectorSettings(
            tenant_id="test-tenant",
            client_id="test-client",
            auth_method="secret",
            client_secret=SecretStr("test-secret"),
            drive_ids=("test-drive",),
            owner="test-owner",
        ),
        parser=_EchoEvidenceParser(),
    )


def _finalize(
    store: SqlAlchemyDocumentLineageStore,
    chunk: Chunk,
    *,
    occurred_at: datetime,
) -> None:
    prepared = store.prepare(chunk)
    store.finalize(
        prepared,
        audit_event_id=f"audit-{chunk.document_id}-{prepared.version_seq}",
        occurred_at=occurred_at,
    )


def test_hash_gated_versions_diff_as_of_and_live_stage() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    clock_value = [now]
    store = SqlAlchemyDocumentLineageStore(
        sa.create_engine("sqlite://"), clock=lambda: clock_value[0]
    )

    first = store.prepare(_chunk("step one", ("ops",)))
    active = store.journey("doc-1", ("ops",))
    assert active is not None
    assert active.selected.status == "indexing"
    assert next(s for s in active.selected.stages if s.stage is DocumentStage.EMBED).status == (
        "active"
    )

    store.finalize(first, audit_event_id="audit-v1", occurred_at=now)
    # Identical source bytes with an ACL refresh are the same content version.
    same = store.prepare(_chunk("step one", ("ops", "risk")))
    assert same.creates_version is False
    store.finalize(same, audit_event_id="audit-repeat", occurred_at=now + timedelta(seconds=5))

    clock_value[0] = now + timedelta(minutes=1)
    second = store.prepare(_chunk("step one\nstep two", ("ops", "risk")))
    store.finalize(second, audit_event_id="audit-v2", occurred_at=clock_value[0])

    latest = store.journey("doc-1", ("ops",))
    assert latest is not None
    assert [version.version_seq for version in latest.versions] == [1, 2]
    assert latest.selected.version_seq == 2
    assert latest.selected.document_type is DocumentType.DOCUMENT
    assert latest.comparison.state is DocumentComparisonState.AVAILABLE
    assert latest.comparison.predecessor_version_seq == 1
    assert latest.versions[0].superseded_at == clock_value[0]
    assert "--- v1" in (latest.diff or "")
    assert "+step two" in (latest.diff or "")

    historical = store.journey("doc-1", ("ops",), as_of=now + timedelta(seconds=30))
    assert historical is not None
    assert historical.selected.version_seq == 1
    assert historical.selected.content == "step one"


def test_historical_acl_is_checked_per_version_and_never_diffed_across_denial() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    store = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"), clock=lambda: now)
    first = store.prepare(_chunk("old restricted body", ("old-team",)))
    store.finalize(first, audit_event_id="audit-v1", occurred_at=now)
    second = store.prepare(_chunk("new restricted body", ("new-team",)))
    store.finalize(second, audit_event_id="audit-v2", occurred_at=now + timedelta(minutes=1))

    # A current-version reader cannot receive the prior body's diff.
    current = store.journey("doc-1", ("new-team",))
    assert current is not None
    assert [version.version_seq for version in current.versions] == [2]
    assert current.diff is None
    assert current.comparison.state is DocumentComparisonState.PREDECESSOR_HIDDEN
    assert current.comparison.predecessor_version_seq is None

    # The old principal can retrieve v1 as-of, but cannot observe the new current.
    assert store.journey("doc-1", ("old-team",)) is None
    historical = store.journey("doc-1", ("old-team",), as_of=now + timedelta(seconds=30))
    assert historical is not None
    assert historical.selected.content == "old restricted body"
    assert store.journey("doc-1", ("neither",), as_of=now) is None


def test_failed_write_aborts_prepared_version_without_phantom_history() -> None:
    store = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"))
    pending = store.prepare(_chunk("not indexed", ("ops",)))
    store.abort(pending)
    assert store.list_documents(("ops",)) == ()
    assert store.journey("doc-1", ("ops",)) is None


def test_resumed_pending_version_survives_a_later_retry_failure() -> None:
    store = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"))
    original = store.prepare(_chunk("durable but not finalized", ("ops",)))

    resumed = store.prepare(_chunk("durable but not finalized", ("ops",)))
    assert resumed.version_seq == original.version_seq
    assert resumed.created_now is False
    store.abort(resumed)

    pending = store.journey("doc-1", ("ops",))
    assert pending is not None
    assert pending.selected.status == "indexing"

    with pytest.raises(DocumentLineageError, match="different hash"):
        store.prepare(_chunk("newer bytes", ("ops",)))


def test_failed_identical_content_refresh_does_not_mutate_current_acl() -> None:
    store = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"))
    first = store.prepare(_chunk("indexed", ("ops",)))
    store.finalize(first, audit_event_id="audit-v1", occurred_at=datetime.now(UTC))

    failed_refresh = store.prepare(_chunk("indexed", ("risk",)))
    store.abort(failed_refresh)

    assert store.journey("doc-1", ("ops",)) is not None
    assert store.journey("doc-1", ("risk",)) is None


def test_document_page_filters_before_seek_pagination_and_never_counts_hidden_rows() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    store = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"), clock=lambda: now)
    _finalize(
        store,
        _chunk("alpha", ("ops",), name="Alpha SOP.DOCX", document_id="doc-a"),
        occurred_at=now,
    )
    _finalize(
        store,
        _chunk(
            "beta",
            ("ops",),
            name="Beta SOP.pdf",
            document_id="doc-b",
            source_system="google-drive",
        ),
        occurred_at=now,
    )
    _finalize(
        store,
        _chunk("gamma", ("ops",), name="Gamma notes.md", document_id="doc-c"),
        occurred_at=now,
    )
    _finalize(
        store,
        _chunk(
            "hidden",
            ("secret",),
            name="Hidden SOP.pptx",
            document_id="doc-hidden",
            source_system="secret-source",
        ),
        occurred_at=now,
    )

    first = store.list_document_page(("ops",), query="  sOp ", limit=1)
    assert [row.document_id for row in first.documents] == ["doc-a"]
    assert first.next_cursor is not None
    assert first.document_types == (
        DocumentType.PDF,
        DocumentType.DOCUMENT,
        DocumentType.TEXT,
    )
    assert first.source_systems == ("google-drive", "m365-sharepoint")

    second = store.list_document_page(("ops",), query="sop", cursor=first.next_cursor, limit=1)
    assert [row.document_id for row in second.documents] == ["doc-b"]
    assert second.next_cursor is None

    filtered = store.list_document_page(
        ("ops",),
        query="SOP",
        document_type=DocumentType.DOCUMENT,
        source_system="M365-SHAREPOINT",
    )
    assert [(row.document_id, row.document_type) for row in filtered.documents] == [
        ("doc-a", DocumentType.DOCUMENT)
    ]


def test_include_structured_filter_is_interim_negraph_suffix_and_fingerprint_bound() -> None:
    """INTERIM (PD-DOC-TITLES): the operational-graph-records filter infers
    structured-ness from the ".negraph.json" connector filename convention.
    REMOVE this test alongside the interim filter (lineage.py's
    ``include_structured`` predicate, ``operational_graph_count``, the ne-api
    ``structured`` query param, and the console's "Include operational graph
    records" toggle) when the real ``structured`` flag lands on the document
    wire.

    Default (``include_structured=False``) EXCLUDES operational-graph rows —
    the directory's default is prose documents only. ``True`` is the explicit
    opt-in that puts them back in the page.
    """
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    store = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"), clock=lambda: now)
    _finalize(
        store,
        _chunk(
            "graph body one",
            ("ops",),
            name="graph-Customer-1.negraph.json",
            document_id="doc-structured-1",
        ),
        occurred_at=now,
    )
    _finalize(
        store,
        _chunk(
            "graph body two",
            ("ops",),
            name="graph-AutoLoan-2.negraph.json",
            document_id="doc-structured-2",
        ),
        occurred_at=now,
    )
    _finalize(
        store,
        _chunk("prose body", ("ops",), name="Servicing SOP.docx", document_id="doc-prose"),
        occurred_at=now,
    )

    excluded = store.list_document_page(("ops",))
    assert {row.document_id for row in excluded.documents} == {"doc-prose"}
    # Control: the honest corpus-wide count still reports both graph rows even
    # though the default page excludes them.
    assert excluded.operational_graph_count == 2

    included = store.list_document_page(("ops",), include_structured=True)
    assert {row.document_id for row in included.documents} == {
        "doc-structured-1",
        "doc-structured-2",
        "doc-prose",
    }
    assert included.operational_graph_count == 2

    first = store.list_document_page(("ops",), include_structured=True, limit=2)
    assert len(first.documents) == 2
    assert first.next_cursor is not None
    second = store.list_document_page(
        ("ops",), include_structured=True, cursor=first.next_cursor, limit=2
    )
    assert len(second.documents) == 1
    assert second.next_cursor is None
    assert {row.document_id for row in (*first.documents, *second.documents)} == {
        "doc-structured-1",
        "doc-structured-2",
        "doc-prose",
    }

    # The cursor is bound to include_structured exactly like the other
    # filters: reusing one minted under a different value fails closed.
    with pytest.raises(DocumentCursorError, match="invalid document cursor"):
        store.list_document_page(("ops",), include_structured=False, cursor=first.next_cursor)


def test_document_page_cursor_is_filter_bound_and_malformed_values_fail_closed() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    store = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"), clock=lambda: now)
    for document_id in ("doc-a", "doc-b"):
        _finalize(
            store,
            _chunk("body", ("ops",), document_id=document_id, name=f"{document_id}.pdf"),
            occurred_at=now,
        )

    page = store.list_document_page(("ops",), query="doc", limit=1)
    assert page.next_cursor is not None
    with pytest.raises(DocumentCursorError, match="invalid document cursor"):
        store.list_document_page(("ops",), query="different", cursor=page.next_cursor)
    with pytest.raises(DocumentCursorError, match="invalid document cursor"):
        store.list_document_page(("ops",), cursor="not a cursor")


def test_hidden_newer_indexing_version_does_not_suppress_visible_current_version() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    clock_value = [now]
    store = SqlAlchemyDocumentLineageStore(
        sa.create_engine("sqlite://"), clock=lambda: clock_value[0]
    )
    _finalize(store, _chunk("visible", ("ops",)), occurred_at=now)
    clock_value[0] += timedelta(minutes=1)
    store.prepare(_chunk("hidden indexing", ("secret",)))

    page = store.list_document_page(("ops",))
    assert [(row.version_seq, row.indexing) for row in page.documents] == [(1, False)]


def test_journey_distinguishes_first_visible_hidden_and_unchanged_predecessors() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    clock_value = [now]
    store = SqlAlchemyDocumentLineageStore(
        sa.create_engine("sqlite://"), clock=lambda: clock_value[0]
    )
    _finalize(store, _chunk("line", ("ops",)), occurred_at=now)
    first = store.journey("doc-1", ("ops",), version_seq=1)
    assert first is not None
    assert first.comparison.state is DocumentComparisonState.FIRST_VERSION

    clock_value[0] += timedelta(minutes=1)
    _finalize(store, _chunk("line\n", ("ops",)), occurred_at=clock_value[0])
    unchanged = store.journey("doc-1", ("ops",), version_seq=2)
    assert unchanged is not None
    assert unchanged.diff == ""
    assert unchanged.comparison.state is DocumentComparisonState.UNCHANGED
    assert unchanged.comparison.predecessor_version_seq == 1

    clock_value[0] += timedelta(minutes=1)
    _finalize(store, _chunk("line\nnew", ("risk",)), occurred_at=clock_value[0])
    hidden = store.journey("doc-1", ("risk",))
    assert hidden is not None
    assert hidden.comparison.state is DocumentComparisonState.PREDECESSOR_HIDDEN
    assert hidden.comparison.predecessor_version_seq is None


def test_stage_evidence_is_exact_version_acl_bound_and_body_hash_enriched() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    clock_value = [now]
    store = SqlAlchemyDocumentLineageStore(
        sa.create_engine("sqlite://"), clock=lambda: clock_value[0]
    )
    _finalize(store, _chunk("old body", ("old-team",)), occurred_at=now)
    clock_value[0] += timedelta(minutes=1)
    _finalize(store, _chunk("new body", ("new-team",)), occurred_at=clock_value[0])
    _finalize(
        store,
        _chunk("foreign", ("new-team",), document_id="doc-2", name="Foreign.pdf"),
        occurred_at=clock_value[0],
    )

    evidence = store.stage_evidence("doc-1", 2, DocumentStage.EMBED, ("new-team",))
    assert evidence is not None
    assert evidence.document_id == "doc-1"
    assert evidence.version_seq == 2
    assert evidence.content_hash == hashlib.sha256(b"new body").hexdigest()
    assert evidence.event.stage is DocumentStage.EMBED
    assert evidence.event.status == "complete"
    assert evidence.comparison.state is DocumentComparisonState.PREDECESSOR_HIDDEN
    assert {fact.key: (fact.value, fact.available) for fact in evidence.facts} == {
        "artifact_count": (None, False),
        "dimensions": (None, False),
        "model": (None, False),
        "index_reference": (None, False),
        "fingerprint": (None, False),
        "numeric_preview": (None, False),
    }
    assert [(artifact.state.value, artifact.reason) for artifact in evidence.artifacts] == [
        ("unavailable", "not_recorded")
    ]

    assert store.stage_evidence("doc-1", 1, DocumentStage.EMBED, ("new-team",)) is None
    assert store.stage_evidence("doc-1", 3, DocumentStage.EMBED, ("new-team",)) is None
    assert store.stage_evidence("doc-2", 2, DocumentStage.EMBED, ("new-team",)) is None

    parse = store.stage_evidence("doc-1", 1, DocumentStage.PARSE, ("old-team",))
    assert parse is not None
    facts = {fact.key: fact for fact in parse.facts}
    assert facts["document_type"].value == "document"
    assert facts["content_bytes"].value == len(b"old body")
    assert facts["parser"].available is False


def test_governed_writer_finalizes_lineage_against_its_real_audit_event(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    lineage = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"))
    stack = write_stack(lineage=lineage)
    item = CuratedKnowledgeItem(
        item_id="connector-doc",
        content=json.dumps({"body": "searchable SOP", "name": "SOP.docx"}),
        metadata={"connector": "m365-sharepoint", "record_id": "sop"},
        provenance="connector:m365-sharepoint:sop",
        acl=("ops",),
    )
    asyncio.run(stack.writer.upsert([item]))

    journey = lineage.journey("connector-doc", ("ops",))
    assert journey is not None
    assert journey.selected.status == "current"
    assert journey.selected.ingested_event_id is not None
    assert {stage.audit_event_id for stage in journey.selected.stages} == {
        journey.selected.ingested_event_id
    }
    chunk_evidence = lineage.stage_evidence(
        "connector-doc", journey.selected.version_seq, DocumentStage.CHUNK, ("ops",)
    )
    assert chunk_evidence is not None
    assert chunk_evidence.artifacts[0].identifier == "connector-doc"
    embed_evidence = lineage.stage_evidence(
        "connector-doc", journey.selected.version_seq, DocumentStage.EMBED, ("ops",)
    )
    assert embed_evidence is not None
    assert embed_evidence.artifacts[0].dimensions == 32
    assert embed_evidence.artifacts[0].model_id == "ne-test-hash-v1"
    assert embed_evidence.artifacts[0].index_reference is not None
    with stack.engine.connect() as conn:
        payload = conn.scalar(
            sa.select(ne_audit_log.c.payload).where(ne_audit_log.c.action == "knowledge.upsert")
        )
    assert payload is not None
    assert json.loads(payload["detail"]) == {
        "content_hash": hashlib.sha256(b"searchable SOP").hexdigest(),
        "prev_content_hash": None,
        "version_seq": 1,
    }


def test_governed_writer_preserves_plain_text_connector_content_with_lineage(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    lineage = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"))
    stack = write_stack(lineage=lineage)
    item = CuratedKnowledgeItem(
        item_id="salesforce-account-001",
        content="Acme account summary",
        metadata={"connector": "salesforce", "record_id": "001000000000001AAA"},
        provenance="connector:salesforce:001000000000001AAA",
        provenance_class=ProvenanceClass.INGESTED,
        acl=("sales",),
    )

    receipt = asyncio.run(stack.writer.upsert([item]))

    assert receipt.accepted == (item.item_id,)
    stored = asyncio.run(stack.store.get([item.item_id]))
    assert stored[0].text == item.content
    journey = lineage.journey(item.item_id, item.acl)
    assert journey is not None
    assert journey.selected.status == "current"


@pytest.mark.parametrize("invalid_first", [False, True])
def test_writer_validates_every_parsed_descriptor_before_any_lineage_transaction(
    write_stack: Callable[..., WriteStack],
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
    invalid_first: bool,
) -> None:
    lineage = SqlAlchemyDocumentLineageStore(sa.create_engine("sqlite://"))
    prepare_calls: list[str] = []
    original_prepare = lineage.prepare

    def observed_prepare(chunk: Chunk, **kwargs):
        prepare_calls.append(chunk.chunk_id)
        return original_prepare(chunk, **kwargs)

    monkeypatch.setattr(lineage, "prepare", observed_prepare)
    contract = ShapeContract(
        name="ne.connect.record",
        version="1",
        json_schema={"type": "object", "required": ["content"]},
    )
    stack = write_stack(
        contracts={contract.name: contract},
        default_shape=contract.name,
        default_provenance_class=ProvenanceClass.INGESTED,
        lineage=lineage,
    )
    connector = _evidence_connector()

    def parsed_item(item_id: str, body: str, *, tamper: bool = False) -> CuratedKnowledgeItem:
        source_id = f"drive:{item_id}"
        payload = asyncio.run(
            connector._body_parser.parse(
                body.encode(),
                document_id=source_id,
                record_id=item_id,
                source_format="docx",
                acl=("ops",),
            )
        )
        payload["name"] = f"{item_id}.docx"
        descriptor = connector.take_parsed_evidence(
            source_id,
            item_id,
            payload,
            ("ops",),
            item_id,
        )
        assert descriptor is not None
        if tamper:
            payload["body"] = f"{body} tampered"
        return CuratedKnowledgeItem(
            item_id=item_id,
            content=json.dumps(payload, sort_keys=True),
            metadata={
                "connector": "m365-sharepoint",
                "source_id": source_id,
                "record_id": item_id,
            },
            provenance=f"connector:m365-sharepoint:{item_id}",
            provenance_class=ProvenanceClass.INGESTED,
            acl=("ops",),
            parsed_source_evidence=descriptor,
        )

    valid = parsed_item("valid-item", "valid body")
    invalid = parsed_item("invalid-item", "invalid body", tamper=True)
    reconstructed = valid.model_copy(
        update={"item_id": "reconstructed-item", "parsed_source_evidence": None}
    )
    with pytest.raises(DocumentLineageError, match="binding is invalid"):
        asyncio.run(stack.writer.upsert([reconstructed]))
    assert prepare_calls == []
    assert asyncio.run(stack.store.get(["reconstructed-item"])) == []

    items = [invalid, valid] if invalid_first else [valid, invalid]
    with pytest.raises(DocumentLineageError, match="binding is invalid"):
        asyncio.run(stack.writer.upsert(items))

    assert prepare_calls == []
    assert lineage.list_documents(("ops",)) == ()
    assert asyncio.run(stack.store.get(["valid-item", "invalid-item"])) == []

    prepare_calls.clear()
    first = parsed_item("first-valid", "first body")
    second = parsed_item("second-valid", "second body")
    swapped = [
        first.model_copy(update={"parsed_source_evidence": second.parsed_source_evidence}),
        second.model_copy(update={"parsed_source_evidence": first.parsed_source_evidence}),
    ]
    with pytest.raises(DocumentLineageError, match="binding is invalid"):
        asyncio.run(stack.writer.upsert(swapped))
    assert prepare_calls == []
    assert asyncio.run(stack.store.get(["first-valid", "second-valid"])) == []

    receipt = asyncio.run(stack.writer.upsert([first, second]))
    assert receipt.accepted == ("first-valid", "second-valid")
    assert prepare_calls == ["first-valid", "second-valid"]
    for item_id in receipt.accepted:
        evidence = lineage.stage_evidence(item_id, 1, DocumentStage.PARSE, ("ops",))
        embedding = lineage.stage_evidence(item_id, 1, DocumentStage.EMBED, ("ops",))
        assert embedding is not None
        assert embedding.artifacts[0].numeric_preview is not None
        assert len(embedding.artifacts[0].numeric_preview) == 8
        assert evidence is not None
        parser = next(fact for fact in evidence.facts if fact.key == "parser")
        assert parser.value == "neutron-m365-document-parser/v1"

    prepare_calls.clear()
    with pytest.raises(DocumentLineageError, match="binding is invalid"):
        asyncio.run(stack.writer.upsert([first, second]))
    assert prepare_calls == []
