# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Gate-checked apply/revert for extraction proposals (design §3.4), mirroring
``er.merge.apply_merge``/``unmerge`` structurally.

``apply_extraction`` refuses unless the NE-066 gate reports APPROVED (BEFORE
any read/write), re-validates the proposal kind + payload shape (F5 clean
refusal, never a bare ``KeyError``/``AttributeError``), resolves EVERY
evidencing chunk's ACL up front, re-runs the F1 ENTITY_CONTRACTS wall over
EVERY ``new_entity`` assertion's properties up front, AND re-checks EVERY
``new_edge`` assertion's ``(subject_class, edge_kind, object_class)`` triple
against ``_EDGE_INDEX`` up front (validate-THEN-write: the graph store is
non-transactional, so a single empty ACL, a single contract-invalid property
set, OR a single invalid edge triple anywhere in the batch means ZERO writes,
not a partial one — all three refusals raise
``ExtractionGateError``/``GraphAclError`` before the first ``upsert``), writes
via
:class:`~ne.brain_graph.extraction.writer.ExtractionWriter` with
``assurance="confirmed"`` (mode-1 §3.4: an approved-then-applied element IS
confirmed), records the apply on the review gate, and emits
``graph.extraction_apply``.

``revert_extraction`` is the reject-path reverse: it refcounts ``evidence``
refs (a node/edge can be evidenced by several proposals — deterministic
identity means a re-evidenced entity converges on ONE key) and deletes only
elements whose evidence set empties AND whose assurance is NOT ``"confirmed"``
(design §3.4: a confirmed element survives a later duplicate-proposal
rejection) via the additive
:meth:`~ne.brain_graph.extraction.writer.ExtractionWriteStore.purge_llm_extracted`.

``write_provisional_extraction`` (NE-T2X-4, modes 2/3) is the IMMEDIATE-write
sibling: it runs the SAME validate-then-write core (refactored into shared
private helpers this unit — ``_resolve_batch_evidence_acls``/
``_validate_new_entities``/``_validate_edge_triples``/
``_write_validated_batch``) against a PROPOSED (not yet approved) batch,
stamping ``assurance="extracted"``, and NEVER calls ``record_apply`` — the
proposal stays ``"proposed"`` and IS the confirmation ledger (design §2.3): a
later ``apply_extraction`` flips the same elements to ``"confirmed"`` via
``ExtractionWriter``'s re-evidence path, and ``revert_extraction`` reverts
them on reject. Its status guard is skip-not-raise (any non-``proposed``
status returns a ``{"skipped_status": ...}`` result without writing), which
is what makes an ``ExtractionRuntime`` composition (NE-T2X-4) safe to re-run
over the SAME content-addressed proposal ids across ingestion re-runs.

Note for readers of the test suite: NE-066's state machine makes ``reject``
reachable only from ``proposed``/``approved`` — there is no "reject an applied
(mode-1-confirmed) proposal" transition. Mode-1's OWN elements are therefore
never directly revertible through THIS code path once applied; the graph-
bearing revert paths are exercised in tests by seeding elements at
``assurance="extracted"`` via the writer directly (the shape modes 2/3 write
immediately) — that is the intended, documented test technique for a spine
this unit shares with NE-T2X-4, not scope creep into modes 2/3.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa
from pydantic import ValidationError

from ne.brain_graph.config import _EDGE_INDEX
from ne.brain_graph.errors import GraphAclError
from ne.brain_graph.extraction import LLM_SOURCE_SYSTEM
from ne.brain_graph.extraction.identity import llm_entity_key
from ne.brain_graph.extraction.writer import ExtractionWriter, validate_extracted_properties
from ne.contracts.governance import AuditSink
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReader,
    SourceAuthorityDecision,
)
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.flywheel.proposals import (
    GraphExtractionAssertion,
    GraphExtractionAssertionKind,
    GraphExtractionBatch,
    ProposalKind,
    ne_fix_proposals,
)
from ne.flywheel.review import ProposalReviewService, ProposalStatus
from ne.flywheel.signals import SignalProvenance, SignalSurface
from ne.schema import AuditOutcome

