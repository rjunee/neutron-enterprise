# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Append-only fix-proposal store tests (NE-065)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from ne.contracts.source_authority import (
    M365SourcePoint,
    SourceAuthorityStamp,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import (
    FixProposal,
    GraphExtractionAssertion,
    GraphExtractionAssertionKind,
    GraphExtractionBatch,
    ProposalKind,
    SqlAlchemyProposalStore,
    graph_extraction_proposal_id,
    graph_extraction_row_id,
)
from ne.schema import DocumentEntity, OrderEntity, TicketEntity
from ne.schema.generated.operational import DocumentType, OrderStatus, TicketStatus


def _proposal(
    *,
    proposal_id: str = "prop-1",
    kind: ProposalKind = ProposalKind.FIX,
    lane: FixLane = FixLane.CONTENT,
    cluster_id: str = "cluster-1",
    confidence: float = 0.5,
    priority: ProposalPriority = ProposalPriority.LOW,
    summary: str = "propose a source-document fix",
    status: str = "proposed",
) -> FixProposal:
    return FixProposal(
        proposal_id=proposal_id,
        kind=kind,
        lane=lane,
        cluster_id=cluster_id,
        evidence_event_ids=("ev-1", "ev-2"),
        evidence_signal_ids=(1, 2),
        summary=summary,
        confidence=confidence,
        priority=priority,
        status=status,
        config_hash="deadbeef",
        created_at=datetime.now(UTC),
        payload={"note": "x"},
    )


def test_append_and_read_roundtrip_preserves_all_fields(
    curation_engine: sa.Engine,
) -> None:
    store = SqlAlchemyProposalStore(curation_engine)
    proposal = _proposal()
    with curation_engine.begin() as conn:
        row_id = store.append(conn, proposal)

    rows = store.read_proposals()
    assert len(rows) == 1
    row = rows[0]
    assert row.id == row_id
    assert row.proposal_id == "prop-1"
    assert row.kind == ProposalKind.FIX.value
    assert row.lane == FixLane.CONTENT.value
    assert row.cluster_id == "cluster-1"
    assert row.status == "proposed"
    assert row.summary == "propose a source-document fix"
    assert row.confidence == 0.5
    assert row.priority == ProposalPriority.LOW.value
    assert row.evidence_event_ids == ("ev-1", "ev-2")
    assert row.evidence_signal_ids == (1, 2)
    assert row.payload == {"note": "x"}
    assert row.config_hash == "deadbeef"
    assert row.created_at.tzinfo is not None


def test_append_is_idempotent_on_duplicate_proposal_id(
    curation_engine: sa.Engine,
) -> None:
    store = SqlAlchemyProposalStore(curation_engine)
    proposal = _proposal(proposal_id="dup")
    with curation_engine.begin() as conn:
        first = store.append(conn, proposal)
        again = store.append(conn, proposal)
        # The enclosing transaction is still usable after the SAVEPOINT rollback.
        extra = store.append(conn, _proposal(proposal_id="other"))
    assert first == again
    assert extra != first
    assert len(store.read_proposals()) == 2


def test_store_mutation_surface_is_append_only(curation_engine: sa.Engine) -> None:
    store = SqlAlchemyProposalStore(curation_engine)
    public_methods = {
        name for name in dir(store) if not name.startswith("_") and callable(getattr(store, name))
    }
    forbidden = {"update", "delete", "edit", "remove", "modify"}
    assert not (public_methods & forbidden)
    assert "append" in public_methods


def test_proposal_rejects_status_other_than_proposed() -> None:
    with pytest.raises(ValidationError, match="only proposes"):
        _proposal(status="approved")


def test_proposal_rejects_out_of_range_confidence_and_empty_summary() -> None:
    with pytest.raises(ValidationError, match=r"confidence must be in \[0, 1\]"):
        _proposal(confidence=1.5)
    with pytest.raises(ValidationError, match="summary must be a non-empty"):
        _proposal(summary="   ")


def test_read_proposals_filters_by_lane_kind_status_and_paginates(
    curation_engine: sa.Engine,
) -> None:
    store = SqlAlchemyProposalStore(curation_engine)
    content = _proposal(proposal_id="c", lane=FixLane.CONTENT)
    retrieval = _proposal(proposal_id="r", lane=FixLane.RETRIEVAL)
    golden = _proposal(
        proposal_id="g",
        lane=FixLane.RETRIEVAL,
        kind=ProposalKind.GOLDEN_SET_CANDIDATE,
    )
    with curation_engine.begin() as conn:
        ids = [store.append(conn, p) for p in (content, retrieval, golden)]

    assert [r.proposal_id for r in store.read_proposals(lane="retrieval")] == ["r", "g"]
    assert [
        r.proposal_id for r in store.read_proposals(kind=ProposalKind.GOLDEN_SET_CANDIDATE.value)
    ] == ["g"]
    assert len(store.read_proposals(status="proposed")) == 3

    page = store.read_proposals(after_id=ids[0], limit=1)
    assert [r.proposal_id for r in page] == ["r"]

    assert store.count_by_lane() == {"content": 1, "retrieval": 2}


