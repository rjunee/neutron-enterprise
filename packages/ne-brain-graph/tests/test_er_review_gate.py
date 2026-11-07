# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T-ER-1/T-ER-2: ER is review-gated end-to-end.

T-ER-1 (structural): ``SplinkResolutionJob`` composed with a SCAN-ONLY store holds
no graph-write object; a full run (fake sidecar) leaves the graph byte-identical
and mutates ONLY ``ne_fix_proposals``; re-runs are idempotent (content-addressed id).
T-ER-2 (gate): ``apply_merge`` on PROPOSED raises + zero writes; on APPROVED writes
a SAME_AS edge + record_apply; a second apply is blocked; a REJECTED proposal is
forever blocked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from ne.audit import ensure_transactional_sqlite
from ne.brain_graph.config import BrainGraphSettings, ErClassConfig
from ne.brain_graph.er.job import SplinkResolutionJob
from ne.brain_graph.er.merge import ER_SOURCE_SYSTEM, MergeGateError, apply_merge
from ne.brain_graph.errors import GraphAclError
from ne.brain_graph.testing.fixtures import make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts import (
    Principal,
    freeze_config_hash,
    get_config_hash,
    reset_config_hash_for_testing,
)
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import FixProposal, ProposalKind, SqlAlchemyProposalStore
from ne.flywheel.review import ProposalReviewService, ProposalStatus, RejectionCode
from ne.flywheel.signals import SignalProvenance, SignalSurface