__all__ = [
    "ChunkAclResolver",
    "ExtractionApplyStore",
    "ExtractionGateError",
    "apply_extraction",
    "revert_extraction",
    "write_provisional_extraction",
]


class ExtractionGateError(Exception):
    """The apply/revert was refused because the gate/kind/payload check failed
    (fail-closed — always raised BEFORE any store write)."""


class ChunkAclResolver(Protocol):
    """Host-composed: resolve an evidencing chunk's CURRENT ACL. An unknown
    ``chunk_ref`` returns ``()`` — which REFUSES the write (fail-closed)."""

    async def acl_for(self, chunk_ref: str) -> tuple[str, ...]: ...


class ExtractionApplyStore(Protocol):
    """The store surface apply/revert need — a superset of
    :class:`~ne.brain_graph.extraction.writer.ExtractionWriteStore` (adds the
    batch scans revert uses to refcount evidence)."""

    async def fetch_nodes(self, keys: Sequence[str]) -> Sequence[Mapping[str, Any]]: ...
    async def upsert_nodes(self, nodes: Sequence[Mapping[str, Any]]) -> None: ...
    async def upsert_edges(self, edges: Sequence[Mapping[str, Any]]) -> None: ...
    async def scan_nodes(self, entity_class: str | None = None) -> Sequence[Mapping[str, Any]]: ...
    async def scan_edges(self) -> Sequence[Mapping[str, Any]]: ...
    async def purge_llm_extracted(self, keys: Sequence[str] | None = None) -> dict[str, int]: ...


def _load_proposal(engine: sa.Engine, proposal_id: str) -> tuple[str, dict[str, Any]]:
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_fix_proposals.c.kind, ne_fix_proposals.c.payload).where(
                ne_fix_proposals.c.proposal_id == proposal_id
            )
        ).first()
    if row is None:
        raise ExtractionGateError(f"proposal {proposal_id!r} is not in ne_fix_proposals")
    return str(row.kind), dict(row.payload)


def _load_extraction_batch(engine: sa.Engine, proposal_id: str) -> GraphExtractionBatch:
    kind, payload = _load_proposal(engine, proposal_id)
    if kind != ProposalKind.GRAPH_EXTRACTION_CANDIDATE.value:
        raise ExtractionGateError(
            f"cannot apply/revert extraction for {proposal_id!r}: proposal kind {kind!r} is not "
            "'graph_extraction_candidate' (fail-closed; nothing written)"
        )
    try:
        return GraphExtractionBatch.model_validate(payload)
    except ValidationError as exc:
        raise ExtractionGateError(
            f"cannot apply/revert extraction for {proposal_id!r}: malformed graph-extraction "
            "payload; nothing written"
        ) from exc


async def _require_current_authority(
    batch: GraphExtractionBatch,
    reader: CurrentSourceAuthorityReader | None,
    *,
    principal_id: str,
    access_context_hash: str,
) -> None:
    if reader is None:
        return
    if not batch.source_authority_stamps:
        raise ExtractionGateError("source authority is unavailable for this extraction")
    for stamp in batch.source_authority_stamps:
        try:
            resolution = await reader.resolve(
                stamp,
                principal_id=principal_id,
                access_context_hash=access_context_hash,
            )
        except Exception as exc:
            raise ExtractionGateError(
                "source authority is unavailable for this extraction"
            ) from exc
        if resolution.decision is not SourceAuthorityDecision.AUTHORIZED:
            raise ExtractionGateError("source authority is unavailable for this extraction")


async def _emit_extraction_audit(
    audit_sink: AuditSink,
    *,
    action: str,
    resource: str,
    detail: Mapping[str, Any],
    actor: str,
    config_hash: str,
) -> None:
    await audit_sink.append(
        new_audit_event(
            action=action,
            resource=resource,
            outcome=AuditOutcome.success,
            actor=actor,
            detail=json.dumps(dict(detail)),
            config_hash=config_hash or None,
            provenance={"source": "ne-brain-graph", "component": "extraction.apply"},
        )
    )


