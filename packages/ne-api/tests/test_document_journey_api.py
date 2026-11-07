# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""BD-3 HTTP proof: historical ACL trimming, as-of content, diff, and audit seq."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from ne.brain.evidence import submitted_embedding_evidence, vector_evidence_descriptor
from ne.brain.ingest.contracts import DocBlock, ParsedDocument
from ne.brain.lineage import (
    DocumentArtifactKind,
    DocumentEvidenceState,
    DocumentStage,
    DocumentStageArtifact,
    GraphEvidenceBatch,
    GraphEvidenceEntity,
    GraphEvidenceRelationship,
)
from ne.brain.parsed_evidence import _consume_parsed_document_evidence
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.connector import M365GraphConnector
from ne.contracts.types import Chunk
from ne.events import new_audit_event
from ne.schema import AuditOutcome

from .conftest import AppBuilder, bind_current_document_source, get_state, run

_VECTOR_EVIDENCE = vector_evidence_descriptor(
    backend="test-vector",
    logical_index="private-test-index",
    model_id="test/embed-v1",
    dimensions=8,
)


def _chunk(
    body: str,
    acl: tuple[str, ...],
    *,
    document_id: str = "doc-lineage-1",
    name: str = "Servicing SOP.docx",
    source_system: str = "m365-sharepoint",
    source_ref: str | None = None,
    source_snapshot_hash: str | None = None,
) -> Chunk:
    metadata: dict[str, object] = {
        "connector": source_system,
        "source_id": document_id,
        "record_id": document_id,
        "provenance": {"source": f"connector:{source_system}:{document_id}"},
    }
    if source_ref is not None and source_snapshot_hash is not None:
        metadata.update(
            source_ref=source_ref,
            source_snapshot_hash=source_snapshot_hash,
            source_acl_hash=_acl_hash(acl),
        )
    return Chunk(
        chunk_id=document_id,
        document_id=document_id,
        text=json.dumps(
            {
                "body": body,
                "body_content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "name": name,
            },
            sort_keys=True,
        ),
        metadata=metadata,
        acl=acl,
    )


