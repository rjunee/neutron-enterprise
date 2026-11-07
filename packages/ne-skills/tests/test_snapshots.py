# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Audit-derived snapshot fold (NE-SKILL-1b): names-only steps, C12, cursor paging."""

from __future__ import annotations

import asyncio

import sqlalchemy as sa

from ne.skills.substrate import PydanticAiSkillSubstrate

from .conftest import seed_workflow


def _sub(engine: sa.Engine) -> PydanticAiSkillSubstrate:
    return PydanticAiSkillSubstrate(engine, audit_engine=engine)


def test_subagent_workflow_folds_ordered_names_only_steps(
    forge_engine: sa.Engine, frozen_config: str
) -> None:
    seed_workflow(
        forge_engine,
        trace_id="t1",
        agent="deployer",
        actions=("k8s.apply", "k8s.rollout-status"),
        succeeded=True,
    )
    page = asyncio.run(_sub(forge_engine).snapshots())
    assert len(page.items) == 1
    snap = page.items[0]
    assert [s.action for s in snap.steps] == ["k8s.apply", "k8s.rollout-status"]
    assert snap.succeeded is True
    assert snap.intent == "subagent:deployer"
    assert snap.principal_id == "user:ada@acme.com"


def test_session_run_folds_department_and_session_id(
    forge_engine: sa.Engine, frozen_config: str
) -> None:
    seed_workflow(
        forge_engine,
        trace_id="t2",
        terminal_action="session.run",
        agent="hr-bot",
        actions=("hr.lookup", "hr.notify"),
        session_id="sess-xyz",
        department="hr",
    )
    page = asyncio.run(_sub(forge_engine).snapshots())
    snap = page.items[0]
    assert snap.intent == "session:hr-bot"
    assert snap.session_id == "sess-xyz"
    assert snap.department == "hr"


def test_c12_hostile_arg_values_absent_from_every_step_field(
    forge_engine: sa.Engine, frozen_config: str
) -> None:
    # A tool.execute event whose detail embeds hostile "arg values" must never
    # surface on a WorkflowStep — the fold reads only resource/outcome/event_id.
    hostile: dict[str, object] = {"password": "hunter2", "url": "https://evil.example/steal"}
    seed_workflow(
        forge_engine,
        trace_id="t3",
        actions=("db.query", "db.export"),
        hostile_detail=hostile,
    )
    page = asyncio.run(_sub(forge_engine).snapshots())
    snap = page.items[0]
    for step in snap.steps:
        blob = f"{step.action}|{step.outcome}|{step.detail_ref}"
        assert "hunter2" not in blob
        assert "evil.example" not in blob


def test_cron_job_run_produces_no_snapshot(forge_engine: sa.Engine, frozen_config: str) -> None:
    # job.run is NOT a terminal snapshot source (cron exclusion, documented).
    seed_workflow(
        forge_engine,
        trace_id="t4",
        terminal_action="job.run",  # not in _TERMINAL_ACTIONS
        actions=("x.a", "x.b"),
    )
    page = asyncio.run(_sub(forge_engine).snapshots())
    assert page.items == ()


def test_cursor_pages_twice_and_is_stable_on_empty(
    forge_engine: sa.Engine, frozen_config: str
) -> None:
    seed_workflow(forge_engine, trace_id="p1", actions=("a", "b"))
    seed_workflow(forge_engine, trace_id="p2", actions=("c", "d"))
    sub = _sub(forge_engine)

    first = asyncio.run(sub.snapshots(limit=1))
    assert len(first.items) == 1
    assert first.exhausted is False

    second = asyncio.run(sub.snapshots(cursor=first.cursor, limit=1))
    assert len(second.items) == 1
    assert second.items[0].snapshot_id != first.items[0].snapshot_id

    third = asyncio.run(sub.snapshots(cursor=second.cursor, limit=1))
    assert third.items == ()
    assert third.exhausted is True
    # Stable cursor on an exhausted page.
    assert third.cursor == second.cursor


def test_no_audit_engine_yields_empty_exhausted_page(engine: sa.Engine) -> None:
    sub = PydanticAiSkillSubstrate(engine)  # no audit_engine (1a behavior)
    page = asyncio.run(sub.snapshots())
    assert page.items == ()
    assert page.exhausted is True
