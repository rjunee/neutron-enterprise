# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ``.claude/skills`` filesystem projection (the ``materialize``/``withdraw`` half).

Layout: ``<root>/surfaces/<quoted-surface-key>/.claude/skills/<pack-name>/SKILL.md``.
``surface_key`` is URL-quoted (``urllib.parse.quote(..., safe="")``) into a
single, filesystem-safe, REVERSIBLE path component — deterministic (the same
key always maps to the same directory) and traversal-safe (a key containing
``/`` or ``..`` can never escape ``surfaces/``, because quoting turns every
non-alnum byte, including ``/``, into a ``%XX`` escape).

C6 (procedural-skills-only): a skill directory holds EXACTLY one file, its
``SKILL.md`` — no ``scripts/``, no executable artifacts, no exec bits set.
Nothing in this module ever writes a second file into a skill directory.

The registry (in the pydantic-ai binding) is the source of truth for what an
active set IS; this store is a pure PROJECTION of an already-decided pack list
onto a directory tree — it makes no authorization decision (ONE-ACL §7.7).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

from ne.contracts.skills import SkillPack
from ne.skills_cc.native import serialize_native

_SKILL_MD = "SKILL.md"


def surface_dir(root: Path, surface_key: str) -> Path:
    """The ``.claude/skills`` directory for one discovery surface.

    ``quote(surface_key, safe="")`` is deterministic and reversible (a given
    key always yields the same, single path component) and filesystem-safe (no
    ``/`` or other path-separator byte survives unescaped, so a hostile
    ``surface_key`` cannot traverse outside ``surfaces/``).

    ``.`` and ``..`` are RFC 3986 unreserved characters, so ``quote`` passes
    them through unchanged: a ``surface_key`` of exactly ``"."`` or ``".."``
    would otherwise quote to a bare dot-segment and resolve OUTSIDE
    ``surfaces/`` (e.g. ``surface_key=".."`` -> ``root/surfaces/../.claude``
    == ``root/.claude``). Since only fully-unreserved input can quote to a
    pure dot-segment in the first place, rejecting those two exact values
    closes the traversal without weakening quoting for any other key.
    """
    quoted = quote(surface_key, safe="")
    if quoted in {".", ".."}:
        raise ValueError(
            f"surface_key {surface_key!r} quotes to a path dot-segment "
            f"({quoted!r}); this would traverse outside surfaces/"
        )
    return root / "surfaces" / quoted / ".claude" / "skills"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_surface(root: Path, packs: Sequence[SkillPack], surface_key: str) -> None:
    """SET-REPLACE the given packs onto ``surface_key`` (mirrors ``upsert_surface``).

    Each pack's ``SKILL.md`` is written atomically (tempfile + ``os.replace``);
    any OTHER child directory of the surface not in the current pack-name set
    is removed. Writes ONLY ``SKILL.md`` — no scripts, no exec artifacts (C6).

    C6 sanitization: a RETAINED skill directory (one that survives from a
    prior materialize because its pack is still in the set) is scrubbed of
    every entry other than the freshly-written ``SKILL.md`` too. Only
    overwriting ``SKILL.md`` in place would let a stale ``scripts/`` or other
    executable artifact — planted by an out-of-band write, a bug, or a prior
    build of this binding — survive re-materialization indefinitely; that
    would silently break the procedural-only guarantee this store exists to
    enforce.
    """
    base = surface_dir(root, surface_key)
    base.mkdir(parents=True, exist_ok=True)
    keep = {p.name for p in packs}

    for pack in packs:
        skill_dir = base / pack.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(skill_dir / _SKILL_MD, serialize_native(pack))
        for entry in skill_dir.iterdir():
            if entry.name == _SKILL_MD:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()

    for child in base.iterdir():
        if child.is_dir() and child.name not in keep:
            shutil.rmtree(child)


def remove_names(root: Path, names: Sequence[str], surface_key: str) -> None:
    """Remove named skill directories from ``surface_key`` (silent no-op if absent)."""
    base = surface_dir(root, surface_key)
    if not base.is_dir():
        return
    for name in names:
        skill_dir = base / name
        if skill_dir.is_dir():
            shutil.rmtree(skill_dir)


def list_surface(root: Path, surface_key: str) -> set[str]:
    """The pack names currently discoverable on ``surface_key`` (empty if absent)."""
    base = surface_dir(root, surface_key)
    if not base.is_dir():
        return set()
    return {
        child.name for child in base.iterdir() if child.is_dir() and (child / _SKILL_MD).is_file()
    }
