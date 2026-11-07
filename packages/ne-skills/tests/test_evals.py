# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-2 SkillTriggerEvalSuite: the deterministic routing+utility birth eval."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from ne.contracts.evalobs import EvalSuite
from ne.contracts.skills import SkillPack, SkillSource, WorkflowSnapshot, WorkflowStep
from ne.eval.store import EvalRunRecord, EvalRunStore
from ne.skills.errors import SkillEvalGateError
from ne.skills.evals import (
    SKILL_TRIGGER_SUITE_NAME,
    SkillTriggerEvalSuite,
    build_eval_ref,
    verify_skill_eval_ref,
)

_BODY = (
    "## When to Use\nWhen a release must be deployed and verified.\n\n"
    "## Procedure\n1. Apply the manifest.\n2. Watch the rollout.\n\n"
    "## Pitfalls\nDo not skip the rollout check.\n\n"
    "## Verification\nConfirm the rollout is healthy.\n"
)


def _pack(
    name: str = "deploy-runbook",
    *,
    triggers: tuple[str, ...] = ("deploy the service", "roll out a release"),
    body: str = _BODY,
    requires_tools: tuple[str, ...] = (),
) -> SkillPack:
    return SkillPack(
        name=name,
        version=1,
        description="deploy and verify",
        triggers=triggers,
        body=body,
        source=SkillSource.FORGED,
        requires_tools=requires_tools,
    )


def _snapshot(actions: tuple[str, ...] = ("k8s.apply", "k8s.rollout-status")) -> WorkflowSnapshot:
    now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    return WorkflowSnapshot(
        snapshot_id="s",
        substrate_id="pydantic-ai",
        intent="subagent:deployer",
        steps=tuple(WorkflowStep(action=a, outcome="ok") for a in actions),
        succeeded=True,
        principal_id="user:a",
        session_id="sess",
        started_at=now,
        finished_at=now,
    )


def _run(suite: SkillTriggerEvalSuite) -> EvalRunRecord:
    return asyncio.run(suite.run_recorded(config_hash="cfg-test"))


def test_suite_satisfies_evalsuite_protocol(forge_engine: sa.Engine) -> None:
    # A typed assignment proves the frozen Protocol conformance under pyright (C5).
    store = EvalRunStore(forge_engine)
    suite: EvalSuite = SkillTriggerEvalSuite(_pack(), (), _snapshot(), store=store)
    assert suite.name == SKILL_TRIGGER_SUITE_NAME


def test_passing_pack_records_run_with_config_hash_and_routing_hash(
    forge_engine: sa.Engine,
) -> None:
    store = EvalRunStore(forge_engine)
    pack = _pack()
    record = _run(SkillTriggerEvalSuite(pack, (), _snapshot(), store=store))
    assert record.passed is True
    assert record.config_hash == "cfg-test"
    assert record.rubric_versions["routing_hash"] == pack.routing_hash
    assert store.get_run(record.run_id) is not None
    # golden_set_hash is deterministic across constructions of the same inputs.
    record2 = _run(SkillTriggerEvalSuite(pack, (), _snapshot(), store=store))
    assert record2.golden_set_hash == record.golden_set_hash


def test_generic_trigger_pack_fails_near_miss(forge_engine: sa.Engine) -> None:
    # A pack with trigger 'help' is hijacked by the 'expense report help' decoy
    # (a full trigger-substring match ⇒ score >= 1.0) ⇒ near_miss_precision < 1.0.
    store = EvalRunStore(forge_engine)
    pack = _pack(name="helper", triggers=("help",))
    record = _run(SkillTriggerEvalSuite(pack, (), _snapshot(), store=store))
    assert record.metrics["near_miss_precision"] < 1.0
    assert record.passed is False


def test_trigger_not_retrieving_fails_recall(forge_engine: sa.Engine) -> None:
    # top_k=1 with a name-tiebreak-stronger competitor for the shared trigger
    # 'deploy' ⇒ the candidate is not retrieved ⇒ trigger_recall < 1.0.
    store = EvalRunStore(forge_engine)
    candidate = _pack(name="weak-skill", triggers=("deploy",))
    competitor = _pack(name="deploy-master", triggers=("deploy",))
    record = _run(
        SkillTriggerEvalSuite(candidate, (competitor,), _snapshot(), store=store, top_k=1)
    )
    assert record.metrics["trigger_recall"] < 1.0
    assert record.passed is False


def test_utility_band_fails_when_body_omits_snapshot_actions(forge_engine: sa.Engine) -> None:
    # A body that mentions no token of the snapshot's actions ⇒ step coverage < 1.
    store = EvalRunStore(forge_engine)
    body = (
        "## When to Use\nWhenever.\n\n## Procedure\n1. Ponder deeply.\n\n"
        "## Pitfalls\nNone.\n\n## Verification\nDone.\n"
    )
    pack = _pack(name="vague", triggers=("deploy the service",), body=body)
    record = _run(SkillTriggerEvalSuite(pack, (), _snapshot(), store=store))
    assert record.metrics["utility_step_coverage"] < 1.0
    assert record.passed is False


def test_anti_vacuous_empty_near_miss_or_triggers_fails(forge_engine: sa.Engine) -> None:
    # No decoys + empty library ⇒ empty near-miss set ⇒ can NEVER pass (NE-CKC6).
    store = EvalRunStore(forge_engine)
    record = _run(SkillTriggerEvalSuite(_pack(), (), _snapshot(), store=store, decoys=()))
    assert record.passed is False
    assert all(status == "fail" for status in record.statuses.values())


def test_deterministic_metrics_across_double_run(forge_engine: sa.Engine) -> None:
    store = EvalRunStore(forge_engine)
    pack = _pack()
    r1 = _run(SkillTriggerEvalSuite(pack, (), _snapshot(), store=store))
    r2 = _run(SkillTriggerEvalSuite(pack, (), _snapshot(), store=store))
    assert r1.metrics == r2.metrics
    assert r1.statuses == r2.statuses
    assert r1.passed == r2.passed


def test_verify_skill_eval_ref_fail_closed_matrix(forge_engine: sa.Engine) -> None:
    store = EvalRunStore(forge_engine)
    pack = _pack()
    record = _run(SkillTriggerEvalSuite(pack, (), _snapshot(), store=store))
    good = build_eval_ref(record)
    rh = pack.routing_hash

    # Baseline: a genuine passing, store-anchored, routing-bound ref is accepted.
    verify_skill_eval_ref(good, expected_routing_hash=rh, engine=forge_engine)

    # (1) missing ref entirely.
    with pytest.raises(SkillEvalGateError):
        verify_skill_eval_ref(None, expected_routing_hash=rh, engine=forge_engine)

    # (2) passed=False.
    with pytest.raises(SkillEvalGateError):
        verify_skill_eval_ref(
            {**good, "passed": False}, expected_routing_hash=rh, engine=forge_engine
        )

    # (3) wrong suite.
    with pytest.raises(SkillEvalGateError):
        verify_skill_eval_ref(
            {**good, "suite": "other"}, expected_routing_hash=rh, engine=forge_engine
        )

    # (4) routing_hash mismatch (expected differs from ref).
    with pytest.raises(SkillEvalGateError):
        verify_skill_eval_ref(good, expected_routing_hash="0" * 64, engine=forge_engine)

    # (5) run_id absent from the store (a forged ref with matching routing_hash).
    with pytest.raises(SkillEvalGateError):
        verify_skill_eval_ref(
            {**good, "run_id": "no-such-run"}, expected_routing_hash=rh, engine=forge_engine
        )
