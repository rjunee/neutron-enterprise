# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Apply/revert lifecycle (design §3.4): gate refusals, floor-stamped applies,
validate-then-write atomicity, re-evidence, and evidence-refcounted revert.

NE-066's state machine makes ``reject`` reachable only from
``proposed``/``approved`` — mode 1's OWN elements only ever exist post-apply
(i.e. post-``approved``), so there is no "reject an applied proposal" path.
The graph-bearing revert tests below therefore seed elements directly via
:class:`~ne.brain_graph.extraction.writer.ExtractionWriter` at
``assurance="extracted"`` (the shape modes 2/3 write immediately) — the
documented test technique the module docstring calls out, not scope creep.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from ne.audit import ensure_transactional_sqlite
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.errors import GraphAclError
from ne.brain_graph.extraction.apply import (
    ExtractionGateError,
    apply_extraction,
    revert_extraction,
    write_provisional_extraction,
)
from ne.brain_graph.extraction.identity import llm_entity_key
from ne.brain_graph.extraction.writer import ExtractionWriter
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts import Principal, freeze_config_hash, reset_config_hash_for_testing
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import (
    FixProposal,
    GraphExtractionAssertion,
    GraphExtractionAssertionKind,
    GraphExtractionBatch,
    ProposalKind,
    SqlAlchemyProposalStore,
    ensure_proposal_schema,
    graph_extraction_proposal_id,
    graph_extraction_row_id,
)
from ne.flywheel.review import ProposalReviewService, RejectionCode
from ne.flywheel.signals import SignalProvenance, SignalSurface

_CFG = "e" * 64


