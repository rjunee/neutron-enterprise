# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ACL-trimmed document lineage and as-of retrieval routes (BD-3).

The lineage store retains historical ACLs with historical content.  Every row is
filtered against the authenticated principal *before* content, hashes, stage
events, or diffs are projected.  A hidden document/version is therefore the same
404 as an absent one; the console cannot use this surface as an existence oracle.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime
from typing import Annotated, Literal

import anyio.to_thread
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict

from ne.api.deps import (
    ApiState,
    api_state,
    bind_authenticated_request_context,
    current_principal,
)
from ne.audit.store import ne_audit_log
from ne.brain.lineage import (
    DocumentComparison,
    DocumentCursorError,
    DocumentJourney,
    DocumentStage,
    DocumentStageEvent,
    DocumentStageEvidence,
    DocumentSummary,
    DocumentType,
    DocumentVersion,
    GraphEvidenceEntity,
    GraphEvidenceView,
)
from ne.brain.trimming import acl_match_labels
from ne.contracts.types import Principal
from ne.runtime.source_results import (
    MissingAuthenticatedContextError,
    require_authenticated_context,
)

_NO_STORE = "private, no-store"


async def _private_no_store(response: Response) -> None:
    """Every response here is principal-specific and ACL-trimmed at read time.

    A shared or replayed cache entry would serve one principal's trimmed
    projection to another, so caching is refused for the whole router rather
    than per-route, where a later route could forget it.
    """

    response.headers["Cache-Control"] = _NO_STORE


_ABSENT_DOCUMENT = "document not found"
_ABSENT_STAGE = "document stage not found"


def _absent(detail: str) -> HTTPException:
    """The ONE not-found response, shared by the hidden and the never-existed case.

    Both callers raise this same object shape, so status, body and headers are
    byte-identical between them; separating the two would hand the caller an
    existence oracle.
    """

    return HTTPException(status_code=404, detail=detail, headers={"Cache-Control": _NO_STORE})


router = APIRouter(
    dependencies=[Depends(bind_authenticated_request_context), Depends(_private_no_store)]
)

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]

_GRAPH_EVIDENCE_LIMIT = 100
_SEMANTIC_CHUNK_LIMIT = 100
_GRAPH_EVIDENCE_AVAILABLE = "principal-visible version-bound graph evidence available"
_GRAPH_EVIDENCE_EMPTY_DETAIL = "no principal-visible version-bound graph evidence"
_GRAPH_EVIDENCE_UNAVAILABLE = "version-bound graph evidence receipt unavailable"
_GRAPH_EVIDENCE_EMPTY = "no visible graph evidence"
_UNBOUND_AUDIT_DETAIL = "evidence unavailable: no bound audit receipt"


async def _currently_authorized(
    state: ApiState, principal: Principal, document_id: str, version: int
) -> bool:
    """Whether the served version is currently source-authorized, as a bool.

    Two-valued by construction: the configured authorizer
    (``ApiState.current_document_authorizer``) returns ``bool``, so
    ``permanent_withheld`` and ``retryable_unknown`` are NOT distinguishable
    here and nothing this router emits carries that distinction.  A refusal and
    a transient failure are the same ``False`` and the same 404.  Separating
    them requires widening the authorizer's own return type in
    ``ne/api/platform.py`` and the fixture that binds it in
    ``tests/conftest.py``; both are outside this module.
    """

    authorizer = state.current_document_authorizer
    if authorizer is None:
        return False
    try:
        context = require_authenticated_context()
        return bool(await authorizer(document_id, version, principal, context.context_hash))
    except (MissingAuthenticatedContextError, TypeError, ValueError):
        return False


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class DocumentSummaryView(_View):
    document_id: str
    display_name: str
    source_system: str
    document_type: str
    version_seq: int
    content_hash: str
    ingested_at: datetime | None
    indexing: bool


class DocumentListView(_View):
    documents: list[DocumentSummaryView]
    next_cursor: str | None
    document_types: list[str]
    source_systems: list[str]
    # INTERIM (PD-DOC-TITLES): honest count of ".negraph.json" rows in the
    # ACL-visible corpus (not the page). REMOVE alongside the rest of
    # PD-DOC-TITLES when the real `structured` flag lands on the document wire.
    operational_graph_count: int


class DocumentStageView(_View):
    stage: str
    status: str
    occurred_at: datetime | None
    audit_seq: int | None
    detail: str


