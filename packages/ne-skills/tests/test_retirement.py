# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The retirement lane — clock-injected candidacy, absence-after-retire, restore."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from ne.contracts.skills import SkillInvocation
from ne.contracts.types import Principal
from ne.skills.active_set import ActiveSetComputer
from ne.skills.errors import SkillNotFoundError
from ne.skills.registry import SqlAlchemySkillRegistry, ne_skill_versions
from ne.skills.retirement import restore_skill, retire_skill, retirement_candidates
from ne.skills.substrate import PydanticAiSkillSubstrate
from ne.skills.telemetry import fold_invocations
from ne.skills.tools import VIEW_TOOL_NAME, build_search_handler, build_view_handler

from .conftest import make_pack

_NOW = datetime(2026, 7, 1, tzinfo=UTC)


def _fire_at(registry: SqlAlchemySkillRegistry, name: str, fired_at: datetime) -> None:
    registry.append_invocation(
        SkillInvocation(
            skill_name=name,
            version=1,
            principal_id="u-1",
            session_id="s-1",
            fired_at=fired_at,
        )
    )


def test_old_unfired_pack_is_candidate(registry: SqlAlchemySkillRegistry, engine) -> None:
    registry.register_pack(make_pack("stale"), origin="bundle")
    future = datetime.now(UTC) + timedelta(days=200)
    cands = retirement_candidates(engine, now=future)
    names = {c.name for c in cands}
    assert "stale" in names
    cand = next(c for c in cands if c.name == "stale")
    assert cand.reason == "unfired-90d"
    assert cand.fired_count == 0


def test_recently_registered_is_not_candidate(registry: SqlAlchemySkillRegistry, engine) -> None:
    registry.register_pack(make_pack("fresh"), origin="bundle")
    soon = datetime.now(UTC) + timedelta(days=1)
    assert retirement_candidates(engine, now=soon) == ()


def test_recently_fired_is_not_candidate(registry: SqlAlchemySkillRegistry, engine) -> None:
    registry.register_pack(make_pack("busy"), origin="bundle")
    future = datetime.now(UTC) + timedelta(days=200)
    # A single fire close to `now` keeps the pack out of the candidate set even
    # though it is well below the min-fires floor.
    _fire_at(registry, "busy", future - timedelta(days=1))
    fold_invocations(engine)
    assert retirement_candidates(engine, now=future) == ()


def test_fired_at_or_above_min_fires_is_not_candidate(
    registry: SqlAlchemySkillRegistry, engine
) -> None:
    registry.register_pack(make_pack("popular"), origin="bundle")
    old = datetime.now(UTC) - timedelta(days=300)
    for _ in range(3):  # >= min_fires (3), even if all fires are ancient
        _fire_at(registry, "popular", old)
    fold_invocations(engine)
    future = datetime.now(UTC) + timedelta(days=200)
    assert retirement_candidates(engine, now=future) == ()


def test_restored_recently_resets_the_window(registry: SqlAlchemySkillRegistry, engine) -> None:
    registry.register_pack(make_pack("revived"), origin="bundle")
    retire_skill(engine, "revived", actor="steward", reason="stale")
    restore_skill(engine, "revived", actor="steward", reason="needed again")
    # `now` just after the restore instant ⇒ the restore resets the 90d window.
    soon = datetime.now(UTC) + timedelta(days=1)
    assert retirement_candidates(engine, now=soon) == ()


def test_retire_restore_round_trip(registry: SqlAlchemySkillRegistry, engine) -> None:
    registry.register_pack(make_pack("alpha"), origin="bundle")
    assert registry.current_status("alpha") == "active"
    retire_skill(engine, "alpha", actor="steward", reason="unused 90d")
    assert registry.current_status("alpha") == "retired"
    restore_skill(engine, "alpha", actor="steward", reason="wanted back")
    assert registry.current_status("alpha") == "active"


