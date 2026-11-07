# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-3 headline: a near-identical re-forge proposes an UPDATE, never a sibling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from ne.contracts.skills import WorkflowSnapshot
from ne.flywheel.proposals import ProposalKind, SqlAlchemyProposalStore
from ne.flywheel.review import ProposalReviewService
from ne.flywheel.signals import SignalProvenance
from ne.skills.apply import SkillEdits, apply_skill_candidate
from ne.skills.forge import ForgeOutcome, SkillForge, ne_skill_forge_cursor
from ne.skills.registry import SqlAlchemySkillRegistry, ne_skill_versions
from ne.skills.substrate import AuthorDispatch, PydanticAiSkillSubstrate

from .conftest import seed_workflow
from .test_forge import good_skill_md

_T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


def _dispatch(output: str) -> AuthorDispatch:
    async def _d(task: str) -> str:
        return output

    return _d


def _substrate(engine: sa.Engine, output: str) -> PydanticAiSkillSubstrate:
    return PydanticAiSkillSubstrate(engine, audit_engine=engine, author_dispatch=_dispatch(output))


def _allow_all(_label: str) -> bool:
    return True


def _apply(
    engine: sa.Engine,
    pid: str,
    prov: SignalProvenance,
    *,
    edits: SkillEdits | None = None,
):
    return apply_skill_candidate(
        review_service=ProposalReviewService(engine),
        skills_engine=engine,
        registry=SqlAlchemySkillRegistry(engine),
        proposal_store=SqlAlchemyProposalStore(engine),
        proposal_id=pid,
        provenance=prov,
        acl_labels=(),
        global_visibility=True,
        label_validator=_allow_all,
        edits=edits,
    )


def _register_deploy_runbook_v1(engine: sa.Engine, prov: SignalProvenance) -> None:
    """Forge + approve + apply ``deploy-runbook`` v1 so the registry holds it."""
    seed_workflow(
        engine,
        trace_id="w1",
        agent="deployer",
        actions=("k8s.apply", "k8s.rollout-status"),
        succeeded=True,
        department="platform",
    )
    forge = SkillForge(engine, _substrate(engine, good_skill_md()), clock=lambda: _T0)
    [pid] = asyncio.run(forge.run_once()).proposal_ids
    ProposalReviewService(engine).approve(pid, prov)
    _apply(engine, pid, prov)


def _forge_update_candidate(engine: sa.Engine) -> ForgeOutcome:
    """Seed a near-identical (different-name, SAME-trigger) workflow + forge it.

    The reversed action order gives w2 a DISTINCT signature (so it is not the same
    forge cluster as w1), while the author emits ``shipit-runbook`` sharing
    ``deploy-runbook``'s trigger phrases — the near-dup that must become an UPDATE.
    """
    seed_workflow(
        engine,
        trace_id="w2",
        agent="shipper",
        actions=("k8s.rollout-status", "k8s.apply"),
        succeeded=True,
        department="platform",
    )
    t1 = _T0 + timedelta(hours=25)  # past the ~1/day cap
    sub = _substrate(engine, good_skill_md("shipit-runbook"))
    forge = SkillForge(engine, sub, clock=lambda: t1)
    return asyncio.run(forge.run_once())