class DocumentVersionView(_View):
    version_seq: int
    content_hash: str
    ingested_at: datetime | None
    ingested_audit_seq: int | None
    superseded_at: datetime | None
    superseded_audit_seq: int | None
    chunk_count: int
    status: str


class DocumentComparisonView(_View):
    state: str
    predecessor_version: int | None


class DocumentJourneyView(_View):
    document_id: str
    display_name: str
    source_system: str
    document_type: str
    selected_version: int
    selected_content_hash: str
    selected_content: str
    diff: str | None
    comparison: DocumentComparisonView
    stages: list[DocumentStageView]
    versions: list[DocumentVersionView]


class DocumentStageFactView(_View):
    key: str
    label: str
    value: str | int | bool | None
    available: bool


class DocumentStageArtifactView(_View):
    kind: str
    label: str
    state: str
    ordinal: int
    identifier: str | None
    size_bytes: int | None
    artifact_count: int | None
    dimensions: int | None
    model: str | None
    index_reference: str | None
    fingerprint: str | None
    numeric_preview: list[float] | None
    reason: str | None


class DocumentSemanticChunkView(_View):
    identifier: str
    size_bytes: int


class DocumentSemanticChunksView(_View):
    state: Literal["available", "unavailable"]
    reason: str | None
    chunks: list[DocumentSemanticChunkView]
    truncated: bool


class DocumentGraphEntityView(_View):
    key: str
    entity_class: str
    display_name: str
    source_system: str


class DocumentGraphRelationshipView(_View):
    key: str
    edge_kind: str
    subject: DocumentGraphEntityView
    object: DocumentGraphEntityView


class DocumentGraphEvidenceView(_View):
    state: Literal["available", "empty", "skipped", "unavailable"]
    reason: str | None
    entities: list[DocumentGraphEntityView]
    relationships: list[DocumentGraphRelationshipView]
    truncated: bool


class DocumentStageEvidenceView(_View):
    document_id: str
    version: int
    content_hash: str
    document_type: str
    stage: str
    status: str
    occurred_at: datetime | None
    audit_seq: int | None
    detail: str
    facts: list[DocumentStageFactView]
    artifacts: list[DocumentStageArtifactView]
    comparison: DocumentComparisonView
    semantic_chunks: DocumentSemanticChunksView | None = None
    graph_evidence: DocumentGraphEvidenceView | None = None


def _audit_sequences(engine: sa.Engine, event_ids: set[str]) -> dict[str, int]:
    if not event_ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(ne_audit_log.c.event_id, ne_audit_log.c.seq).where(
                ne_audit_log.c.event_id.in_(sorted(event_ids))
            )
        )
    return {str(event_id): int(seq) for event_id, seq in rows}


def _summary_view(row: DocumentSummary) -> DocumentSummaryView:
    return DocumentSummaryView(
        document_id=row.document_id,
        display_name=row.display_name,
        source_system=row.source_system,
        document_type=row.document_type.value,
        version_seq=row.version_seq,
        content_hash=row.content_hash,
        ingested_at=row.ingested_at,
        indexing=row.indexing,
    )


def _comparison_view(comparison: DocumentComparison) -> DocumentComparisonView:
    return DocumentComparisonView(
        state=comparison.state.value,
        predecessor_version=comparison.predecessor_version_seq,
    )


def _graph_entity_view(entity: GraphEvidenceEntity) -> DocumentGraphEntityView:
    """Project only link/render fields; receipt ACLs and hashes never cross HTTP."""
    return DocumentGraphEntityView(
        key=entity.key,
        entity_class=entity.entity_class,
        display_name=entity.display_name,
        source_system=entity.source_system,
    )