def test_retire_guards(registry: SqlAlchemySkillRegistry, engine) -> None:
    with pytest.raises(LookupError):
        retire_skill(engine, "ghost", actor="steward", reason="x")
    registry.register_pack(make_pack("alpha"), origin="bundle")
    retire_skill(engine, "alpha", actor="steward", reason="x")
    # a second retire (already retired) is a wrong-status ValueError
    with pytest.raises(ValueError):
        retire_skill(engine, "alpha", actor="steward", reason="x")
    # restoring a never-retired pack is a wrong-status ValueError
    registry.register_pack(make_pack("beta"), origin="bundle")
    with pytest.raises(ValueError):
        restore_skill(engine, "beta", actor="steward", reason="x")


def test_retired_pack_absent_from_every_read_path(
    registry: SqlAlchemySkillRegistry, engine, expander, settings
) -> None:
    """The permission-trap discipline: after retire the pack is gone from the
    active set, default search, the view, and every materialized surface (even
    after a fresh recompute) — while its content rows survive; restore brings it
    back."""
    registry.register_pack(make_pack("alpha", description="the alpha skill"), origin="bundle")
    principal = Principal(id="u-1", claims={"sid": "sess"})
    granted = frozenset({VIEW_TOOL_NAME})
    substrate = PydanticAiSkillSubstrate(engine)
    computer = ActiveSetComputer(registry, expander, settings)

    # Materialize a surface that lists the pack (the pre-retire state).
    packs = computer.compute(principal, granted_tools=granted, substrate_id="pydantic-ai")
    assert {p.name for p in packs} == {"alpha"}
    asyncio.run(substrate.materialize(packs, surface_key="surface-1"))
    surface = registry.read_surface("surface-1")
    assert surface is not None
    idx, names = surface
    assert "alpha" in names and "- alpha (" in idx

    # --- retire ---
    retire_skill(engine, "alpha", actor="steward", reason="rot")

    # absent from verified_current_packs (structural status fold)
    verified, _t = registry.verified_current_packs()
    assert all(p.name != "alpha" for p in verified)

    # absent from the REAL search handler
    search = build_search_handler(registry, expander, settings, lambda: principal)
    assert asyncio.run(search({"query": "alpha skill"})) == "no skills matched"

    # the view raises SkillNotFoundError (indistinguishable from absent)
    view = build_view_handler(registry, expander, settings, lambda: principal)
    with pytest.raises(SkillNotFoundError):
        asyncio.run(view({"name": "alpha"}))

    # withdrawn from the existing materialized surface
    surface2 = registry.read_surface("surface-1")
    assert surface2 is not None
    idx2, names2 = surface2
    assert "alpha" not in names2 and "- alpha (" not in idx2

    # a fresh recompute + materialize keeps it absent
    packs2 = computer.compute(principal, granted_tools=granted, substrate_id="pydantic-ai")
    assert packs2 == ()

    # content rows are NEVER deleted (never silent-delete)
    with engine.begin() as conn:
        version_rows = conn.execute(
            sa.select(ne_skill_versions).where(ne_skill_versions.c.name == "alpha")
        ).all()
    assert len(version_rows) >= 1

    # --- restore brings it back ---
    restore_skill(engine, "alpha", actor="steward", reason="wanted")
    verified3, _t3 = registry.verified_current_packs()
    assert any(p.name == "alpha" for p in verified3)


def test_retire_is_atomic_lifecycle_and_surface(registry: SqlAlchemySkillRegistry, engine) -> None:
    """Retire commits the 'retired' lifecycle AND every surface withdrawal in ONE
    transaction — a retired pack is never left advertised on a stale surface
    (Codex P2). Here: a materialized surface carries the pack; after retire it is
    folded-retired AND gone from the surface index + pack_names together.
    """
    registry.register_pack(make_pack("gamma"), origin="bundle")
    # Materialize a surface that advertises gamma (mirrors an active-set render).
    registry.upsert_surface("dept:eng", "- gamma (does a thing)", ("gamma",))

    retire_skill(engine, "gamma", actor="steward", reason="unused 90d")

    assert registry.current_status("gamma") == "retired"
    surface = registry.read_surface("dept:eng")
    assert surface is not None
    index_text, pack_names = surface
    assert "gamma" not in pack_names
    assert "- gamma (" not in index_text