async def _resolve_batch_evidence_acls(
    batch: GraphExtractionBatch,
    acl_resolver: ChunkAclResolver,
    *,
    proposal_id: str,
) -> dict[str, tuple[str, ...]]:
    """Resolve EVERY evidencing chunk's ACL up front (validate-then-write).

    Shared by :func:`apply_extraction` and :func:`write_provisional_extraction`
    (NE-T2X-4) — the SAME up-front resolution, whichever assurance the caller
    is about to stamp.
    """
    acl_by_ref: dict[str, tuple[str, ...]] = {}
    for assertion in batch.assertions:
        if assertion.kind is GraphExtractionAssertionKind.LINK:
            continue
        ref = assertion.evidence_ref
        if ref in acl_by_ref:
            continue
        acl = await acl_resolver.acl_for(ref)
        if not acl:
            raise GraphAclError(
                f"extraction write {proposal_id!r}: empty ACL for evidencing chunk {ref!r}; "
                "nothing written (fail-closed, validate-then-write)"
            )
        acl_by_ref[ref] = acl
    return acl_by_ref


def _validate_new_entities(batch: GraphExtractionBatch, *, proposal_id: str) -> dict[str, str]:
    """Re-validate every ``new_entity`` assertion's identity + F1 contract wall
    up front. Returns ``{recomputed_key: entity_class}`` for every validated
    entity (an edge-endpoint-class map seeded with this batch's OWN new
    entities). Shared by :func:`apply_extraction` and
    :func:`write_provisional_extraction` (NE-T2X-4).

    Resolve edge endpoint classes: created-by-this-batch or already-in-graph;
    anything else refuses BEFORE any write.

    Argus P2 (round 1): ``write_entity`` deliberately RECOMPUTES the node key
    from ``(entity_class, display)`` and never trusts ``assertion.entity_key``
    for the actual write, because a deserialized/stale payload's
    ``entity_key`` may disagree with it. The edge path takes
    ``subject_key``/``object_key`` straight from the deserialized payload, so
    it must independently verify every NEW_ENTITY's declared ``entity_key``
    against the SAME recomputation BEFORE using it to resolve an endpoint —
    otherwise a mismatched payload would write the node under one key and
    wire the edge to another (a dangling edge, or worse, a ``fetch_nodes``
    fallback hit on an unrelated pre-existing node).

    Argus P2 (round 2): the SAME atomicity discipline applies to the F1
    ENTITY_CONTRACTS wall, not just chunk ACLs and entity-key identity —
    ``validate_extracted_properties`` is therefore re-run HERE, in this
    up-front loop, for every NEW_ENTITY assertion, and a failure raises
    ``ExtractionGateError`` (never a bare ``ValidationError``/``ValueError``
    escaping to the caller) BEFORE the first store write. Without this, a
    multi-assertion batch could write its first N-1 entities and only then
    discover the Nth's properties fail contract validation — mid-write, with
    ``record_apply`` never called (the proposal stays "approved" and
    ``revert_extraction`` refuses anything not "rejected"), stranding an
    orphan node reachable only via the wholesale ``extraction purge`` verb.
    Reachable non-adversarially: ``apply_extraction``/
    ``write_provisional_extraction`` take ``schema_version`` as a parameter
    precisely because the generated schema moves over time — a steward
    approving a week-old proposal after a schema regeneration hits exactly
    this path.
    """
    endpoint_classes: dict[str, str] = {}
    for a in batch.assertions:
        if a.kind is not GraphExtractionAssertionKind.NEW_ENTITY:
            continue
        recomputed_key = llm_entity_key(a.entity_class, a.display)
        if a.entity_key != recomputed_key:
            raise ExtractionGateError(
                f"extraction write {proposal_id!r}: assertion row {a.row_id!r} entity_key "
                f"{a.entity_key!r} does not match the identity recomputed from "
                f"(entity_class={a.entity_class!r}, display={a.display!r}) = "
                f"{recomputed_key!r} — refusing a possibly tampered/stale payload "
                "(fail-closed; nothing written)"
            )
        try:
            validate_extracted_properties(a.entity_class, a.properties, recomputed_key)
        except (ValueError, ValidationError) as exc:
            raise ExtractionGateError(
                f"extraction write {proposal_id!r}: assertion row {a.row_id!r} properties fail "
                f"the ENTITY_CONTRACTS wall for entity_class={a.entity_class!r} (fail-closed; "
                "nothing written, validate-then-write)"
            ) from exc
        endpoint_classes[recomputed_key] = a.entity_class
    return endpoint_classes