def test_read_proposal_direct_lookup_by_id(curation_engine: sa.Engine) -> None:
    """Direct id lookup returns the row (or None), never a paged scan.

    Codex P2 (NE-SKILL-1b): the apply seam must fetch a proposal by its unique
    id without scanning a capped page of the shared table.
    """
    store = SqlAlchemyProposalStore(curation_engine)
    a = _proposal(proposal_id="sha256:a", lane=FixLane.CONTENT)
    b = _proposal(proposal_id="sha256:b", lane=FixLane.RETRIEVAL)
    with curation_engine.begin() as conn:
        store.append(conn, a)
        store.append(conn, b)

    got_b = store.read_proposal("sha256:b")
    assert got_b is not None and got_b.proposal_id == "sha256:b"
    got_a = store.read_proposal("sha256:a")
    assert got_a is not None and got_a.lane == FixLane.CONTENT.value
    assert store.read_proposal("sha256:missing") is None


def test_entity_merge_candidate_round_trips_through_store(
    curation_engine: sa.Engine,
) -> None:
    proposal = _proposal(
        proposal_id="sha256:er-1",
        kind=ProposalKind.ENTITY_MERGE_CANDIDATE,
        lane=FixLane.ENTITY_RESOLUTION,
        cluster_id="er:Person:abcd",
        priority=ProposalPriority.HIGH,
        summary="candidate merge: Alice Ng == Alice Ng (0.99)",
    )
    store = SqlAlchemyProposalStore(curation_engine)
    with curation_engine.begin() as conn:
        row_id = store.append(conn, proposal)
    [stored] = store.read_proposals(after_id=row_id - 1, limit=1)
    assert stored.kind == ProposalKind.ENTITY_MERGE_CANDIDATE.value
    assert stored.lane == FixLane.ENTITY_RESOLUTION.value
    assert stored.cluster_id == "er:Person:abcd"


def test_skill_candidate_round_trips_through_store(
    curation_engine: sa.Engine,
) -> None:
    proposal = _proposal(
        proposal_id="sha256:skill-1",
        kind=ProposalKind.SKILL_CANDIDATE,
        lane=FixLane.SKILLS,
        cluster_id="deadbeefsig:r0",
        priority=ProposalPriority.LOW,
        summary="forged skill 'deploy-runbook' from a 4-step workflow",
    )
    store = SqlAlchemyProposalStore(curation_engine)
    with curation_engine.begin() as conn:
        row_id = store.append(conn, proposal)
    [stored] = store.read_proposals(after_id=row_id - 1, limit=1)
    assert stored.kind == ProposalKind.SKILL_CANDIDATE.value
    assert stored.lane == FixLane.SKILLS.value
    assert stored.cluster_id == "deadbeefsig:r0"


# --------------------------------------------------------------------------- #
# NE-T2X-1: graph_extraction_candidate schema + store round-trip.
# --------------------------------------------------------------------------- #


def _link_assertion(**overrides: object) -> GraphExtractionAssertion:
    fields: dict[str, object] = {
        "row_id": "row-link-1",
        "kind": GraphExtractionAssertionKind.LINK,
        "entity_class": "Account",
        "entity_key": "k-acme",
        "confidence": 0.93,
        "evidence_ref": "chunk:doc-9:c3",
    }
    fields.update(overrides)
    return GraphExtractionAssertion(**fields)  # type: ignore[arg-type]


def _new_entity_assertion(**overrides: object) -> GraphExtractionAssertion:
    fields: dict[str, object] = {
        "row_id": "row-entity-1",
        "kind": GraphExtractionAssertionKind.NEW_ENTITY,
        "entity_class": "Account",
        # A new-entity assertion's entity_key is the extraction job's
        # provisional/candidate key for the not-yet-canonical entity (e.g. a
        # minted "ne:llm:<hash>" id) -- required so two distinct new entities
        # of the same class do not collide on an identical content-address
        # tuple (see test_graph_extraction_distinct_new_entities_...).
        "entity_key": "ne:llm:globex-corp",
        "display": "Globex Corp",
        "properties": {"domain": "globex.example"},
        "confidence": 0.81,
        "evidence_ref": "chunk:doc-9:c4",
    }
    fields.update(overrides)
    return GraphExtractionAssertion(**fields)  # type: ignore[arg-type]


