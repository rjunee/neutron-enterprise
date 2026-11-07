# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ``.claude/skills`` filesystem projection: set-replace, C6 tripwire, atomicity."""

from __future__ import annotations

from pathlib import Path

import pytest

from ne.skills_cc import store
from ne.skills_cc.native import parse_native

from .conftest import make_pack


def test_write_surface_then_list_surface(tmp_path: Path) -> None:
    packs = [make_pack("alpha"), make_pack("bravo"), make_pack("charlie")]
    store.write_surface(tmp_path, packs, surface_key="surf-1")
    assert store.list_surface(tmp_path, "surf-1") == {"alpha", "bravo", "charlie"}


def test_set_replace_removes_extra_dir(tmp_path: Path) -> None:
    packs = [make_pack("alpha"), make_pack("bravo"), make_pack("charlie")]
    store.write_surface(tmp_path, packs, surface_key="surf-1")
    store.write_surface(tmp_path, [make_pack("alpha")], surface_key="surf-1")
    assert store.list_surface(tmp_path, "surf-1") == {"alpha"}
    base = store.surface_dir(tmp_path, "surf-1")
    assert not (base / "bravo").exists()
    assert not (base / "charlie").exists()


def test_remove_names_removes_only_named(tmp_path: Path) -> None:
    packs = [make_pack("alpha"), make_pack("bravo"), make_pack("charlie")]
    store.write_surface(tmp_path, packs, surface_key="surf-1")
    store.remove_names(tmp_path, ["bravo"], surface_key="surf-1")
    assert store.list_surface(tmp_path, "surf-1") == {"alpha", "charlie"}


def test_remove_names_silent_on_missing_name(tmp_path: Path) -> None:
    store.write_surface(tmp_path, [make_pack("alpha")], surface_key="surf-1")
    store.remove_names(tmp_path, ["does-not-exist"], surface_key="surf-1")
    assert store.list_surface(tmp_path, "surf-1") == {"alpha"}


def test_remove_names_silent_on_missing_surface(tmp_path: Path) -> None:
    # No prior materialize on this surface at all — must not raise.
    store.remove_names(tmp_path, ["alpha"], surface_key="never-materialized")
    assert store.list_surface(tmp_path, "never-materialized") == set()


def test_skill_dir_contains_exactly_skill_md(tmp_path: Path) -> None:
    store.write_surface(tmp_path, [make_pack("alpha")], surface_key="surf-1")
    skill_dir = store.surface_dir(tmp_path, "surf-1") / "alpha"
    entries = list(skill_dir.iterdir())
    assert [e.name for e in entries] == ["SKILL.md"]
    assert entries[0].is_file()


def test_on_disk_skill_md_reparses_to_identical_pack(tmp_path: Path) -> None:
    pack = make_pack("alpha", acl_labels=("dept:eng",))
    store.write_surface(tmp_path, [pack], surface_key="surf-1")
    skill_md = store.surface_dir(tmp_path, "surf-1") / "alpha" / "SKILL.md"
    reparsed = parse_native(skill_md.read_text(encoding="utf-8"))
    assert reparsed == pack
    assert reparsed.content_hash == pack.content_hash


def test_surface_key_is_quote_encoded_no_traversal(tmp_path: Path) -> None:
    hostile_key = "session:user@dept/x"
    store.write_surface(tmp_path, [make_pack("alpha")], surface_key=hostile_key)
    base = store.surface_dir(tmp_path, hostile_key)
    # The quoted key is a SINGLE path component directly under 'surfaces/'.
    quoted_component = base.relative_to(tmp_path / "surfaces").parts[0]
    assert "/" not in quoted_component
    assert (tmp_path / "surfaces" / quoted_component).is_dir()
    assert store.list_surface(tmp_path, hostile_key) == {"alpha"}


def test_surface_key_dot_segment_rejected(tmp_path: Path) -> None:
    # "." and ".." are RFC 3986 unreserved so quote() passes them through
    # unchanged; without an explicit reject these would resolve OUTSIDE
    # surfaces/ (root/surfaces/.. == root). Both must fail closed.
    for hostile_key in (".", ".."):
        with pytest.raises(ValueError, match="dot-segment"):
            store.surface_dir(tmp_path, hostile_key)
        with pytest.raises(ValueError, match="dot-segment"):
            store.write_surface(tmp_path, [make_pack("alpha")], surface_key=hostile_key)
    # No escape onto tmp_path itself: no unexpected .claude dir appears there.
    assert not (tmp_path / ".claude").exists()


def test_materialize_scrubs_stale_artifacts_from_retained_dir(tmp_path: Path) -> None:
    # First materialize creates the skill dir normally.
    store.write_surface(tmp_path, [make_pack("alpha")], surface_key="surf-1")
    skill_dir = store.surface_dir(tmp_path, "surf-1") / "alpha"

    # Simulate a stale/planted executable artifact surviving in the RETAINED
    # directory (out-of-band write, prior bug, etc.) — the pack is still in
    # the set on the next materialize, so the dir is retained, not recreated
    # from scratch by the "remove extras" pass.
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (skill_dir / "notes.txt").write_text("stale", encoding="utf-8")

    # Re-materialize the SAME pack name — the dir is retained, but must be
    # scrubbed back to exactly SKILL.md (C6 procedural-only guarantee).
    store.write_surface(tmp_path, [make_pack("alpha")], surface_key="surf-1")
    entries = sorted(e.name for e in skill_dir.iterdir())
    assert entries == ["SKILL.md"]


def test_overwrite_goes_through_replace_no_tmp_litter(tmp_path: Path) -> None:
    pack_v1 = make_pack("alpha", description="version one")
    store.write_surface(tmp_path, [pack_v1], surface_key="surf-1")
    skill_dir = store.surface_dir(tmp_path, "surf-1") / "alpha"
    first_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    pack_v2 = make_pack("alpha", description="version two")
    store.write_surface(tmp_path, [pack_v2], surface_key="surf-1")
    second_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    assert first_text != second_text
    assert "version two" in second_text
    entries = sorted(e.name for e in skill_dir.iterdir())
    assert entries == ["SKILL.md"]