@pytest.fixture(autouse=True)
def _frozen_config() -> Iterator[str]:
    reset_config_hash_for_testing()
    digest = freeze_config_hash(BrainGraphSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


def _engine() -> sa.Engine:
    from ne.flywheel.review import ensure_review_schema

    eng = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ensure_transactional_sqlite(eng)
    ensure_review_schema(eng)
    return eng


class _ScanOnly:
    """A structurally scan-ONLY view — no upsert/delete is reachable (T-ER-1)."""

    def __init__(self, inner: InMemoryGraphStore) -> None:
        self._inner = inner

    async def scan_nodes(self, entity_class: str | None = None) -> Sequence[Mapping[str, Any]]:
        return await self._inner.scan_nodes(entity_class)


class _FakeSidecar:
    def __init__(self, pairs: list[dict[str, Any]]) -> None:
        self._pairs = pairs
        self.calls = 0

    def resolve(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self._pairs)


class _NullAudit:
    """A minimal AuditSink used to satisfy the (now-required) merge audit sink."""

    def __init__(self) -> None:
        self.actions: list[str] = []

    async def append(self, event: Any) -> None:
        self.actions.append(getattr(event, "action", ""))

    async def verify(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _person(key: str, email: str, acl: tuple[str, ...] = ("tenant",)) -> dict[str, Any]:
    return make_node(
        key,
        "Person",
        acl,
        given_name="Alice",
        family_name="Ng",
        email=email,
        display_name=f"Alice Ng {key}",
        source_ref=f"Contact:{key}",
    )


def _er_settings() -> BrainGraphSettings:
    return BrainGraphSettings(
        er_enabled=True,
        er_proposal_threshold=0.95,
        er_classes={
            "Person": ErClassConfig(
                compare_fields=("given_name", "family_name", "email"),
                blocking_fields=("email",),
            )
        },
    )


def _pair(left: str, right: str, prob: float) -> dict[str, Any]:
    return {
        "left_key": left,
        "right_key": right,
        "match_probability": prob,
        "match_weight": 12.0,
        "blocking_rule": "l.email = r.email",
    }


def test_er1_job_mutates_only_proposals_and_is_idempotent() -> None:
    store = InMemoryGraphStore()
    asyncio.run(store.upsert_nodes([_person("p1", "a@x.com"), _person("p2", "a@x.com")]))
    before_nodes = store.node_count()
    engine = _engine()
    sidecar = _FakeSidecar([_pair("p1", "p2", 0.995)])
    job = SplinkResolutionJob(
        _ScanOnly(store),  # structurally scan-only: no graph write reachable
        engine,
        _er_settings(),
        sidecar,
        config_hash="cfg",
        clock=lambda: datetime(2026, 7, 9, tzinfo=UTC),
    )

    appended = asyncio.run(job.run())
    assert len(appended) == 1
    # Graph is byte-identical (no node/edge written by the job).
    assert store.node_count() == before_nodes
    assert store.edge_count() == 0
    proposals = SqlAlchemyProposalStore(engine).read_proposals()
    assert len(proposals) == 1
    assert proposals[0].kind == ProposalKind.ENTITY_MERGE_CANDIDATE.value
    assert proposals[0].lane == FixLane.ENTITY_RESOLUTION.value
    assert proposals[0].priority == ProposalPriority.HIGH.value  # p>=0.99

    # Re-run: content-addressed id → SAVEPOINT-idempotent append (no new rows).
    again = asyncio.run(job.run())
    assert again == appended
    assert len(SqlAlchemyProposalStore(engine).read_proposals()) == 1


def test_er1_below_threshold_pairs_are_dropped() -> None:
    store = InMemoryGraphStore()
    asyncio.run(store.upsert_nodes([_person("p1", "a@x.com"), _person("p2", "b@x.com")]))
    engine = _engine()
    job = SplinkResolutionJob(
        _ScanOnly(store),
        engine,
        _er_settings(),
        _FakeSidecar([_pair("p1", "p2", 0.80)]),  # below 0.95 threshold
        config_hash="cfg",
        clock=lambda: datetime(2026, 7, 9, tzinfo=UTC),
    )
    assert asyncio.run(job.run()) == []
    assert SqlAlchemyProposalStore(engine).read_proposals() == []


def _seed_merge_proposal(engine: sa.Engine, proposal_id: str = "sha256:er-1") -> str:
    proposal = FixProposal(
        proposal_id=proposal_id,
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
            "match_probability": 0.99,
        },
    )
    store = SqlAlchemyProposalStore(engine)
    with engine.begin() as conn:
        store.append(conn, proposal)
    return proposal_id


def _prov() -> SignalProvenance:
    return SignalProvenance(
        principal=Principal(id="steward"), surface=SignalSurface.CONSOLE, session_id="s1"
    )


def test_er2_apply_merge_on_proposed_raises_zero_writes() -> None:
    engine = _engine()
    pid = _seed_merge_proposal(engine)
    store = InMemoryGraphStore()
    asyncio.run(store.upsert_nodes([_person("p1", "a@x.com"), _person("p2", "a@x.com")]))
    review = ProposalReviewService(engine)

    with pytest.raises(MergeGateError):
        asyncio.run(
            apply_merge(
                engine, review, store, pid, actor="op", audit_sink=_NullAudit(), config_hash="cfg"
            )
        )
    assert store.edge_count() == 0  # nothing written


def test_er2_approved_merge_writes_same_as_and_records_apply() -> None:
    engine = _engine()
    pid = _seed_merge_proposal(engine)
    store = InMemoryGraphStore()
    asyncio.run(store.upsert_nodes([_person("p1", "a@x.com"), _person("p2", "a@x.com")]))
    review = ProposalReviewService(engine)
    review.approve(pid, _prov())

    edge_key = asyncio.run(
        apply_merge(
            engine,
            review,
            store,
            pid,
            actor="op",
            audit_sink=_NullAudit(),
            config_hash=get_config_hash(),
        )
    )
    edge = store.raw_edge(edge_key)
    assert edge is not None
    assert edge["edge_kind"] == "SAME_AS"
    assert edge["source_system"] == ER_SOURCE_SYSTEM
    assert review.status_of(pid) is ProposalStatus.APPLIED
    reviews = review.read_reviews(proposal_id=pid)
    assert any(r.action == "apply" and r.applied_ref == edge_key for r in reviews)

    # A second apply is blocked (status is now APPLIED).
    with pytest.raises(MergeGateError):
        asyncio.run(
            apply_merge(
                engine, review, store, pid, actor="op", audit_sink=_NullAudit(), config_hash="cfg"
            )
        )


def test_er2_rejected_proposal_is_forever_blocked() -> None:
    engine = _engine()
    pid = _seed_merge_proposal(engine)
    store = InMemoryGraphStore()
    asyncio.run(store.upsert_nodes([_person("p1", "a@x.com"), _person("p2", "a@x.com")]))
    review = ProposalReviewService(engine)
    review.reject(pid, _prov(), code=RejectionCode.INCORRECT)

    with pytest.raises(MergeGateError):
        asyncio.run(
            apply_merge(
                engine, review, store, pid, actor="op", audit_sink=_NullAudit(), config_hash="cfg"
            )
        )
    assert store.edge_count() == 0


def test_er2_empty_acl_intersection_refuses() -> None:
    engine = _engine()
    pid = _seed_merge_proposal(engine)
    store = InMemoryGraphStore()
    # Disjoint ACLs -> empty intersection -> GraphAclError, nothing written.
    asyncio.run(
        store.upsert_nodes(
            [_person("p1", "a@x.com", acl=("a",)), _person("p2", "a@x.com", acl=("b",))]
        )
    )
    review = ProposalReviewService(engine)
    review.approve(pid, _prov())
    with pytest.raises(GraphAclError):
        asyncio.run(
            apply_merge(
                engine, review, store, pid, actor="op", audit_sink=_NullAudit(), config_hash="cfg"
            )
        )
    assert store.edge_count() == 0