def _new_edge_assertion(**overrides: object) -> GraphExtractionAssertion:
    fields: dict[str, object] = {
        "row_id": "row-edge-1",
        "kind": GraphExtractionAssertionKind.NEW_EDGE,
        "subject_key": "k-acme",
        "edge_kind": "PARTNERS_WITH",
        "object_key": "k-globex",
        "confidence": 0.77,
        "evidence_ref": "chunk:doc-9:c4",
    }
    fields.update(overrides)
    return GraphExtractionAssertion(**fields)  # type: ignore[arg-type]


def _extraction_batch(**overrides: object) -> GraphExtractionBatch:
    fields: dict[str, object] = {
        "document_ref": "doc:doc-9",
        "run_id": "run-1",
        "source_system": "sharepoint",
        "assertions": (_link_assertion(), _new_entity_assertion(), _new_edge_assertion()),
    }
    fields.update(overrides)
    return GraphExtractionBatch(**fields)  # type: ignore[arg-type]


def _source_stamp(generation: int) -> SourceAuthorityStamp:
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="a" * 64
    )
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant", site_id="", drive_id="drive", item_id="item"
        ),
        generation=generation,
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision="head",
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=1,
        authority_receipt_id=f"receipt-{generation}",
        authority_receipt_hash="e" * 64,
    )


def test_graph_extraction_candidate_round_trips_through_store(
    curation_engine: sa.Engine,
) -> None:
    batch = _extraction_batch()
    proposal = _proposal(
        proposal_id="sha256:extract-1",
        kind=ProposalKind.GRAPH_EXTRACTION_CANDIDATE,
        lane=FixLane.ENTITY_RESOLUTION,
        cluster_id="extract:doc-9:abcd",
        priority=ProposalPriority.MEDIUM,
        summary="graph-extraction batch over doc:doc-9 (3 assertions)",
    )
    proposal = proposal.model_copy(update={"payload": batch.model_dump(mode="json")})
    store = SqlAlchemyProposalStore(curation_engine)
    with curation_engine.begin() as conn:
        row_id = store.append(conn, proposal)
    [stored] = store.read_proposals(after_id=row_id - 1, limit=1)
    assert stored.kind == ProposalKind.GRAPH_EXTRACTION_CANDIDATE.value
    assert stored.lane == FixLane.ENTITY_RESOLUTION.value
    assert stored.payload == batch.model_dump(mode="json")


def test_graph_extraction_batch_rejects_empty_assertions() -> None:
    with pytest.raises(ValidationError):
        GraphExtractionBatch(document_ref="doc:x", run_id="run-1", assertions=())


def test_graph_extraction_batch_rejects_duplicate_row_ids() -> None:
    dup = _link_assertion(row_id="same")
    dup2 = _new_entity_assertion(row_id="same")
    with pytest.raises(ValidationError, match="same"):
        GraphExtractionBatch(document_ref="doc:x", run_id="run-1", assertions=(dup, dup2))


@pytest.mark.parametrize(
    ("factory", "missing_field"),
    [
        # LINK: both required fields, each missing in isolation.
        (lambda: _link_assertion(entity_class=""), "entity_class"),
        (lambda: _link_assertion(entity_key=""), "entity_key"),
        # NEW_ENTITY: all three required fields, each missing in isolation.
        (lambda: _new_entity_assertion(entity_class=""), "entity_class"),
        (lambda: _new_entity_assertion(display=""), "display"),
        (lambda: _new_entity_assertion(entity_key=""), "entity_key"),
        # NEW_EDGE: all three required fields, each missing in isolation.
        (lambda: _new_edge_assertion(subject_key=""), "subject_key"),
        (lambda: _new_edge_assertion(edge_kind=""), "edge_kind"),
        (lambda: _new_edge_assertion(object_key=""), "object_key"),
        # Whitespace-only must be rejected exactly like empty (the .strip()
        # fix in _kind_shape_is_coherent) -- a bare truthiness check would
        # let a whitespace-only entity_key sail through.
        (lambda: _link_assertion(entity_key="   "), "entity_key"),
    ],
)
def test_graph_extraction_assertion_kind_shape_coherence(factory, missing_field: str) -> None:
    with pytest.raises(ValidationError, match=missing_field):
        factory()
    # A well-formed instance of each kind constructs fine.
    _link_assertion()
    _new_entity_assertion()
    _new_edge_assertion()