@pytest.fixture(autouse=True)
def _frozen_config() -> Any:
    reset_config_hash_for_testing()
    digest = freeze_config_hash(BrainGraphSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class _DictAclResolver:
    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self._mapping = mapping

    async def acl_for(self, chunk_ref: str) -> tuple[str, ...]:
        return self._mapping.get(chunk_ref, ())


def _engine() -> sa.Engine:
    from ne.flywheel.review import ensure_review_schema

    eng = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ensure_transactional_sqlite(eng)
    ensure_review_schema(eng)
    return eng


def _mint(
    kind: GraphExtractionAssertionKind,
    *,
    entity_class: str = "",
    entity_key: str = "",
    display: str = "",
    properties: dict[str, Any] | None = None,
    subject_key: str = "",
    edge_kind: str = "",
    object_key: str = "",
    confidence: float = 0.9,
    evidence_ref: str,
) -> GraphExtractionAssertion:
    draft = GraphExtractionAssertion(
        row_id="pending",
        kind=kind,
        entity_class=entity_class,
        entity_key=entity_key,
        display=display,
        properties=properties or {},
        subject_key=subject_key,
        edge_kind=edge_kind,
        object_key=object_key,
        confidence=confidence,
        evidence_ref=evidence_ref,
    )
    return draft.model_copy(update={"row_id": graph_extraction_row_id(draft)})


def _seed_extraction_proposal(
    engine: sa.Engine,
    document_ref: str,
    assertions: Sequence[GraphExtractionAssertion],
    run_id: str = "r1",
) -> str:
    ensure_proposal_schema(engine)
    proposal_id, sha16 = graph_extraction_proposal_id(document_ref, assertions)
    batch = GraphExtractionBatch(
        document_ref=document_ref, run_id=run_id, source_system="docs", assertions=tuple(assertions)
    )
    proposal = FixProposal(
        proposal_id=proposal_id,
        kind=ProposalKind.GRAPH_EXTRACTION_CANDIDATE,
        lane=FixLane.ENTITY_RESOLUTION,
        cluster_id=f"extract:{sha16}",
        evidence_event_ids=(),
        evidence_signal_ids=(),
        summary=f"graph-extraction batch over {document_ref}",
        confidence=max(a.confidence for a in assertions),
        priority=ProposalPriority.MEDIUM,
        config_hash=_CFG,
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        payload=batch.model_dump(mode="json"),
    )
    with engine.begin() as conn:
        SqlAlchemyProposalStore(engine).append(conn, proposal)
    return proposal_id


def _seed_other_kind_proposal(engine: sa.Engine) -> str:
    ensure_proposal_schema(engine)
    proposal = FixProposal(
        proposal_id="sha256:not-extraction",
        kind=ProposalKind.ENTITY_MERGE_CANDIDATE,
        lane=FixLane.ENTITY_RESOLUTION,
        cluster_id="er:Account:zz",
        evidence_event_ids=(),
        evidence_signal_ids=(),
        summary="not an extraction proposal",
        confidence=0.9,
        priority=ProposalPriority.MEDIUM,
        config_hash=_CFG,
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        payload={
            "left": {"entity_class": "Account", "entity_key": "a"},
            "right": {"entity_class": "Account", "entity_key": "b"},
        },
    )
    with engine.begin() as conn:
        SqlAlchemyProposalStore(engine).append(conn, proposal)
    return proposal.proposal_id


def _seed_malformed_extraction_proposal(engine: sa.Engine) -> str:
    ensure_proposal_schema(engine)
    proposal = FixProposal(
        proposal_id="sha256:malformed",
        kind=ProposalKind.GRAPH_EXTRACTION_CANDIDATE,
        lane=FixLane.ENTITY_RESOLUTION,
        cluster_id="extract:zz",
        evidence_event_ids=(),
        evidence_signal_ids=(),
        summary="malformed payload",
        confidence=0.9,
        priority=ProposalPriority.MEDIUM,
        config_hash=_CFG,
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        payload={"not": "a valid batch"},
    )
    with engine.begin() as conn:
        SqlAlchemyProposalStore(engine).append(conn, proposal)
    return proposal.proposal_id


def _prov(actor: str = "steward") -> SignalProvenance:
    return SignalProvenance(
        principal=Principal(id=actor), surface=SignalSurface.CONSOLE, session_id="s1"
    )


def _apply(
    engine: sa.Engine,
    review: ProposalReviewService,
    store: InMemoryGraphStore,
    resolver: Any,
    proposal_id: str,
) -> str:
    return asyncio.run(
        apply_extraction(
            engine,
            review,
            store,
            resolver,
            proposal_id,
            actor="op",
            audit_sink=_RecordingAudit(),
            config_hash=_CFG,
            schema_version="1.0.0",
        )
    )


def test_apply_on_proposed_raises_and_writes_nothing() -> None:
    engine = _engine()
    store = InMemoryGraphStore()
    a = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", "Acme"),
        display="Acme",
        properties={"name": "Acme"},
        evidence_ref="c1",
    )
    pid = _seed_extraction_proposal(engine, "d1", [a])
    review = ProposalReviewService(engine)
    with pytest.raises(ExtractionGateError):
        _apply(engine, review, store, _DictAclResolver({"c1": ("g1",)}), pid)
    assert store.node_count() == 0


def test_approved_apply_writes_confirmed_floor_stamped_elements() -> None:
    engine = _engine()
    store = InMemoryGraphStore()
    a = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", "Acme"),
        display="Acme",
        properties={"name": "Acme"},
        confidence=0.9,
        evidence_ref="c1",
    )
    pid = _seed_extraction_proposal(engine, "d1", [a])
    review = ProposalReviewService(engine)
    review.approve(pid, _prov())
    applied_ref = _apply(engine, review, store, _DictAclResolver({"c1": ("g1",)}), pid)
    assert applied_ref == "extraction:1n:0e"

    key = llm_entity_key("Account", "Acme")
    node = store.raw_node(key)
    assert node is not None
    assert node["source_system"] == "ne:llm"
    assert node["assurance"] == "confirmed"
    assert node["acl"] == ["g1"]
    sha16 = pid.removeprefix("sha256:")[:16]
    assert node["evidence"] == [f"{pid}|c1"]
    assert node["source_ref"] == f"llm:{sha16}:c1"

    reviews = review.read_reviews(proposal_id=pid)
    assert any(r.action == "apply" and r.applied_ref == applied_ref for r in reviews)

    # A second apply is blocked (status is now APPLIED).
    with pytest.raises(ExtractionGateError):
        _apply(engine, review, store, _DictAclResolver({"c1": ("g1",)}), pid)


