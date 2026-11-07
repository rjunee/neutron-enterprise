# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Hazard B: the one-transaction apply-skill seam (register + audit or roll back)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from ne.audit import verify_chain
from ne.audit.store import ne_audit_log
from ne.flywheel.errors import InvalidTransitionError, UnknownProposalError
from ne.flywheel.proposals import SqlAlchemyProposalStore
from ne.flywheel.review import ProposalReviewService, ProposalStatus
from ne.flywheel.signals import SignalProvenance
from ne.skills.apply import apply_skill_candidate
from ne.skills.errors import (
    SkillApplyGateError,
    SkillApplyIntegrityError,
    SkillApplyLabelError,
    SkillApplyVersionError,
)
from ne.skills.forge import SkillForge
from ne.skills.registry import (
    SqlAlchemySkillRegistry,
    ne_skill_lifecycle,
    ne_skill_versions,
    ne_skills,
)
from ne.skills.substrate import PydanticAiSkillSubstrate

from .conftest import seed_workflow
from .test_forge import good_skill_md


def _allow_all(_label: str) -> bool:
    return True


def _forge_a_candidate(engine: sa.Engine, *, output: str | None = None) -> str:
    seed_workflow(
        engine,
        trace_id="w1",
        agent="deployer",
        actions=("k8s.apply", "k8s.rollout-status"),
        succeeded=True,
        department="platform",
    )
    sub = PydanticAiSkillSubstrate(
        engine, audit_engine=engine, author_dispatch=_dispatch(output or good_skill_md())
    )
    outcome = asyncio.run(SkillForge(engine, sub).run_once())
    [pid] = outcome.proposal_ids
    return pid


def _dispatch(output: str):
    async def _d(task: str) -> str:
        return output

    return _d


def _approve(engine: sa.Engine, pid: str, prov: SignalProvenance) -> None:
    ProposalReviewService(engine).approve(pid, prov)


def _apply(
    engine: sa.Engine,
    pid: str,
    prov: SignalProvenance,
    *,
    acl_labels: tuple[str, ...] = ("grp:platform",),
    global_visibility: bool = False,
    label_validator: Callable[[str], bool] = _allow_all,
    edits: object | None = None,
):
    return apply_skill_candidate(
        review_service=ProposalReviewService(engine),
        skills_engine=engine,
        registry=SqlAlchemySkillRegistry(engine),
        proposal_store=SqlAlchemyProposalStore(engine),
        proposal_id=pid,
        provenance=prov,
        acl_labels=acl_labels,
        global_visibility=global_visibility,
        label_validator=label_validator,
        edits=edits,  # type: ignore[arg-type]
    )


def _count(engine: sa.Engine, table: sa.Table) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


def test_apply_registers_and_chains_atomically(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)
    _approve(forge_engine, pid, prov)

    applied = _apply(forge_engine, pid, prov, acl_labels=("grp:platform",))

    assert applied.name == "deploy-runbook"
    assert applied.version == 1
    assert applied.acl_labels == ("grp:platform",)
    # parent + version + lifecycle('registered') + record_apply fold all landed.
    assert _count(forge_engine, ne_skills) == 1
    assert _count(forge_engine, ne_skill_versions) == 1
    assert ProposalReviewService(forge_engine).status_of(pid) is ProposalStatus.APPLIED
    with forge_engine.connect() as conn:
        events = conn.execute(
            sa.select(ne_audit_log).where(ne_audit_log.c.action == "skill.apply")
        ).all()
    assert len(events) == 1
    import json

    detail = json.loads(events[0].payload["detail"])
    assert detail["content_hash"]
    assert detail["acl_labels"] == ["grp:platform"]
    assert detail["review_id"] == applied.review.review_id
    assert detail["proposal_id"] == pid
    assert verify_chain(forge_engine).valid
    # The stored version row carries the assigned labels (relabel = version identity).
    with forge_engine.connect() as conn:
        row = conn.execute(sa.select(ne_skill_versions.c.acl_labels)).first()
    assert row is not None
    assert list(row.acl_labels) == ["grp:platform"]


