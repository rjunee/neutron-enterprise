# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-1b: the POST /v1/steward/proposals/{id}/apply-skill route."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from ne.audit import list_records
from ne.contracts.skills import SkillPack, SkillSource, WorkflowSnapshot, WorkflowStep
from ne.eval.store import EvalRunStore, ensure_eval_schema
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import FixProposal, ProposalKind
from ne.skills.evals import SkillTriggerEvalSuite, build_eval_ref

from .conftest import AppBuilder, get_state, run

_ROLES = ("steward",)


def _snapshot() -> WorkflowSnapshot:
    now = datetime.now(UTC)
    return WorkflowSnapshot(
        snapshot_id="snap",
        substrate_id="pydantic-ai",
        intent="subagent:deployer",
        steps=(
            WorkflowStep(action="apply", outcome="ok"),
            WorkflowStep(action="verify", outcome="ok"),
        ),
        succeeded=True,
        principal_id="user:ada",
        session_id="sess-1",
        started_at=now,
        finished_at=now,
    )


def _pack() -> SkillPack:
    body = (
        "## When to Use\nWhen deploying.\n## Procedure\n1. apply\n2. verify\n"
        "## Pitfalls\nNone.\n## Verification\nHealthy.\n"
    )
    return SkillPack(
        name="deploy-runbook",
        version=1,
        description="deploy and verify",
        triggers=("deploy",),
        body=body,
        source=SkillSource.FORGED,
        acl_labels=(),
        provenance={"session_id": "sess-1", "forged_at": "2026-07-21T00:00:00+00:00"},
    )


def _record_passing_eval(engine: Any, config_hash: str, pack: SkillPack) -> dict[str, Any]:
    """Run the REAL SkillTriggerEvalSuite against the skills engine + return its ref.

    Driven synchronously (the suite never awaits) so it can run inside the test's
    running event loop where ``asyncio.run`` would deadlock.
    """
    ensure_eval_schema(engine)
    now = datetime.now(UTC)
    snapshot = WorkflowSnapshot(
        snapshot_id="snap",
        substrate_id="pydantic-ai",
        intent="subagent:deployer",
        steps=(
            WorkflowStep(action="apply", outcome="ok"),
            WorkflowStep(action="verify", outcome="ok"),
        ),
        succeeded=True,
        principal_id="user:ada",
        session_id="sess-1",
        started_at=now,
        finished_at=now,
    )
    suite = SkillTriggerEvalSuite(pack, (), snapshot, store=EvalRunStore(engine))
    coro = suite.run_recorded(config_hash=config_hash)
    try:
        coro.send(None)
        raise AssertionError("eval coroutine suspended unexpectedly")
    except StopIteration as stop:
        record = stop.value
    return build_eval_ref(record)


_DEFAULT_EVAL = object()


def _seed_candidate(app: Any, *, eval_run: Any = _DEFAULT_EVAL) -> str:
    state = get_state(app)
    assert state.proposal_store is not None
    assert state.skills_engine is not None
    pack = _pack()
    if eval_run is _DEFAULT_EVAL:
        eval_run = _record_passing_eval(state.skills_engine, state.config_hash, pack)
    proposal_id = uuid.uuid4().hex
    fp = FixProposal(
        proposal_id=proposal_id,
        kind=ProposalKind.SKILL_CANDIDATE,
        lane=FixLane.SKILLS,
        cluster_id="sig:r0",
        evidence_event_ids=("ev-1",),
        evidence_signal_ids=(),
        summary="[skill-candidate] deploy-runbook",
        confidence=0.5,
        priority=ProposalPriority.LOW,
        config_hash=state.config_hash,
        created_at=datetime.now(UTC),
        payload={
            "pack": pack.model_dump(mode="json"),
            "signature": "sig",
            "worthiness": {"floor": "v1", "succeeded": True, "distinct_actions": 2},
            "suggested_acl_labels": ["grp:platform"],
            "near_dup": None,
            "eval_run": eval_run,
            # NE-SKILL-3: the names-only snapshot powers the apply-time fresh eval.
            "snapshot": _snapshot().model_dump(mode="json"),
        },
    )
    with state.proposal_store.engine.begin() as conn:
        state.proposal_store.append(conn, fp)
    return proposal_id


def test_apply_skill_unauthorized_is_403_even_with_malformed_body(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/steward/proposals/does-not-matter/apply-skill",
                headers=h.bearer("user:nobody", groups=[]),
                json={"bogus": True},  # malformed body still 403 (403-before-422)
            )
            assert r.status_code == 403

    run(_t())