def _graph_evidence_view(
    evidence: DocumentStageEvidence,
    graph_evidence: GraphEvidenceView | None,
    audit_seq: int | None,
) -> DocumentGraphEvidenceView | None:
    event = evidence.event
    if event.stage is not DocumentStage.GRAPH:
        return None
    if graph_evidence is None:
        if event.status == "skipped":
            return DocumentGraphEvidenceView(
                state="skipped",
                reason=event.detail,
                entities=[],
                relationships=[],
                truncated=False,
            )
        return DocumentGraphEvidenceView(
            state="unavailable",
            reason=_GRAPH_EVIDENCE_UNAVAILABLE,
            entities=[],
            relationships=[],
            truncated=False,
        )
    if (
        event.audit_event_id is None
        or audit_seq is None
        or graph_evidence.audit_event_id != event.audit_event_id
    ):
        return DocumentGraphEvidenceView(
            state="unavailable",
            reason=_GRAPH_EVIDENCE_UNAVAILABLE,
            entities=[],
            relationships=[],
            truncated=False,
        )

    entities = list(graph_evidence.entities[:_GRAPH_EVIDENCE_LIMIT])
    relationships = list(graph_evidence.relationships[:_GRAPH_EVIDENCE_LIMIT])
    has_visible_evidence = bool(graph_evidence.entities or graph_evidence.relationships)
    return DocumentGraphEvidenceView(
        state="available" if has_visible_evidence else "empty",
        reason=None if has_visible_evidence else _GRAPH_EVIDENCE_EMPTY,
        entities=[_graph_entity_view(entity) for entity in entities],
        relationships=[
            DocumentGraphRelationshipView(
                key=relationship.key,
                edge_kind=relationship.edge_kind,
                subject=_graph_entity_view(relationship.subject),
                object=_graph_entity_view(relationship.object),
            )
            for relationship in relationships
        ],
        truncated=(
            len(graph_evidence.entities) > _GRAPH_EVIDENCE_LIMIT
            or len(graph_evidence.relationships) > _GRAPH_EVIDENCE_LIMIT
        ),
    )


def _stage_evidence_view(
    evidence: DocumentStageEvidence,
    audit_engine: sa.Engine,
    graph_evidence: GraphEvidenceView | None = None,
    *,
    show_acl_labels: bool = False,
) -> DocumentStageEvidenceView:
    event = evidence.event
    seqs = _audit_sequences(
        audit_engine,
        {event.audit_event_id} if event.audit_event_id is not None else set(),
    )
    audit_seq = seqs.get(event.audit_event_id) if event.audit_event_id is not None else None
    unbound_audit = audit_seq is None
    graph_view = _graph_evidence_view(evidence, graph_evidence, audit_seq)
    detail = event.detail
    if graph_view is not None and graph_view.state != "skipped":
        detail = {
            "available": _GRAPH_EVIDENCE_AVAILABLE,
            "empty": _GRAPH_EVIDENCE_EMPTY_DETAIL,
            "unavailable": _GRAPH_EVIDENCE_UNAVAILABLE,
        }[graph_view.state]
    semantic_view = None
    if event.stage is DocumentStage.CHUNK:
        available = [
            (artifact.identifier, artifact.size_bytes)
            for artifact in evidence.semantic_chunks
            if audit_seq is not None
            and artifact.state.value == "available"
            and artifact.identifier is not None
            and artifact.size_bytes is not None
        ]
        semantic_view = DocumentSemanticChunksView(
            state="available" if available else "unavailable",
            reason=(
                None
                if available
                else ("unbound_audit_receipt" if unbound_audit else "semantic_chunks_not_recorded")
            ),
            chunks=[
                DocumentSemanticChunkView(
                    identifier=identifier,
                    size_bytes=size_bytes,
                )
                for identifier, size_bytes in available[:_SEMANTIC_CHUNK_LIMIT]
            ],
            truncated=len(available) > _SEMANTIC_CHUNK_LIMIT,
        )
    if unbound_audit:
        detail = "evidence unavailable: no bound audit receipt"

    def fact_view(fact) -> DocumentStageFactView:
        return DocumentStageFactView(
            key=fact.key,
            label=fact.label,
            value=(
                None
                if unbound_audit
                else (
                    detail
                    if event.stage is DocumentStage.GRAPH and fact.key == "graph_result"
                    else (
                        ", ".join(evidence.acl_labels)
                        if event.stage is DocumentStage.ACL
                        and fact.key == "acl_labels"
                        and show_acl_labels
                        else fact.value
                    )
                )
            ),
            available=(
                False
                if unbound_audit
                else (
                    True
                    if event.stage is DocumentStage.ACL
                    and fact.key == "acl_labels"
                    and show_acl_labels
                    else fact.available
                )
            ),
        )

    return DocumentStageEvidenceView(
        document_id=evidence.document_id,
        version=evidence.version_seq,
        content_hash=evidence.content_hash,
        document_type=evidence.document_type.value,
        stage=event.stage.value,
        status="unavailable" if unbound_audit else event.status,
        occurred_at=None if unbound_audit else event.occurred_at,
        audit_seq=audit_seq,
        detail=detail,
        facts=[fact_view(fact) for fact in evidence.facts],
        artifacts=[
            DocumentStageArtifactView(
                kind=artifact.kind.value,
                label=(
                    "Record chunk" if artifact.kind.value == "chunk" else "Vector artifact receipt"
                ),
                state="unavailable" if unbound_audit else artifact.state.value,
                ordinal=artifact.ordinal,
                identifier=None if unbound_audit else artifact.identifier,
                size_bytes=None if unbound_audit else artifact.size_bytes,
                artifact_count=None if unbound_audit else artifact.artifact_count,
                dimensions=None if unbound_audit else artifact.dimensions,
                model=None if unbound_audit else artifact.model_id,
                index_reference=None if unbound_audit else artifact.index_reference,
                fingerprint=None if unbound_audit else artifact.fingerprint,
                numeric_preview=(
                    None
                    if unbound_audit or artifact.numeric_preview is None
                    else list(artifact.numeric_preview)
                ),
                reason="unbound_audit_receipt" if unbound_audit else artifact.reason,
            )
            for artifact in evidence.artifacts
        ],
        comparison=_comparison_view(evidence.comparison),
        semantic_chunks=semantic_view,
        graph_evidence=(
            DocumentGraphEvidenceView(
                state="unavailable",
                reason="evidence unavailable: no bound audit receipt",
                entities=[],
                relationships=[],
                truncated=False,
            )
            if unbound_audit and event.stage is DocumentStage.GRAPH
            else graph_view
        ),
    )


