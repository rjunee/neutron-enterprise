# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SkillForge.run_once end-to-end (NE-SKILL-1b): propose, idempotency, floor, audit."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from ne.audit import verify_chain
from ne.audit.store import ne_audit_log
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import FixProposal, ProposalKind, SqlAlchemyProposalStore
from ne.skills.forge import SkillForge, ne_skill_forge_cursor
from ne.skills.substrate import AuthorDispatch, PydanticAiSkillSubstrate

from .conftest import seed_workflow


def good_skill_md(name: str = "deploy-runbook") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: Repeat the standard deploy-and-verify workflow\n"
        "version: 1\n"
        "triggers:\n"
        "  - deploy the service\n"
        "  - roll out a release\n"
        "source: forged\n"
        "---\n"
        "## When to Use\nWhen a release must be deployed and verified.\n\n"
        "## Procedure\n1. Apply the manifest.\n2. Watch the rollout.\n\n"
        "## Pitfalls\nDo not skip the rollout check.\n\n"
        "## Verification\nConfirm the rollout is healthy.\n"
    )


def _scripted_dispatch(output: str):
    async def _dispatch(task: str) -> str:
        return output

    return _dispatch


def _substrate(engine: sa.Engine, output: str) -> PydanticAiSkillSubstrate:
    return PydanticAiSkillSubstrate(
        engine, audit_engine=engine, author_dispatch=_scripted_dispatch(output)
    )


def _seed_good_workflow(engine: sa.Engine) -> None:
    seed_workflow(
        engine,
        trace_id="w1",
        agent="deployer",
        actions=("k8s.apply", "k8s.rollout-status"),
        succeeded=True,
        department="platform",
    )


def test_forge_end_to_end_proposes_skill_candidate(
    forge_engine: sa.Engine, frozen_config: str
) -> None:
    seed_workflow(
        forge_engine,
        trace_id="w1",
        terminal_action="session.run",
        agent="deployer",
        actions=("k8s.apply", "k8s.rollout-status"),
        succeeded=True,
        session_id="sess-1",
        department="platform",
    )
    forge = SkillForge(forge_engine, _substrate(forge_engine, good_skill_md()))
    outcome = asyncio.run(forge.run_once())

    assert outcome.authored == 1
    assert outcome.floor_passed == 1
    [pid] = outcome.proposal_ids

    store = SqlAlchemyProposalStore(forge_engine)
    [row] = store.read_proposals(kind=ProposalKind.SKILL_CANDIDATE.value)
    assert row.proposal_id == pid
    assert row.lane == FixLane.SKILLS.value
    assert row.cluster_id.endswith(":r0")
    payload = row.payload
    assert payload["pack"]["name"] == "deploy-runbook"
    assert payload["pack"]["source"] == "forged"
    assert payload["near_dup"] is None
    assert payload["worthiness"]["floor"] == "v1"
    assert payload["worthiness"]["distinct_actions"] == 2
    assert payload["suggested_acl_labels"] == ["grp:platform"]
    # content_hash present in the dumped pack (carried for the apply-seam check).
    assert "content_hash" in payload["pack"]
    # NE-SKILL-2: the payload now carries a PASSING eval ref whose routing_hash
    # matches the pack (the D-12 label-excluded binding the apply seam enforces).
    eval_ref = payload["eval_run"]
    assert eval_ref is not None
    assert eval_ref["suite"] == "skill-trigger"
    assert eval_ref["passed"] is True
    assert eval_ref["routing_hash"] == payload["pack"]["routing_hash"]


def test_forge_rerun_appends_nothing(forge_engine: sa.Engine, frozen_config: str) -> None:
    seed_workflow(
        forge_engine,
        trace_id="w1",
        terminal_action="session.run",
        agent="deployer",
        actions=("k8s.apply", "k8s.rollout-status"),
        session_id="sess-1",
    )
    sub = _substrate(forge_engine, good_skill_md())
    forge = SkillForge(forge_engine, sub)
    asyncio.run(forge.run_once())
    # Reset the cursor so the same snapshot is re-read (proves content-address
    # idempotency, not just cursor advance).
    with forge_engine.begin() as conn:
        conn.execute(ne_skill_forge_cursor.delete())
    asyncio.run(forge.run_once())

    store = SqlAlchemyProposalStore(forge_engine)
    rows = store.read_proposals(kind=ProposalKind.SKILL_CANDIDATE.value)
    assert len(rows) == 1