def test_wrong_kind_proposal_raises_gate_error() -> None:
    engine = _engine()
    store = InMemoryGraphStore()
    pid = _seed_other_kind_proposal(engine)
    review = ProposalReviewService(engine)
    review.approve(pid, _prov())
    with pytest.raises(ExtractionGateError):
        _apply(engine, review, store, _DictAclResolver({}), pid)


def test_malformed_payload_raises_gate_error() -> None:
    engine = _engine()
    store = InMemoryGraphStore()
    pid = _seed_malformed_extraction_proposal(engine)
    review = ProposalReviewService(engine)
    review.approve(pid, _prov())
    with pytest.raises(ExtractionGateError):
        _apply(engine, review, store, _DictAclResolver({}), pid)


def test_unresolvable_chunk_acl_refuses_even_with_other_valid_assertions() -> None:
    engine = _engine()
    store = InMemoryGraphStore()
    a1 = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", "GoodCo"),
        display="GoodCo",
        properties={"name": "GoodCo"},
        evidence_ref="c-good",
    )
    a2 = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", "BadCo"),
        display="BadCo",
        properties={"name": "BadCo"},
        evidence_ref="c-bad",
    )
    pid = _seed_extraction_proposal(engine, "d1", [a1, a2])
    review = ProposalReviewService(engine)
    review.approve(pid, _prov())
    with pytest.raises(GraphAclError):
        _apply(
            engine, review, store, _DictAclResolver({"c-good": ("g1",)}), pid
        )  # c-bad unresolved
    assert store.node_count() == 0


def test_apply_refuses_when_entity_key_disagrees_with_recomputed_identity() -> None:
    """Argus P2: ``write_entity`` never trusts ``assertion.entity_key`` for the
    actual node write — it recomputes the key from ``(entity_class, display)``.
    A deserialized/tampered payload whose declared ``entity_key`` disagrees
    with that recomputation must be refused BEFORE the edge-endpoint
    resolution loop, or the edge would be wired against a key the writer
    never actually uses (a dangling edge, or a false hit on an unrelated
    pre-existing node via the ``fetch_nodes`` fallback)."""
    engine = _engine()
    store = InMemoryGraphStore()
    tampered_entity = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key="not-the-recomputed-key",  # disagrees with llm_entity_key(...)
        display="Acme",
        properties={"name": "Acme"},
        evidence_ref="c1",
    )
    edge = _mint(
        GraphExtractionAssertionKind.NEW_EDGE,
        subject_key="not-the-recomputed-key",
        edge_kind="WORKS_AT",
        object_key="not-the-recomputed-key",
        evidence_ref="c1",
    )
    pid = _seed_extraction_proposal(engine, "d1", [tampered_entity, edge])
    review = ProposalReviewService(engine)
    review.approve(pid, _prov())
    with pytest.raises(ExtractionGateError):
        _apply(engine, review, store, _DictAclResolver({"c1": ("g1",)}), pid)
    assert store.node_count() == 0
    assert store.edge_count() == 0


