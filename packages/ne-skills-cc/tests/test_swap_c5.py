# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE C5 dual-binding swap test: identical scenarios, identical observations.

Runs the SAME scenario (splice, author, snapshot paging, invocation drain)
against BOTH ``PydanticAiSkillSubstrate`` and ``ClaudeCodeSkillSubstrate`` and
asserts the Protocol-observable observations are equal — turning "any stack we
support" from an asserted claim into a proven one. Timestamps are excluded
from the cross-binding comparison (audit ``occurred_at`` is not
seed-controllable on the pydantic-ai side); tz-awareness and ordering are
instead asserted per-binding inside the scenario runner.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from ne.contracts.skills import AuthoringStandard, SkillSubstrate
from ne.skills.registry import SqlAlchemySkillRegistry, ensure_skills_schema
from ne.skills.substrate import PydanticAiSkillSubstrate
from ne.skills_cc import store
from ne.skills_cc.substrate import ClaudeCodeSkillSubstrate

from .conftest import _good_author_md, make_pack, seed_cc_root, seed_pydantic_engine

_SWAP_SURFACE = "swap:surface@1"


def test_slot_registers_both_bindings(tmp_path: Path) -> None:
    eps = importlib.metadata.entry_points(group="ne.plugins.skill_substrate")
    names = {ep.name for ep in eps}
    # Superset, not exact — future bindings must not break this assertion.
    assert {"pydantic-ai", "claude-code"} <= names

    pydantic_ep = next(ep for ep in eps if ep.name == "pydantic-ai")
    cc_ep = next(ep for ep in eps if ep.name == "claude-code")
    pydantic_cls = pydantic_ep.load()
    cc_cls = cc_ep.load()
    assert pydantic_cls is PydanticAiSkillSubstrate
    assert cc_cls is ClaudeCodeSkillSubstrate

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'slot.db'}")
    ensure_skills_schema(engine)
    # Assigning to a SkillSubstrate-annotated variable makes pyright enforce
    # structural Protocol conformance for both constructed instances.
    pydantic_instance: SkillSubstrate = pydantic_cls(engine)
    cc_instance: SkillSubstrate = cc_cls(tmp_path / "cc-root")
    assert pydantic_instance.substrate_id == "pydantic-ai"
    assert cc_instance.substrate_id == "claude-code"


