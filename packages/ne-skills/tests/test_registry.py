# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Registry: schema idempotence, append-only writes, folds, cursor paging, exact pins."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from ne.contracts.skills import SkillInvocation
from ne.skills.registry import SkillPin, SqlAlchemySkillRegistry, ensure_skills_schema

from .conftest import make_pack


def test_schema_idempotent(engine: sa.Engine) -> None:
    ensure_skills_schema(engine)
    ensure_skills_schema(engine)  # no error on second run


def test_register_and_current_fold(registry: SqlAlchemySkillRegistry) -> None:
    registry.register_pack(make_pack("alpha"), origin="bundle")
    packs, tampered = registry.verified_current_packs()
    assert tampered == ()
    assert [p.name for p in packs] == ["alpha"]
    assert registry.get_current("alpha") is not None
    assert registry.get_current("absent") is None


def test_append_version_folds_to_max(registry: SqlAlchemySkillRegistry) -> None:
    registry.register_pack(make_pack("alpha", version=1, description="v1"), origin="bundle")
    registry.append_version(make_pack("alpha", version=2, description="v2"), origin="bundle")
    current = registry.get_current("alpha")
    assert current is not None
    assert current.version == 2
    assert current.description == "v2"
    assert registry.current_version_of("alpha") == 2


def test_status_fold(registry: SqlAlchemySkillRegistry) -> None:
    registry.register_pack(make_pack("alpha"), origin="bundle")
    # retired ⇒ excluded from verified_current_packs + get_current
    registry.append_lifecycle("alpha", "retired", actor="steward", reason="obsolete")
    assert registry.get_current("alpha") is None
    packs, _ = registry.verified_current_packs()
    assert "alpha" not in {p.name for p in packs}
    # restored ⇒ active again
    registry.append_lifecycle("alpha", "restored", actor="steward")
    assert registry.get_current("alpha") is not None
    # quarantined ⇒ excluded
    registry.append_lifecycle("alpha", "quarantined", actor="steward", reason="bad")
    assert registry.get_current("alpha") is None


def test_usage_counts(registry: SqlAlchemySkillRegistry) -> None:
    registry.register_pack(make_pack("alpha"), origin="bundle")
    for _ in range(3):
        registry.append_invocation(
            SkillInvocation(
                skill_name="alpha",
                version=1,
                principal_id="u-1",
                session_id="s-1",
                fired_at=datetime.now(UTC),
            )
        )
    assert registry.usage_counts() == {"alpha": 3}


def test_invocation_cursor_paging(registry: SqlAlchemySkillRegistry) -> None:
    registry.register_pack(make_pack("alpha"), origin="bundle")
    for i in range(5):
        registry.append_invocation(
            SkillInvocation(
                skill_name="alpha",
                version=1,
                principal_id=f"u-{i}",
                session_id=f"s-{i}",
                fired_at=datetime.now(UTC),
            )
        )
    first = registry.invocations_page(None, limit=2)
    assert len(first.items) == 2
    assert first.exhausted is False
    second = registry.invocations_page(first.cursor, limit=2)
    assert len(second.items) == 2
    # no overlap
    assert {i.principal_id for i in first.items}.isdisjoint({i.principal_id for i in second.items})
    third = registry.invocations_page(second.cursor, limit=2)
    assert len(third.items) == 1
    assert third.exhausted is True
    # stable cursor on exhausted
    again = registry.invocations_page(third.cursor, limit=2)
    assert len(again.items) == 0
    assert again.cursor == third.cursor
    assert again.exhausted is True


def test_naive_datetime_normalized_on_read(registry: SqlAlchemySkillRegistry) -> None:
    registry.register_pack(make_pack("alpha"), origin="bundle")
    registry.append_invocation(
        SkillInvocation(
            skill_name="alpha",
            version=1,
            principal_id="u-1",
            session_id="s-1",
            fired_at=datetime.now(UTC),
        )
    )
    page = registry.invocations_page(None)
    assert page.items[0].fired_at.tzinfo is not None


def test_library_rows_include_quarantined(registry: SqlAlchemySkillRegistry) -> None:
    registry.register_pack(make_pack("alpha"), origin="bundle")
    registry.register_quarantined("pending-one", "manual", reason="pending-forge")
    rows = {r.name: r for r in registry.library_rows()}
    assert rows["alpha"].status == "active"
    assert rows["alpha"].current_version == 1
    assert rows["pending-one"].status == "quarantined"
    assert rows["pending-one"].current_version is None


# -- PRE-011B: the exact-pin chokepoint -----------------------------------------


def test_verified_pinned_pack_serves_the_exact_version_after_a_rollover(
    registry: SqlAlchemySkillRegistry,
) -> None:
    v1 = make_pack("alpha", version=1, description="frozen v1")
    registry.register_pack(v1, origin="bundle")
    registry.append_version(
        make_pack("alpha", version=2, description="rolled-over v2"), origin="bundle"
    )
    # Control: the current-version read HAS rolled over.
    current = registry.get_current("alpha")
    assert current is not None and current.version == 2

    pinned = registry.verified_pinned_pack(SkillPin("alpha", 1, v1.content_hash))
    assert pinned is not None
    assert (pinned.version, pinned.description) == (1, "frozen v1")


