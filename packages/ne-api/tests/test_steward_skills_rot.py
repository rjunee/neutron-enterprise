# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-4: retire/restore routes + digest skills section + batch-reject-stale."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from ne.audit import list_records
from ne.contracts.skills import SkillPack, SkillSource
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import FixProposal, ProposalKind
from ne.flywheel.store import ne_correction_signals
from ne.skills.config import SkillsSettings
from ne.skills.gardener import SkillGardener
from ne.skills.registry import SqlAlchemySkillRegistry

from .conftest import AppBuilder, get_state, run

_ROLES = ("steward",)
_GOOD_BODY = (
    "## When to Use\nWhen the trigger fires.\n## Procedure\n1. do it.\n"
    "## Pitfalls\nNone.\n## Verification\nConfirm.\n"
)


def _pack(name: str, *, triggers: tuple[str, ...] = ("use it",)) -> SkillPack:
    return SkillPack(
        name=name,
        version=1,
        description=f"skill {name}",
        triggers=triggers,
        body=_GOOD_BODY,
        source=SkillSource.BUNDLED,
    )


def _register(app: Any, pack: SkillPack) -> None:
    state = get_state(app)
    assert state.skills_engine is not None
    SqlAlchemySkillRegistry(state.skills_engine).register_pack(pack, origin="bundle")


def _status(app: Any, name: str) -> str:
    state = get_state(app)
    assert state.skills_engine is not None
    return SqlAlchemySkillRegistry(state.skills_engine).current_status(name)


def _seed_skill_proposal(app: Any) -> str:
    state = get_state(app)
    assert state.proposal_store is not None
    pid = uuid.uuid4().hex
    fp = FixProposal(
        proposal_id=pid,
        kind=ProposalKind.SKILL_CANDIDATE,
        lane=FixLane.SKILLS,
        cluster_id=f"sig-{pid}:r0",
        evidence_event_ids=("ev-1",),
        evidence_signal_ids=(),
        summary=f"[skill-candidate] {pid}",
        confidence=0.5,
        priority=ProposalPriority.LOW,
        config_hash=state.config_hash,
        created_at=datetime.now(UTC),
        payload={
            "pack": {"name": f"cand-{pid[:6]}"},
            "signature": "fixture-signature",
            "worthiness": {},
            "suggested_acl_labels": [],
            "near_dup": None,
            "eval_run": {},
            "snapshot": {},
        },
    )
    with state.proposal_store.engine.begin() as conn:
        state.proposal_store.append(conn, fp)
    return pid


# -- (a) retire / restore guard ladder ----------------------------------------


def test_retire_unauthorized_is_403_and_audited(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/steward/skills/whatever/retire",
                headers=h.bearer("user:nobody", groups=[]),
                json={"bogus": True},  # malformed body still 403 (403-before-422)
            )
            assert r.status_code == 403

    run(_t())
    engine = sa.create_engine(h.audit_url)
    try:
        rows = list_records(engine, action="api.steward.unauthorized")
    finally:
        engine.dispose()
    assert any(r.actor == "user:nobody" for r in rows)


def test_retire_503_when_skills_unconfigured(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES, kpi_configured=False)
    assert get_state(h.app).skills_engine is None

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/steward/skills/x/retire",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={"reason": "rot"},
            )
            assert r.status_code == 503

    run(_t())


def test_retire_unknown_is_404(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/steward/skills/ghost/retire",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={"reason": "rot"},
            )
            assert r.status_code == 404

    run(_t())


def test_retire_wrong_status_is_409(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    _register(h.app, _pack("beta"))

    async def _t() -> None:
        async with h.client() as client:
            # restoring a never-retired skill is a wrong-status 409
            r = await client.post(
                "/api/v1/steward/skills/beta/restore",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={"reason": "why"},
            )
            assert r.status_code == 409

    run(_t())


def test_retire_then_restore_success_flips_status_and_audits(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    _register(h.app, _pack("alpha"))
    assert _status(h.app, "alpha") == "active"

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/steward/skills/alpha/retire",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={"reason": "unused 90d"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["decision"] == "skill_retire"

            r2 = await client.post(
                "/api/v1/steward/skills/alpha/restore",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={"reason": "wanted back"},
            )
            assert r2.status_code == 200, r2.text
            assert r2.json()["decision"] == "skill_restore"

    run(_t())
    assert _status(h.app, "alpha") == "active"

    engine = sa.create_engine(h.audit_url)
    try:
        rows = list_records(engine, action="api.steward.submitted")
    finally:
        engine.dispose()
    decisions = {json.loads(r.payload["detail"]).get("decision") for r in rows}
    assert {"skill_retire", "skill_restore"} <= decisions


# -- (b) digest skills section end-to-end -------------------------------------


def test_digest_skills_null_before_any_gardener_run(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/steward/digest",
                headers=h.bearer("user:steward", groups=["steward"]),
            )
            assert r.status_code == 200, r.text
            assert r.json()["skills"] is None

    run(_t())


def test_digest_carries_skills_section_from_gardener_run(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    # Seed registry state that produces at least one retirement candidate, then run
    # the REAL gardener over the skills engine.
    _register(h.app, _pack("lonely"))
    state = get_state(h.app)
    assert state.skills_engine is not None
    from datetime import timedelta

    gardener = SkillGardener(
        state.skills_engine,
        settings=SkillsSettings(),
        audit_engine=None,
        clock=lambda: datetime.now(UTC) + timedelta(days=200),
    )
    gardener.run_once()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/steward/digest",
                headers=h.bearer("user:steward", groups=["steward"]),
            )
            assert r.status_code == 200, r.text
            skills = r.json()["skills"]
            assert skills is not None
            assert skills["ran_at"]
            names = {c["name"] for c in skills["retirement_candidates"]}
            assert "lonely" in names

    run(_t())


# -- (c) batch-reject-stale drives the REAL review service --------------------


def test_batch_reject_stale_drives_real_review_service(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    pid1 = _seed_skill_proposal(h.app)
    pid2 = _seed_skill_proposal(h.app)

    async def _t() -> None:
        async with h.client() as client:
            for pid in (pid1, pid2):
                r = await client.post(
                    f"/api/v1/steward/proposals/{pid}/reject",
                    headers=h.bearer("user:steward", groups=["steward"]),
                    json={"code": "stale", "note": "gardener flagged stale"},
                )
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["to_status"] == "rejected"
                assert body["rejection_code"] == "stale"

    run(_t())

    # Per-item rejection FEEDBACK signals landed in the correction-signal store.
    state = get_state(h.app)
    assert state.review_service is not None
    engine = state.review_service.engine
    with engine.connect() as conn:
        subjects = {
            str(r.subject_ref) for r in conn.execute(sa.select(ne_correction_signals)).all()
        }
    assert f"proposal:{pid1}" in subjects
    assert f"proposal:{pid2}" in subjects