def test_reforge_near_identical_proposes_update_never_sibling(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    _register_deploy_runbook_v1(forge_engine, prov)

    outcome = _forge_update_candidate(forge_engine)
    assert outcome.authored == 1
    [pid] = outcome.proposal_ids

    store = SqlAlchemyProposalStore(forge_engine)
    # NO skill_candidate was minted for the re-forge — only an UPDATE candidate.
    updates = store.read_proposals(kind=ProposalKind.SKILL_UPDATE_CANDIDATE.value)
    assert len(updates) == 1
    row = updates[0]
    assert row.proposal_id == pid
    assert row.kind == ProposalKind.SKILL_UPDATE_CANDIDATE.value
    pack = row.payload["pack"]
    assert pack["name"] == "deploy-runbook"  # the EXISTING name, never "shipit-runbook"
    assert pack["version"] == 2  # current_version + 1
    near_dup = row.payload["near_dup"]
    assert near_dup["target"] == "deploy-runbook"
    assert near_dup["verdict"] == "duplicate"
    assert near_dup["method"] == "trigger_phrase"
    assert near_dup["score"] == 1.0

    # Apply the update → deploy-runbook v2 in place; v1 still restorable; no sibling.
    ProposalReviewService(forge_engine).approve(pid, prov)
    applied = _apply(forge_engine, pid, prov)
    assert applied.name == "deploy-runbook"
    assert applied.version == 2

    reg = SqlAlchemySkillRegistry(forge_engine)
    assert reg.current_version_of("deploy-runbook") == 2
    assert reg.get_current("deploy-runbook").version == 2  # type: ignore[union-attr]
    with forge_engine.connect() as conn:
        versions = conn.execute(
            sa.select(ne_skill_versions.c.name, ne_skill_versions.c.version).order_by(
                ne_skill_versions.c.version
            )
        ).all()
    names = {v.name for v in versions}
    # The prior version row is STILL present (restorable) alongside v2.
    assert ("deploy-runbook", 1) in {(v.name, v.version) for v in versions}
    assert ("deploy-runbook", 2) in {(v.name, v.version) for v in versions}
    # A "-2" sibling / the Y-name is structurally impossible.
    assert "shipit-runbook" not in names
    assert "deploy-runbook-2" not in names
    assert not reg.name_exists("shipit-runbook")


def test_update_eval_excludes_target_pack(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    # The update shares deploy-runbook v1's triggers; if the eval library did NOT
    # exclude the same-name pack, near_miss_precision would false-fail against the
    # very pack being updated. The update authoring PROVES the exclusion.
    prov = make_provenance()
    _register_deploy_runbook_v1(forge_engine, prov)
    outcome = _forge_update_candidate(forge_engine)
    assert outcome.authored == 1
    assert outcome.eval_failed == 0


def test_update_candidate_not_reproposed_on_rerun(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    _register_deploy_runbook_v1(forge_engine, prov)
    _forge_update_candidate(forge_engine)
    store = SqlAlchemyProposalStore(forge_engine)
    assert len(store.read_proposals(kind=ProposalKind.SKILL_UPDATE_CANDIDATE.value)) == 1

    # Reset the cursor so w2 is re-scanned; a run well past the daily cap would be
    # ALLOWED to author, but the open UPDATE candidate's cluster is folded (both
    # kinds) ⇒ nothing new is appended.
    with forge_engine.begin() as conn:
        conn.execute(ne_skill_forge_cursor.delete())
    t2 = _T0 + timedelta(hours=50)
    forge = SkillForge(
        forge_engine, _substrate(forge_engine, good_skill_md("shipit-runbook")), clock=lambda: t2
    )
    again = asyncio.run(forge.run_once())
    assert again.authored == 0
    assert len(store.read_proposals(kind=ProposalKind.SKILL_UPDATE_CANDIDATE.value)) == 1


def test_no_near_dup_payload_carries_snapshot_and_none(
    forge_engine: sa.Engine, frozen_config: str
) -> None:
    # A fresh candidate against an EMPTY registry: near_dup is None and the payload
    # carries a validatable, names-only WorkflowSnapshot for the apply-time re-eval.
    seed_workflow(
        forge_engine,
        trace_id="w1",
        terminal_action="session.run",
        agent="deployer",
        actions=("k8s.apply", "k8s.rollout-status"),
        session_id="sess-1",
        department="platform",
    )
    forge = SkillForge(forge_engine, _substrate(forge_engine, good_skill_md()))
    outcome = asyncio.run(forge.run_once())
    assert outcome.authored == 1
    [pid] = outcome.proposal_ids
    store = SqlAlchemyProposalStore(forge_engine)
    [row] = store.read_proposals(kind=ProposalKind.SKILL_CANDIDATE.value)
    assert row.proposal_id == pid
    assert row.payload["near_dup"] is None
    snap = WorkflowSnapshot.model_validate(row.payload["snapshot"])
    assert snap.snapshot_id
    assert tuple(s.action for s in snap.steps) == ("k8s.apply", "k8s.rollout-status")
