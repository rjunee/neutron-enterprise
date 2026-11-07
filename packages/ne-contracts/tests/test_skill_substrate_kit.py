# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SkillSubstrateContractKit self-test against an in-memory fake.

The fake ``MemorySkillSubstrate`` is defined IN THIS TEST FILE (deliberately NOT
in ``ne/contracts/testing/memory.py`` and with NO entry point) so nothing in this
unit implies a registered ``ne.plugins.skill_substrate`` plugin — the brief
forbids declaring the first entry point here (NE-SKILL-1a/5 own that).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from ne.contracts.skills import (
    AuthoringStandard,
    InvocationPage,
    SkillInvocation,
    SkillPack,
    SkillSource,
    SkillSubstrate,
    SnapshotPage,
    WorkflowSnapshot,
    WorkflowStep,
)
from ne.contracts.testing.kits.skill_substrate import SkillSubstrateContractKit

_START = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = slug or "skill"
    if len(slug) < 2:
        slug = f"{slug}-skill"
    return slug[:64]


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    return int(cursor.split(":", 1)[1])


class MemorySkillSubstrate:
    """A minimal, dependency-free in-memory SkillSubstrate for the kit self-test."""

    def __init__(self) -> None:
        self._snapshots: list[WorkflowSnapshot] = [
            WorkflowSnapshot(
                snapshot_id=f"snap-{i}",
                substrate_id="memory",
                intent=f"complete task number {i}",
                steps=(
                    WorkflowStep(action="write.internal.step", outcome="ok"),
                    WorkflowStep(action="read.knowledge.search", outcome="found"),
                ),
                succeeded=True,
                principal_id=f"p{i}",
                session_id=f"s{i}",
                started_at=_START + timedelta(minutes=i),
                finished_at=_START + timedelta(minutes=i, seconds=30),
            )
            for i in range(6)
        ]
        self._invocations: list[SkillInvocation] = [
            SkillInvocation(
                skill_name=f"skill-{i}",
                version=1,
                principal_id=f"p{i}",
                session_id=f"s{i}",
                fired_at=_START + timedelta(minutes=i),
                run_succeeded=True,
            )
            for i in range(6)
        ]
        self._surfaces: dict[str, dict[str, SkillPack]] = {}

    @property
    def substrate_id(self) -> str:
        return "memory"

    async def snapshots(self, *, cursor: str | None = None, limit: int = 50) -> SnapshotPage:
        offset = _cursor_offset(cursor)
        items = self._snapshots[offset : offset + limit]
        new_offset = offset + len(items)
        return SnapshotPage(
            items=items,
            cursor=f"mem:{new_offset}",
            exhausted=new_offset >= len(self._snapshots),
        )

    async def author(
        self,
        snapshot: WorkflowSnapshot,
        *,
        standard: AuthoringStandard,
        existing: SkillPack | None = None,
    ) -> SkillPack:
        name = existing.name if existing is not None else _slugify(snapshot.intent)
        version = existing.version + 1 if existing is not None else 1
        return SkillPack(
            name=name,
            version=version,
            description=snapshot.intent[: standard.description_max_chars],
            triggers=(snapshot.intent or "use skill",),
            body="## When to Use\n## Procedure\n## Pitfalls\n## Verification\n",
            source=SkillSource.FORGED,
            acl_labels=(),
        )

    async def materialize(self, packs: Sequence[SkillPack], *, surface_key: str) -> None:
        self._surfaces[surface_key] = {pack.name: pack for pack in packs}

    async def withdraw(self, names: Sequence[str], *, surface_key: str) -> None:
        surface = self._surfaces.get(surface_key, {})
        for name in names:
            surface.pop(name, None)

    async def collect_invocations(
        self, *, cursor: str | None = None, limit: int = 500
    ) -> InvocationPage:
        offset = _cursor_offset(cursor)
        items = self._invocations[offset : offset + limit]
        new_offset = offset + len(items)
        return InvocationPage(
            items=items,
            cursor=f"mem:{new_offset}",
            exhausted=new_offset >= len(self._invocations),
        )

    def surface_names(self, surface_key: str) -> set[str]:
        return set(self._surfaces.get(surface_key, {}))


# Static structural assertion: MemorySkillSubstrate satisfies the Protocol
# (pyright fails here if any method signature drifts from SkillSubstrate).
_substrate_check: SkillSubstrate = MemorySkillSubstrate()


class TestMemorySkillSubstrateKit(SkillSubstrateContractKit):
    """Full-capability self-test — the entire kit runs against the fake.

    The kit calls ``create_substrate()`` once per test method; we capture that
    instance in ``_live`` so ``read_surface`` observes the same one the test
    mutated.
    """

    _live: MemorySkillSubstrate

    def create_substrate(self) -> SkillSubstrate:
        self._live = MemorySkillSubstrate()
        return self._live

    def read_surface(self, surface_key: str) -> set[str]:
        return self._live.surface_names(surface_key)


class TestReadOnlyMemorySkillSubstrateKit(SkillSubstrateContractKit):
    """Honesty-mode self-test (NE-SKILL-1a): the two author checks SKIP."""

    capabilities: ClassVar[frozenset[str]] = frozenset({"snapshots", "invocations", "materialize"})

    _live: MemorySkillSubstrate

    def create_substrate(self) -> SkillSubstrate:
        self._live = MemorySkillSubstrate()
        return self._live

    def read_surface(self, surface_key: str) -> set[str]:
        return self._live.surface_names(surface_key)
