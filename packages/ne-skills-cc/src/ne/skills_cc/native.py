# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The native ``.claude/skills/SKILL.md`` projection (metadata.neutron.* mapping).

This is DELIBERATELY a different frontmatter shape from
:mod:`ne.skills.packio` (the core's flat mapping, which stays untouched — it is
the format the FORGE/apply seam read and write). Per the SPEC §641 portability
invariant, a Claude-Code-native SKILL.md keeps only the cross-agent trio at the
top level — ``name``, ``description``, ``version`` — so the file reads like any
other agent's skill file to a human or a foreign tool. Every Neutron-specific
field (``source``, ``triggers``, ``category``, ``tags``, ``requires_tools``,
``platforms``, ``acl_labels``, ``provenance``) nests under
``metadata.neutron.*``, the namespaced-extension convention other agent
ecosystems already use for vendor metadata.

:func:`serialize_native` / :func:`parse_native` round-trip a :class:`SkillPack`
losslessly and byte-stably (``serialize_native(parse_native(serialize_native(p)))
== serialize_native(p)``) by construction: both directions go through
``yaml.safe_dump(..., sort_keys=True, default_flow_style=False)`` /
``yaml.safe_load`` exclusively (never ``yaml.load``), and empty-default optional
fields are omitted deterministically on the way out (mirroring
``ne.skills.packio``'s ``_OPTIONAL_KEYS`` discipline) so there is only one
canonical serialization per pack.

:func:`parse_native` is for the round-trip/import path only — it raises
``ValueError`` on malformed input. It is NOT the violations-not-exceptions gate
(``ne.skills.packio.pack_from_dir`` / ``ne.skills.standard.validate_pack_fields``
own that path for house-standard admission); this parser's job is "did this text
decode into the pack the frontmatter claims", not "is this pack good enough to
publish".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml

from ne.contracts.skills import SkillPack, SkillSource
from ne.skills.packio import parse_skill_md

#: metadata.neutron keys omitted from serialization when equal to their empty
#: default — mirrors packio._OPTIONAL_KEYS, plus "provenance" (packio has no
#: provenance-in-frontmatter concept; the native mapping does).
_OPTIONAL_NEUTRON_KEYS = ("category", "tags", "requires_tools", "platforms", "acl_labels")


def _seq(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),)


def serialize_native(pack: SkillPack) -> str:
    """Serialize a pack to native ``.claude/skills`` SKILL.md text.

    Top-level frontmatter keys are ONLY ``name``/``description``/``version``.
    Everything Neutron-specific nests under ``metadata.neutron``: ``source`` and
    ``triggers`` (order PRESERVED — never sorted) are always present; the
    remaining optional fields are omitted when equal to their empty default.
    """
    neutron: dict[str, Any] = {
        "source": pack.source.value,
        "triggers": list(pack.triggers),
    }
    for key in _OPTIONAL_NEUTRON_KEYS:
        value = getattr(pack, key)
        if key == "category":
            if value:
                neutron[key] = value
        elif value:
            neutron[key] = list(value)
    provenance = pack.model_dump(mode="json")["provenance"]
    if provenance:
        neutron["provenance"] = provenance

    front: dict[str, Any] = {
        "name": pack.name,
        "description": pack.description,
        "version": pack.version,
        "metadata": {"neutron": neutron},
    }
    front_text = yaml.safe_dump(front, sort_keys=True, default_flow_style=False).rstrip("\n")
    return f"---\n{front_text}\n---\n{pack.body}"


def parse_native(text: str) -> SkillPack:
    """Parse native SKILL.md text into a :class:`SkillPack`.

    Reuses ``ne.skills.packio.parse_skill_md`` for the frontmatter/body split
    (``yaml.safe_load`` ONLY — never ``yaml.load``). Raises ``ValueError`` with a
    clear message on any structural or field-validation failure; this is a
    round-trip/import parser, not the violations-not-exceptions admission gate.
    """
    try:
        frontmatter, body = parse_skill_md(text)
    except yaml.YAMLError as exc:
        # parse_skill_md lets yaml.safe_load's error propagate on malformed
        # frontmatter; normalize it into this parser's documented ValueError API
        # so callers handling import failures as ValueError don't see a YAMLError.
        raise ValueError(f"ne.skills_cc.native: malformed YAML frontmatter: {exc}") from exc
    if not frontmatter:
        raise ValueError("ne.skills_cc.native: no frontmatter block found")
    if not isinstance(frontmatter, Mapping):
        raise ValueError("ne.skills_cc.native: frontmatter is not a mapping")

    missing = [k for k in ("name", "description", "version") if k not in frontmatter]
    if missing:
        raise ValueError(f"ne.skills_cc.native: missing top-level key(s): {missing}")

    metadata = frontmatter.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("ne.skills_cc.native: 'metadata' must be a mapping")
    neutron = metadata.get("neutron") or {}
    if not isinstance(neutron, Mapping):
        raise ValueError("ne.skills_cc.native: 'metadata.neutron' must be a mapping")
    if "source" not in neutron:
        raise ValueError("ne.skills_cc.native: missing metadata.neutron.source")

    try:
        source = SkillSource(str(neutron["source"]))
    except ValueError as exc:
        raise ValueError(f"ne.skills_cc.native: invalid source: {neutron['source']!r}") from exc

    try:
        return SkillPack(
            name=str(frontmatter["name"]),
            version=int(frontmatter["version"]),
            description=str(frontmatter["description"]),
            triggers=_seq(neutron.get("triggers")),
            body=body,
            source=source,
            category=str(neutron.get("category") or ""),
            tags=_seq(neutron.get("tags")),
            requires_tools=_seq(neutron.get("requires_tools")),
            platforms=_seq(neutron.get("platforms")),
            acl_labels=_seq(neutron.get("acl_labels")),
            provenance=neutron.get("provenance") or {},
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ne.skills_cc.native: invalid pack fields: {exc}") from exc
