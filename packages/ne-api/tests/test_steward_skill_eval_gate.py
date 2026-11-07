# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-2: the fail-closed eval gate at the kind-guarded steward routes."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from ne.flywheel.proposals import ne_fix_proposals

from .conftest import AppBuilder, get_state, run, seed_proposal
from .test_steward_skill_apply import _seed_candidate

_ROLES = ("steward",)


async def _approve(client: Any, h: Any, pid: str) -> Any:
    return await client.post(
        f"/api/v1/steward/proposals/{pid}/approve",
        headers=h.bearer("user:steward", groups=["steward"]),
        json={"note": ""},
    )


def test_approve_refuses_skill_candidate_without_passing_eval_409(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            pid = _seed_candidate(h.app, eval_run=None)  # no passing eval ref
            r = await _approve(client, h, pid)
            assert r.status_code == 409, r.text

    run(_t())


def test_approve_generic_kind_unaffected(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            pid = seed_proposal(h.app, lane="content", summary="fix the retriever")
            r = await _approve(client, h, pid)
            # A generic (non-skill) kind takes zero new code paths — it approves.
            assert r.status_code == 200, r.text

    run(_t())


def test_apply_skill_refuses_missing_eval_409(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            # Seed WITH a passing eval, approve through the router, then strip the
            # eval ref from the stored payload so the apply seam's re-verify fails.
            pid = _seed_candidate(h.app)
            r = await _approve(client, h, pid)
            assert r.status_code == 200, r.text
            state = get_state(h.app)
            assert state.proposal_store is not None
            with state.proposal_store.engine.begin() as conn:
                row = conn.execute(
                    sa.select(ne_fix_proposals.c.id, ne_fix_proposals.c.payload).where(
                        ne_fix_proposals.c.proposal_id == pid
                    )
                ).one()
                payload = dict(row.payload)
                payload["eval_run"] = None
                conn.execute(
                    ne_fix_proposals.update()
                    .where(ne_fix_proposals.c.id == row.id)
                    .values(payload=payload)
                )
            r2 = await client.post(
                f"/api/v1/steward/proposals/{pid}/apply-skill",
                headers=h.bearer("user:steward", groups=["steward"]),
                json={"global_visibility": True},
            )
            assert r2.status_code == 409, r2.text

    run(_t())


def test_approve_skill_kind_503_when_skills_unconfigured(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES, kpi_configured=False)
    state = get_state(h.app)
    assert state.skills_engine is None

    async def _t() -> None:
        async with h.client() as client:
            r = await _approve(client, h, "any-id")
            # Skills is coupled to the flywheel store; unconfigured ⇒ 503 (guard
            # ladder: role 403 > store/skills 503), never a silent approve.
            assert r.status_code == 503, r.text

    run(_t())
