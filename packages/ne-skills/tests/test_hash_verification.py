# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""R-28: a DB-tampered row is refused on search, view, the active set AND an exact pin."""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa

from ne.contracts.types import Principal
from ne.skills.active_set import ActiveSetComputer
from ne.skills.errors import SkillNotFoundError
from ne.skills.registry import SkillPin, SqlAlchemySkillRegistry, ne_skill_versions
from ne.skills.tools import VIEW_TOOL_NAME, build_search_handler, build_view_handler

from .conftest import StaticExpander, make_pack

P = Principal(id="u-1")
GRANTED = frozenset({VIEW_TOOL_NAME})


def _widen_labels(engine: sa.Engine, name: str) -> None:
    """Raw-SQL widen acl_labels to () WITHOUT recomputing content_hash (tamper)."""
    with engine.begin() as conn:
        conn.execute(
            ne_skill_versions.update().where(ne_skill_versions.c.name == name).values(acl_labels=[])
        )


def _tamper_body(engine: sa.Engine, name: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            ne_skill_versions.update()
            .where(ne_skill_versions.c.name == name)
            .values(body="INJECTED BODY")
        )


def test_widened_labels_excluded_everywhere(
    registry: SqlAlchemySkillRegistry, engine: sa.Engine, settings
) -> None:
    registry.register_pack(
        make_pack("finance", triggers=("quarterly revenue",), acl_labels=("grp:team-b",)),
        origin="bundle",
    )
    _widen_labels(engine, "finance")

    # verified_current_packs excludes + reports tampered
    packs, tampered = registry.verified_current_packs()
    assert "finance" not in {p.name for p in packs}
    assert "finance" in tampered
    assert registry.get_current("finance") is None

    expander = StaticExpander({"u-1": frozenset({"u-1"})})  # NOT team-b
    # search: nothing surfaces
    search = build_search_handler(registry, expander, settings, lambda: P)
    result = asyncio.run(search({"query": "quarterly revenue"}))
    assert result == "no skills matched"

    # view: not found (no oracle)
    view = build_view_handler(registry, expander, settings, lambda: P)
    with pytest.raises(SkillNotFoundError):
        asyncio.run(view({"name": "finance"}))

    # active set: excluded
    computer = ActiveSetComputer(registry, expander, settings)
    active = computer.compute(P, granted_tools=GRANTED, substrate_id="pydantic-ai")
    assert "finance" not in {p.name for p in active}


def test_tampered_body_refused(
    registry: SqlAlchemySkillRegistry, engine: sa.Engine, settings
) -> None:
    registry.register_pack(make_pack("alpha"), origin="bundle")
    _tamper_body(engine, "alpha")
    assert registry.get_current("alpha") is None
    packs, tampered = registry.verified_current_packs()
    assert "alpha" in tampered
    assert "alpha" not in {p.name for p in packs}


def test_pinned_pack_with_a_tampered_row_is_refused_under_its_unchanged_stored_hash(
    registry: SqlAlchemySkillRegistry, engine: sa.Engine, settings
) -> None:
    """The row-integrity half of the pin chokepoint, isolated from the pin-hash half.

    ``_tamper_body`` rewrites the body WITHOUT recomputing the stored
    ``content_hash`` column, so the declared pin still matches what is stored and
    only the reconstruct-and-recompute check can catch it.
    """
    pack = make_pack("alpha")
    registry.register_pack(pack, origin="bundle")
    pin = SkillPin("alpha", 1, pack.content_hash)
    assert registry.verified_pinned_pack(pin) is not None  # positive control

    _tamper_body(engine, "alpha")
    with engine.begin() as conn:
        stored = conn.execute(
            sa.select(ne_skill_versions.c.content_hash).where(ne_skill_versions.c.name == "alpha")
        ).scalar_one()
    assert stored == pin.content_hash  # the declared pin still matches the stored column
    assert registry.verified_pinned_pack(pin) is None

    # The tampered body is refused on the pinned governed view too, with the SAME
    # error type and message FORMAT an absent name raises (no oracle).
    pins = {"alpha": pin, "ghost": SkillPin("ghost", 1, pack.content_hash)}
    view = build_view_handler(
        registry, StaticExpander(), settings, lambda: P, pin_provider=lambda: pins
    )
    with pytest.raises(SkillNotFoundError) as tampered:
        asyncio.run(view({"name": "alpha"}))
    with pytest.raises(SkillNotFoundError) as absent:
        asyncio.run(view({"name": "ghost"}))
    assert str(tampered.value) == "skill not found: alpha"
    assert str(absent.value) == "skill not found: ghost"