def test_apply_skill_503_when_skills_unconfigured(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES, kpi_configured=False)
    state = get_state(h.app)
    assert state.skills_engine is None

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/steward/proposals/x/apply-skill",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={"global_visibility": True},
            )
            assert r.status_code == 503

    run(_t())


def test_apply_skill_success_registers_and_audits(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            pid = _seed_candidate(h.app)
            # queue view carries the SkillCandidateView (raw payload never on wire).
            q = await client.get(
                "/api/v1/steward/proposals",
                headers=h.bearer("user:steward", groups=["steward"]),
            )
            assert q.status_code == 200
            row = next(p for p in q.json()["proposals"] if p["proposal_id"] == pid)
            assert row["skill"]["name"] == "deploy-runbook"
            assert row["skill"]["suggested_acl_labels"] == ["grp:platform"]
            assert "SMUGGLE" not in q.text  # no raw summary/payload leak

            await _approve_async(client, h, pid)
            r = await client.post(
                f"/api/v1/steward/proposals/{pid}/apply-skill",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={"global_visibility": True, "note": "ship it"},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["name"] == "deploy-runbook"
            assert data["version"] == 1
            assert data["acl_labels"] == []
            assert data["content_hash"]

    run(_t())


def test_apply_skill_empty_labels_without_global_is_422(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            pid = _seed_candidate(h.app)
            await _approve_async(client, h, pid)
            r = await client.post(
                f"/api/v1/steward/proposals/{pid}/apply-skill",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={"acl_labels": [], "global_visibility": False},
            )
            assert r.status_code == 422

    run(_t())


def test_apply_skill_on_non_approved_is_409(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            pid = _seed_candidate(h.app)
            # NOT approved.
            r = await client.post(
                f"/api/v1/steward/proposals/{pid}/apply-skill",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={"global_visibility": True},
            )
            assert r.status_code == 409

    run(_t())


def test_apply_skill_with_edits_success(make_app: AppBuilder) -> None:
    # NE-SKILL-3: a steward name edit re-runs the routing eval server-side, commits,
    # and the response + audit reflect the edit.
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            pid = _seed_candidate(h.app)
            await _approve_async(client, h, pid)
            r = await client.post(
                f"/api/v1/steward/proposals/{pid}/apply-skill",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={
                    "global_visibility": True,
                    "edits": {"description": "deploy and verify, refined"},
                },
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["name"] == "deploy-runbook"
            assert data["version"] == 1

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        rows = list_records(engine, action="api.steward.submitted")
    finally:
        engine.dispose()
    import json

    details = [json.loads(r.payload["detail"]) for r in rows if r.actor == "user:steward"]
    skill_applies = [d for d in details if d.get("decision") == "skill_apply"]
    assert len(skill_applies) == 1
    assert skill_applies[0]["edited"] == "True"


def test_apply_skill_edit_breaking_routing_409(make_app: AppBuilder) -> None:
    # A routing-breaking trigger edit fails the fresh eval ⇒ 409, proposal stays approved.
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            pid = _seed_candidate(h.app)
            await _approve_async(client, h, pid)
            r = await client.post(
                f"/api/v1/steward/proposals/{pid}/apply-skill",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={"global_visibility": True, "edits": {"triggers": ["help"]}},
            )
            assert r.status_code == 409, r.text
            assert "fail-closed" in r.json()["detail"]
            # The proposal is untouched — still approvable/appliable.
            q = await client.get(
                "/api/v1/steward/proposals",
                headers=h.bearer("user:steward", groups=["steward"]),
            )
            row = next(p for p in q.json()["proposals"] if p["proposal_id"] == pid)
            assert row["status"] == "approved"

    run(_t())


def test_apply_skill_body_edit_structurally_422(make_app: AppBuilder) -> None:
    # A body edit is structurally impossible at the wire (extra='forbid') ⇒ 422;
    # the proposal stays approved (validation fails before the handler runs).
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            pid = _seed_candidate(h.app)
            await _approve_async(client, h, pid)
            r = await client.post(
                f"/api/v1/steward/proposals/{pid}/apply-skill",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={"global_visibility": True, "edits": {"body": "## Pwned\n"}},
            )
            assert r.status_code == 422
            q = await client.get(
                "/api/v1/steward/proposals",
                headers=h.bearer("user:steward", groups=["steward"]),
            )
            row = next(p for p in q.json()["proposals"] if p["proposal_id"] == pid)
            assert row["status"] == "approved"

    run(_t())


async def _approve_async(client: Any, h: Any, pid: str) -> None:
    r = await client.post(
        f"/api/v1/steward/proposals/{pid}/approve",
        headers=h.bearer("user:steward", groups=["steward"]),
        json={"note": ""},
    )
    assert r.status_code == 200, r.text