def test_apply_chain_failure_rolls_back_everything(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)
    _approve(forge_engine, pid, prov)

    # Inject a failure in the chain append inside the extra_writes closure.
    import ne.skills.apply as apply_mod

    def _boom(conn: sa.Connection, event: object) -> None:
        raise RuntimeError("chain append failed")

    monkeypatch.setattr(apply_mod, "append_record", _boom)

    with pytest.raises(RuntimeError):
        _apply(forge_engine, pid, prov)

    # Nothing landed; fold stays approved.
    assert _count(forge_engine, ne_skills) == 0
    assert _count(forge_engine, ne_skill_versions) == 0
    assert _count(forge_engine, ne_skill_lifecycle) == 0
    assert ProposalReviewService(forge_engine).status_of(pid) is ProposalStatus.APPROVED
    assert verify_chain(forge_engine).valid


def test_apply_unknown_proposal_raises_unknown_not_gate(
    forge_engine: sa.Engine,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    """A genuinely-unknown id must raise UnknownProposalError (route → 404),
    NOT SkillApplyGateError (409). Regression for the Codex P2: apply used a
    capped 10k page scan, so it both missed proposals past the page AND
    mislabelled a real miss as a gate error. Now a direct indexed lookup.
    """
    prov = make_provenance()
    with pytest.raises(UnknownProposalError):
        _apply(forge_engine, "sha256:does-not-exist", prov)


def test_apply_on_non_approved_raises_gate_error(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)
    # Not approved.
    with pytest.raises(SkillApplyGateError):
        _apply(forge_engine, pid, prov)


def test_apply_tampered_payload_refused(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)
    _approve(forge_engine, pid, prov)
    # Tamper the stored payload pack body without updating content_hash.
    from ne.flywheel.proposals import ne_fix_proposals

    with forge_engine.begin() as conn:
        row = conn.execute(
            sa.select(ne_fix_proposals.c.id, ne_fix_proposals.c.payload).where(
                ne_fix_proposals.c.proposal_id == pid
            )
        ).one()
        payload = dict(row.payload)
        pack = dict(payload["pack"])
        pack["body"] = pack["body"] + "\n## Extra\ntampered\n"
        payload["pack"] = pack
        conn.execute(
            ne_fix_proposals.update().where(ne_fix_proposals.c.id == row.id).values(payload=payload)
        )
    with pytest.raises(SkillApplyIntegrityError):
        _apply(forge_engine, pid, prov)


def test_apply_empty_labels_without_global_refused(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)
    _approve(forge_engine, pid, prov)
    with pytest.raises(SkillApplyLabelError):
        _apply(forge_engine, pid, prov, acl_labels=(), global_visibility=False)


def test_apply_unknown_label_refused(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)
    _approve(forge_engine, pid, prov)
    with pytest.raises(SkillApplyLabelError):
        _apply(
            forge_engine,
            pid,
            prov,
            acl_labels=("grp:unknown",),
            label_validator=lambda label: label == "grp:known",
        )


def test_apply_global_visibility_registers_unlabelled(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)
    _approve(forge_engine, pid, prov)
    applied = _apply(forge_engine, pid, prov, acl_labels=(), global_visibility=True)
    assert applied.acl_labels == ()
    assert _count(forge_engine, ne_skill_versions) == 1


def test_apply_candidate_when_name_exists_refused(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)
    _approve(forge_engine, pid, prov)
    _apply(forge_engine, pid, prov)  # first apply registers deploy-runbook v1
    # A second candidate for the same name must refuse (propose an update). The
    # forge would never MINT a same-name skill_candidate (the trigger eval can't
    # distinguish it from the registered pack — that near-dup is NE-SKILL-3's
    # UPDATE path), so we seed a second same-name candidate DIRECTLY with a real
    # passing eval to exercise the apply seam's defence-in-depth version guard.
    reg = SqlAlchemySkillRegistry(forge_engine)
    assert reg.name_exists("deploy-runbook")
    pid2 = _seed_same_name_candidate_with_eval(forge_engine)
    _approve(forge_engine, pid2, prov)
    with pytest.raises(SkillApplyVersionError):
        _apply(forge_engine, pid2, prov)


def _seed_same_name_candidate_with_eval(engine: sa.Engine) -> str:
    """Seed a second ``deploy-runbook`` skill_candidate with a REAL passing eval."""
    from ne.contracts import get_config_hash
    from ne.contracts.skills import SkillPack, SkillSource, WorkflowSnapshot, WorkflowStep
    from ne.eval.store import EvalRunStore
    from ne.flywheel.lanes import FixLane, ProposalPriority
    from ne.flywheel.proposals import FixProposal, ProposalKind
    from ne.skills.evals import SkillTriggerEvalSuite, build_eval_ref

    pack = SkillPack(
        name="deploy-runbook",
        version=1,
        description="Repeat the standard deploy-and-verify workflow",
        triggers=("deploy the service", "roll out a release"),
        body=(
            "## When to Use\nWhen a release must be deployed and verified.\n\n"
            "## Procedure\n1. Apply the manifest.\n2. Watch the rollout.\n\n"
            "## Pitfalls\nDo not skip the rollout check.\n\n"
            "## Verification\nConfirm the rollout is healthy.\n"
        ),
        source=SkillSource.FORGED,
    )
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
    snapshot = WorkflowSnapshot(
        snapshot_id="snap-2",
        substrate_id="pydantic-ai",
        intent="subagent:deployer",
        steps=(
            WorkflowStep(action="k8s.apply", outcome="ok"),
            WorkflowStep(action="k8s.rollout-status", outcome="ok"),
        ),
        succeeded=True,
        principal_id="user:ada@acme.com",
        session_id="sess-2",
        started_at=now,
        finished_at=now,
    )
    # An EMPTY library ⇒ the pack routes to itself cleanly (a passing eval).
    record = asyncio.run(
        SkillTriggerEvalSuite(pack, (), snapshot, store=EvalRunStore(engine)).run_recorded(
            config_hash=get_config_hash()
        )
    )
    proposal_id = "dup-candidate"
    fp = FixProposal(
        proposal_id=proposal_id,
        kind=ProposalKind.SKILL_CANDIDATE,
        lane=FixLane.SKILLS,
        cluster_id="sig2:r0",
        evidence_event_ids=("snap-2",),
        evidence_signal_ids=(),
        summary="[skill-candidate] deploy-runbook (dup)",
        confidence=0.5,
        priority=ProposalPriority.LOW,
        config_hash=get_config_hash(),
        created_at=now,
        payload={
            "pack": pack.model_dump(mode="json"),
            "signature": "sig2",
            "worthiness": {"floor": "v1", "succeeded": True, "distinct_actions": 2},
            "suggested_acl_labels": [],
            "near_dup": None,
            "eval_run": build_eval_ref(record),
        },
    )
    with engine.begin() as conn:
        SqlAlchemyProposalStore(engine).append(conn, fp)
    return proposal_id


def _patch_payload(engine: sa.Engine, pid: str, mutate: Callable[[dict], None]) -> None:
    from ne.flywheel.proposals import ne_fix_proposals

    with engine.begin() as conn:
        row = conn.execute(
            sa.select(ne_fix_proposals.c.id, ne_fix_proposals.c.payload).where(
                ne_fix_proposals.c.proposal_id == pid
            )
        ).one()
        payload = dict(row.payload)
        mutate(payload)
        conn.execute(
            ne_fix_proposals.update().where(ne_fix_proposals.c.id == row.id).values(payload=payload)
        )


def test_apply_refuses_missing_eval_ref(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    # The D-12 fail-closed gate: a candidate whose payload lacks a passing eval ref
    # is refused at apply (SkillEvalGateError → SkillApplyGateError → 409).
    from ne.skills.errors import SkillEvalGateError

    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)
    _patch_payload(forge_engine, pid, lambda p: p.__setitem__("eval_run", None))
    _approve(forge_engine, pid, prov)
    with pytest.raises(SkillEvalGateError):
        _apply(forge_engine, pid, prov)


def test_apply_refuses_forged_eval_ref(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    # A forged ref (right shape, right routing_hash, but a run_id with NO store
    # row) is refused — the stored row is the tamper-resistant anchor.
    from ne.skills.errors import SkillEvalGateError

    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)

    def _forge(payload: dict) -> None:
        ref = dict(payload["eval_run"])
        ref["run_id"] = "no-such-run"
        payload["eval_run"] = ref

    _patch_payload(forge_engine, pid, _forge)
    _approve(forge_engine, pid, prov)
    with pytest.raises(SkillEvalGateError):
        _apply(forge_engine, pid, prov)


def test_apply_refuses_routing_hash_mismatch(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    # A ref whose routing_hash does not match the recomputed pack (a stale/edited
    # eval) is refused before the store is even consulted.
    from ne.skills.errors import SkillEvalGateError

    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)

    def _mismatch(payload: dict) -> None:
        ref = dict(payload["eval_run"])
        ref["routing_hash"] = "0" * 64
        payload["eval_run"] = ref

    _patch_payload(forge_engine, pid, _mismatch)
    _approve(forge_engine, pid, prov)
    with pytest.raises(SkillEvalGateError):
        _apply(forge_engine, pid, prov)


def test_apply_accepts_passing_eval_after_relabel(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    # THE D-12 HEADLINE: acl_labels are stamped at apply, mutating content_hash but
    # NOT routing_hash — so the passing eval (bound to routing_hash) stays valid and
    # the labelled apply succeeds.
    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)
    _approve(forge_engine, pid, prov)
    applied = _apply(forge_engine, pid, prov, acl_labels=("grp:platform",))
    assert applied.name == "deploy-runbook"
    assert applied.acl_labels == ("grp:platform",)


def test_reject_carries_rejected_proposal_id(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    from ne.flywheel.review import RejectionCode
    from ne.flywheel.store import ne_correction_signals

    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)
    ProposalReviewService(forge_engine).reject(pid, prov, code=RejectionCode.UNSAFE)
    with forge_engine.connect() as conn:
        row = conn.execute(sa.select(ne_correction_signals)).first()
    assert row is not None
    assert row.subject_ref == f"proposal:{pid}"
    # Applying a rejected proposal is an illegal transition.
    ProposalReviewService(forge_engine)
    with pytest.raises((InvalidTransitionError, SkillApplyGateError)):
        _apply(forge_engine, pid, prov)


def test_edit_introducing_trigger_overlap_is_refused(
    forge_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    """NE-SKILL-4: a steward edit that makes the candidate's trigger collide with
    ANOTHER active pack is refused at the apply gate (SkillApplyIntegrityError →
    422 path) — the creation-time overlap lint closes the steward-edit hole."""
    from ne.skills.apply import SkillEdits

    from .conftest import make_pack

    # An existing ACTIVE pack claiming a distinctive trigger phrase.
    SqlAlchemySkillRegistry(forge_engine).register_pack(
        make_pack("incident-triage", triggers=("page the oncall",)), origin="bundle"
    )
    prov = make_provenance()
    pid = _forge_a_candidate(forge_engine)
    _approve(forge_engine, pid, prov)

    with pytest.raises(SkillApplyIntegrityError):
        _apply(
            forge_engine,
            pid,
            prov,
            acl_labels=("grp:platform",),
            edits=SkillEdits(triggers=("page the oncall",)),
        )
