# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SkillSubstrate conformance kit (C5 — every Protocol ships a kit).

Precedent: :mod:`ne.contracts.testing.kits.object_store` /
:mod:`~ne.contracts.testing.kits.secret_store`. Async contract methods are
exercised via ``asyncio.run`` (pytest-asyncio is NOT a dependency and must not
be added).

The kit is CAPABILITIES-AWARE so an early binding can honestly run the subset it
implements: NE-SKILL-1a lands the read side (``snapshots``/``invocations``/
``materialize``) but not ``author``, so its binding suite overrides
:attr:`~SkillSubstrateContractKit.capabilities` to drop ``author`` and the two
author checks SKIP rather than fail. The read subset = every check except the
two author checks; the FULL kit is the NE-SKILL-1b/5 bar, and the C5 dual-binding
swap test becomes CI-blocking when the second binding (NE-SKILL-5) lands.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from ne.contracts.skills import (
    AuthoringStandard,
    SkillPack,
    SkillSource,
    SkillSubstrate,
)

_STANDARD_BODY = (
    "## When to Use\nWhen the trigger fires.\n\n"
    "## Procedure\n1. Do the thing.\n\n"
    "## Pitfalls\nNone known.\n\n"
    "## Verification\nConfirm the outcome.\n"
)


class SkillSubstrateContractKit:
    """Subclass and override the factory hooks to prove SkillSubstrate conformance.

    A binding proves conformance by subclassing this kit (with a ``Test``-prefixed
    name so pytest collects it) and overriding :meth:`create_substrate` +
    :meth:`read_surface`. A binding that implements only the read side overrides
    :attr:`capabilities` to the honest subset.
    """

    #: The capabilities this suite asserts. A read-only binding overrides this to
    #: e.g. ``frozenset({"snapshots", "invocations", "materialize"})`` so the two
    #: author checks skip. The FULL bar is all four.
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {"snapshots", "invocations", "materialize", "author"}
    )

    def create_substrate(self) -> SkillSubstrate:
        """Return a fresh substrate exposing >=5 pageable snapshots and >=5
        pageable invocations."""
        raise NotImplementedError

    def read_surface(self, surface_key: str) -> set[str]:
        """Return the pack names currently discoverable on ``surface_key``.

        This is how the kit observes ``materialize``/``withdraw`` without
        reaching into substrate internals.
        """
        raise NotImplementedError

    def make_pack(self, name: str, *, acl_labels: tuple[str, ...] = ()) -> SkillPack:
        """Build a valid pack for the materialize checks."""
        return SkillPack(
            name=name,
            version=1,
            description=f"skill {name}",
            triggers=(f"use {name}",),
            body=_STANDARD_BODY,
            source=SkillSource.FORGED,
            acl_labels=acl_labels,
        )

    def _require(self, cap: str) -> None:
        if cap not in self.capabilities:
            pytest.skip(f"substrate does not declare the {cap!r} capability")

    # -- substrate identity ------------------------------------------------

    def test_substrate_id_nonempty(self) -> None:
        substrate = self.create_substrate()
        assert substrate.substrate_id

    # -- snapshot cursor discipline ---------------------------------------

    def test_snapshot_pages_resume_without_overlap(self) -> None:
        self._require("snapshots")
        substrate = self.create_substrate()

        async def scenario() -> None:
            first = await substrate.snapshots(limit=2)
            second = await substrate.snapshots(cursor=first.cursor, limit=2)
            first_ids = {s.snapshot_id for s in first.items}
            second_ids = {s.snapshot_id for s in second.items}
            assert len(first.items) == 2
            assert len(second.items) == 2
            assert first_ids.isdisjoint(second_ids)

        asyncio.run(scenario())

    def test_snapshot_cursor_stable_when_exhausted(self) -> None:
        self._require("snapshots")
        substrate = self.create_substrate()

        async def scenario() -> None:
            cursor: str | None = None
            page = await substrate.snapshots(cursor=cursor, limit=2)
            guard = 0
            while not page.exhausted:
                guard += 1
                assert guard < 100, "snapshots never reported exhausted"
                page = await substrate.snapshots(cursor=page.cursor, limit=2)
            again = await substrate.snapshots(cursor=page.cursor, limit=2)
            # ``items`` is typed Sequence — accept any empty Sequence ([], (), ...).
            assert len(again.items) == 0
            assert again.cursor == page.cursor
            assert again.exhausted is True

        asyncio.run(scenario())

    def test_snapshot_steps_are_names_only(self) -> None:
        self._require("snapshots")
        substrate = self.create_substrate()

        async def scenario() -> None:
            page = await substrate.snapshots(limit=50)
            for snap in page.items:
                for step in snap.steps:
                    # WorkflowStep carries an action NAME + scrubbed outcome, never
                    # argument values (C12). This kit asserts the shape the contract
                    # actually publishes — the fields exist and are strings — and
                    # does NOT impose a length bound: neither the model nor the
                    # Protocol defines one (Codex P2, NE-SKILL-0 review), so a
                    # length assertion here would fail a conformant binding that
                    # legitimately returns a long scrubbed outcome. "Names-only,
                    # no smuggled values" is a per-binding obligation the binding's
                    # own tests must prove; if a hard limit is ever wanted it
                    # belongs as a max_length on the FROZEN WorkflowStep field
                    # (a deliberate contract change), not as an undocumented kit rule.
                    assert isinstance(step.action, str) and step.action
                    assert isinstance(step.outcome, str)
                    assert isinstance(step.detail_ref, str)

        asyncio.run(scenario())

    # -- invocation cursor discipline -------------------------------------

    def test_invocation_pages_resume_without_overlap(self) -> None:
        self._require("invocations")
        substrate = self.create_substrate()

        async def scenario() -> None:
            first = await substrate.collect_invocations(limit=2)
            second = await substrate.collect_invocations(cursor=first.cursor, limit=2)
            assert len(first.items) == 2
            assert len(second.items) == 2
            first_keys = {(i.skill_name, i.session_id) for i in first.items}
            second_keys = {(i.skill_name, i.session_id) for i in second.items}
            assert first_keys.isdisjoint(second_keys)

        asyncio.run(scenario())

    def test_invocation_cursor_stable_when_exhausted(self) -> None:
        self._require("invocations")
        substrate = self.create_substrate()

        async def scenario() -> None:
            page = await substrate.collect_invocations(limit=2)
            guard = 0
            while not page.exhausted:
                guard += 1
                assert guard < 100, "collect_invocations never reported exhausted"
                page = await substrate.collect_invocations(cursor=page.cursor, limit=2)
            again = await substrate.collect_invocations(cursor=page.cursor, limit=2)
            # ``items`` is typed Sequence — accept any empty Sequence ([], (), ...).
            assert len(again.items) == 0
            assert again.cursor == page.cursor
            assert again.exhausted is True

        asyncio.run(scenario())

    # -- materialize / withdraw -------------------------------------------

    def test_materialize_idempotent_and_withdraw_restores(self) -> None:
        self._require("materialize")
        substrate = self.create_substrate()
        surface = "kit-surface-1"
        packs = [self.make_pack(n) for n in ("alpha", "bravo", "charlie")]

        async def scenario() -> None:
            await substrate.materialize(packs, surface_key=surface)
            await substrate.materialize(packs, surface_key=surface)
            assert self.read_surface(surface) == {"alpha", "bravo", "charlie"}
            await substrate.withdraw(["bravo"], surface_key=surface)
            assert self.read_surface(surface) == {"alpha", "charlie"}
            await substrate.materialize(packs, surface_key=surface)
            assert self.read_surface(surface) == {"alpha", "bravo", "charlie"}

        asyncio.run(scenario())

    def test_materialize_does_not_refilter(self) -> None:
        self._require("materialize")
        substrate = self.create_substrate()
        surface = "kit-surface-acl"
        # The ONE-ACL substrate-half tripwire (design section 7.7): packs arrive
        # ALREADY ACL-filtered by the core; a substrate that drops or re-filters
        # ANY of these by their labels fails.
        packs = [
            self.make_pack("global-skill", acl_labels=()),
            self.make_pack("finance-skill", acl_labels=("dept:finance",)),
            self.make_pack("eng-skill", acl_labels=("dept:eng", "role:admin")),
            self.make_pack("restricted-skill", acl_labels=("acl:restricted",)),
        ]

        async def scenario() -> None:
            await substrate.materialize(packs, surface_key=surface)
            assert self.read_surface(surface) == {
                "global-skill",
                "finance-skill",
                "eng-skill",
                "restricted-skill",
            }

        asyncio.run(scenario())

    # -- author (OPTIONAL until implemented) ------------------------------

    def test_author_returns_valid_draft(self) -> None:
        self._require("author")
        substrate = self.create_substrate()

        async def scenario() -> None:
            page = await substrate.snapshots(limit=1)
            assert page.items, "kit substrate must expose at least one snapshot"
            draft = await substrate.author(page.items[0], standard=AuthoringStandard())
            assert isinstance(draft, SkillPack)
            assert draft.triggers  # non-empty
            # Draft invariants from the Protocol docstring: the core ALONE
            # assigns these security-sensitive fields — a draft must carry
            # empty provenance and acl_labels=().
            assert draft.acl_labels == ()
            assert len(draft.provenance) == 0
            # A valid draft's computed hashes are consistent by construction.
            assert draft.content_hash and draft.routing_hash

        asyncio.run(scenario())

    def test_author_existing_produces_update(self) -> None:
        self._require("author")
        substrate = self.create_substrate()

        async def scenario() -> None:
            page = await substrate.snapshots(limit=1)
            assert page.items
            existing = self.make_pack("existing-skill")
            draft = await substrate.author(
                page.items[0], standard=AuthoringStandard(), existing=existing
            )
            assert draft.name == existing.name
            assert draft.version == existing.version + 1

        asyncio.run(scenario())