def _journey_view(journey: DocumentJourney, audit_engine: sa.Engine) -> DocumentJourneyView:
    event_ids = {
        event_id
        for version in journey.versions
        for event_id in (
            version.ingested_event_id,
            version.superseded_event_id,
            *(stage.audit_event_id for stage in version.stages),
        )
        if event_id is not None
    }
    seqs = _audit_sequences(audit_engine, event_ids)

    def version_view(version: DocumentVersion) -> DocumentVersionView:
        return DocumentVersionView(
            version_seq=version.version_seq,
            content_hash=version.content_hash,
            ingested_at=version.ingested_at,
            ingested_audit_seq=(
                seqs.get(version.ingested_event_id)
                if version.ingested_event_id is not None
                else None
            ),
            superseded_at=version.superseded_at,
            superseded_audit_seq=(
                seqs.get(version.superseded_event_id)
                if version.superseded_event_id is not None
                else None
            ),
            chunk_count=version.chunk_count,
            status=version.status,
        )

    def stage_view(stage: DocumentStageEvent) -> DocumentStageView:
        audit_seq = seqs.get(stage.audit_event_id) if stage.audit_event_id is not None else None
        return DocumentStageView(
            stage=stage.stage.value,
            status="unavailable" if audit_seq is None else stage.status,
            occurred_at=None if audit_seq is None else stage.occurred_at,
            audit_seq=audit_seq,
            detail=_UNBOUND_AUDIT_DETAIL if audit_seq is None else stage.detail,
        )

    selected = journey.selected
    return DocumentJourneyView(
        document_id=selected.document_id,
        display_name=selected.display_name,
        source_system=selected.source_system,
        document_type=selected.document_type.value,
        selected_version=selected.version_seq,
        selected_content_hash=selected.content_hash,
        selected_content=selected.content,
        diff=journey.diff,
        comparison=_comparison_view(journey.comparison),
        stages=[stage_view(stage) for stage in selected.stages],
        versions=[version_view(version) for version in journey.versions],
    )