def test_graph_extraction_assertion_field_set_is_pinned() -> None:
    """Structural falsifiability companion to
    ``test_graph_extraction_models_forbid_extra_keys`` (Argus round-2 P3):
    pins the COMPLETE field set so adding any new field -- e.g. a
    text-carrying one -- trips THIS assertion directly, rather than relying
    solely on ``extra="forbid"`` rejecting an ad-hoc constructor kwarg. A
    schema change that adds a field must update this set explicitly, forcing
    a reviewer to confront the C12 no-smuggling discipline (design §0
    E-P2-2) at the point a new field is proposed.
    """
    assert set(GraphExtractionAssertion.model_fields) == {
        "row_id",
        "kind",
        "entity_class",
        "entity_key",
        "display",
        "properties",
        "subject_key",
        "edge_kind",
        "object_key",
        "confidence",
        "evidence_ref",
    }
    assert set(GraphExtractionBatch.model_fields) == {
        "document_ref",
        "run_id",
        "source_system",
        "source_authority_stamps",
        "assertions",
    }


def test_graph_extraction_assertion_bounds() -> None:
    with pytest.raises(ValidationError):
        _link_assertion(confidence=1.1)
    with pytest.raises(ValidationError):
        _link_assertion(confidence=-0.1)
    with pytest.raises(ValidationError):
        _link_assertion(evidence_ref="")
    with pytest.raises(ValidationError):
        _link_assertion(row_id="")


