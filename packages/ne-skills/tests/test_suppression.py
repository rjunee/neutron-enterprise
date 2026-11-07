# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-2 decline suppression — the FIRST consumer of the NE-066 rejection signal.

Highest-care item A: suppression is a BUILD over the existing-but-unconsumed
``thumbs_down`` signal (design §5.2, verification-corrected). These tests drive the
REAL rejection path (``ProposalReviewService.reject`` emits the signal), then prove
the forge honours the fold: a rejected signature is not re-proposed within 90d
UNLESS its recurrence grows ≥3, and a re-proposal bumps the cluster epoch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import FixProposal, ProposalKind, SqlAlchemyProposalStore
from ne.flywheel.review import ProposalReviewService, RejectionCode
from ne.flywheel.signals import SignalProvenance
from ne.flywheel.store import SqlAlchemyCorrectionSignalStore
from ne.skills.forge import ForgeOutcome, SkillForge
from ne.skills.substrate import AuthorDispatch, PydanticAiSkillSubstrate
from ne.skills.suppression import suppression_decisions
from ne.skills.worthiness import SightingEntry, record_sightings

from .conftest import seed_workflow
from .test_forge import good_skill_md

_ACTIONS = ("k8s.apply", "k8s.rollout-status")


def _dispatch(output: str) -> AuthorDispatch:
    async def _d(task: str) -> str:
        return output

    return _d


def _sub(engine: sa.Engine) -> PydanticAiSkillSubstrate:
    return PydanticAiSkillSubstrate(
        engine, audit_engine=engine, author_dispatch=_dispatch(good_skill_md())
    )


def _forge_and_reject(engine: sa.Engine, prov: SignalProvenance) -> tuple[str, datetime]:
    """Forge a candidate, reject it, and return ``(signature, rejected_at)``."""
    seed_workflow(engine, trace_id="w1", agent="deployer", actions=_ACTIONS, succeeded=True)
    outcome = asyncio.run(SkillForge(engine, _sub(engine)).run_once())
    [pid] = outcome.proposal_ids
    store = SqlAlchemyProposalStore(engine)
    [proposal] = store.read_proposals(kind=ProposalKind.SKILL_CANDIDATE.value)
    signature = str(proposal.payload["signature"])
    ProposalReviewService(engine).reject(pid, prov, code=RejectionCode.DUPLICATE)
    [signal] = SqlAlchemyCorrectionSignalStore(engine).read_signals(kinds=["thumbs_down"])
    return signature, signal.occurred_at


def _run_forge(engine: sa.Engine, at: datetime) -> ForgeOutcome:
    return asyncio.run(SkillForge(engine, _sub(engine), clock=lambda: at).run_once())


def _skill_clusters(engine: sa.Engine) -> set[str]:
    store = SqlAlchemyProposalStore(engine)
    return {p.cluster_id for p in store.read_proposals(kind=ProposalKind.SKILL_CANDIDATE.value)}


