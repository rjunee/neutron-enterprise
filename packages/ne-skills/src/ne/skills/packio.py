# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SKILL.md + YAML-frontmatter pack format (D-4).

pyyaml is already in the workspace lock, so declaring it a direct dep adds ZERO
packages to the closure (C4-clean, MIT). SECURITY: ``yaml.safe_load`` ONLY — never
``yaml.load`` (arbitrary-object construction). A pack is validated against the
house standard (``standard.py``) BEFORE a ``SkillPack`` is constructed, so an
invalid pack yields typed violations instead of a pydantic explosion.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from ne.contracts.skills import AuthoringStandard, SkillPack, SkillSource
from ne.skills.standard import StandardViolation, validate_pack_fields

_DELIM = "---"

#: Frontmatter keys omitted from serialization when equal to their empty default.
_OPTIONAL_KEYS = ("category", "tags", "requires_tools", "platforms", "acl_labels")


def parse_skill_md(text: str) -> tuple[Mapping[str, Any], str]:
    """Split a SKILL.md into (frontmatter mapping, body).

    The frontmatter is a leading ``---\\n...\\n---`` block parsed with
    ``yaml.safe_load``. Missing/blank frontmatter yields an empty mapping and the
    whole text as the body.
    """
    stripped = text.lstrip("﻿")
    if not stripped.startswith(_DELIM):
        return {}, text
    # split('\n') (NOT splitlines) preserves a trailing newline so the body
    # round-trips byte-for-byte through serialize/parse (content-hash equal).
    lines = stripped.split("\n")
    # lines[0] == '---'; find the closing '---'.
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _DELIM:
            end = i
            break
    if end is None:
        return {}, text
    front_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    loaded = yaml.safe_load(front_text) if front_text.strip() else {}
    if not isinstance(loaded, dict):
        return {}, body
    return loaded, body


def _fields_from(frontmatter: Mapping[str, Any], body: str) -> dict[str, Any]:
    fields = dict(frontmatter)
    fields["body"] = body
    return fields


def pack_from_dir(
    path: Path,
    *,
    standard: AuthoringStandard,
    known_tools: frozenset[str] | None = None,
) -> SkillPack | tuple[StandardViolation, ...]:
    """Parse ``path/SKILL.md`` into a ``SkillPack`` or a tuple of violations.

    Reading a missing SKILL.md, malformed YAML, or a house-standard violation ALL
    yield typed violations (never an exception). ``acl_labels`` is allowed in the
    frontmatter so engagement packs can ship labels.
    """
    skill_md = path / "SKILL.md"
    if not skill_md.is_file():
        return (StandardViolation("frontmatter-missing", "no SKILL.md in pack dir"),)
    try:
        text = skill_md.read_text(encoding="utf-8")
        frontmatter, body = parse_skill_md(text)
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        return (StandardViolation("yaml", f"malformed SKILL.md: {type(exc).__name__}"),)

    fields = _fields_from(frontmatter, body)
    violations = validate_pack_fields(fields, standard, known_tools=known_tools)
    if violations:
        return violations
    return _pack_from_fields(fields)


def _seq(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),)


def _pack_from_fields(fields: Mapping[str, Any]) -> SkillPack:
    return SkillPack(
        name=str(fields["name"]),
        version=int(fields["version"]),
        description=str(fields["description"]),
        triggers=_seq(fields.get("triggers")),
        body=str(fields["body"]),
        source=SkillSource(str(fields["source"])),
        category=str(fields.get("category") or ""),
        tags=_seq(fields.get("tags")),
        requires_tools=_seq(fields.get("requires_tools")),
        platforms=_seq(fields.get("platforms")),
        acl_labels=_seq(fields.get("acl_labels")),
    )


def serialize_pack(pack: SkillPack) -> str:
    """Serialize a pack back to SKILL.md text (round-trips to an equal pack).

    Empty-default optional keys are omitted deterministically. ``triggers`` order
    is preserved (never sorted — the eval targets).
    """
    front: dict[str, Any] = {
        "name": pack.name,
        "version": pack.version,
        "description": pack.description,
        "triggers": list(pack.triggers),
        "source": pack.source.value,
    }
    for key in _OPTIONAL_KEYS:
        value = getattr(pack, key)
        if key == "category":
            if value:
                front[key] = value
        elif value:
            front[key] = list(value)
    front_text = yaml.safe_dump(front, sort_keys=True, default_flow_style=False).rstrip("\n")
    return f"{_DELIM}\n{front_text}\n{_DELIM}\n{pack.body}"
