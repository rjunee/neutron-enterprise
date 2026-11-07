# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Conformance kit (read subset) + direct substrate behavior tests."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
import sqlalchemy as sa

from ne.contracts.skills import SkillInvocation, SkillSubstrate
from ne.contracts.testing.kits.skill_substrate import SkillSubstrateContractKit
from ne.skills.errors import SkillAuthoringUnavailableError
from ne.skills.registry import SqlAlchemySkillRegistry, ensure_skills_schema
from ne.skills.substrate import PydanticAiSkillSubstrate

from .conftest import make_pack


def _seed_engine() -> sa.Engine:
    tmp = Path(tempfile.mkdtemp()) / "kit.db"
    engine = sa.create_engine(f"sqlite:///{tmp}")
    ensure_skills_schema(engine)
    registry = SqlAlchemySkillRegistry(engine)
    registry.register_pack(make_pack("alpha"), origin="bundle")
    for i in range(6):
        registry.append_invocation(
            SkillInvocation(
                skill_name="alpha",
                version=1,
                principal_id=f"u-{i}",
                session_id=f"s-{i}",
                fired_at=datetime.now(UTC),
            )
        )
    return engine


class TestPydanticAiSkillSubstrateConformance(SkillSubstrateContractKit):
    """Read-capability conformance: snapshots stay empty until 1b (dropped here)."""

    capabilities: ClassVar[frozenset[str]] = frozenset({"invocations", "materialize"})

    def create_substrate(self) -> SkillSubstrate:
        engine = _seed_engine()
        self._engine = engine
        return PydanticAiSkillSubstrate(engine)

    def read_surface(self, surface_key: str) -> set[str]:
        registry = SqlAlchemySkillRegistry(self._engine)
        current = registry.read_surface(surface_key)
        return set(current[1]) if current is not None else set()


def _good_author_md() -> str:
    return (
        "---\n"
        "name: kit-forged\n"
        "description: a forged kit skill\n"
        "version: 1\n"
        "triggers:\n"
        "  - do the kit thing\n"
        "source: forged\n"
        "---\n"
        "## When to Use\nWhen the trigger fires.\n\n"
        "## Procedure\n1. Do the thing.\n\n"
        "## Pitfalls\nNone known.\n\n"
        "## Verification\nConfirm the outcome.\n"
    )


def _seed_full_engine() -> sa.Engine:
    """Skills + audit closure with 6 pageable snapshots + 6 pageable invocations."""
    from ne.audit import append_record, ensure_audit_schema, ensure_transactional_sqlite
    from ne.events import new_audit_event
    from ne.schema import AuditOutcome

    tmp = Path(tempfile.mkdtemp()) / "kitfull.db"
    engine = sa.create_engine(f"sqlite:///{tmp}")
    ensure_transactional_sqlite(engine)
    ensure_skills_schema(engine)
    ensure_audit_schema(engine)
    registry = SqlAlchemySkillRegistry(engine)
    registry.register_pack(make_pack("alpha"), origin="bundle")
    for i in range(6):
        registry.append_invocation(
            SkillInvocation(
                skill_name="alpha",
                version=1,
                principal_id=f"u-{i}",
                session_id=f"s-{i}",
                fired_at=datetime.now(UTC),
            )
        )
    for i in range(6):
        with engine.begin() as conn:
            for action in (f"tool.a{i}", f"tool.b{i}"):
                append_record(
                    conn,
                    new_audit_event(
                        action="tool.execute",
                        resource=f"tool:{action}",
                        outcome=AuditOutcome.success,
                        actor="user:kit",
                        detail='{"status": "ok"}',
                        trace_id=f"kit-{i}",
                        config_hash="a" * 64,
                    ),
                )
            append_record(
                conn,
                new_audit_event(
                    action="subagent.complete",
                    resource="subagent:kit",
                    outcome=AuditOutcome.success,
                    actor="user:kit",
                    detail='{"agent": "kit"}',
                    trace_id=f"kit-{i}",
                    config_hash="a" * 64,
                ),
            )
    return engine


class TestPydanticAiSkillSubstrateFullConformance(SkillSubstrateContractKit):
    """FULL-capability conformance (1b): snapshots + author now implemented."""

    capabilities: ClassVar[frozenset[str]] = frozenset(
        {"snapshots", "invocations", "materialize", "author"}
    )

    def create_substrate(self) -> SkillSubstrate:
        engine = _seed_full_engine()
        self._engine = engine

        async def _dispatch(task: str) -> str:
            return _good_author_md()

        return PydanticAiSkillSubstrate(engine, audit_engine=engine, author_dispatch=_dispatch)

    def read_surface(self, surface_key: str) -> set[str]:
        registry = SqlAlchemySkillRegistry(self._engine)
        current = registry.read_surface(surface_key)
        return set(current[1]) if current is not None else set()


# -- direct tests --------------------------------------------------------------


def test_snapshots_always_empty_exhausted() -> None:
    substrate = PydanticAiSkillSubstrate(_seed_engine())
    page = asyncio.run(substrate.snapshots(limit=10))
    assert list(page.items) == []
    assert page.exhausted is True
    assert page.cursor == "0"


def test_author_raises() -> None:
    substrate = PydanticAiSkillSubstrate(_seed_engine())
    snap_page = asyncio.run(substrate.snapshots())
    from ne.contracts.skills import AuthoringStandard, WorkflowSnapshot

    snap = WorkflowSnapshot(
        snapshot_id="s1",
        substrate_id="pydantic-ai",
        intent="x",
        steps=(),
        succeeded=True,
        principal_id="u",
        session_id="s",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    _ = snap_page
    with pytest.raises(SkillAuthoringUnavailableError):
        asyncio.run(substrate.author(snap, standard=AuthoringStandard()))


def test_materialize_does_not_refilter() -> None:
    engine = _seed_engine()
    substrate = PydanticAiSkillSubstrate(engine)
    packs = [
        make_pack("global-skill", acl_labels=()),
        make_pack("finance-skill", acl_labels=("dept:finance",)),
        make_pack("eng-skill", acl_labels=("dept:eng", "role:admin")),
    ]
    asyncio.run(substrate.materialize(packs, surface_key="surf"))
    registry = SqlAlchemySkillRegistry(engine)
    surface = registry.read_surface("surf")
    assert surface is not None
    _text, names = surface
    assert set(names) == {"global-skill", "finance-skill", "eng-skill"}


def test_withdraw_removes_only_named() -> None:
    engine = _seed_engine()
    substrate = PydanticAiSkillSubstrate(engine)
    packs = [make_pack("alpha"), make_pack("bravo"), make_pack("charlie")]
    asyncio.run(substrate.materialize(packs, surface_key="surf"))
    asyncio.run(substrate.withdraw(["bravo"], surface_key="surf"))
    registry = SqlAlchemySkillRegistry(engine)
    surface = registry.read_surface("surf")
    assert surface is not None
    text, names = surface
    assert set(names) == {"alpha", "charlie"}
    assert "- bravo (" not in text
    assert "- alpha (" in text