def test_rejected_signature_suppressed_within_90d(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    signature, rejected_at = _forge_and_reject(forge_engine, prov)

    # A fresh recurrence of the SAME signature arrives; a re-run within 90d (no
    # growth) suppresses it — no new proposal.
    seed_workflow(forge_engine, trace_id="w2", agent="deployer", actions=_ACTIONS, succeeded=True)
    outcome = _run_forge(forge_engine, rejected_at + timedelta(hours=1))

    assert outcome.suppressed == 1
    assert outcome.authored == 0
    assert f"{signature}:r1" not in _skill_clusters(forge_engine)


def test_growth_of_3_reproposes_with_epoch_bump(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    signature, rejected_at = _forge_and_reject(forge_engine, prov)

    # THREE new sightings AFTER the rejection ⇒ recurrence grew ≥3 ⇒ the signature
    # earns a fresh look and is re-proposed at the bumped epoch (:r1).
    grow_at = rejected_at + timedelta(minutes=1)
    entries = [
        SightingEntry(
            signature=signature,
            snapshot_id=f"grow-{i}",
            principal_id=f"user:{i}",
            session_id="sess",
            department="",
            step_count=2,
            distinct_actions=2,
            finished_at=grow_at,
        )
        for i in range(3)
    ]
    with forge_engine.begin() as conn:
        record_sightings(conn, entries, grow_at)

    seed_workflow(forge_engine, trace_id="w2", agent="deployer", actions=_ACTIONS, succeeded=True)
    # +25h: past the daily window, within 90d.
    outcome = _run_forge(forge_engine, rejected_at + timedelta(hours=25))

    assert outcome.suppressed == 0
    assert outcome.authored == 1
    assert f"{signature}:r1" in _skill_clusters(forge_engine)


def test_growth_of_3_in_same_page_reproposes(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    # Codex P1 regression: the 3 overriding sightings FIRST arrive in this forge
    # page (recorded only in the run's final transaction), not in a prior committed
    # batch. suppression_decisions must fold the page's occurrences into growth, or
    # the signature is wrongly suppressed, dropped, and lost as the cursor advances.
    prov = make_provenance()
    signature, rejected_at = _forge_and_reject(forge_engine, prov)

    for i in range(3):
        seed_workflow(
            forge_engine,
            trace_id=f"grow-page-{i}",
            agent="deployer",
            actions=_ACTIONS,
            succeeded=True,
        )
    # +25h: past the daily window, within 90d.
    outcome = _run_forge(forge_engine, rejected_at + timedelta(hours=25))

    assert outcome.suppressed == 0
    assert outcome.authored == 1
    assert f"{signature}:r1" in _skill_clusters(forge_engine)


def test_suppression_expires_after_90d_with_epoch_bump(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    signature, rejected_at = _forge_and_reject(forge_engine, prov)

    seed_workflow(forge_engine, trace_id="w2", agent="deployer", actions=_ACTIONS, succeeded=True)
    # +91d: the 90d window has expired.
    outcome = _run_forge(forge_engine, rejected_at + timedelta(days=91))

    assert outcome.suppressed == 0
    assert outcome.authored == 1
    assert f"{signature}:r1" in _skill_clusters(forge_engine)


def test_non_skill_lane_rejection_ignored(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    # A rejection on a NON-skills lane emits a thumbs_down whose provenance lane is
    # not 'skills' — the fold ignores it entirely.
    prov = make_provenance()
    store = SqlAlchemyProposalStore(forge_engine)
    content = FixProposal(
        proposal_id="content-1",
        kind=ProposalKind.FIX,
        lane=FixLane.CONTENT,
        cluster_id="c1:r0",
        evidence_event_ids=("ev-1",),
        evidence_signal_ids=(),
        summary="a content fix",
        confidence=0.5,
        priority=ProposalPriority.MEDIUM,
        config_hash=frozen_config,
        created_at=datetime.now(UTC),
        payload={},
    )
    with forge_engine.begin() as conn:
        store.append(conn, content)
    ProposalReviewService(forge_engine).reject(
        content.proposal_id, prov, code=RejectionCode.INCORRECT
    )

    decisions = suppression_decisions(forge_engine, store, now=datetime.now(UTC))
    assert decisions == {}


def test_suppression_pages_past_the_signal_limit(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    """A newer skill rejection beyond the first page must still be folded.

    Regression for the Codex P2: the fold used a single oldest-first
    ``read_signals(limit=500)``, so once the store held more thumbs_down rows
    than the page, newer skill rejections fell outside it and suppression
    silently stopped applying — the moat failing at scale. Driven with
    ``signal_limit=1`` so the second rejection is on page 2; the paged read must
    surface BOTH signatures.
    """
    prov = make_provenance()
    store = SqlAlchemyProposalStore(forge_engine)
    # sig_a: a real forged+rejected skill (rejection id lands first).
    sig_a, _ = _forge_and_reject(forge_engine, prov)
    # sig_b: a second SKILLS-lane rejection, appended AFTER sig_a so its thumbs_down
    # has a higher signal id (page 2 under signal_limit=1). Manually constructed
    # (mirrors test_non_skill_lane) so this test isolates the fold's paging from the
    # forge's run-to-run state.
    sig_b = "sha256:sigB"
    proposal_b = FixProposal(
        proposal_id="skill-b-1",
        kind=ProposalKind.SKILL_CANDIDATE,
        lane=FixLane.SKILLS,
        cluster_id=f"{sig_b}:r0",
        evidence_event_ids=("ev-b",),
        evidence_signal_ids=(),
        summary="a second forged skill",
        confidence=0.5,
        priority=ProposalPriority.MEDIUM,
        config_hash=frozen_config,
        created_at=datetime.now(UTC),
        payload={},
    )
    with forge_engine.begin() as conn:
        store.append(conn, proposal_b)
    ProposalReviewService(forge_engine).reject(
        proposal_b.proposal_id, prov, code=RejectionCode.DUPLICATE
    )

    decisions = suppression_decisions(forge_engine, store, now=datetime.now(UTC), signal_limit=1)
    # The OLD oldest-first `read_signals(limit=1)` would return only sig_a (page 1);
    # the paged read surfaces both.
    assert sig_a in decisions
    assert sig_b in decisions