def test_apply_refuses_before_any_write_when_a_later_assertion_fails_contract_wall() -> None:
    """Argus P2 (round 2): the F1 ENTITY_CONTRACTS wall is part of
    validate-then-write atomicity, not just chunk-ACL resolution. A batch
    whose FIRST assertion is contract-valid and SECOND is not must write
    NEITHER — proving the wall is re-run in the up-front loop (before any
    ``upsert_nodes``), not only inside ``ExtractionWriter.write_entity``
    mid-write."""
    engine = _engine()
    store = InMemoryGraphStore()
    good = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", "GoodCo"),
        display="GoodCo",
        properties={"name": "GoodCo"},
        evidence_ref="c-good",
    )
    # AccountEntity requires 'name'; empty properties fails the wall.
    bad = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", "BadCo"),
        display="BadCo",
        properties={},
        evidence_ref="c-bad",
    )
    pid = _seed_extraction_proposal(engine, "d1", [good, bad])
    review = ProposalReviewService(engine)
    review.approve(pid, _prov())
    with pytest.raises(ExtractionGateError):
        _apply(
            engine,
            review,
            store,
            _DictAclResolver({"c-good": ("g1",), "c-bad": ("g1",)}),
            pid,
        )
    assert store.node_count() == 0  # neither entity written — validate-then-write
    # The proposal is still 'approved' (record_apply never ran) — but that is
    # an orthogonal, documented recovery gap (see module docstring); this
    # test only pins the write-side atomicity.
    assert review.status_of(pid).value == "approved"


def test_apply_refuses_atomically_when_edge_triple_is_not_in_edge_index() -> None:
    """Argus P2 (round 3): the ``_EDGE_INDEX`` triple wall is part of
    validate-then-write atomicity — before this fix, both entities were
    written and then ``write_edge`` raised, half-applying the batch. A batch
    with two valid ``NEW_ENTITY`` assertions plus one ``NEW_EDGE`` whose
    ``(subject_class, edge_kind, object_class)`` triple is not a valid
    EDGE_DEFS relation must write NEITHER entity — proving the triple check
    is re-run in the up-front loop (before ``ExtractionWriter`` is even
    constructed), not only inside ``ExtractionWriter.write_edge`` mid-write.
    """
    engine = _engine()
    store = InMemoryGraphStore()
    e1 = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", "Acme"),
        display="Acme",
        properties={"name": "Acme"},
        evidence_ref="c1",
    )
    e2 = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", "Globex"),
        display="Globex",
        properties={"name": "Globex"},
        evidence_ref="c1",
    )
    # ("Account", "NOT_A_RELATION", "Account") is guaranteed absent from
    # _EDGE_INDEX — no generated schema ever defines this relation.
    edge = _mint(
        GraphExtractionAssertionKind.NEW_EDGE,
        subject_key=llm_entity_key("Account", "Acme"),
        edge_kind="NOT_A_RELATION",
        object_key=llm_entity_key("Account", "Globex"),
        evidence_ref="c1",
    )
    pid = _seed_extraction_proposal(engine, "d1", [e1, e2, edge])
    review = ProposalReviewService(engine)
    review.approve(pid, _prov())
    with pytest.raises(ExtractionGateError):
        _apply(engine, review, store, _DictAclResolver({"c1": ("g1",)}), pid)
    assert store.node_count() == 0  # neither entity written — validate-then-write
    assert store.edge_count() == 0
    assert review.status_of(pid).value == "approved"


def test_re_evidence_apply_appends_evidence_and_never_widens() -> None:
    engine = _engine()
    store = InMemoryGraphStore()
    review = ProposalReviewService(engine)

    a1 = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", "Acme"),
        display="Acme",
        properties={"name": "Acme"},
        evidence_ref="c1",
    )
    p1 = _seed_extraction_proposal(engine, "d1", [a1], run_id="r1")
    review.approve(p1, _prov())
    _apply(engine, review, store, _DictAclResolver({"c1": ("g1",)}), p1)

    a2 = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", "Acme"),
        display="Acme",
        properties={"name": "Acme"},
        evidence_ref="c2",
    )
    p2 = _seed_extraction_proposal(engine, "d2", [a2], run_id="r2")
    review.approve(p2, _prov())
    _apply(engine, review, store, _DictAclResolver({"c2": ("g1", "g2")}), p2)

    key = llm_entity_key("Account", "Acme")
    node = store.raw_node(key)
    assert node is not None
    assert node["acl"] == ["g1"]
    assert set(node["evidence"]) == {f"{p1}|c1", f"{p2}|c2"}