async def _run_scenario(
    substrate: SkillSubstrate, read_surface: Callable[[str], set[str]]
) -> dict[str, Any]:
    # -- [splice] materialize / withdraw / re-materialize (ONE-ACL §7.7 set) --
    packs = [
        make_pack("global-skill", acl_labels=()),
        make_pack("finance-skill", acl_labels=("dept:finance",)),
        make_pack("eng-skill", acl_labels=("dept:eng", "role:admin")),
        make_pack("restricted-skill", acl_labels=("acl:restricted",)),
    ]
    await substrate.materialize(packs, surface_key=_SWAP_SURFACE)
    after_materialize = read_surface(_SWAP_SURFACE)
    await substrate.withdraw(["finance-skill"], surface_key=_SWAP_SURFACE)
    after_withdraw = read_surface(_SWAP_SURFACE)
    await substrate.materialize(packs, surface_key=_SWAP_SURFACE)
    after_rematerialize = read_surface(_SWAP_SURFACE)

    # -- [author] same canned dispatch output, new draft + update draft ------
    snap_page = await substrate.snapshots(limit=1)
    assert snap_page.items, "seed must expose at least one snapshot"
    first_snapshot = snap_page.items[0]
    draft_new = await substrate.author(first_snapshot, standard=AuthoringStandard())
    draft_update = await substrate.author(
        first_snapshot,
        standard=AuthoringStandard(),
        existing=make_pack("existing-skill"),
    )

    # -- [snapshots] page to exhaustion ---------------------------------------
    snapshot_pages: list[tuple[int, bool]] = []
    snapshot_observations: list[tuple[str, str, str, bool, tuple[tuple[str, str], ...]]] = []
    cursor: str | None = None
    guard = 0
    while True:
        guard += 1
        assert guard < 50, "snapshots never exhausted"
        page = await substrate.snapshots(cursor=cursor, limit=2)
        snapshot_pages.append((len(page.items), page.exhausted))
        for snap in page.items:
            assert snap.started_at.tzinfo is not None
            assert snap.finished_at.tzinfo is not None
            assert snap.finished_at >= snap.started_at
            snapshot_observations.append(
                (
                    snap.intent,
                    snap.principal_id,
                    snap.session_id,
                    snap.succeeded,
                    tuple((s.action, s.outcome) for s in snap.steps),
                )
            )
        if page.exhausted:
            break
        cursor = page.cursor

    # -- [invocations] page to exhaustion --------------------------------------
    # Only the drained CONTENT is recorded (a sorted multiset), never per-page
    # shape. Both bindings compute `exhausted` as an end-of-sequence fact per the
    # InvocationPage contract ("True when no further items are available") — the
    # earlier "fewer than limit" rule was an off-by-one (Codex NE-SKILL-5 P2),
    # fixed on both sides. The comparison stays content-only regardless, so the
    # swap test does not over-couple to each reader's internal batching (a SQL
    # lookahead vs a materialized-line scan); the Protocol guarantees the drained
    # set, not the page boundaries. (Snapshot pages ARE compared per-page shape,
    # which is why both readers' snapshot exhaustion had to be made identical.)
    invocation_multiset: list[tuple[str, int, str, str, str, bool | None]] = []
    inv_cursor: str | None = None
    guard = 0
    while True:
        guard += 1
        assert guard < 50, "invocations never exhausted"
        page = await substrate.collect_invocations(cursor=inv_cursor, limit=2)
        for inv in page.items:
            invocation_multiset.append(
                (
                    inv.skill_name,
                    inv.version,
                    inv.principal_id,
                    inv.session_id,
                    inv.fired_at.isoformat(),
                    inv.run_succeeded,
                )
            )
        if page.exhausted:
            break
        inv_cursor = page.cursor

    return {
        "substrate_id": substrate.substrate_id,
        "after_materialize": after_materialize,
        "after_withdraw": after_withdraw,
        "after_rematerialize": after_rematerialize,
        "author_new": (
            draft_new.name,
            draft_new.version,
            draft_new.source.value,
            draft_new.acl_labels,
            dict(draft_new.provenance),
            draft_new.content_hash,
            draft_new.routing_hash,
        ),
        "author_update": (draft_update.name, draft_update.version),
        "snapshot_pages": snapshot_pages,
        "snapshot_observations": sorted(snapshot_observations),
        "invocation_multiset": sorted(invocation_multiset, key=lambda t: (t[2], t[3])),
    }


async def _good_dispatch(task: str) -> str:
    return _good_author_md()


def test_swap_observations_identical(tmp_path: Path) -> None:
    pydantic_root = tmp_path / "pydantic"
    pydantic_root.mkdir()
    cc_root = tmp_path / "cc"
    cc_root.mkdir()

    engine = seed_pydantic_engine(pydantic_root)
    pydantic_substrate: SkillSubstrate = PydanticAiSkillSubstrate(
        engine, audit_engine=engine, author_dispatch=_good_dispatch
    )

    seed_cc_root(cc_root)
    cc_substrate: SkillSubstrate = ClaudeCodeSkillSubstrate(cc_root, author_dispatch=_good_dispatch)

    def read_pydantic_surface(key: str) -> set[str]:
        registry = SqlAlchemySkillRegistry(engine)
        current = registry.read_surface(key)
        return set(current[1]) if current is not None else set()

    def read_cc_surface(key: str) -> set[str]:
        return store.list_surface(cc_root, key)

    pydantic_obs = asyncio.run(_run_scenario(pydantic_substrate, read_pydantic_surface))
    cc_obs = asyncio.run(_run_scenario(cc_substrate, read_cc_surface))

    # Non-vacuous: the two substrate_ids must differ within the SAME run that
    # asserts observation equality (never running one binding twice).
    assert pydantic_obs["substrate_id"] == "pydantic-ai"
    assert cc_obs["substrate_id"] == "claude-code"
    assert pydantic_obs["substrate_id"] != cc_obs["substrate_id"]

    pydantic_cmp = {k: v for k, v in pydantic_obs.items() if k != "substrate_id"}
    cc_cmp = {k: v for k, v in cc_obs.items() if k != "substrate_id"}
    assert pydantic_cmp == cc_cmp