def test_graph_extraction_models_forbid_extra_keys() -> None:
    with pytest.raises(ValidationError):
        GraphExtractionAssertion(
            row_id="r1",
            kind=GraphExtractionAssertionKind.LINK,
            entity_class="Account",
            entity_key="k-acme",
            confidence=0.9,
            evidence_ref="chunk:doc-9:c3",
            chunk_text="the quarterly report says Acme acquired Globex",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        GraphExtractionBatch(
            document_ref="doc:x",
            run_id="run-1",
            assertions=(_link_assertion(),),
            raw_text="the quarterly report says Acme acquired Globex",  # type: ignore[call-arg]
        )


def test_graph_extraction_proposal_id_is_content_addressed() -> None:
    a1 = _link_assertion()
    a2 = _new_entity_assertion()
    a3 = _new_edge_assertion()

    id1, sha16_1 = graph_extraction_proposal_id("doc:doc-9", [a1, a2, a3])
    # Same document + same assertion set in a DIFFERENT order, with DIFFERENT
    # run_id/confidence/display/properties (not content-address fields).
    a1_variant = _link_assertion(confidence=0.5)
    a2_variant = _new_entity_assertion(
        display="Globex Corporation", properties={"domain": "other.example"}
    )
    a3_variant = _new_edge_assertion(confidence=0.11)
    id2, sha16_2 = graph_extraction_proposal_id("doc:doc-9", [a3_variant, a1_variant, a2_variant])
    assert id1 == id2
    assert sha16_1 == sha16_2
    assert id1.startswith("sha256:")
    assert len(id1) == len("sha256:") + 64
    assert sha16_1 == id1.removeprefix("sha256:")[:16]

    # Changing evidence_ref changes the id.
    a1_changed = _link_assertion(evidence_ref="chunk:doc-9:c9")
    id3, _ = graph_extraction_proposal_id("doc:doc-9", [a1_changed, a2, a3])
    assert id3 != id1

    # Changing entity_key changes the id.
    a1_other_key = _link_assertion(entity_key="k-other")
    id4, _ = graph_extraction_proposal_id("doc:doc-9", [a1_other_key, a2, a3])
    assert id4 != id1


def test_graph_extraction_proposal_id_binds_exact_source_authority_stamp() -> None:
    assertion = _new_entity_assertion()
    current_id, _ = graph_extraction_proposal_id("doc:doc-9", [assertion], [_source_stamp(1)])
    replaced_generation_id, _ = graph_extraction_proposal_id(
        "doc:doc-9", [assertion], [_source_stamp(2)]
    )
    assert replaced_generation_id != current_id


def test_graph_extraction_row_id_is_stable_under_display_noise_given_fixed_keys() -> None:
    """Row-id idempotency holds for confidence/display CHURN when the content-
    address keys (entity_class/entity_key/...) are unchanged -- because
    neither confidence nor display is in :func:`_assertion_content_tuple`.

    This is NOT a claim that display re-wording is always harmless: design
    §3.3 derives ``entity_key`` from a normalized ``canonical_form`` of
    ``display``, so a producer that FOLLOWS §3.3 and re-words past
    canonical-form equality WILL mint a different ``entity_key`` -- see
    ``test_graph_extraction_row_id_changes_when_canonical_form_derived_entity_key_changes``
    for that (expected, non-idempotent) companion case. This test only pins
    the case where ``entity_key`` is held fixed (e.g. two runs converge on
    the identical canonical form, or the producer reuses a previously-minted
    key) -- confirming confidence/display churn alone cannot perturb the id.
    """
    a = _new_entity_assertion()
    a_variant = _new_entity_assertion(display="Something else entirely", confidence=0.2)
    assert graph_extraction_row_id(a) == graph_extraction_row_id(a_variant)
    a_changed = _new_entity_assertion(entity_class="Person")
    assert graph_extraction_row_id(a) != graph_extraction_row_id(a_changed)


def _canonical_form(display: str) -> str:
    """A minimal stand-in for design §3.3's ``canonical_form`` normalization
    (casefold + whitespace/punctuation collapse) -- just enough to
    demonstrate the entity_key derivation the design specifies, without
    importing ne-brain-graph (out of this unit's mayTouch)."""
    return "-".join(display.casefold().replace(".", "").replace(",", "").split())


def test_graph_extraction_row_id_changes_when_canonical_form_derived_entity_key_changes() -> None:
    """Honest companion to the stability test above (Argus round-2 P2): a
    §3.3-compliant producer that re-words a display name PAST canonical-form
    equality mints a DIFFERENT entity_key, and therefore a different row id
    / proposal id on re-run over the same document. This is design §3.3's
    declared consequence ("surface-form variance... fragments into two
    nodes... healed by the existing Splink ER lane"), not an idempotency
    bug -- see the caveat on :func:`graph_extraction_proposal_id`.
    """
    display_a = "Globex Corp"
    display_b = "Globex Corporation"
    assert _canonical_form(display_a) != _canonical_form(display_b)  # genuinely different forms

    a = _new_entity_assertion(entity_key=f"ne:llm:{_canonical_form(display_a)}", display=display_a)
    b = _new_entity_assertion(entity_key=f"ne:llm:{_canonical_form(display_b)}", display=display_b)

    assert graph_extraction_row_id(a) != graph_extraction_row_id(b)
    id_a, _ = graph_extraction_proposal_id("doc:doc-9", [a])
    id_b, _ = graph_extraction_proposal_id("doc:doc-9", [b])
    assert id_a != id_b


def test_graph_extraction_distinct_new_entities_same_class_get_distinct_ids() -> None:
    """Two DIFFERENT new entities of the same class, same evidence, must not collide.

    Regression guard for the E-P1-1 defect: before entity_key was required on
    NEW_ENTITY assertions, every new-entity row of the same class shared an
    identical (kind, class, "", "", "", "", ref) content tuple regardless of
    which entity it named -- so a batch with two distinct new entities from
    one chunk (a common case, e.g. one chunk naming two new Accounts) was
    UNBUILDABLE (duplicate row_id -> ValidationError), and a re-run
    extracting a genuinely different second entity would silently collide
    with -- and be discarded in favour of -- the first at the store's
    idempotent-append boundary (proposal_id is unique).
    """
    e1 = _new_entity_assertion(
        row_id="row-e1",
        entity_key="ne:llm:globex-corp",
        display="Globex Corp",
        evidence_ref="chunk:doc-9:c4",
    )
    e2 = _new_entity_assertion(
        row_id="row-e2",
        entity_key="ne:llm:initech-llc",
        display="Initech LLC",
        evidence_ref="chunk:doc-9:c4",
    )

    assert graph_extraction_row_id(e1) != graph_extraction_row_id(e2)

    # The batch MUST build -- two distinct new entities from one chunk is the
    # common case, not a corner case.
    batch = _extraction_batch(document_ref="doc:doc-9", assertions=(e1, e2))
    assert len(batch.assertions) == 2

    id1, _ = graph_extraction_proposal_id("doc:doc-9", [e1])
    id2, _ = graph_extraction_proposal_id("doc:doc-9", [e2])
    assert id1 != id2


#: The verbatim source-chunk prose a real extraction job would have READ to
#: produce a typed assertion. Threaded through `_distilled_new_entity_from_chunk`
#: below (not left a dead local) so the "absent from payload" assertion in
#: `test_graph_extraction_payload_carries_refs_never_chunk_text` is wired to a
#: code path the raw text genuinely reaches, rather than asserting the
#: absence of a string nothing ever touched.
_CANARY = "CANARY the quarterly report says Acme acquired Globex"
_SOURCE_CHUNK_TEXT = (
    f"{_CANARY}, discussing partnership terms and financial details in section 4 of the filing."
)


def _distilled_new_entity_from_chunk(
    source_chunk_text: str, *, evidence_ref: str
) -> GraphExtractionAssertion:
    """Simulate NE-T2X-2's typed distillation of ONE source chunk.

    Takes the RAW chunk text as input (the shape a real extraction job takes
    -- prose in) and returns a typed assertion carrying only the DISTILLED
    business values + a ref, never the prose itself. Passing
    ``source_chunk_text`` through here (rather than a canary that is never
    referenced) is what gives the leak test below real wiring.
    """
    assert source_chunk_text  # the raw prose is read here; never stored below
    return GraphExtractionAssertion(
        row_id="row-entity-distilled",
        kind=GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key="ne:llm:globex-corp",
        display="Globex Corp",
        properties={"domain": "globex.example"},
        confidence=0.81,
        evidence_ref=evidence_ref,
    )


def test_graph_extraction_payload_carries_refs_never_chunk_text() -> None:
    """C12 (design §0 E-P2-2): the disciplined distillation path never leaks prose.

    This is the "good path" half of the C12 guard. The STRUCTURAL half — no
    field exists to carry raw text, and none can be added without tripping
    ``test_graph_extraction_models_forbid_extra_keys`` — is what actually
    makes smuggling via a NEW field impossible; ``extra="forbid"`` is that
    guard. This test instead pins that the standard distillation path (typed
    fields + refs, mirroring what NE-T2X-2 will do) produces a payload the
    canary never reaches, using a helper that genuinely reads the raw prose
    (see ``_distilled_new_entity_from_chunk``) rather than a dead local.

    See ``test_graph_extraction_properties_leak_by_convention_only_not_by_schema``
    for the honest counterpart: ``properties`` is a free dict this schema does
    NOT structurally stop from carrying prose if a job populates it badly —
    that boundary is job-authoring discipline (NE-T2X-2), not something this
    unit enforces, and that companion test is what proves THIS test is
    falsifiable rather than vacuously true.
    """
    distilled = _distilled_new_entity_from_chunk(_SOURCE_CHUNK_TEXT, evidence_ref="chunk:doc-9:c4")
    batch = _extraction_batch(assertions=(_link_assertion(), distilled, _new_edge_assertion()))
    batch_json = batch.model_dump_json()
    assert _CANARY not in batch_json

    proposal = _proposal(
        proposal_id="sha256:extract-canary",
        kind=ProposalKind.GRAPH_EXTRACTION_CANDIDATE,
        lane=FixLane.ENTITY_RESOLUTION,
    ).model_copy(update={"payload": batch.model_dump(mode="json")})
    proposal_json = proposal.model_dump_json()
    assert _CANARY not in proposal_json
    assert "chunk:doc-9:c3" in proposal_json
    assert "chunk:doc-9:c4" in proposal_json


def test_graph_extraction_properties_leak_by_convention_only_not_by_schema() -> None:
    """Honest counterpart: proves the leak test above has teeth, not vacuousness.

    ``properties: dict[str, PropertyValue]`` (Argus round-3 P2-A/P2-B) closes
    every CONTAINER vector (see
    ``test_graph_extraction_properties_reject_non_scalar_values``), but it is
    NOT, and cannot be, a content-based leak scrubber: a job that writes raw
    chunk prose into an EXISTING property value as a plain ``str`` still sails
    straight through unvalidated, because nothing distinguishes a legitimate
    business string from smuggled prose at the type level, and there is
    deliberately no length cap to lean on either
    (:data:`ne.flywheel.proposals.PropertyValue`). This test deliberately
    smuggles ``_SOURCE_CHUNK_TEXT * 3`` (399 chars — comfortably over the
    removed 200-char cap; this is the EXACT length class the deleted
    ``test_graph_extraction_properties_rejects_oversized_strings`` used to
    reject) and confirms it DOES appear in the serialized payload. That (a)
    proves the same ``_CANARY not in ...json()`` assertion style, applied to
    a genuinely leaking payload, correctly flags the leak — i.e. the primary
    test is falsifiable — and (b) demonstrates, with an input that would have
    tripped the old cap, that length is no longer a defense — a plain
    ``_SOURCE_CHUNK_TEXT`` alone (133 chars) would have sailed through even
    under the OLD cap and would prove nothing about its removal. Documents
    plainly that closing this residual (plain-str, any-length) gap requires
    the ENTITY_CLASSES contract validation NE-T2X-2's ``ExtractionWriter``
    performs at write time, not a content scan or length cap this schema
    performs.
    """
    smuggled_text = _SOURCE_CHUNK_TEXT * 3
    assert len(smuggled_text) > 200  # would have tripped the removed cap
    smuggled = _new_entity_assertion(row_id="row-smuggled", properties={"excerpt": smuggled_text})
    batch = _extraction_batch(assertions=(smuggled,))
    assert _CANARY in batch.model_dump_json()


@pytest.mark.parametrize(
    "bad_value",
    [
        {"chunks": [_CANARY, _CANARY]},
        [_CANARY],
        (_CANARY, _CANARY),
        {_CANARY},
        object(),
    ],
    ids=["dict", "list", "tuple", "set", "object"],
)
def test_graph_extraction_properties_reject_non_scalar_values(bad_value: object) -> None:
    """Argus round-3 P2-A: the previous reject-list validator rejected only
    ``dict``/``list`` while ``properties`` stayed typed ``dict[str, Any]`` —
    pydantic does NOT validate values typed ``Any``, so a ``tuple`` or ``set``
    value bypassed
    BOTH the nesting check and the length cap and serialized straight into the
    stored payload as a JSON array via ``model_dump(mode="json")``, reaching
    ``artifact.pr_body`` end to end (Argus reproduced this in the worktree: a
    2-tuple of a large string yielded a 2446-char payload). The ``tuple`` case
    below IS that exact bypass repro.

    The fix inverts to a POSITIVE allow-list
    (:data:`ne.flywheel.proposals.PropertyValue` =
    ``str | int | float | bool | None``): the value's TYPE closes the class of
    container vectors, so construction itself fails for ANY container
    (dict/list/tuple/set) or arbitrary object — a canary riding a container can
    never reach ``model_dump``/the renderer via the model path at all.
    """
    with pytest.raises(ValidationError, match="excerpt"):
        _new_entity_assertion(row_id="row-nonscalar", properties={"excerpt": bad_value})


@pytest.mark.parametrize(
    "bad_value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        1e999,
    ],
    ids=["nan", "inf", "neg-inf", "overflow-literal-1e999"],
)
def test_graph_extraction_properties_reject_non_finite_floats(bad_value: float) -> None:
    """Codex C1: ``PropertyValue``'s ``float`` member admits NaN/Infinity
    unless separately bounded -- pydantic's ``float`` validator does not
    reject them, and a Python float literal with an out-of-range exponent
    (``1e999``) silently overflows to ``inf`` rather than raising, so it is
    the SAME vector as the two explicit ``float("inf")``/``float("-inf")``
    cases, not a distinct one. None of these four values is a valid JSON
    number: persisting one into the PostgreSQL JSONB ``payload`` column can
    fail outright, which -- under BATCH granularity -- would abort the WHOLE
    document's proposal, not just the one offending assertion.
    ``GraphExtractionAssertion._properties_floats_are_finite`` rejects all
    four at construction.
    """
    with pytest.raises(ValidationError, match="excerpt"):
        _new_entity_assertion(row_id="row-nonfinite", properties={"excerpt": bad_value})