async def _seed_extracted_node(
    store: InMemoryGraphStore,
    *,
    proposal_id: str,
    chunk_ref: str,
    display: str,
    acl: tuple[str, ...],
    assurance: str,
) -> str:
    writer = ExtractionWriter(
        store,
        config_hash=_CFG,
        schema_version="1.0.0",
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    assertion = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", display),
        display=display,
        properties={"name": display},
        evidence_ref=chunk_ref,
    )
    return await writer.write_entity(
        assertion, chunk_acl=acl, proposal_id=proposal_id, assurance=assurance
    )


def test_revert_removes_refs_deletes_empty_evidence_and_spares_confirmed() -> None:
    engine = _engine()
    store = InMemoryGraphStore()
    review = ProposalReviewService(engine)

    a = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", "RejectMe"),
        display="RejectMe",
        properties={"name": "RejectMe"},
        evidence_ref="c1",
    )
    pid = _seed_extraction_proposal(engine, "d1", [a])
    # Seed the graph-bearing element directly (mode-1's OWN reject path never
    # reaches the graph; this exercises the SHARED revert mechanics NE-T2X-4's
    # modes 2/3 rely on — see module docstring).
    asyncio.run(
        _seed_extracted_node(
            store,
            proposal_id=pid,
            chunk_ref="c1",
            display="RejectMe",
            acl=("g1",),
            assurance="extracted",
        )
    )
    review.reject(pid, _prov(), code=RejectionCode.INCORRECT)

    result = asyncio.run(
        revert_extraction(
            engine, review, store, pid, actor="op", audit_sink=_RecordingAudit(), config_hash=_CFG
        )
    )
    assert result["nodes_deleted"] == 1
    assert result["refs_removed"] == 1
    key = llm_entity_key("Account", "RejectMe")
    assert store.raw_node(key) is None


def test_revert_confirmed_survives_duplicate_reject() -> None:
    engine = _engine()
    store = InMemoryGraphStore()
    review = ProposalReviewService(engine)

    key = llm_entity_key("Account", "Survivor")
    p1 = "sha256:" + "1" * 64
    p2 = _seed_extraction_proposal(
        engine,
        "d2",
        [
            _mint(
                GraphExtractionAssertionKind.NEW_ENTITY,
                entity_class="Account",
                entity_key=key,
                display="Survivor",
                properties={"name": "Survivor"},
                evidence_ref="c2",
            )
        ],
    )
    # Seed a CONFIRMED node evidenced by both p1 and p2.
    asyncio.run(
        _seed_extracted_node(
            store,
            proposal_id=p1,
            chunk_ref="c1",
            display="Survivor",
            acl=("g1",),
            assurance="confirmed",
        )
    )
    node = store.raw_node(key)
    assert node is not None
    node["evidence"].append(f"{p2}|c2")

    review.reject(p2, _prov(), code=RejectionCode.DUPLICATE)
    result = asyncio.run(
        revert_extraction(
            engine, review, store, p2, actor="op", audit_sink=_RecordingAudit(), config_hash=_CFG
        )
    )
    assert result["nodes_deleted"] == 0
    survivor = store.raw_node(key)
    assert survivor is not None
    assert survivor["evidence"] == [f"{p1}|c1"]  # p2's ref removed, node survives


def test_revert_on_non_rejected_status_refuses() -> None:
    engine = _engine()
    store = InMemoryGraphStore()
    a = _mint(
        GraphExtractionAssertionKind.NEW_ENTITY,
        entity_class="Account",
        entity_key=llm_entity_key("Account", "Pending"),
        display="Pending",
        properties={"name": "Pending"},
        evidence_ref="c1",
    )
    pid = _seed_extraction_proposal(engine, "d1", [a])
    review = ProposalReviewService(engine)
    with pytest.raises(ExtractionGateError):
        asyncio.run(
            revert_extraction(
                engine,
                review,
                store,
                pid,
                actor="op",
                audit_sink=_RecordingAudit(),
                config_hash=_CFG,
            )
        )


