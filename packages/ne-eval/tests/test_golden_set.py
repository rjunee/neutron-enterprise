# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Golden-set load/save/hash/management tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ne.eval.errors import GoldenSetError
from ne.eval.golden import (
    GoldenItem,
    GoldenSet,
    add_items,
    filter_by_tag,
    golden_set_hash,
    load_golden_set,
    save_golden_set,
)


def test_load_sample(sample_golden_path: Path) -> None:
    gs = load_golden_set(sample_golden_path)
    assert len(gs.items) == 6
    assert any(len(it.relevant_document_ids) == 2 for it in gs.items)
    assert any("permission-trap" in it.tags for it in gs.items)


def test_duplicate_id_raises(tmp_path: Path) -> None:
    p = tmp_path / "dup.jsonl"
    p.write_text(
        '{"id":"a","question":"q","reference_answer":"r"}\n'
        '{"id":"a","question":"q2","reference_answer":"r2"}\n',
        encoding="utf-8",
    )
    with pytest.raises(GoldenSetError, match="duplicate"):
        load_golden_set(p)


def test_blank_line_raises(tmp_path: Path) -> None:
    p = tmp_path / "blank.jsonl"
    p.write_text('{"id":"a","question":"q","reference_answer":"r"}\n\n', encoding="utf-8")
    with pytest.raises(GoldenSetError, match="blank"):
        load_golden_set(p)


def test_garbage_line_raises(tmp_path: Path) -> None:
    p = tmp_path / "garbage.jsonl"
    p.write_text("not json at all\n", encoding="utf-8")
    with pytest.raises(GoldenSetError, match="invalid JSON"):
        load_golden_set(p)


def test_empty_file_raises(tmp_path: Path) -> None:
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    with pytest.raises(GoldenSetError, match="empty"):
        load_golden_set(p)


def test_save_load_round_trip_byte_stable(sample_golden_path: Path, tmp_path: Path) -> None:
    gs = load_golden_set(sample_golden_path)
    out = tmp_path / "out.jsonl"
    save_golden_set(gs, out)
    reloaded = load_golden_set(out)
    out2 = tmp_path / "out2.jsonl"
    save_golden_set(reloaded, out2)
    assert out.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8")


def test_hash_stable_across_reordering() -> None:
    a = GoldenItem(id="a", question="qa", reference_answer="ra")
    b = GoldenItem(id="b", question="qb", reference_answer="rb")
    gs1 = GoldenSet(items=(a, b))
    gs2 = GoldenSet(items=(b, a))
    assert golden_set_hash(gs1) == golden_set_hash(gs2)


def test_hash_changes_when_field_changes() -> None:
    a = GoldenItem(id="a", question="qa", reference_answer="ra")
    a2 = GoldenItem(id="a", question="qa", reference_answer="DIFFERENT")
    assert golden_set_hash(GoldenSet(items=(a,))) != golden_set_hash(GoldenSet(items=(a2,)))


def test_add_items_rejects_dupes() -> None:
    a = GoldenItem(id="a", question="qa", reference_answer="ra")
    gs = GoldenSet(items=(a,))
    assert (
        len(add_items(gs, (GoldenItem(id="b", question="qb", reference_answer="rb"),)).items) == 2
    )
    with pytest.raises(GoldenSetError):
        add_items(gs, (GoldenItem(id="a", question="dup", reference_answer="x"),))


def test_filter_by_tag() -> None:
    a = GoldenItem(id="a", question="qa", reference_answer="ra", tags=("x",))
    b = GoldenItem(id="b", question="qb", reference_answer="rb", tags=("y",))
    gs = GoldenSet(items=(a, b))
    filtered = filter_by_tag(gs, "x")
    assert [it.id for it in filtered.items] == ["a"]


def test_invalid_id_rejected() -> None:
    with pytest.raises(ValueError, match="slug"):
        GoldenItem(id="bad id!", question="q", reference_answer="r")
