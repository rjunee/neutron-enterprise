# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deliverable 2: native SKILL.md byte-stable + identity-stable round-trip."""

from __future__ import annotations

import pytest

from ne.contracts.skills import SkillPack, SkillSource
from ne.skills.packio import parse_skill_md
from ne.skills_cc.native import parse_native, serialize_native

MINIMAL_PACK = SkillPack(
    name="min-pack",
    version=1,
    description="a minimal pack",
    triggers=("do the minimal thing",),
    body="## When to Use\nAlways.\n",
    source=SkillSource.BUNDLED,
)

MAXIMAL_PACK = SkillPack(
    name="max-pack",
    version=3,
    description="a maximal pack\nwith a second line and unicode: café, 日本語",
    triggers=("second trigger", "first trigger"),  # deliberately unsorted
    body="## When to Use\nUnicode body: naïve résumé 世界\n\n## Procedure\n1. Go.\n",
    source=SkillSource.ENGAGEMENT,
    category="ops",
    tags=("b-tag", "a-tag"),
    requires_tools=("tool.z", "tool.a"),
    platforms=("linux", "darwin"),
    acl_labels=("dept:finance",),
    provenance={
        "nested": {"a": 1, "b": None, "c": [1, 2, "three"]},
        "flag": True,
        "count": 0,
        "note": None,
    },
)

DASH_BODY_PACK = SkillPack(
    name="dash-body-pack",
    version=1,
    description="a pack whose body contains a delimiter-lookalike line",
    triggers=("trigger one",),
    body="## When to Use\nSomething\n---\nMore text after a lone dash line.\n",
    source=SkillSource.MANUAL,
)

ALL_PACKS = (MINIMAL_PACK, MAXIMAL_PACK, DASH_BODY_PACK)


@pytest.mark.parametrize("pack", ALL_PACKS, ids=lambda p: p.name)
def test_roundtrip_identity(pack: SkillPack) -> None:
    reparsed = parse_native(serialize_native(pack))
    assert reparsed == pack
    assert reparsed.content_hash == pack.content_hash
    assert reparsed.routing_hash == pack.routing_hash


@pytest.mark.parametrize("pack", ALL_PACKS, ids=lambda p: p.name)
def test_roundtrip_byte_stable(pack: SkillPack) -> None:
    once = serialize_native(pack)
    twice = serialize_native(parse_native(once))
    assert twice == once


def test_minimal_frontmatter_shape_omits_empty_keys() -> None:
    text = serialize_native(MINIMAL_PACK)
    front, _body = parse_skill_md(text)
    assert set(front.keys()) == {"name", "description", "version", "metadata"}
    neutron = front["metadata"]["neutron"]
    assert set(neutron.keys()) == {"source", "triggers"}


def test_maximal_frontmatter_shape_carries_every_neutron_field() -> None:
    text = serialize_native(MAXIMAL_PACK)
    front, _body = parse_skill_md(text)
    assert set(front.keys()) == {"name", "description", "version", "metadata"}
    neutron = front["metadata"]["neutron"]
    assert set(neutron.keys()) == {
        "source",
        "triggers",
        "category",
        "tags",
        "requires_tools",
        "platforms",
        "acl_labels",
        "provenance",
    }
    assert neutron["provenance"] == MAXIMAL_PACK.model_dump(mode="json")["provenance"]


def test_triggers_order_preserved_verbatim() -> None:
    pack = SkillPack(
        name="order-pack",
        version=1,
        description="order matters",
        triggers=("b", "a"),
        body="## When to Use\nX\n",
        source=SkillSource.LEARNED,
    )
    text = serialize_native(pack)
    front, _body = parse_skill_md(text)
    assert front["metadata"]["neutron"]["triggers"] == ["b", "a"]
    reparsed = parse_native(text)
    assert reparsed.triggers == ("b", "a")


@pytest.mark.parametrize(
    "bad_text",
    [
        "no frontmatter at all, just body text",
        "---\n[1, 2, 3]\n---\nbody",  # non-mapping yaml
        "---\nname: x\ndescription: d\nversion: 1\n---\nbody",  # missing metadata.neutron.source
    ],
)
def test_malformed_native_raises_value_error(bad_text: str) -> None:
    with pytest.raises(ValueError, match=r"ne\.skills_cc\.native"):
        parse_native(bad_text)


def test_parse_native_malformed_yaml_raises_valueerror() -> None:
    # Regression (Codex P2): malformed YAML frontmatter must surface as the
    # documented ValueError, not a leaked yaml.YAMLError, so callers that handle
    # import failures as ValueError keep working.
    bad = "---\nname: [a, b\ndescription: x\n---\n## When to Use\nx\n"
    with pytest.raises(ValueError):
        parse_native(bad)