def _covered_dispatch() -> tuple[AuthorDispatch, list[int]]:
    """An author dispatch returning a distinct good pack per call (body covers the
    apply/rollout snapshot actions so the NE-SKILL-2 utility band passes)."""
    counter = [0]

    async def _dispatch(task: str) -> str:
        counter[0] += 1
        return good_skill_md(name=f"runbook-{counter[0]}")

    return _dispatch, counter


def test_forge_ranks_and_queues_excess_never_dropped(
    forge_engine: sa.Engine, frozen_config: str
) -> None:
    # Two DISTINCT worthy workflows (different action ORDER ⇒ different signatures,
    # both covered by the good pack) in one page. The rate cap authors at most ONE
    # per run — the ranked runner-up QUEUES (never dropped) and is authored on a
    # later run once the daily window resets. Nothing is lost (highest-care item C).
    seed_workflow(
        forge_engine, trace_id="w1", agent="deployer", actions=("k8s.apply", "k8s.rollout-status")
    )
    seed_workflow(
        forge_engine, trace_id="w2", agent="deployer2", actions=("k8s.rollout-status", "k8s.apply")
    )
    dispatch, _counter = _covered_dispatch()
    sub = PydanticAiSkillSubstrate(
        forge_engine, audit_engine=forge_engine, author_dispatch=dispatch
    )
    store = SqlAlchemyProposalStore(forge_engine)

    t0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    outcome = asyncio.run(SkillForge(forge_engine, sub, clock=lambda: t0).run_once())
    assert outcome.floor_passed == 2
    assert outcome.authored == 1
    assert outcome.queued == 1
    assert len(store.read_proposals(kind=ProposalKind.SKILL_CANDIDATE.value)) == 1

    # +25h: the daily window has reset and one open slot remains ⇒ the queued
    # candidate is authored. Two proposals total; the runner-up was never dropped.
    t1 = t0 + timedelta(hours=25)
    follow = asyncio.run(SkillForge(forge_engine, sub, clock=lambda: t1).run_once())
    assert follow.authored == 1
    assert len(store.read_proposals(kind=ProposalKind.SKILL_CANDIDATE.value)) == 2


def test_forge_daily_cap_queues_second_in_day(forge_engine: sa.Engine, frozen_config: str) -> None:
    # Two worthy candidates; run one authors the first, a run <20h later authors
    # NONE (the ~1/day cap) and the runner-up stays queued (open cap is not hit).
    seed_workflow(
        forge_engine, trace_id="w1", agent="deployer", actions=("k8s.apply", "k8s.rollout-status")
    )
    seed_workflow(
        forge_engine, trace_id="w2", agent="deployer2", actions=("k8s.rollout-status", "k8s.apply")
    )
    dispatch, _counter = _covered_dispatch()
    sub = PydanticAiSkillSubstrate(
        forge_engine, audit_engine=forge_engine, author_dispatch=dispatch
    )
    store = SqlAlchemyProposalStore(forge_engine)

    t0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    asyncio.run(SkillForge(forge_engine, sub, clock=lambda: t0).run_once())
    t_same_day = t0 + timedelta(hours=2)
    outcome = asyncio.run(SkillForge(forge_engine, sub, clock=lambda: t_same_day).run_once())
    assert outcome.authored == 0
    assert outcome.queued == 1
    assert len(store.read_proposals(kind=ProposalKind.SKILL_CANDIDATE.value)) == 1


def _seed_open_skill_proposal(
    engine: sa.Engine, config_hash: str, tag: str, created_at: datetime
) -> None:
    fp = FixProposal(
        proposal_id=f"open-{tag}",
        kind=ProposalKind.SKILL_CANDIDATE,
        lane=FixLane.SKILLS,
        cluster_id=f"{tag}:r0",
        evidence_event_ids=(f"ev-{tag}",),
        evidence_signal_ids=(),
        summary=f"[skill-candidate] {tag}",
        confidence=0.5,
        priority=ProposalPriority.LOW,
        config_hash=config_hash,
        created_at=created_at,
        payload={"pack": {}, "signature": tag, "eval_run": None},
    )
    with engine.begin() as conn:
        SqlAlchemyProposalStore(engine).append(conn, fp)