def test_verified_pinned_pack_refuses_a_declared_hash_that_is_not_the_stored_one(
    registry: SqlAlchemySkillRegistry,
) -> None:
    pack = make_pack("alpha")
    registry.register_pack(pack, origin="bundle")
    assert registry.verified_pinned_pack(SkillPin("alpha", 1, pack.content_hash)) is not None
    assert registry.verified_pinned_pack(SkillPin("alpha", 1, "0" * 64)) is None


def test_verified_pinned_pack_refuses_an_unregistered_version(
    registry: SqlAlchemySkillRegistry,
) -> None:
    pack = make_pack("alpha", version=1)
    registry.register_pack(pack, origin="bundle")
    assert registry.verified_pinned_pack(SkillPin("alpha", 2, pack.content_hash)) is None
    assert registry.verified_pinned_pack(SkillPin("absent", 1, pack.content_hash)) is None


def test_verified_pinned_pack_refuses_a_retired_name(
    registry: SqlAlchemySkillRegistry,
) -> None:
    pack = make_pack("alpha")
    registry.register_pack(pack, origin="bundle")
    pin = SkillPin("alpha", 1, pack.content_hash)
    assert registry.verified_pinned_pack(pin) is not None
    registry.append_lifecycle("alpha", "retired", actor="user:steward")
    assert registry.verified_pinned_pack(pin) is None


# -- PRE-011B: the surface invariant (no packs => no index text) -----------------


def _surface_of(registry: SqlAlchemySkillRegistry, key: str) -> tuple[str, tuple[str, ...]]:
    surface = registry.read_surface(key)
    assert surface is not None
    return surface


def test_retiring_the_last_pack_leaves_no_index_text_rather_than_a_bare_header(
    registry: SqlAlchemySkillRegistry, engine: sa.Engine
) -> None:
    """``retire`` line-filters instead of re-rendering, so it must not strand a header."""
    from ne.skills.active_set import render_index
    from ne.skills.retirement import retire_skill

    alpha = make_pack("alpha")
    bravo = make_pack("bravo")
    for pack in (alpha, bravo):
        registry.register_pack(pack, origin="bundle")
    registry.upsert_surface("dept:eng", render_index((alpha, bravo)), ("alpha", "bravo"))

    # POSITIVE CONTROL: with a pack still standing, the line-filtered survivor is
    # byte-identical to what a FRESH composition of the survivors renders — the
    # same equality the substrate-withdraw twin below asserts. Substring checks
    # ("- bravo (" present, "- alpha (" absent) are weaker: they would pass on a
    # filtered text that had also lost the header, kept a stray fragment of the
    # retired entry, or reordered what remained.
    retire_skill(engine, "alpha", actor="steward", reason="unused")
    index_text, pack_names = _surface_of(registry, "dept:eng")
    assert pack_names == ("bravo",)
    assert index_text == render_index((bravo,))

    # Retiring the LAST pack must empty the index, not leave the header behind.
    retire_skill(engine, "bravo", actor="steward", reason="unused")
    index_text, pack_names = _surface_of(registry, "dept:eng")
    assert pack_names == ()
    assert index_text == ""


def test_withdrawing_every_pack_leaves_no_index_text_rather_than_a_bare_header(
    registry: SqlAlchemySkillRegistry, engine: sa.Engine
) -> None:
    """The SUBSTRATE withdraw path, proved through the same registry chokepoint.

    ``PydanticAiSkillSubstrate.withdraw`` also line-filters rather than
    re-rendering, and it writes through ``upsert_surface`` — which is why the
    invariant is enforced there and needs no edit in the substrate itself.
    """
    import asyncio

    from ne.skills.active_set import render_index
    from ne.skills.substrate import PydanticAiSkillSubstrate

    alpha = make_pack("alpha")
    bravo = make_pack("bravo")
    substrate = PydanticAiSkillSubstrate(engine)
    asyncio.run(substrate.materialize((alpha, bravo), surface_key="s:1"))

    # POSITIVE CONTROL: withdrawing one of two keeps a well-formed index.
    asyncio.run(substrate.withdraw(("alpha",), surface_key="s:1"))
    index_text, pack_names = _surface_of(registry, "s:1")
    assert pack_names == ("bravo",)
    assert index_text == render_index((bravo,))

    asyncio.run(substrate.withdraw(("bravo",), surface_key="s:1"))
    index_text, pack_names = _surface_of(registry, "s:1")
    assert pack_names == ()
    assert index_text == ""
    # ...and the emptied surface equals what a FRESH composition would render,
    # which is the equality control 4 asserts for the materialized surface.
    assert index_text == render_index(())