async def _validate_edge_triples(
    batch: GraphExtractionBatch,
    endpoint_classes: dict[str, str],
    store: ExtractionApplyStore,
    *,
    proposal_id: str,
) -> list[GraphExtractionAssertion]:
    """Re-validate every ``new_edge`` assertion's ``_EDGE_INDEX`` triple up
    front (mutates ``endpoint_classes`` in place with any already-in-graph
    endpoints it resolves). Returns the batch's ``new_edge`` assertions.
    Shared by :func:`apply_extraction` and :func:`write_provisional_extraction`
    (NE-T2X-4).

    Argus P2 (round 3): the SAME atomicity discipline applies to the
    ``_EDGE_INDEX`` triple check — before this fix, that check lived ONLY
    inside ``ExtractionWriter.write_edge``, which runs AFTER the entity-write
    loop. A batch of valid ``NEW_ENTITY`` assertions plus one ``NEW_EDGE``
    whose ``(subject_class, edge_kind, object_class)`` triple is not a valid
    EDGE_DEFS relation would therefore write every entity and only THEN
    discover the edge is invalid, raising mid-batch with the entities already
    persisted (the graph store is non-transactional) — the same
    half-applied-graph failure mode the chunk-ACL and ENTITY_CONTRACTS walls
    already close. Reachable non-adversarially the same way as the
    entity-key check: a stale/tampered stored payload, or an EDGE_DEFS schema
    regeneration between proposal creation and steward approval that removes
    a relation the proposal still names. The triple check therefore runs
    HERE, before ``ExtractionWriter`` is even constructed. The in-writer
    check at ``writer.py``'s ``write_edge`` stays as defence-in-depth for any
    future caller that bypasses this validation.
    """
    edge_assertions = [
        a for a in batch.assertions if a.kind is GraphExtractionAssertionKind.NEW_EDGE
    ]
    for assertion in edge_assertions:
        for key in (assertion.subject_key, assertion.object_key):
            if key in endpoint_classes:
                continue
            fetched = await store.fetch_nodes([key])
            if not fetched:
                raise ExtractionGateError(
                    f"extraction write {proposal_id!r}: edge endpoint {key!r} does not exist in "
                    "the graph and is not created by this batch; nothing written"
                )
            endpoint_classes[key] = str(fetched[0]["entity_class"])
        triple = (
            endpoint_classes[assertion.subject_key],
            assertion.edge_kind,
            endpoint_classes[assertion.object_key],
        )
        if triple not in _EDGE_INDEX:
            raise ExtractionGateError(
                f"extraction write {proposal_id!r}: assertion row {assertion.row_id!r} edge "
                f"triple {triple!r} is not a valid EDGE_DEFS relation (fail-closed; nothing "
                "written, validate-then-write)"
            )
    return edge_assertions


async def _write_validated_batch(
    batch: GraphExtractionBatch,
    edge_assertions: Sequence[GraphExtractionAssertion],
    acl_by_ref: Mapping[str, tuple[str, ...]],
    endpoint_classes: Mapping[str, str],
    writer: ExtractionWriter,
    *,
    proposal_id: str,
    assurance: str,
) -> tuple[int, int, int]:
    """Write entities then edges (so same-batch endpoints resolve). Returns
    ``(nodes_written, edges_written, links_noop)``. Shared by
    :func:`apply_extraction` (``assurance="confirmed"``) and
    :func:`write_provisional_extraction` (``assurance="extracted"``,
    NE-T2X-4) — ONE write core, two entry points (derive, don't
    transliterate)."""
    nodes_written = 0
    edges_written = 0
    links_noop = 0
    for assertion in batch.assertions:
        if assertion.kind is GraphExtractionAssertionKind.LINK:
            links_noop += 1
            continue
        if assertion.kind is GraphExtractionAssertionKind.NEW_ENTITY:
            await writer.write_entity(
                assertion,
                chunk_acl=acl_by_ref[assertion.evidence_ref],
                proposal_id=proposal_id,
                assurance=assurance,
            )
            nodes_written += 1
    for assertion in edge_assertions:
        await writer.write_edge(
            assertion,
            chunk_acl=acl_by_ref[assertion.evidence_ref],
            proposal_id=proposal_id,
            assurance=assurance,
            subject_key=assertion.subject_key,
            subject_class=endpoint_classes[assertion.subject_key],
            object_key=assertion.object_key,
            object_class=endpoint_classes[assertion.object_key],
        )
        edges_written += 1
    return nodes_written, edges_written, links_noop