def test_forge_open_cap_two_open_queues_third(forge_engine: sa.Engine, frozen_config: str) -> None:
    # Two OPEN skill proposals already exist (no review rows ⇒ folded 'proposed').
    # A third worthy candidate must QUEUE — the open cap of 2 blocks authoring even
    # though the daily window has long passed.
    old = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    _seed_open_skill_proposal(forge_engine, frozen_config, "aaa", old)
    _seed_open_skill_proposal(forge_engine, frozen_config, "bbb", old)
    seed_workflow(
        forge_engine, trace_id="w1", agent="deployer", actions=("k8s.apply", "k8s.rollout-status")
    )
    sub = _substrate(forge_engine, good_skill_md())
    now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    outcome = asyncio.run(SkillForge(forge_engine, sub, clock=lambda: now).run_once())
    assert outcome.authored == 0
    assert outcome.queued == 1
    store = SqlAlchemyProposalStore(forge_engine)
    # Only the two pre-seeded opens exist — nothing new authored past the cap.
    assert len(store.read_proposals(kind=ProposalKind.SKILL_CANDIDATE.value)) == 2


_EVAL_FAILING_MD = (
    "---\n"
    "name: helper\n"
    "description: a generic helper\n"
    "version: 1\n"
    "triggers:\n"
    "  - help\n"
    "source: forged\n"
    "---\n"
    "## When to Use\nWhen the action runs.\n\n"
    "## Procedure\n1. Do apply.\n2. Watch the rollout.\n\n"
    "## Pitfalls\nNone.\n\n"
    "## Verification\nConfirm.\n"
)


def test_forge_eval_failed_candidate_not_proposed(
    forge_engine: sa.Engine, frozen_config: str
) -> None:
    # A standard-passing but ROUTING-GARBAGE pack (trigger 'help' is hijacked by
    # the 'expense report help' decoy) FAILS the eval suite ⇒ zero proposals, and
    # the audit detail carries the eval_failed count.
    _seed_good_workflow(forge_engine)
    forge = SkillForge(forge_engine, _substrate(forge_engine, _EVAL_FAILING_MD))
    outcome = asyncio.run(forge.run_once())
    assert outcome.eval_failed == 1
    assert outcome.authored == 0
    store = SqlAlchemyProposalStore(forge_engine)
    assert store.read_proposals(kind=ProposalKind.SKILL_CANDIDATE.value) == []
    with forge_engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_audit_log).where(ne_audit_log.c.action == "skill.forge_run")
        ).one()
    detail = json.loads(row.payload["detail"])
    assert detail["eval_failed"] == 1


def test_forge_floor_rejects_failed_and_single_action(
    forge_engine: sa.Engine, frozen_config: str
) -> None:
    # A failed workflow and a single-distinct-action workflow both fail the floor.
    seed_workflow(forge_engine, trace_id="f1", actions=("a", "b"), succeeded=False)
    seed_workflow(forge_engine, trace_id="f2", actions=("a", "a"), succeeded=True)
    forge = SkillForge(forge_engine, _substrate(forge_engine, good_skill_md()))
    outcome = asyncio.run(forge.run_once())
    assert outcome.floor_passed == 0
    assert outcome.authored == 0


def test_forge_standard_violating_draft_dropped_and_counted(
    forge_engine: sa.Engine, frozen_config: str
) -> None:
    _seed_good_workflow(forge_engine)
    bad = good_skill_md().replace("## Verification\nConfirm the rollout is healthy.\n", "")
    forge = SkillForge(forge_engine, _substrate(forge_engine, bad))
    outcome = asyncio.run(forge.run_once())
    assert outcome.rejected_by_standard == 1
    assert outcome.authored == 0
    store = SqlAlchemyProposalStore(forge_engine)
    assert store.read_proposals(kind=ProposalKind.SKILL_CANDIDATE.value) == []


def test_forge_authoring_failure_counts_and_cursor_advances(
    forge_engine: sa.Engine, frozen_config: str
) -> None:
    _seed_good_workflow(forge_engine)
    forge = SkillForge(forge_engine, _substrate(forge_engine, "not a skill document at all"))
    outcome = asyncio.run(forge.run_once())
    assert outcome.authoring_failed == 1
    assert outcome.authored == 0
    # Cursor advanced past the scanned terminal (skip-on-failure).
    assert outcome.cursor_end != "0"


def test_forge_run_dual_appends_audit_event(forge_engine: sa.Engine, frozen_config: str) -> None:
    _seed_good_workflow(forge_engine)
    forge = SkillForge(forge_engine, _substrate(forge_engine, good_skill_md()))
    asyncio.run(forge.run_once())
    with forge_engine.connect() as conn:
        rows = conn.execute(
            sa.select(ne_audit_log).where(ne_audit_log.c.action == "skill.forge_run")
        ).all()
    assert len(rows) == 1
    assert verify_chain(forge_engine).valid