# --- NE-T2X-4 Argus P2: graph.extraction_write had ZERO test coverage --------
# It is the ONLY accountability record for a modes-2/3 write — a write that
# lands in the graph WITHOUT prior human review — and apply.py:492 states its
# detail grammar normatively (C12, "counts-and-ids only"). A normative claim
# with no test is the defect class this epic keeps reshipping, so it is pinned
# here rather than left to the docstring.


def _write_provisional(
    engine: sa.Engine,
    review: ProposalReviewService,
    store: InMemoryGraphStore,
    resolver: Any,
    proposal_id: str,
    audit: Any,
) -> dict[str, Any]:
    return asyncio.run(
        write_provisional_extraction(
            engine,
            review,
            store,
            resolver,
            proposal_id,
            actor="op",
            audit_sink=audit,
            config_hash=_CFG,
            schema_version="1.0.0",
        )
    )


def _proposed_single_entity_batch(engine: sa.Engine, doc: str = "d-aw") -> str:
    return _seed_extraction_proposal(
        engine,
        doc,
        [
            _mint(
                GraphExtractionAssertionKind.NEW_ENTITY,
                entity_class="Account",
                entity_key=llm_entity_key("Account", "Acme"),
                display="Acme",
                properties={"name": "Acme"},
                evidence_ref="c1",
            )
        ],
    )


def test_provisional_write_emits_extraction_write_audit_with_counts_and_ids_only() -> None:
    """C12: the ``graph.extraction_write`` detail grammar is counts-and-ids ONLY.

    Asserts the detail key set EXACTLY rather than checking that the expected
    keys are present. A presence-only assertion is satisfied by a detail that
    ALSO carries extracted display strings or raw payload — i.e. by the very
    leak the "counts-and-ids only" claim exists to forbid — which is the
    field-presence vacuity shape this repo has now shipped four times.
    """
    engine = _engine()
    store = InMemoryGraphStore()
    pid = _proposed_single_entity_batch(engine)
    review = ProposalReviewService(engine)
    audit = _RecordingAudit()

    result = _write_provisional(
        engine, review, store, _DictAclResolver({"c1": ("g1",)}), pid, audit
    )

    assert result == {"nodes": 1, "edges": 0, "links_noop": 0}
    writes = [e for e in audit.events if e.action == "graph.extraction_write"]
    assert len(writes) == 1, f"expected exactly one extraction_write event, got {audit.events!r}"
    # ``detail`` is serialized JSON on the event, not a dict — parse before
    # comparing, or the key-set assertion silently compares CHARACTERS.
    detail = json.loads(writes[0].detail)
    assert set(detail) == {"proposal_id", "nodes", "edges", "links_noop"}, (
        "C12 violated: extraction_write detail must be counts-and-ids ONLY — "
        f"got extra/missing keys: {sorted(detail)}"
    )
    assert detail["proposal_id"] == pid
    assert (detail["nodes"], detail["edges"], detail["links_noop"]) == (1, 0, 0)
    # No extracted content may ride the audit detail (the display string is the
    # cheapest thing to leak, so it is the canary).
    assert "Acme" not in json.dumps(detail)


def test_provisional_write_skipped_by_status_emits_no_audit_event() -> None:
    """A status-skipped batch must emit NOTHING.

    Control for the test above: without it, an implementation emitting
    ``extraction_write`` unconditionally — including on batches it did NOT
    write — would still satisfy the positive assertion while making the audit
    trail claim writes that never happened.
    """
    engine = _engine()
    store = InMemoryGraphStore()
    pid = _proposed_single_entity_batch(engine, doc="d-skip")
    review = ProposalReviewService(engine)
    review.reject(pid, _prov(), code=RejectionCode.OUT_OF_SCOPE, note="not wanted")

    audit = _RecordingAudit()
    result = _write_provisional(
        engine, review, store, _DictAclResolver({"c1": ("g1",)}), pid, audit
    )

    assert "skipped_status" in result
    assert store.node_count() == 0
    assert [e for e in audit.events if e.action == "graph.extraction_write"] == []