async def apply_extraction(
    engine: sa.Engine,
    review_service: ProposalReviewService,
    store: ExtractionApplyStore,
    acl_resolver: ChunkAclResolver,
    proposal_id: str,
    *,
    actor: str,
    audit_sink: AuditSink,
    config_hash: str,
    schema_version: str,
    source_authority: CurrentSourceAuthorityReader | None = None,
    authority_access_context_hash: str = "",
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Apply an APPROVED extraction proposal → confirmed nodes/edges. Returns
    ``applied_ref``.

    Fail-closed (F5), gate FIRST: refuses unless the NE-066 gate reports
    APPROVED. Validate-then-write (atomicity discipline, the graph store is
    non-transactional): every evidencing chunk's ACL is resolved, every
    ``new_entity`` assertion's properties are re-checked against the F1
    ENTITY_CONTRACTS wall, AND every ``new_edge`` assertion's
    ``(subject_class, edge_kind, object_class)`` triple is re-checked against
    ``_EDGE_INDEX`` — all BEFORE any write — so an empty ACL, a contract
    violation, or an invalid edge triple anywhere in the batch means ZERO
    writes, never a partial one.

    NE-T2X-4: the validation + write core (``_resolve_batch_evidence_acls``/
    ``_validate_new_entities``/``_validate_edge_triples``/
    ``_write_validated_batch``) is SHARED with
    :func:`write_provisional_extraction` — this function's own behavior is
    byte-identical to before that refactor.
    """
    status = review_service.status_of(proposal_id)
    if status is not ProposalStatus.APPROVED:
        raise ExtractionGateError(
            f"cannot apply extraction for {proposal_id!r}: status is {status.value!r}, not "
            "'approved' (the NE-066 gate must approve first; fail-closed)"
        )

    batch = _load_extraction_batch(engine, proposal_id)
    await _require_current_authority(
        batch,
        source_authority,
        principal_id=actor,
        access_context_hash=authority_access_context_hash,
    )
    acl_by_ref = await _resolve_batch_evidence_acls(batch, acl_resolver, proposal_id=proposal_id)
    endpoint_classes = _validate_new_entities(batch, proposal_id=proposal_id)
    edge_assertions = await _validate_edge_triples(
        batch, endpoint_classes, store, proposal_id=proposal_id
    )

    writer = ExtractionWriter(
        store, config_hash=config_hash, schema_version=schema_version, clock=clock
    )
    nodes_written, edges_written, links_noop = await _write_validated_batch(
        batch,
        edge_assertions,
        acl_by_ref,
        endpoint_classes,
        writer,
        proposal_id=proposal_id,
        assurance="confirmed",
    )

    applied_ref = f"extraction:{nodes_written}n:{edges_written}e"
    provenance = SignalProvenance(
        principal=Principal(id=actor),
        surface=SignalSurface.API,
        session_id=f"extract:{proposal_id}",
    )
    review_service.record_apply(proposal_id, provenance, applied_ref=applied_ref)
    await _emit_extraction_audit(
        audit_sink,
        action="graph.extraction_apply",
        resource=f"graph://extraction/apply/{proposal_id}",
        detail={
            "proposal_id": proposal_id,
            "nodes": nodes_written,
            "edges": edges_written,
            "links_noop": links_noop,
        },
        actor=actor,
        config_hash=config_hash,
    )
    return applied_ref


async def write_provisional_extraction(
    engine: sa.Engine,
    review_service: ProposalReviewService,
    store: ExtractionApplyStore,
    acl_resolver: ChunkAclResolver,
    proposal_id: str,
    *,
    actor: str,
    audit_sink: AuditSink,
    config_hash: str,
    schema_version: str,
    source_authority: CurrentSourceAuthorityReader | None = None,
    authority_access_context_hash: str = "",
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Write a PROPOSED extraction batch IMMEDIATELY, at ``assurance="extracted"``
    (design §3.4, modes 2/3 — NE-T2X-4).

    **The proposal stays ``proposed`` — this is the mode-2/3 confirmation
    ledger (design §2.3), not a gate.** Unlike :func:`apply_extraction`, this
    function NEVER calls ``review_service.record_apply``: the write happens
    BEFORE any human vouching, and the SAME proposal row is what a steward
    later approves (flipping the written elements to ``"confirmed"`` via
    ``apply_extraction``'s existing re-evidence path — see
    ``ExtractionWriter.write_entity``'s docstring) or rejects (reverting via
    :func:`revert_extraction`, which already handles ``assurance="extracted"``
    elements — see that function's docstring).

    **Status guard: PROPOSED-only, skip-not-raise.** Requires the gate report
    ``PROPOSED``; any OTHER status (``approved``/``applied``/``rejected``)
    returns ``{"skipped_status": <status>}`` WITHOUT writing anything —
    deliberately a skip, not a raised error. An ``ExtractionRuntime`` (NE-T2X-4)
    composition iterates ``ExtractionRunReport.batch_proposal_ids`` across
    RE-RUNS of the same source; those ids are content-addressed and therefore
    IDENTICAL across runs over unchanged chunks (E-P1-1), so a re-run
    naturally re-presents proposal ids whose batch was already
    written-and-since-approved/rejected. Raising there would make every
    re-run after the first human action fail; skipping makes the composition
    idempotent and self-healing — an approved/rejected proposal is simply
    left alone, exactly the confirmation-ledger semantics design §2.3
    describes.

    Uses the SAME validate-then-write core as :func:`apply_extraction`
    (shared helpers above) — an empty evidencing-chunk ACL, a contract
    violation, or an invalid edge triple anywhere in the batch means ZERO
    writes here too.

    Emits ``graph.extraction_write`` (counts-and-ids only, C12 — the
    ``graph.extraction_apply`` detail grammar) with
    ``{proposal_id, nodes, edges, links_noop}``. Returns
    ``{"nodes": int, "edges": int, "links_noop": int}`` on a real write.
    """
    status = review_service.status_of(proposal_id)
    if status is not ProposalStatus.PROPOSED:
        return {"skipped_status": status.value}

    batch = _load_extraction_batch(engine, proposal_id)  # kind/payload guard, fail-closed
    await _require_current_authority(
        batch,
        source_authority,
        principal_id=actor,
        access_context_hash=authority_access_context_hash,
    )
    acl_by_ref = await _resolve_batch_evidence_acls(batch, acl_resolver, proposal_id=proposal_id)
    endpoint_classes = _validate_new_entities(batch, proposal_id=proposal_id)
    edge_assertions = await _validate_edge_triples(
        batch, endpoint_classes, store, proposal_id=proposal_id
    )

    writer = ExtractionWriter(
        store, config_hash=config_hash, schema_version=schema_version, clock=clock
    )
    nodes_written, edges_written, links_noop = await _write_validated_batch(
        batch,
        edge_assertions,
        acl_by_ref,
        endpoint_classes,
        writer,
        proposal_id=proposal_id,
        assurance="extracted",
    )

    await _emit_extraction_audit(
        audit_sink,
        action="graph.extraction_write",
        resource=f"graph://extraction/write/{proposal_id}",
        detail={
            "proposal_id": proposal_id,
            "nodes": nodes_written,
            "edges": edges_written,
            "links_noop": links_noop,
        },
        actor=actor,
        config_hash=config_hash,
    )
    return {"nodes": nodes_written, "edges": edges_written, "links_noop": links_noop}


async def revert_extraction(
    engine: sa.Engine,
    review_service: ProposalReviewService,
    store: ExtractionApplyStore,
    proposal_id: str,
    *,
    actor: str,
    audit_sink: AuditSink,
    config_hash: str,
) -> dict[str, int]:
    """Reverse a REJECTED extraction proposal: evidence-refcounted delete.

    Removes this proposal's evidence refs from every ``ne:llm`` node/edge that
    carries one; an element whose evidence set thereby EMPTIES AND whose
    ``assurance`` is not ``"confirmed"`` is deleted (via the additive,
    source-scoped :meth:`purge_llm_extracted`); a confirmed element, or one
    still evidenced by another proposal, survives with just its refs trimmed
    — PROVIDED it is not itself collateral of a node deletion.

    **The confirmed-survives rule is second to the dangling-edge rule, not
    equal to it (Argus P3).** ``purge_llm_extracted`` is DETACH semantics
    (see its docstring): deleting a node ALSO removes every edge incident to
    it, of ANY source_system and ANY assurance — a graph store cannot leave
    an edge pointing at a vertex that no longer exists. So an ``ne:llm`` edge
    with ``assurance="confirmed"`` that happens to be incident to a node this
    call deletes is collateral-removed too; only an edge that is NEITHER
    itself evidence-emptied-and-unconfirmed NOR incident to a deleted node is
    guaranteed to survive. Unreachable in the shipped mode-1 path today
    (``apply_extraction`` always stamps ``assurance="confirmed"`` on nodes,
    and this module never deletes a confirmed node on its own), but stated
    here because NE-T2X-4's modes 2/3 write ``assurance="extracted"`` nodes
    directly and WILL hit this interaction.
    """
    status = review_service.status_of(proposal_id)
    if status is not ProposalStatus.REJECTED:
        raise ExtractionGateError(
            f"cannot revert extraction for {proposal_id!r}: status is {status.value!r}, not "
            "'rejected' (fail-closed)"
        )
    _load_extraction_batch(engine, proposal_id)  # kind/payload guard, result unused here

    prefix = f"{proposal_id}|"
    to_delete: set[str] = set()
    to_update_nodes: list[dict[str, Any]] = []
    to_update_edges: list[dict[str, Any]] = []
    refs_removed = 0

    for node in await store.scan_nodes():
        if node.get("source_system") != LLM_SOURCE_SYSTEM:
            continue
        evidence = [str(e) for e in (node.get("evidence") or [])]
        matching = [e for e in evidence if e.startswith(prefix)]
        if not matching:
            continue
        refs_removed += len(matching)
        remaining = [e for e in evidence if not e.startswith(prefix)]
        if not remaining and node.get("assurance") != "confirmed":
            to_delete.add(str(node["key"]))
        else:
            updated = dict(node)
            updated["evidence"] = remaining
            to_update_nodes.append(updated)

    for edge in await store.scan_edges():
        if edge.get("source_system") != LLM_SOURCE_SYSTEM:
            continue
        evidence = [str(e) for e in (edge.get("evidence") or [])]
        matching = [e for e in evidence if e.startswith(prefix)]
        if not matching:
            continue
        refs_removed += len(matching)
        remaining = [e for e in evidence if not e.startswith(prefix)]
        if not remaining and edge.get("assurance") != "confirmed":
            to_delete.add(str(edge["key"]))
        else:
            updated = dict(edge)
            updated["evidence"] = remaining
            to_update_edges.append(updated)

    if to_update_nodes:
        await store.upsert_nodes(to_update_nodes)
    if to_update_edges:
        await store.upsert_edges(to_update_edges)
    purge_counts = {"nodes": 0, "edges": 0}
    if to_delete:
        purge_counts = await store.purge_llm_extracted(keys=list(to_delete))

    result = {
        "nodes_deleted": purge_counts.get("nodes", 0),
        "edges_deleted": purge_counts.get("edges", 0),
        "refs_removed": refs_removed,
    }
    await _emit_extraction_audit(
        audit_sink,
        action="graph.extraction_revert",
        resource=f"graph://extraction/revert/{proposal_id}",
        detail={"proposal_id": proposal_id, **result},
        actor=actor,
        config_hash=config_hash,
    )
    return result
