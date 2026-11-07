# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the ne-skills suite."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.audit import append_record, ensure_transactional_sqlite
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.contracts.skills import SkillPack, SkillSource
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.flywheel.review import ensure_review_schema
from ne.flywheel.signals import SignalProvenance, SignalSurface
from ne.schema import AuditOutcome
from ne.skills.config import SkillsSettings
from ne.skills.forge import ensure_forge_schema
from ne.skills.registry import SqlAlchemySkillRegistry, ensure_skills_schema
from ne.skills.visibility import GroupExpander


class _DemoSettings(NeSettings):
    demo: int = 7


GOOD_BODY = (
    "## When to Use\nWhen the trigger fires.\n\n"
    "## Procedure\n1. Do the thing.\n\n"
    "## Pitfalls\nNone known.\n\n"
    "## Verification\nConfirm the outcome.\n"
)


class StaticExpander:
    """A GroupExpander backed by a static principal_id -> labels dict."""

    def __init__(self, table: dict[str, frozenset[str]] | None = None) -> None:
        self._table = table or {}

    def expand(self, principal: Principal | str) -> frozenset[str]:
        pid = principal if isinstance(principal, str) else principal.id
        return self._table.get(pid, frozenset({pid}))


def make_pack(
    name: str,
    *,
    version: int = 1,
    description: str | None = None,
    triggers: tuple[str, ...] | None = None,
    body: str = GOOD_BODY,
    source: SkillSource = SkillSource.BUNDLED,
    requires_tools: tuple[str, ...] = (),
    platforms: tuple[str, ...] = (),
    acl_labels: tuple[str, ...] = (),
) -> SkillPack:
    return SkillPack(
        name=name,
        version=version,
        description=description or f"skill {name}",
        triggers=triggers or (f"use {name}",),
        body=body,
        source=source,
        requires_tools=requires_tools,
        platforms=platforms,
        acl_labels=acl_labels,
    )


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[sa.Engine]:
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'skills.db'}")
    ensure_skills_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def registry(engine: sa.Engine) -> SqlAlchemySkillRegistry:
    return SqlAlchemySkillRegistry(engine)


@pytest.fixture
def expander() -> GroupExpander:
    return StaticExpander()


@pytest.fixture
def settings() -> SkillsSettings:
    return SkillsSettings()


# -- NE-SKILL-1b forge / apply fixtures ---------------------------------------


@pytest.fixture
def frozen_config() -> Iterator[str]:
    """Freeze a deterministic config_hash for the duration of a test."""
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


@pytest.fixture
def forge_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    """A transactional file-sqlite engine with the review + skills + forge closure.

    File-based (not in-memory) so the review-service engine and the skills engine
    share ONE database URL — the apply seam's same-database guard needs that. The
    BEGIN-recipe makes the fail-closed audit-rollback tests faithful to Postgres.
    """
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'forge.db'}")
    ensure_transactional_sqlite(eng)
    ensure_review_schema(eng)
    ensure_skills_schema(eng)
    ensure_forge_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _make_provenance(
    *,
    principal_id: str = "user:ada@acme.com",
    session_id: str = "sess-1",
) -> SignalProvenance:
    return SignalProvenance(
        principal=Principal(id=principal_id),
        surface=SignalSurface.CONSOLE,
        session_id=session_id,
    )


@pytest.fixture
def make_provenance() -> Callable[..., SignalProvenance]:
    return _make_provenance


def seed_workflow(
    engine: sa.Engine,
    *,
    trace_id: str,
    terminal_action: str = "subagent.complete",
    agent: str = "deployer",
    actions: tuple[str, ...] = ("k8s.apply", "k8s.rollout-status"),
    succeeded: bool = True,
    principal_id: str = "user:ada@acme.com",
    session_id: str | None = None,
    department: str | None = None,
    hostile_detail: dict[str, object] | None = None,
) -> str:
    """Seed a completed workflow into ne_audit_log: N tool.execute steps + terminal.

    Returns the terminal event's ``event_id`` (== the snapshot_id). The steps
    share ``trace_id`` and precede the terminal by id/seq. ``hostile_detail`` (if
    given) is merged into a tool.execute step's detail to prove C12 redaction —
    argument-like values never surface on a WorkflowStep.
    """
    import json as _json

    detail: dict[str, object] = {"agent": agent}
    if terminal_action == "session.run":
        detail["session_id"] = session_id or trace_id
        detail["succeeded"] = succeeded
        if department is not None:
            detail["department"] = department
    with engine.begin() as conn:
        for i, action in enumerate(actions):
            step_detail: dict[str, object] = {"status": "ok", "tier": 0}
            if hostile_detail is not None and i == 0:
                step_detail.update(hostile_detail)
            append_record(
                conn,
                new_audit_event(
                    action="tool.execute",
                    resource=f"tool:{action}",
                    outcome=AuditOutcome.success,
                    actor=principal_id,
                    detail=_json.dumps(step_detail),
                    trace_id=trace_id,
                ),
            )
        terminal = new_audit_event(
            action=terminal_action,
            resource=(
                f"subagent:{agent}"
                if terminal_action == "subagent.complete"
                else f"session:{session_id or trace_id}"
            ),
            outcome=AuditOutcome.success if succeeded else AuditOutcome.error,
            actor=principal_id,
            detail=_json.dumps(detail),
            trace_id=trace_id,
        )
        append_record(conn, terminal)
    return terminal.event_id
