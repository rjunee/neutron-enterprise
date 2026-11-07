# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T-ER-3: non-destructive merge semantics + intersection ACL + audit.

* the SAME_AS edge acl == sorted label-set intersection of the two nodes;
* a principal who sees only one side never sees the SAME_AS edge in a traversal;
* unmerge deletes the edge + clears canonical_key;
* graph.entity_merge audit is emitted on BOTH merge and unmerge.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from ne.audit import ensure_transactional_sqlite
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.er.merge import MergeGateError, apply_merge, unmerge
from ne.brain_graph.model import EntityKey
from ne.brain_graph.read import EntityGraph
from ne.brain_graph.testing.fixtures import make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts import (
    Principal,
    freeze_config_hash,
    get_config_hash,
    reset_config_hash_for_testing,
)
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditVerification
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import FixProposal, ProposalKind, SqlAlchemyProposalStore
from ne.flywheel.review import ProposalReviewService, ensure_review_schema
from ne.flywheel.signals import SignalProvenance, SignalSurface


@pytest.fixture(autouse=True)
def _frozen_config() -> Iterator[str]:
    reset_config_hash_for_testing()
    digest = freeze_config_hash(BrainGraphSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


class _FakeAudit:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.details: list[str] = []

    async def append(self, event: AuditEventLike) -> None:
        self.actions.append(getattr(event, "action", ""))
        self.details.append(getattr(event, "detail", "") or "")

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:  # pragma: no cover - satisfies the AuditSink protocol
        raise NotImplementedError


def _engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ensure_transactional_sqlite(eng)
    ensure_review_schema(eng)
    return eng


def _person(key: str, acl: tuple[str, ...]) -> dict[str, object]:
    return make_node(
        key, "Person", acl, given_name="Alice", family_name="Ng", source_ref=f"Contact:{key}"
    )


def _seed_and_approve(engine: sa.Engine) -> tuple[str, ProposalReviewService]:
    proposal = FixProposal(
        proposal_id="sha256:er-1",
        kind=ProposalKind.ENTITY_MERGE_CANDIDATE,
        lane=FixLane.ENTITY_RESOLUTION,
        cluster_id="er:Person:abcd",
        evidence_event_ids=(),
        evidence_signal_ids=(),
        summary="candidate merge p1 == p2",
        confidence=0.99,
        priority=ProposalPriority.HIGH,
        config_hash="cfg",
        created_at=datetime(2026, 7, 9, tzinfo=UTC),
        payload={
            "left": {"entity_class": "Person", "entity_key": "p1", "display": "Alice"},
            "right": {"entity_class": "Person", "entity_key": "p2", "display": "Alice"},
        },
    )
    with engine.begin() as conn:
        SqlAlchemyProposalStore(engine).append(conn, proposal)
    review = ProposalReviewService(engine)
    review.approve(
        "sha256:er-1",
        SignalProvenance(
            principal=Principal(id="steward"), surface=SignalSurface.CONSOLE, session_id="s1"
        ),
    )
    return "sha256:er-1", review


def test_er3_same_as_acl_is_intersection_and_audit_emitted() -> None:
    engine = _engine()
    pid, review = _seed_and_approve(engine)
    store = InMemoryGraphStore()
    asyncio.run(store.upsert_nodes([_person("p1", ("a", "b")), _person("p2", ("b", "c"))]))
    audit = _FakeAudit()

    edge_key = asyncio.run(
        apply_merge(
            engine, review, store, pid, actor="op", audit_sink=audit, config_hash=get_config_hash()
        )
    )
    edge = store.raw_edge(edge_key)
    assert edge is not None
    assert tuple(edge["acl"]) == ("b",)  # sorted intersection {a,b} & {b,c}
    # canonical_key set on the non-canonical node (other = max(p1,p2) = p2).
    p2 = store.raw_node("p2")
    assert p2 is not None and p2["canonical_key"] == "p1"
    assert "graph.entity_merge" in audit.actions
    assert any("merge" in d for d in audit.details)


def test_er3_one_sided_principal_never_sees_same_as() -> None:
    engine = _engine()
    pid, review = _seed_and_approve(engine)
    store = InMemoryGraphStore()
    asyncio.run(store.upsert_nodes([_person("p1", ("a", "b")), _person("p2", ("b", "c"))]))
    edge_key = asyncio.run(
        apply_merge(
            engine,
            review,
            store,
            pid,
            actor="op",
            audit_sink=_FakeAudit(),
            config_hash=get_config_hash(),
        )
    )

    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    # A principal with only 'a' sees p1 but NOT p2 nor the intersection-ACL'd edge.
    one_sided = asyncio.run(
        graph.neighbors(
            [EntityKey(entity_class="Person", key="p1")], principal=Principal(id="x", groups=("a",))
        )
    )
    assert all(e.key != edge_key for e in one_sided.edges)
    # A principal with 'b' (in the intersection) DOES see the SAME_AS edge.
    both = asyncio.run(
        graph.neighbors(
            [EntityKey(entity_class="Person", key="p1")], principal=Principal(id="y", groups=("b",))
        )
    )
    assert any(e.key == edge_key for e in both.edges)


def test_er3_unmerge_deletes_edge_and_clears_canonical_key() -> None:
    engine = _engine()
    pid, review = _seed_and_approve(engine)
    store = InMemoryGraphStore()
    asyncio.run(store.upsert_nodes([_person("p1", ("a", "b")), _person("p2", ("b", "c"))]))
    audit = _FakeAudit()
    edge_key = asyncio.run(
        apply_merge(
            engine,
            review,
            store,
            pid,
            actor="op",
            audit_sink=audit,
            config_hash=get_config_hash(),
        )
    )
    p2_before = store.raw_node("p2")
    assert p2_before is not None and p2_before["canonical_key"] == "p1"

    asyncio.run(
        unmerge(store, edge_key, "p2", actor="op", audit_sink=audit, config_hash=get_config_hash())
    )
    assert store.raw_edge(edge_key) is None
    p2_after = store.raw_node("p2")
    assert p2_after is not None and "canonical_key" not in p2_after
    assert "graph.entity_merge" in audit.actions
    assert any("unmerge" in d for d in audit.details)


# --- F5 (NE-BRAIN-T2-WIRING): apply_merge proposal-kind + payload-shape guard ---


def _seed_and_approve_kind(
    engine: sa.Engine,
    *,
    kind: ProposalKind,
    lane: FixLane,
    payload: dict[str, object],
) -> tuple[str, ProposalReviewService]:
    proposal = FixProposal(
        proposal_id="sha256:guard-1",
        kind=kind,
        lane=lane,
        cluster_id="cl-guard",
        evidence_event_ids=(),
        evidence_signal_ids=(),
        summary="a non-ER or malformed proposal",
        confidence=0.99,
        priority=ProposalPriority.HIGH,
        config_hash="cfg",
        created_at=datetime(2026, 7, 9, tzinfo=UTC),
        payload=payload,
    )
    with engine.begin() as conn:
        SqlAlchemyProposalStore(engine).append(conn, proposal)
    review = ProposalReviewService(engine)
    review.approve(
        "sha256:guard-1",
        SignalProvenance(
            principal=Principal(id="steward"), surface=SignalSurface.CONSOLE, session_id="s1"
        ),
    )
    return "sha256:guard-1", review


def test_apply_merge_refuses_non_er_proposal_kind() -> None:
    # An APPROVED but NON-ER proposal kind is a clean MergeGateError (not a KeyError):
    # zero edges written, no graph.entity_merge audit emitted (fail-closed, graceful).
    engine = _engine()
    pid, review = _seed_and_approve_kind(
        engine,
        kind=ProposalKind.FIX,
        lane=FixLane.CONTENT,
        payload={"anything": "not-an-er-payload"},
    )
    store = InMemoryGraphStore()
    audit = _FakeAudit()
    with pytest.raises(MergeGateError, match="kind"):
        asyncio.run(
            apply_merge(
                engine,
                review,
                store,
                pid,
                actor="op",
                audit_sink=audit,
                config_hash=get_config_hash(),
            )
        )
    assert store.edge_count() == 0
    assert "graph.entity_merge" not in audit.actions


def test_apply_merge_refuses_malformed_er_payload() -> None:
    # ENTITY_MERGE_CANDIDATE kind but a payload missing left/right ⇒ MergeGateError,
    # nothing written (a graceful message, never a bare KeyError).
    engine = _engine()
    pid, review = _seed_and_approve_kind(
        engine,
        kind=ProposalKind.ENTITY_MERGE_CANDIDATE,
        lane=FixLane.ENTITY_RESOLUTION,
        payload={},
    )
    store = InMemoryGraphStore()
    audit = _FakeAudit()
    with pytest.raises(MergeGateError, match="malformed"):
        asyncio.run(
            apply_merge(
                engine,
                review,
                store,
                pid,
                actor="op",
                audit_sink=audit,
                config_hash=get_config_hash(),
            )
        )
    assert store.edge_count() == 0
    assert "graph.entity_merge" not in audit.actions