def _parsed_chunk(
    *,
    document_id: str = "doc-semantic-1",
    acl: tuple[str, ...] = ("ops", "team"),
) -> Chunk:
    body = "Disputes\n\nInvestigate the claim.\n\nThresholds\n\nAmount | Review\n500 | fast"
    chunk = _chunk(body, acl, document_id=document_id, name="Semantic SOP.docx")
    payload = json.loads(chunk.text)
    payload.update(
        body_blocks=[
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
        body_block_count=4,
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


async def _prepare_parsed(store, chunk: Chunk):
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
    parsed_payload = await connector._body_parser.parse(
        str(payload["body"]).encode(),
        document_id=source_id,
        record_id=record_id,
        source_format="docx",
        acl=chunk.acl,
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
        [(descriptor, chunk.chunk_id, source_id, record_id, chunk.acl, payload)]
    )
    return store.prepare(
        chunk,
        parse_evidence=store.prepare_parsed_source_evidence(chunk, descriptor),
    )


def _acl_hash(acl: tuple[str, ...]) -> str:
    payload = json.dumps(
        {"acl": sorted(set(acl))},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _graph_entity(
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


async def _finalize_graph_source(
    app: Any,
    *,
    document_id: str,
    source_ref: str,
    snapshot_hash: str,
    acl: tuple[str, ...],
    occurred_at: datetime,
) -> None:
    state = get_state(app)
    source_chunk = _chunk(
        f"body for {document_id}",
        acl,
        document_id=document_id,
        source_ref=source_ref,
        source_snapshot_hash=snapshot_hash,
    )
    prepared = state.document_lineage.prepare(source_chunk)
    event = new_audit_event(
        action="knowledge.upsert",
        resource=f"knowledge://{document_id}",
        outcome=AuditOutcome.success,
        actor="test",
        occurred_at=occurred_at,
    )
    await state.sink.append(event)
    state.document_lineage.finalize(
        prepared,
        audit_event_id=event.event_id,
        occurred_at=event.occurred_at,
    )
    bind_current_document_source(
        app,
        document_id=document_id,
        labels=acl,
        content=source_chunk.text.encode(),
    )


async def _bind_graph_batch(
    app: Any,
    *,
    entities: tuple[GraphEvidenceEntity, ...],
    relationships: tuple[GraphEvidenceRelationship, ...],
    occurred_at: datetime,
    append_audit: bool = True,
) -> str:
    state = get_state(app)
    event = new_audit_event(
        action="graph.sync",
        resource="graph://m365-sharepoint",
        outcome=AuditOutcome.success,
        actor="test",
        occurred_at=occurred_at,
    )
    if append_audit:
        await state.sink.append(event)
    state.document_lineage.bind_graph_evidence(
        GraphEvidenceBatch(
            audit_event_id=event.event_id,
            occurred_at=event.occurred_at,
            entities=entities,
            relationships=relationships,
        )
    )
    return event.event_id


async def _seed(app: Any, *, bind_authority: bool = True) -> tuple[datetime, datetime]:
    state = get_state(app)
    first_at = datetime(2026, 8, 18, 12, tzinfo=UTC)
    second_at = first_at + timedelta(minutes=1)
    first_chunk = _chunk("old private body", ("old-team",))
    first = state.document_lineage.prepare(first_chunk, vector_evidence=_VECTOR_EVIDENCE)
    event1 = new_audit_event(
        action="knowledge.upsert",
        resource="knowledge://doc-lineage-1",
        outcome=AuditOutcome.success,
        actor="test",
        occurred_at=first_at,
    )
    await state.sink.append(event1)
    state.document_lineage.finalize(
        first, audit_event_id=event1.event_id, occurred_at=event1.occurred_at
    )
    if bind_authority:
        bind_current_document_source(
            app,
            document_id=first_chunk.document_id,
            labels=first_chunk.acl,
            content=first_chunk.text.encode(),
        )
    second_chunk = _chunk("new private body", ("new-team",))
    second = state.document_lineage.prepare(
        second_chunk,
        vector_evidence=submitted_embedding_evidence(
            _VECTOR_EVIDENCE,
            [0.125, -0.25, 0.5, 1.0, -2.0, 4.0, 8.0, -16.0],
        ),
    )
    event2 = new_audit_event(
        action="knowledge.upsert",
        resource="knowledge://doc-lineage-1",
        outcome=AuditOutcome.success,
        actor="test",
        occurred_at=second_at,
    )
    await state.sink.append(event2)
    state.document_lineage.finalize(
        second, audit_event_id=event2.event_id, occurred_at=event2.occurred_at
    )
    if bind_authority:
        bind_current_document_source(
            app,
            document_id=second_chunk.document_id,
            labels=second_chunk.acl,
            content=second_chunk.text.encode(),
        )
    return first_at, second_at


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(_contains_key(child, key) for child in value)
    return False


def test_document_without_current_authority_binding_is_not_disclosed(
    make_app: AppBuilder,
) -> None:
    harness = make_app()

    async def scenario() -> None:
        await _seed(harness.app, bind_authority=False)
        headers = harness.bearer("user:new", groups=("new-team",))
        async with harness.client() as client:
            listed = await client.get("/api/v1/documents", headers=headers)
            journey = await client.get("/api/v1/documents/doc-lineage-1/journey", headers=headers)
            assert listed.status_code == 200
            assert listed.json()["documents"] == []
            assert journey.status_code == 404

    run(scenario())


def test_document_journey_is_version_acl_trimmed_and_audit_anchored(
    make_app: AppBuilder,
) -> None:
    harness = make_app()

    async def scenario() -> None:
        first_at, _ = await _seed(harness.app)
        async with harness.client() as client:
            current_headers = harness.bearer("user:new", groups=("new-team",))
            listed = await client.get("/api/v1/documents", headers=current_headers)
            assert listed.status_code == 200
            assert [d["document_id"] for d in listed.json()["documents"]] == ["doc-lineage-1"]
            assert listed.json()["documents"][0]["document_type"] == "document"
            assert listed.json()["next_cursor"] is None
            assert listed.json()["document_types"] == ["document"]
            assert listed.json()["source_systems"] == ["m365-sharepoint"]

            current = await client.get(
                "/api/v1/documents/doc-lineage-1/journey", headers=current_headers
            )
            assert current.status_code == 200
            body = current.json()
            assert body["selected_version"] == 2
            assert body["selected_content_hash"] == hashlib.sha256(b"new private body").hexdigest()
            assert body["document_type"] == "document"
            assert body["selected_content"] == "new private body"
            assert body["diff"] is None  # v1 is ACL-hidden; never diff across it.
            assert body["comparison"] == {
                "state": "predecessor_hidden",
                "predecessor_version": None,
            }
            assert [version["version_seq"] for version in body["versions"]] == [2]
            assert [stage["stage"] for stage in body["stages"]] == [
                "parse",
                "chunk",
                "acl",
                "embed",
                "graph",
                "supersede",
            ]
            assert all(stage["audit_seq"] is not None for stage in body["stages"])
            assert all(stage["occurred_at"] is not None for stage in body["stages"])
            assert all(stage["status"] != "unavailable" for stage in body["stages"])
            assert all(
                stage["detail"] != "evidence unavailable: no bound audit receipt"
                for stage in body["stages"]
            )
            assert _contains_key(body, "acl") is False
            assert _contains_key(body, "source_ref") is False

            old_headers = harness.bearer("user:old", groups=("old-team",))
            latest_denied = await client.get(
                "/api/v1/documents/doc-lineage-1/journey", headers=old_headers
            )
            assert latest_denied.status_code == 404
            historical = await client.get(
                "/api/v1/documents/doc-lineage-1/journey",
                params={"as_of": (first_at + timedelta(seconds=10)).isoformat()},
                headers=old_headers,
            )
            assert historical.status_code == 404

            denied = await client.get(
                "/api/v1/documents/doc-lineage-1/journey",
                headers=harness.bearer("user:none"),
            )
            assert denied.status_code == 404

    run(scenario())


def test_superseded_version_stays_readable_while_document_is_currently_admitted(
    make_app: AppBuilder,
) -> None:
    """Temporal provenance: supersession must not orphan history.

    A superseded version's own retained receipt can never match the current
    authority head again (its source revision moved on), so authorizing a
    historical read against the historical stamp would 404 every version-history
    and as-of read the moment a document gains a second version — which is
    exactly what the version selector, the diff, and "which version did that
    answer use" deep-links exist to serve.  Document-level authority is the
    CURRENT version's; per-version visibility stays with the retained
    historical ACL.
    """

    harness = make_app()

    async def scenario() -> None:
        first_at, _ = await _seed(harness.app)
        both_headers = harness.bearer("user:both", groups=("old-team", "new-team"))
        async with harness.client() as client:
            # Exact historical version: readable, with its own content.
            historical = await client.get(
                "/api/v1/documents/doc-lineage-1/journey",
                params={"version": 1},
                headers=both_headers,
            )
            assert historical.status_code == 200
            body = historical.json()
            assert body["selected_version"] == 1
            assert body["selected_content"] == "old private body"
            assert [v["version_seq"] for v in body["versions"]] == [1, 2]

            # As-of the v1 window resolves to the same historical version.
            as_of = await client.get(
                "/api/v1/documents/doc-lineage-1/journey",
                params={"as_of": (first_at + timedelta(seconds=10)).isoformat()},
                headers=both_headers,
            )
            assert as_of.status_code == 200
            assert as_of.json()["selected_version"] == 1

            # Historical stage evidence is readable under the same rule.
            stage = await client.get(
                "/api/v1/documents/doc-lineage-1/versions/1/stages/parse",
                headers=both_headers,
            )
            assert stage.status_code == 200
            assert stage.json()["version"] == 1

            # The historical ACL still trims: a principal without the v1 label
            # cannot read v1 even though the document is currently admitted.
            acl_hidden = await client.get(
                "/api/v1/documents/doc-lineage-1/journey",
                params={"version": 1},
                headers=harness.bearer("user:new", groups=("new-team",)),
            )
            assert acl_hidden.status_code == 404

    run(scenario())


def test_journey_rejects_ambiguous_version_and_as_of(make_app: AppBuilder) -> None:
    harness = make_app()

    async def scenario() -> None:
        await _seed(harness.app)
        async with harness.client() as client:
            response = await client.get(
                "/api/v1/documents/doc-lineage-1/journey",
                params={"version": 2, "as_of": datetime.now(UTC).isoformat()},
                headers=harness.bearer("user:new", groups=("new-team",)),
            )
            assert response.status_code == 422
            assert response.json()["detail"] == "choose version or as_of, not both"

    run(scenario())


def test_document_list_search_filters_and_cursor_are_server_authoritative(
    make_app: AppBuilder,
) -> None:
    harness = make_app()
    state = get_state(harness.app)
    listed_chunks = (
        _chunk("alpha", ("ops",), document_id="doc-a", name="Alpha SOP.DOCX"),
        _chunk(
            "beta",
            ("ops",),
            document_id="doc-b",
            name="Beta SOP.pdf",
            source_system="google-drive",
        ),
        _chunk("notes", ("ops",), document_id="doc-c", name="Notes.md"),
        _chunk(
            "hidden",
            ("secret",),
            document_id="doc-hidden",
            name="Hidden SOP.pptx",
            source_system="secret-source",
        ),
    )
    for listed_chunk in listed_chunks:
        state.document_lineage.prepare(listed_chunk)
        bind_current_document_source(
            harness.app,
            document_id=listed_chunk.document_id,
            labels=listed_chunk.acl,
            content=listed_chunk.text.encode(),
        )

    async def scenario() -> None:
        headers = harness.bearer("user:ops", groups=("ops",))
        async with harness.client() as client:
            first = await client.get(
                "/api/v1/documents",
                params={"q": "sOp", "limit": 1},
                headers=headers,
            )
            assert first.status_code == 200
            first_body = first.json()
            assert len(first_body["documents"]) == 1
            assert first_body["documents"][0]["display_name"].casefold().find("sop") >= 0
            assert first_body["next_cursor"] is not None
            assert first_body["document_types"] == ["pdf", "document", "text"]
            assert first_body["source_systems"] == ["google-drive", "m365-sharepoint"]

            second = await client.get(
                "/api/v1/documents",
                params={
                    "q": "SOP",
                    "limit": 1,
                    "cursor": first_body["next_cursor"],
                },
                headers=headers,
            )
            assert second.status_code == 200
            assert len(second.json()["documents"]) == 1
            assert second.json()["documents"] != first_body["documents"]
            assert second.json()["next_cursor"] is None

            filtered = await client.get(
                "/api/v1/documents",
                params={"q": "sop", "type": "document", "source": "M365-SHAREPOINT"},
                headers=headers,
            )
            assert filtered.status_code == 200
            assert [row["document_id"] for row in filtered.json()["documents"]] == ["doc-a"]

            reused = await client.get(
                "/api/v1/documents",
                params={"q": "different", "cursor": first_body["next_cursor"]},
                headers=headers,
            )
            assert reused.status_code == 422
            assert reused.json()["detail"] == "invalid document cursor"

            malformed = await client.get(
                "/api/v1/documents",
                params={"cursor": "not a cursor"},
                headers=headers,
            )
            assert malformed.status_code == 422
            assert malformed.json()["detail"] == "invalid document cursor"

    run(scenario())


def test_document_list_structured_filter_is_interim_negraph_suffix(make_app: AppBuilder) -> None:
    """INTERIM (PD-DOC-TITLES): the ``structured`` query param infers
    structured-ness from the ".negraph.json" connector filename convention.
    REMOVE this test alongside the interim filter (ne-brain's
    ``include_structured`` predicate, ``operational_graph_count``, this
    ``structured`` param, and the console's "Include operational graph
    records" toggle) when the real ``structured`` flag lands on the document
    wire.

    Default (no ``structured`` param, or ``structured=false``) EXCLUDES
    operational-graph rows — the directory's default is prose documents only.
    ``structured=true`` is the explicit opt-in that puts them back in the page.
    """
    harness = make_app()
    state = get_state(harness.app)
    seeded = (
        _chunk(
            "graph body",
            ("ops",),
            document_id="doc-structured",
            name="graph-Customer-1.negraph.json",
        ),
        _chunk("prose body", ("ops",), document_id="doc-prose", name="Servicing SOP.docx"),
    )
    for chunk in seeded:
        state.document_lineage.prepare(chunk)
        bind_current_document_source(
            harness.app,
            document_id=chunk.document_id,
            labels=chunk.acl,
            content=chunk.text.encode(),
        )

    async def scenario() -> None:
        headers = harness.bearer("user:ops", groups=("ops",))
        async with harness.client() as client:
            excluded = await client.get("/api/v1/documents", headers=headers)
            assert excluded.status_code == 200
            excluded_body = excluded.json()
            excluded_ids = [row["document_id"] for row in excluded_body["documents"]]
            assert excluded_ids == ["doc-prose"]
            # Control: the honest corpus-wide count still reports the graph
            # row even though the default page excludes it.
            assert excluded_body["operational_graph_count"] == 1

            included = await client.get(
                "/api/v1/documents",
                params={"structured": "true"},
                headers=headers,
            )
            assert included.status_code == 200
            included_body = included.json()
            assert {row["document_id"] for row in included_body["documents"]} == {
                "doc-structured",
                "doc-prose",
            }
            assert included_body["operational_graph_count"] == 1

    run(scenario())


def test_document_stage_evidence_requires_exact_visible_version_and_valid_stage(
    make_app: AppBuilder,
) -> None:
    harness = make_app()

    async def scenario() -> None:
        await _seed(harness.app)
        async with harness.client() as client:
            new_headers = harness.bearer("user:new", groups=("new-team",))
            response = await client.get(
                "/api/v1/documents/doc-lineage-1/versions/2/stages/embed",
                headers=new_headers,
            )
            assert response.status_code == 200
            body = response.json()
            assert body["document_id"] == "doc-lineage-1"
            assert body["version"] == 2
            assert body["content_hash"] == hashlib.sha256(b"new private body").hexdigest()
            assert body["document_type"] == "document"
            assert body["stage"] == "embed"
            assert body["status"] == "complete"
            assert body["audit_seq"] is not None
            assert body["comparison"] == {
                "state": "predecessor_hidden",
                "predecessor_version": None,
            }
            assert {fact["key"]: fact["available"] for fact in body["facts"]} == {
                "artifact_count": True,
                "dimensions": True,
                "model": True,
                "index_reference": True,
                "fingerprint": True,
                "numeric_preview": True,
            }
            assert body["artifacts"] == [
                {
                    "kind": "embedding",
                    "label": "Vector artifact receipt",
                    "state": "available",
                    "ordinal": 0,
                    "identifier": None,
                    "size_bytes": None,
                    "artifact_count": 1,
                    "dimensions": 8,
                    "model": "test/embed-v1",
                    "index_reference": _VECTOR_EVIDENCE.index_reference,
                    "fingerprint": _VECTOR_EVIDENCE.index_fingerprint,
                    "numeric_preview": [0.125, -0.25, 0.5, 1.0, -2.0, 4.0, 8.0, -16.0],
                    "reason": None,
                }
            ]
            assert body["graph_evidence"] is None
            assert _contains_key(body, "acl") is False
            assert _contains_key(body, "source_ref") is False

            hidden_version = await client.get(
                "/api/v1/documents/doc-lineage-1/versions/1/stages/embed",
                headers=new_headers,
            )
            assert hidden_version.status_code == 404
            assert hidden_version.json()["detail"] == "document stage not found"

            wrong_version = await client.get(
                "/api/v1/documents/doc-lineage-1/versions/3/stages/embed",
                headers=new_headers,
            )
            assert wrong_version.status_code == 404

            wrong_document = await client.get(
                "/api/v1/documents/other/versions/2/stages/embed",
                headers=new_headers,
            )
            assert wrong_document.status_code == 404

            bad_stage = await client.get(
                "/api/v1/documents/doc-lineage-1/versions/2/stages/not-a-stage",
                headers=new_headers,
            )
            assert bad_stage.status_code == 422

    run(scenario())


def test_document_stage_projects_version_bound_semantic_parse_and_ops_acl_truth(
    make_app: AppBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_app(ops_roles=("ops",))

    async def scenario() -> None:
        state = get_state(harness.app)
        chunk = _parsed_chunk()
        prepared = await _prepare_parsed(state.document_lineage, chunk)
        event = new_audit_event(
            action="knowledge.upsert",
            resource="knowledge://doc-semantic-1",
            outcome=AuditOutcome.success,
            actor="test",
            occurred_at=datetime(2026, 8, 21, 14, tzinfo=UTC),
        )
        await state.sink.append(event)
        state.document_lineage.finalize(
            prepared,
            audit_event_id=event.event_id,
            occurred_at=event.occurred_at,
        )
        bind_current_document_source(
            harness.app,
            document_id=chunk.document_id,
            labels=chunk.acl,
            content=chunk.text.encode(),
        )
        path = "/api/v1/documents/doc-semantic-1/versions/1/stages"
        async with harness.client() as client:
            team_headers = harness.bearer("user:team", groups=("team",))
            chunk = await client.get(f"{path}/chunk", headers=team_headers)
            assert chunk.status_code == 200
            chunk_body = chunk.json()
            assert chunk_body["semantic_chunks"] == {
                "state": "available",
                "reason": None,
                "chunks": [
                    {
                        "identifier": "doc-semantic-1:0000",
                        "size_bytes": len(b"Disputes\n\nInvestigate the claim."),
                    },
                    {
                        "identifier": "doc-semantic-1:0001",
                        "size_bytes": len(b"Thresholds\n\nAmount | Review\n500 | fast"),
                    },
                ],
                "truncated": False,
            }
            assert chunk_body["artifacts"][0]["label"] == "Record chunk"
            assert _contains_key(chunk_body["semantic_chunks"], "acl") is False
            assert _contains_key(chunk_body["semantic_chunks"], "source_ref") is False

            parsed = await client.get(f"{path}/parse", headers=team_headers)
            assert parsed.status_code == 200
            parse_facts = {fact["key"]: fact for fact in parsed.json()["facts"]}
            assert parse_facts["parser"]["value"] == "neutron-m365-document-parser/v1"
            assert parse_facts["block_count"]["value"] == 4
            assert parse_facts["page_count"]["value"] == 2
            assert parse_facts["table_count"]["value"] == 1
            assert parsed.json()["semantic_chunks"] is None

            non_ops_acl = await client.get(f"{path}/acl", headers=team_headers)
            assert non_ops_acl.status_code == 200
            non_ops_fact = next(
                fact for fact in non_ops_acl.json()["facts"] if fact["key"] == "acl_labels"
            )
            assert non_ops_fact == {
                "key": "acl_labels",
                "label": "Sensitive ACL labels",
                "value": None,
                "available": False,
            }
            assert "ops, team" not in json.dumps(non_ops_acl.json(), sort_keys=True)

            ops_acl = await client.get(
                f"{path}/acl",
                headers=harness.bearer("user:ops", groups=("ops", "team")),
            )
            assert ops_acl.status_code == 200
            ops_fact = next(fact for fact in ops_acl.json()["facts"] if fact["key"] == "acl_labels")
            assert ops_fact["value"] == "ops, team"
            assert ops_fact["available"] is True

            real = state.document_lineage.stage_evidence(
                "doc-semantic-1", 1, DocumentStage.CHUNK, ("team",)
            )
            assert real is not None
            monkeypatch.setattr(
                state.document_lineage,
                "stage_evidence",
                lambda *_args: replace(
                    real,
                    semantic_chunks=tuple(
                        DocumentStageArtifact(
                            kind=DocumentArtifactKind.SEMANTIC_CHUNK,
                            state=DocumentEvidenceState.AVAILABLE,
                            ordinal=index,
                            identifier=f"doc-semantic-1:{index:04d}",
                            size_bytes=index,
                        )
                        for index in range(101)
                    ),
                ),
            )
            capped = await client.get(f"{path}/chunk", headers=team_headers)
            assert capped.status_code == 200
            assert len(capped.json()["semantic_chunks"]["chunks"]) == 100
            assert capped.json()["semantic_chunks"]["truncated"] is True

    run(scenario())


def test_empty_ops_roles_never_disclose_document_acl_labels(make_app: AppBuilder) -> None:
    harness = make_app()

    async def scenario() -> None:
        state = get_state(harness.app)
        source_chunk = _parsed_chunk(acl=("team",))
        prepared = state.document_lineage.prepare(source_chunk)
        event = new_audit_event(
            action="knowledge.upsert",
            resource="knowledge://doc-semantic-1",
            outcome=AuditOutcome.success,
            actor="test",
        )
        await state.sink.append(event)
        state.document_lineage.finalize(
            prepared,
            audit_event_id=event.event_id,
            occurred_at=event.occurred_at,
        )
        bind_current_document_source(
            harness.app,
            document_id=source_chunk.document_id,
            labels=source_chunk.acl,
            content=source_chunk.text.encode(),
        )
        async with harness.client() as client:
            response = await client.get(
                "/api/v1/documents/doc-semantic-1/versions/1/stages/acl",
                headers=harness.bearer("user:ops", groups=("ops", "team")),
            )
        assert response.status_code == 200
        fact = next(fact for fact in response.json()["facts"] if fact["key"] == "acl_labels")
        assert fact["value"] is None
        assert fact["available"] is False
        assert '"team"' not in json.dumps(response.json(), sort_keys=True)

    run(scenario())


def test_parser_and_semantic_evidence_fail_closed_without_a_real_audit_row(
    make_app: AppBuilder,
) -> None:
    harness = make_app(ops_roles=("ops",))

    async def scenario() -> None:
        state = get_state(harness.app)
        chunk = _parsed_chunk()
        prepared = await _prepare_parsed(state.document_lineage, chunk)
        unrelated = new_audit_event(
            action="knowledge.upsert",
            resource="knowledge://different-version",
            outcome=AuditOutcome.success,
            actor="test",
        )
        await state.sink.append(unrelated)
        state.document_lineage.finalize(
            prepared,
            audit_event_id="missing-audit-event",
            occurred_at=datetime(2026, 8, 21, 15, tzinfo=UTC),
        )
        bind_current_document_source(
            harness.app,
            document_id=chunk.document_id,
            labels=chunk.acl,
            content=chunk.text.encode(),
        )
        path = "/api/v1/documents/doc-semantic-1/versions/1/stages"
        headers = harness.bearer("user:ops", groups=("ops", "team"))
        async with harness.client() as client:
            responses = {
                stage: await client.get(f"{path}/{stage}", headers=headers)
                for stage in ("parse", "chunk", "acl", "embed", "graph", "supersede")
            }
            journey = await client.get(
                "/api/v1/documents/doc-semantic-1/journey",
                params={"version": 1},
                headers=headers,
            )

        for response in responses.values():
            assert response.status_code == 200
            body = response.json()
            assert body["audit_seq"] is None
            assert body["occurred_at"] is None
            assert body["status"] == "unavailable"
            assert body["detail"] == "evidence unavailable: no bound audit receipt"
            assert all(fact["value"] is None for fact in body["facts"])
            assert all(fact["available"] is False for fact in body["facts"])
            assert "ops, team" not in json.dumps(body, sort_keys=True)
            for artifact in body["artifacts"]:
                assert artifact["state"] == "unavailable"
                assert artifact["identifier"] is None
                assert artifact["fingerprint"] is None
        assert journey.status_code == 200
        journey_stages = journey.json()["stages"]
        assert [stage["stage"] for stage in journey_stages] == [
            "parse",
            "chunk",
            "acl",
            "embed",
            "graph",
            "supersede",
        ]
        assert all(stage["status"] == "unavailable" for stage in journey_stages)
        assert all(stage["audit_seq"] is None for stage in journey_stages)
        assert all(stage["occurred_at"] is None for stage in journey_stages)
        assert all(
            stage["detail"] == "evidence unavailable: no bound audit receipt"
            for stage in journey_stages
        )
        chunk_body = responses["chunk"].json()
        assert chunk_body["semantic_chunks"] == {
            "state": "unavailable",
            "reason": "unbound_audit_receipt",
            "chunks": [],
            "truncated": False,
        }
        chunk_facts = {fact["key"]: fact for fact in chunk_body["facts"]}
        assert chunk_facts["semantic_chunk_count"]["available"] is False
        assert chunk_facts["semantic_chunk_count"]["value"] is None
        assert chunk_facts["segmentation"]["available"] is False
        assert chunk_facts["segmentation"]["value"] is None
        assert responses["graph"].json()["graph_evidence"] == {
            "state": "unavailable",
            "reason": "evidence unavailable: no bound audit receipt",
            "entities": [],
            "relationships": [],
            "truncated": False,
        }

    run(scenario())


def test_graph_stage_is_gated_by_the_exact_historical_document_acl(
    make_app: AppBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_app()

    async def scenario() -> None:
        await _seed(harness.app)
        state = get_state(harness.app)
        original = state.document_lineage.graph_evidence
        calls: list[tuple[str, int, tuple[str, ...]]] = []

        def observed(document_id: str, version: int, labels: tuple[str, ...]):
            calls.append((document_id, version, labels))
            return original(document_id, version, labels)

        monkeypatch.setattr(state.document_lineage, "graph_evidence", observed)
        async with harness.client() as client:
            headers = harness.bearer("user:new", groups=("new-team",))
            hidden = await client.get(
                "/api/v1/documents/doc-lineage-1/versions/1/stages/graph",
                headers=headers,
            )
            assert hidden.status_code == 404
            assert calls == []

            skipped = await client.get(
                "/api/v1/documents/doc-lineage-1/versions/2/stages/graph",
                headers=headers,
            )
            assert skipped.status_code == 200
            assert len(calls) == 1
            assert calls[0][0:2] == ("doc-lineage-1", 2)
            assert "new-team" in calls[0][2]
            body = skipped.json()
            assert body["audit_seq"] is not None
            skipped_detail = "SKIPPED — no version-bound entities/relationships claimed"
            assert body["detail"] == skipped_detail
            assert (
                next(fact["value"] for fact in body["facts"] if fact["key"] == "graph_result")
                == skipped_detail
            )
            assert body["graph_evidence"] == {
                "state": "skipped",
                "reason": skipped_detail,
                "entities": [],
                "relationships": [],
                "truncated": False,
            }

    run(scenario())


def test_graph_evidence_projects_only_principal_visible_safe_receipts(
    make_app: AppBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_app()

    async def scenario() -> None:
        at = datetime(2026, 8, 21, 12, tzinfo=UTC)
        subject_ref = "drive-a:subject:subject"
        object_ref = "drive-a:object:object"
        subject_hash = "1" * 64
        object_hash = "2" * 64
        await _finalize_graph_source(
            harness.app,
            document_id="subject-document",
            source_ref=subject_ref,
            snapshot_hash=subject_hash,
            acl=("ops",),
            occurred_at=at,
        )
        await _finalize_graph_source(
            harness.app,
            document_id="object-document",
            source_ref=object_ref,
            snapshot_hash=object_hash,
            acl=("finance",),
            occurred_at=at + timedelta(seconds=1),
        )
        subject = _graph_entity(
            source_ref=subject_ref,
            snapshot_hash=subject_hash,
            key="entity-subject",
            display_name="Subject procedure",
        )
        obj = _graph_entity(
            source_ref=object_ref,
            snapshot_hash=object_hash,
            key="entity-object",
            display_name="Object procedure",
            acl=("finance",),
        )
        await _bind_graph_batch(
            harness.app,
            entities=(subject, obj),
            relationships=(
                GraphEvidenceRelationship(
                    key="relationship-subject-object",
                    edge_kind="REFERENCES",
                    subject=subject,
                    object=obj,
                ),
            ),
            occurred_at=at + timedelta(seconds=2),
        )

        path = "/api/v1/documents/subject-document/versions/1/stages/graph"
        async with harness.client() as client:
            ops = await client.get(
                path,
                headers=harness.bearer("user:ops", groups=("ops",)),
            )
            assert ops.status_code == 200
            ops_body = ops.json()
            serialized_ops = json.dumps(ops_body, sort_keys=True)
            assert "relationships=1" not in serialized_ops
            assert (
                "version-bound graph evidence receipt committed (entities=1 relationships=1)"
                not in serialized_ops
            )
            safe_detail = "principal-visible version-bound graph evidence available"
            assert ops_body["detail"] == safe_detail
            assert (
                next(fact["value"] for fact in ops_body["facts"] if fact["key"] == "graph_result")
                == safe_detail
            )
            assert ops_body["graph_evidence"] == {
                "state": "available",
                "reason": None,
                "entities": [
                    {
                        "key": "entity-subject",
                        "entity_class": "Document",
                        "display_name": "Subject procedure",
                        "source_system": "m365-sharepoint",
                    }
                ],
                "relationships": [],
                "truncated": False,
            }

            both = await client.get(
                path,
                headers=harness.bearer("user:both", groups=("ops", "finance")),
            )
            assert both.status_code == 200
            both_body = both.json()
            assert both_body["audit_seq"] is not None
            relationship = both_body["graph_evidence"]["relationships"]
            assert relationship == [
                {
                    "key": "relationship-subject-object",
                    "edge_kind": "REFERENCES",
                    "subject": {
                        "key": "entity-subject",
                        "entity_class": "Document",
                        "display_name": "Subject procedure",
                        "source_system": "m365-sharepoint",
                    },
                    "object": {
                        "key": "entity-object",
                        "entity_class": "Document",
                        "display_name": "Object procedure",
                        "source_system": "m365-sharepoint",
                    },
                }
            ]
            for forbidden in (
                "acl",
                "source_ref",
                "source_snapshot_hash",
                "source_acl_hash",
                "audit_event_id",
            ):
                assert _contains_key(both_body, forbidden) is False

            state = get_state(harness.app)
            real = state.document_lineage.graph_evidence("subject-document", 1, ("ops", "finance"))
            assert real is not None
            monkeypatch.setattr(
                state.document_lineage,
                "graph_evidence",
                lambda *_args: replace(real, audit_event_id="mismatched-audit-event"),
            )
            mismatch = await client.get(
                path,
                headers=harness.bearer("user:both", groups=("ops", "finance")),
            )
            assert mismatch.status_code == 200
            assert mismatch.json()["graph_evidence"] == {
                "state": "unavailable",
                "reason": "version-bound graph evidence receipt unavailable",
                "entities": [],
                "relationships": [],
                "truncated": False,
            }

    run(scenario())


def test_graph_evidence_distinguishes_empty_from_missing_audit_receipt(
    make_app: AppBuilder,
) -> None:
    harness = make_app()

    async def scenario() -> None:
        at = datetime(2026, 8, 21, 13, tzinfo=UTC)
        subject_ref = "drive-b:subject:subject"
        object_ref = "drive-b:object:object"
        await _finalize_graph_source(
            harness.app,
            document_id="empty-subject",
            source_ref=subject_ref,
            snapshot_hash="3" * 64,
            acl=("ops",),
            occurred_at=at,
        )
        await _finalize_graph_source(
            harness.app,
            document_id="empty-object",
            source_ref=object_ref,
            snapshot_hash="4" * 64,
            acl=("finance",),
            occurred_at=at + timedelta(seconds=1),
        )
        subject = _graph_entity(
            source_ref=subject_ref,
            snapshot_hash="3" * 64,
            key="empty-subject-entity",
            display_name="Empty subject",
        )
        obj = _graph_entity(
            source_ref=object_ref,
            snapshot_hash="4" * 64,
            key="hidden-object-entity",
            display_name="Hidden object",
            acl=("finance",),
        )
        await _bind_graph_batch(
            harness.app,
            entities=(),
            relationships=(
                GraphEvidenceRelationship(
                    key="hidden-relationship",
                    edge_kind="REFERENCES",
                    subject=subject,
                    object=obj,
                ),
            ),
            occurred_at=at + timedelta(seconds=2),
        )

        unaudited_ref = "drive-b:unaudited:unaudited"
        await _finalize_graph_source(
            harness.app,
            document_id="unaudited-document",
            source_ref=unaudited_ref,
            snapshot_hash="5" * 64,
            acl=("ops",),
            occurred_at=at + timedelta(seconds=3),
        )
        unaudited = _graph_entity(
            source_ref=unaudited_ref,
            snapshot_hash="5" * 64,
            key="unaudited-entity",
            display_name="Unaudited entity",
        )
        await _bind_graph_batch(
            harness.app,
            entities=(unaudited,),
            relationships=(),
            occurred_at=at + timedelta(seconds=4),
            append_audit=False,
        )

        headers = harness.bearer("user:ops", groups=("ops",))
        async with harness.client() as client:
            empty = await client.get(
                "/api/v1/documents/empty-subject/versions/1/stages/graph",
                headers=headers,
            )
            assert empty.status_code == 200
            empty_body = empty.json()
            assert empty_body["detail"] == "no principal-visible version-bound graph evidence"
            assert empty_body["graph_evidence"] == {
                "state": "empty",
                "reason": "no visible graph evidence",
                "entities": [],
                "relationships": [],
                "truncated": False,
            }

            unavailable = await client.get(
                "/api/v1/documents/unaudited-document/versions/1/stages/graph",
                headers=headers,
            )
            assert unavailable.status_code == 200
            body = unavailable.json()
            assert body["audit_seq"] is None
            assert body["status"] == "unavailable"
            assert body["detail"] == "evidence unavailable: no bound audit receipt"
            assert body["graph_evidence"] == {
                "state": "unavailable",
                "reason": "evidence unavailable: no bound audit receipt",
                "entities": [],
                "relationships": [],
                "truncated": False,
            }

    run(scenario())


def test_graph_evidence_caps_entities_and_relationships_independently_after_acl_trim(
    make_app: AppBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_app()

    async def scenario() -> None:
        at = datetime(2026, 8, 21, 14, tzinfo=UTC)
        source_ref = "drive-c:bounded:bounded"
        snapshot_hash = "6" * 64
        await _finalize_graph_source(
            harness.app,
            document_id="bounded-document",
            source_ref=source_ref,
            snapshot_hash=snapshot_hash,
            acl=("ops",),
            occurred_at=at,
        )
        entities = tuple(
            _graph_entity(
                source_ref=source_ref,
                snapshot_hash=snapshot_hash,
                key=f"entity-{index:03d}",
                display_name=f"Entity {index:03d}",
            )
            for index in range(101)
        )
        relationships = tuple(
            GraphEvidenceRelationship(
                key=f"relationship-{index:03d}",
                edge_kind="RELATED_TO",
                subject=entities[index],
                object=entities[(index + 1) % len(entities)],
            )
            for index in range(101)
        )
        await _bind_graph_batch(
            harness.app,
            entities=entities,
            relationships=relationships,
            occurred_at=at + timedelta(seconds=1),
        )

        path = "/api/v1/documents/bounded-document/versions/1/stages/graph"
        headers = harness.bearer("user:ops", groups=("ops",))
        async with harness.client() as client:
            response = await client.get(path, headers=headers)
            assert response.status_code == 200
            graph = response.json()["graph_evidence"]
            assert len(graph["entities"]) == 100
            assert len(graph["relationships"]) == 100
            assert graph["entities"][0]["key"] == "entity-000"
            assert graph["entities"][-1]["key"] == "entity-099"
            assert graph["relationships"][0]["key"] == "relationship-000"
            assert graph["relationships"][-1]["key"] == "relationship-099"
            assert graph["truncated"] is True

            state = get_state(harness.app)
            exact = state.document_lineage.graph_evidence("bounded-document", 1, ("ops",))
            assert exact is not None
            monkeypatch.setattr(
                state.document_lineage,
                "graph_evidence",
                lambda *_args: replace(
                    exact,
                    entities=exact.entities[:100],
                    relationships=exact.relationships[:100],
                ),
            )
            exact_limit = await client.get(path, headers=headers)
            assert exact_limit.status_code == 200
            assert exact_limit.json()["graph_evidence"]["truncated"] is False

    run(scenario())


# --- No-oracle hardening: withheld and absent must be indistinguishable -------
#
# The authorizer this router is given returns bool, so the server holds two
# values here, not three. These controls prove the two-valued properties that ARE
# true: one canonical 404, no caching, no ordering tell, and no document material
# in the refused shell.
#
# The WITHHELD fixture is a document seeded WITHOUT a current-authority binding
# and read by a principal whose ACLs do admit it: the lineage returns a real
# journey and the authority gate refuses it. Reading it as `user:old` instead
# would make the lineage itself return None, which is the absent path wearing a
# different name and would compare that path against itself.
_WITHHELD_DOC = "doc-lineage-1"
_ABSENT_DOC = "doc-nope"
_SENTINEL_MATERIAL = (
    "Servicing SOP.docx",  # title
    "new private body",  # content
    "old private body",
    "m365-sharepoint",  # source identity
)


def _authorize_now(harness: Any) -> None:
    """Flip the seeded document from withheld to authorized, changing nothing else."""

    bind_current_document_source(
        harness.app,
        document_id=_WITHHELD_DOC,
        labels=("new-team",),
        content=b"new private body",
    )


def test_withheld_and_absent_documents_are_one_indistinguishable_response(
    make_app: AppBuilder,
) -> None:
    harness = make_app()

    async def scenario() -> None:
        await _seed(harness.app, bind_authority=False)
        headers = harness.bearer("user:new", groups=("new-team",))
        async with harness.client() as client:
            withheld = await client.get(
                f"/api/v1/documents/{_WITHHELD_DOC}/journey", headers=headers
            )
            absent = await client.get(f"/api/v1/documents/{_ABSENT_DOC}/journey", headers=headers)

            assert withheld.status_code == absent.status_code == 404
            assert withheld.json() == absent.json()
            assert withheld.content == absent.content
            assert len(withheld.content) == len(absent.content)
            assert withheld.headers["cache-control"] == "private, no-store"
            assert absent.headers["cache-control"] == "private, no-store"
            varying = {"date", "server", "content-length"}
            assert {k: v for k, v in withheld.headers.items() if k.lower() not in varying} == {
                k: v for k, v in absent.headers.items() if k.lower() not in varying
            }

            # Positive control: authority binding is the ONLY thing that changed,
            # and it turns the same request into a 200 — so these guards cannot be
            # satisfied by a router that refuses everything.
            _authorize_now(harness)
            allowed = await client.get(
                f"/api/v1/documents/{_WITHHELD_DOC}/journey", headers=headers
            )
            assert allowed.status_code == 200
            assert allowed.json()["selected_content"] == "new private body"
            assert allowed.headers["cache-control"] == "private, no-store"

    run(scenario())


def test_withheld_and_absent_documents_do_the_same_work_in_the_same_order(
    make_app: AppBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_app()

    async def scenario() -> None:
        await _seed(harness.app, bind_authority=False)
        headers = harness.bearer("user:new", groups=("new-team",))
        state = get_state(harness.app)
        trace: list[str] = []
        real_auth = state.current_document_authorizer
        assert real_auth is not None
        real_journey = state.document_lineage.journey

        async def traced_auth(*args: Any, **kwargs: Any) -> Any:
            trace.append("authorize")
            return await real_auth(*args, **kwargs)

        def traced_journey(*args: Any, **kwargs: Any) -> Any:
            trace.append("journey")
            return real_journey(*args, **kwargs)

        monkeypatch.setattr(state, "current_document_authorizer", traced_auth)
        monkeypatch.setattr(state.document_lineage, "journey", traced_journey)

        async with harness.client() as client:
            withheld = await client.get(
                f"/api/v1/documents/{_WITHHELD_DOC}/journey", headers=headers
            )
            withheld_order = list(trace)
            trace.clear()

            absent = await client.get(f"/api/v1/documents/{_ABSENT_DOC}/journey", headers=headers)
            absent_order = list(trace)
            trace.clear()

            assert withheld.status_code == absent.status_code == 404
            # Neither case may perform work the other skips. A body-only
            # comparison cannot see this: the two responses are byte-identical
            # even when the authorizer is skipped for one of them.
            assert withheld_order == absent_order
            # Pin the order too, so the equality above cannot be satisfied by
            # both paths degenerating to the same short-circuit.
            assert withheld_order == ["journey", "authorize"]

            # Positive control: an authorized 200 walks that same sequence, so
            # the expected order is not an artefact of refusal.
            _authorize_now(harness)
            allowed = await client.get(
                f"/api/v1/documents/{_WITHHELD_DOC}/journey", headers=headers
            )
            assert allowed.status_code == 200
            assert list(trace) == ["journey", "authorize"]

    run(scenario())


def test_refused_document_shell_carries_no_content_source_or_locator(
    make_app: AppBuilder,
) -> None:
    harness = make_app()

    async def scenario() -> None:
        await _seed(harness.app, bind_authority=False)
        headers = harness.bearer("user:new", groups=("new-team",))
        async with harness.client() as client:
            withheld = await client.get(
                f"/api/v1/documents/{_WITHHELD_DOC}/journey", headers=headers
            )
            stage = await client.get(
                f"/api/v1/documents/{_WITHHELD_DOC}/versions/2/stages/parse", headers=headers
            )
            assert withheld.status_code == 404
            assert stage.status_code == 404

            for refused in (withheld, stage):
                blob = refused.content.decode() + repr(dict(refused.headers))
                # VALUES, not key names: the recursive key sweep elsewhere in this
                # file cannot see a title or source echoed into a detail string.
                for material in _SENTINEL_MATERIAL:
                    assert material not in blob
                body = refused.json()
                assert set(body) == {"detail"}
                assert body["detail"] in {"document not found", "document stage not found"}
                # Shape, not just names: a fixed refusal cannot grow a payload.
                assert len(refused.content) < 64

            # Positive control: those sentinels are real and DO cross the wire
            # once authorized, so their absence above is meaningful rather than a
            # test asserting against strings the server never had.
            _authorize_now(harness)
            allowed = await client.get(
                f"/api/v1/documents/{_WITHHELD_DOC}/journey", headers=headers
            )
            assert allowed.status_code == 200
            allowed_blob = allowed.content.decode()
            assert "new private body" in allowed_blob
            assert "m365-sharepoint" in allowed_blob
            assert "Servicing SOP.docx" in allowed_blob

    run(scenario())
