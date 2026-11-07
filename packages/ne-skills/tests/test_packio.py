# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""packio: safe_load parse, serialize round-trip, violations not exceptions."""

from __future__ import annotations

from pathlib import Path

from ne.contracts.skills import AuthoringStandard, SkillPack
from ne.skills.packio import pack_from_dir, parse_skill_md, serialize_pack
from ne.skills.standard import StandardViolation

from .conftest import GOOD_BODY, make_pack

STANDARD = AuthoringStandard()

VALID_MD = f"""---
name: sample-skill
version: 1
description: a sample skill
source: bundled
triggers:
  - do the sample
  - sample it
acl_labels:
  - grp:team-a
---
{GOOD_BODY}"""


def test_parse_frontmatter_and_body() -> None:
    front, body = parse_skill_md(VALID_MD)
    assert front["name"] == "sample-skill"
    assert front["triggers"] == ["do the sample", "sample it"]
    assert "## When to Use" in body


def test_pack_from_dir_valid(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text(VALID_MD, encoding="utf-8")
    result = pack_from_dir(tmp_path, standard=STANDARD)
    assert isinstance(result, SkillPack)
    assert result.name == "sample-skill"
    assert result.acl_labels == ("grp:team-a",)


def test_round_trip_content_hash_equal() -> None:
    pack = make_pack(
        "alpha",
        acl_labels=("grp:team-b",),
        triggers=("first", "second"),
    )
    reparsed_front, reparsed_body = parse_skill_md(serialize_pack(pack))
    reparsed = SkillPack(
        name=reparsed_front["name"],
        version=reparsed_front["version"],
        description=reparsed_front["description"],
        triggers=tuple(reparsed_front["triggers"]),
        body=reparsed_body,
        source=reparsed_front["source"],
        acl_labels=tuple(reparsed_front.get("acl_labels", ())),
    )
    assert reparsed.content_hash == pack.content_hash
    assert reparsed.triggers == pack.triggers  # order preserved


def test_missing_skill_md_yields_violation(tmp_path: Path) -> None:
    result = pack_from_dir(tmp_path, standard=STANDARD)
    assert isinstance(result, tuple)
    assert all(isinstance(v, StandardViolation) for v in result)


def test_malformed_yaml_yields_violation(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("---\n: : bad: yaml: [\n---\nbody", encoding="utf-8")
    result = pack_from_dir(tmp_path, standard=STANDARD)
    assert isinstance(result, tuple)


def test_invalid_pack_yields_violations_not_exception(tmp_path: Path) -> None:
    bad = "---\nname: BadName\nversion: 1\nsource: bundled\n---\nno sections"
    (tmp_path / "SKILL.md").write_text(bad, encoding="utf-8")
    result = pack_from_dir(tmp_path, standard=STANDARD)
    assert isinstance(result, tuple)
    rules = {v.rule for v in result}
    assert "slug" in rules
