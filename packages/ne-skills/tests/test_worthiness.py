# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-2 worthiness: the composite rank, idempotent sightings, the cap fold."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import FixProposal, ProposalKind, SqlAlchemyProposalStore
from ne.flywheel.review import ProposalReviewService, RejectionCode
from ne.flywheel.signals import SignalProvenance
from ne.skills.worthiness import (
    CandidateStat,
    SightingEntry,
    open_skill_proposal_count,
    rank_candidates,
    record_sightings,
    repetition_counts,
)


def test_rank_orders_by_repetition_then_principals_then_steps_then_recency() -> None:
    t = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    a = CandidateStat("a", repetition_30d=1, distinct_principals_30d=9, step_count=9, finished_at=t)
    b = CandidateStat("b", repetition_30d=3, distinct_principals_30d=1, step_count=9, finished_at=t)
    c = CandidateStat("c", repetition_30d=3, distinct_principals_30d=2, step_count=1, finished_at=t)
    d = CandidateStat("d", repetition_30d=3, distinct_principals_30d=2, step_count=5, finished_at=t)
    e = CandidateStat(
        "e",
        repetition_30d=3,
        distinct_principals_30d=2,
        step_count=5,
        finished_at=t + timedelta(hours=1),
    )
    ranked = rank_candidates([a, b, c, d, e])
    # repetition PRIMARY (a is rep-1, sinks last); then distinct principals (b has
    # 1 → after the pr-2 group); then step count (c has 1 → after d,e); then
    # recency (e newer than d).
    assert [s.signature for s in ranked] == ["e", "d", "c", "b", "a"]


def test_rank_signature_tiebreak_is_deterministic() -> None:
    t = datetime(2026, 7, 1, tzinfo=UTC)
    x = CandidateStat(
        "zzz", repetition_30d=2, distinct_principals_30d=1, step_count=2, finished_at=t
    )
    y = CandidateStat(
        "aaa", repetition_30d=2, distinct_principals_30d=1, step_count=2, finished_at=t
    )
    assert [s.signature for s in rank_candidates([x, y])] == ["aaa", "zzz"]


def test_sightings_idempotent_on_snapshot_id(forge_engine: sa.Engine) -> None:
    now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    entry = SightingEntry(
        signature="sig",
        snapshot_id="s1",
        principal_id="user:a",
        session_id="sess",
        department="",
        step_count=2,
        distinct_actions=2,
        finished_at=now,
    )
    with forge_engine.begin() as conn:
        record_sightings(conn, [entry], now)
    with forge_engine.begin() as conn:
        record_sightings(conn, [entry], now)  # same snapshot_id ⇒ no new row
    assert repetition_counts(forge_engine, ["sig"], since=now - timedelta(days=1)) == {"sig": 1}


def _append_skill_proposal(engine: sa.Engine, config_hash: str, tag: str) -> str:
    proposal_id = f"p-{tag}"
    fp = FixProposal(
        proposal_id=proposal_id,
        kind=ProposalKind.SKILL_CANDIDATE,
        lane=FixLane.SKILLS,
        cluster_id=f"{tag}:r0",
        evidence_event_ids=(f"ev-{tag}",),
        evidence_signal_ids=(),
        summary=f"[skill-candidate] {tag}",
        confidence=0.5,
        priority=ProposalPriority.LOW,
        config_hash=config_hash,
        created_at=datetime.now(UTC),
        payload={"pack": {}, "signature": tag, "eval_run": None},
    )
    with engine.begin() as conn:
        SqlAlchemyProposalStore(engine).append(conn, fp)
    return proposal_id


def test_open_skill_proposal_count_folds_review_log(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    store = SqlAlchemyProposalStore(forge_engine)
    service = ProposalReviewService(forge_engine)
    prov = make_provenance()

    _append_skill_proposal(forge_engine, frozen_config, "prop")  # stays 'proposed'
    p_approved = _append_skill_proposal(forge_engine, frozen_config, "appr")
    p_rejected = _append_skill_proposal(forge_engine, frozen_config, "rej")
    p_applied = _append_skill_proposal(forge_engine, frozen_config, "appl")

    service.approve(p_approved, prov)
    service.reject(p_rejected, prov, code=RejectionCode.DUPLICATE)
    service.approve(p_applied, prov)
    service.record_apply(p_applied, prov, applied_ref="skill:x@v1")

    # OPEN = proposed or approved: p_proposed + p_approved = 2; rejected/applied
    # (terminal) do not count (the fold mirrors folded_status_map).
    assert open_skill_proposal_count(forge_engine, store) == 2