def test_graph_extraction_properties_accept_contract_scale_values() -> None:
    """Argus round-3 P2-B: the removed 200-char cap rejected values the real
    generated write-side contract ACCEPTS — under BATCH granularity, one
    rejected assertion loses the WHOLE document's proposal. This test IS the
    trace from the no-cap decision to ``ne.schema``'s ``ENTITY_CONTRACTS``
    (design §2.1 F1: extracted elements must validate through the generated
    contract), per the repo rule that normative claims live in tests, not
    comments.

    Also pins exact scalar-type preservation (str/int/float/bool/None) through
    ``properties`` to guard against a future smart-union coercion regression
    (e.g. ``True`` silently becoming ``1``).

    Argus round-3 (post-REVISE) P2 second finding: the allow-list is
    exactly the JSON-scalar set, which is narrower than the contract for two
    field TYPES the SAME generated contract also declares --
    ``TicketEntity.opened_at`` (``datetime``, required) and
    ``OrderEntity.total_amount`` (``Decimal``, ``ge=0``). This test also
    pins: (a) a raw ``datetime`` object is REJECTED by ``properties`` (would
    lose a whole document's proposal under batch granularity) while a raw
    ``Decimal`` is silently coerced to ``float`` (a money-precision risk,
    not a rejection) -- both are exactly why the CONVENTION is to carry
    both field types as their ISO-8601 / decimal-literal STRING form; and
    (b) that convention round-trips: the same ISO / decimal strings that
    ``properties`` stores verbatim are ALSO accepted, and lax-coerced back
    to ``datetime``/``Decimal``, by the real generated
    ``TicketEntity.opened_at`` / ``OrderEntity.total_amount`` contract
    fields -- so the convention is test-traced to the contract, not left
    for NE-T2X-2's producer to discover as a hard reject. Finally, pins
    that ``bytes``/``bytearray`` values are ACCEPTED (pydantic lax-decodes
    them to ``str``) rather than rejected as a container, per the
    :data:`ne.flywheel.proposals.PropertyValue` comment.
    """
    long_description = (
        "The customer reported intermittent timeouts on checkout under load. "
        "Escalated to platform engineering for a root-cause investigation. "
    ) * 10
    assert len(long_description) > 900
    long_uri = (
        "https://contoso.sharepoint.com/sites/platform-eng-incident-response/"
        "Shared%20Documents/Incident%20Reports/2026/Q3/root-cause-analyses/"
        "checkout-timeout-rca-and-remediation-plan-final-reviewed-and-signed-"
        "off-by-engineering-leadership-and-the-customer-success-team-and-the-"
        "vp-of-platform-engineering-after-the-quarterly-postmortem-review.docx"
    )
    assert len(long_uri) > 300

    # The REAL write-side contract accepts these values unbounded.
    ticket = TicketEntity(
        id="t-1",
        subject="s",
        status=TicketStatus.open,
        opened_at=datetime.now(UTC),
        description=long_description,
    )
    document = DocumentEntity(
        id="d-1",
        title="t",
        doc_type=DocumentType.contract,
        uri=long_uri,
    )
    assert ticket.description == long_description
    assert document.uri == long_uri

    # THIS schema must accept the SAME values -- no numeric cap anywhere.
    a = _new_entity_assertion(
        row_id="row-contract-scale",
        properties={"description": long_description, "uri": long_uri},
    )
    dumped = a.model_dump(mode="json")["properties"]
    assert dumped["description"] == long_description
    assert dumped["uri"] == long_uri

    # Scalar-type preservation: no smart-union coercion across str/int/float/
    # bool/None.
    typed = _new_entity_assertion(
        row_id="row-scalar-types",
        properties={"s": "x", "i": 3, "f": 1.5, "b": True, "n": None},
    )
    assert type(typed.properties["s"]) is str
    assert type(typed.properties["i"]) is int
    assert type(typed.properties["f"]) is float
    assert type(typed.properties["b"]) is bool
    assert typed.properties["n"] is None

    # `bytes`/`bytearray` are ACCEPTED, not rejected as a container -- pydantic
    # lax-decodes them to `str` before the union ever sees a container, so
    # post-validation they collapse into the plain-str residual, not a
    # container vector (PropertyValue comment).
    bytes_typed = _new_entity_assertion(
        row_id="row-bytes",
        properties={"raw": b"hello", "raw_array": bytearray(b"world")},
    )
    assert bytes_typed.properties["raw"] == "hello"
    assert type(bytes_typed.properties["raw"]) is str
    assert bytes_typed.properties["raw_array"] == "world"
    assert type(bytes_typed.properties["raw_array"]) is str

    # A raw `datetime` OBJECT is rejected -- under batch granularity that
    # loses the whole document's proposal, the same harm the removed length
    # cap caused.
    with pytest.raises(ValidationError):
        _new_entity_assertion(
            row_id="row-datetime-object", properties={"opened_at": ticket.opened_at}
        )

    # A raw `Decimal` OBJECT is accepted but silently coerced to `float` --
    # a money-precision risk, which is exactly why the convention below
    # (string form) exists rather than passing Decimal objects through.
    decimal_coerced = _new_entity_assertion(
        row_id="row-decimal-object", properties={"total_amount": Decimal("1.50")}
    )
    assert decimal_coerced.properties["total_amount"] == 1.5
    assert type(decimal_coerced.properties["total_amount"]) is float

    # The CONVENTION: carry `datetime`/`Decimal` contract fields as their
    # ISO-8601 / decimal-literal STRING form through `properties`, and prove
    # the SAME strings round-trip back through the real generated contract.
    # `.model_validate(dict)` (not the keyword constructor) deliberately
    # exercises this AS A CALLER WOULD -- a producer holding an untyped
    # `properties` dict, not statically-typed `datetime`/`Decimal` literals.
    opened_at_str = datetime.now(UTC).isoformat()
    total_amount_str = "1999.99"

    ticket_from_str = TicketEntity.model_validate(
        {
            "id": "t-2",
            "subject": "s",
            "status": TicketStatus.open,
            "opened_at": opened_at_str,
            "description": "d",
        }
    )
    assert ticket_from_str.opened_at.isoformat() == opened_at_str
    order_from_str = OrderEntity.model_validate(
        {
            "id": "o-1",
            "order_number": "ORD-1234",
            "status": OrderStatus.placed,
            "currency": "USD",
            "total_amount": total_amount_str,
            "placed_at": opened_at_str,
        }
    )
    assert str(order_from_str.total_amount) == total_amount_str

    temporal_and_decimal = _new_entity_assertion(
        row_id="row-temporal-decimal",
        properties={"opened_at": opened_at_str, "total_amount": total_amount_str},
    )
    dumped_temporal = temporal_and_decimal.model_dump(mode="json")["properties"]
    assert dumped_temporal["opened_at"] == opened_at_str
    assert dumped_temporal["total_amount"] == total_amount_str