@router.get("/v1/documents")
async def list_documents(
    principal: _Principal,
    state: _State,
    q: Annotated[str | None, Query(max_length=200)] = None,
    document_type: Annotated[DocumentType | None, Query(alias="type")] = None,
    source: Annotated[str | None, Query(max_length=128)] = None,
    # INTERIM (PD-DOC-TITLES): structured-ness inferred from the
    # ".negraph.json" connector filename convention; REMOVE when the real
    # `structured` flag lands on the document wire. Default (absent/false)
    # EXCLUDES operational-graph rows — the directory's default is prose
    # documents only. `structured=true` is the explicit opt-in that puts
    # them back in the page.
    structured: Annotated[bool, Query()] = False,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> DocumentListView:
    labels = acl_match_labels(principal, state.expander)
    try:
        page = await anyio.to_thread.run_sync(
            functools.partial(
                state.document_lineage.list_document_page,
                labels,
                query=q,
                document_type=document_type,
                source_system=source,
                include_structured=structured,
                cursor=cursor,
                limit=limit,
            )
        )
    except DocumentCursorError:
        raise HTTPException(status_code=422, detail="invalid document cursor") from None
    visible = [
        row
        for row in page.documents
        if await _currently_authorized(state, principal, row.document_id, row.version_seq)
    ]
    return DocumentListView(
        documents=[_summary_view(row) for row in visible],
        next_cursor=page.next_cursor,
        document_types=[kind.value for kind in page.document_types],
        source_systems=list(page.source_systems),
        # INTERIM (PD-DOC-TITLES): REMOVE alongside the rest of PD-DOC-TITLES.
        operational_graph_count=page.operational_graph_count,
    )


@router.get("/v1/documents/{document_id}/versions/{version}/stages/{stage}")
async def document_stage_evidence(
    document_id: str,
    version: Annotated[int, Path(ge=1)],
    stage: DocumentStage,
    principal: _Principal,
    state: _State,
) -> DocumentStageEvidenceView:
    labels = acl_match_labels(principal, state.expander)
    # Same fixed order for every outcome: authorize, then probe, then decide. The
    # probe is ACL-trimmed and its result is discarded unless authorized, so an
    # unauthorized caller cannot learn that the stage exists from the work done.
    #
    # Authority is evaluated against the document's CURRENT version, mirroring
    # the journey route: a superseded version's retained receipt can never match
    # the current authority head, and stage evidence for historical versions
    # must stay readable while the document itself is still admitted for this
    # principal.  Per-version ACL trimming happens inside ``stage_evidence``.
    current_seq = await anyio.to_thread.run_sync(
        state.document_lineage.current_version_seq, document_id
    )
    authorized = await _currently_authorized(
        state, principal, document_id, current_seq if current_seq is not None else version
    )
    evidence = await anyio.to_thread.run_sync(
        state.document_lineage.stage_evidence,
        document_id,
        version,
        stage,
        labels,
    )
    if not authorized or evidence is None:
        raise _absent(_ABSENT_STAGE)
    graph_evidence = None
    if stage is DocumentStage.GRAPH:
        graph_evidence = await anyio.to_thread.run_sync(
            state.document_lineage.graph_evidence,
            document_id,
            version,
            labels,
        )
    return await anyio.to_thread.run_sync(
        functools.partial(
            _stage_evidence_view,
            evidence,
            state.audit_engine,
            graph_evidence,
            show_acl_labels=(
                bool(state.settings.ops_roles)
                and not set(principal.groups).isdisjoint(state.settings.ops_roles)
            ),
        )
    )


@router.get("/v1/documents/{document_id}/journey")
async def document_journey(
    document_id: str,
    principal: _Principal,
    state: _State,
    version: Annotated[int | None, Query(ge=1)] = None,
    as_of: datetime | None = None,
) -> DocumentJourneyView:
    if version is not None and as_of is not None:
        raise HTTPException(status_code=422, detail="choose version or as_of, not both")
    if as_of is not None and as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    labels = acl_match_labels(principal, state.expander)
    journey = await anyio.to_thread.run_sync(
        functools.partial(
            state.document_lineage.journey,
            document_id,
            labels,
            version_seq=version,
            as_of=as_of,
        )
    )
    # Authority is resolved for BOTH outcomes, in this fixed order, so an absent
    # document and one hidden from this principal cost the same calls in the same
    # sequence.  Short-circuiting on ``journey is None`` would skip the authorizer
    # and make "never existed" observably cheaper than "withheld from you".
    #
    # The version handed to the authorizer is the document's CURRENT one, not the
    # selection: a superseded version's own retained receipt can never equal the
    # current authority head again (its source revision moved on the moment it
    # was superseded), so evaluating the historical stamp would make every
    # version-history and as-of read permanently 404 the instant a document
    # gains a second version.  Document-level authority ("may this principal
    # currently see this document at all") lives on the current version's
    # receipt; per-version visibility was already ACL-trimmed inside
    # ``journey()`` over each version's retained historical ACL.
    current_seq = await anyio.to_thread.run_sync(
        state.document_lineage.current_version_seq, document_id
    )
    authorized = await _currently_authorized(
        state,
        principal,
        document_id,
        current_seq
        if current_seq is not None
        else (journey.selected.version_seq if journey is not None else (version or 1)),
    )
    if journey is None or not authorized:
        raise _absent(_ABSENT_DOCUMENT)
    return await anyio.to_thread.run_sync(
        functools.partial(_journey_view, journey, state.audit_engine)
    )
