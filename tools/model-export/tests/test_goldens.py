# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Cosine math on known vectors, the committed golden TEXTS file's shape (16
unique non-empty entries incl. the Qwen3 instruct prefix), and the golden
VECTORS schema round-trip on synthetic data."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from ne.model_export import goldens as g

_GOLDEN_TEXTS_PATH = (
    Path(__file__).resolve().parents[1] / "golden" / "qwen3-embedding-0.6b.texts.json"
)


def test_cosine_identical_vectors_is_one() -> None:
    v = [1.0, 2.0, 3.0]
    assert g.cosine(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero() -> None:
    assert g.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_vectors_is_minus_one() -> None:
    assert g.cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_known_value() -> None:
    # cos(theta) between (1,1) and (1,0) = 1/sqrt(2)
    assert g.cosine([1.0, 1.0], [1.0, 0.0]) == pytest.approx(1.0 / math.sqrt(2))


def test_cosine_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        g.cosine([1.0, 2.0], [1.0])


def test_cosine_rejects_zero_norm_vector() -> None:
    with pytest.raises(ValueError, match="zero-norm"):
        g.cosine([0.0, 0.0], [1.0, 1.0])


def test_committed_golden_texts_file_has_16_unique_nonempty_entries() -> None:
    texts = g.load_golden_texts(_GOLDEN_TEXTS_PATH)
    assert len(texts) == 16
    assert len(set(texts)) == 16
    assert all(t.strip() for t in texts)


def test_committed_golden_texts_includes_the_qwen3_instruct_prefix() -> None:
    texts = g.load_golden_texts(_GOLDEN_TEXTS_PATH)
    instruct_texts = [t for t in texts if t.startswith("Instruct:")]
    assert len(instruct_texts) >= 4
    assert all("\nQuery:" in t for t in instruct_texts)


def test_load_golden_texts_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(g.GoldenValidationError, match="no golden texts file"):
        g.load_golden_texts(tmp_path / "absent.json")


def test_load_golden_texts_rejects_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "texts.json"
    path.write_text('{"texts": ["a", "a"]}', encoding="utf-8")
    with pytest.raises(g.GoldenValidationError, match="duplicate"):
        g.load_golden_texts(path)


def test_load_golden_texts_rejects_empty_string(tmp_path: Path) -> None:
    path = tmp_path / "texts.json"
    path.write_text('{"texts": ["a", "  "]}', encoding="utf-8")
    with pytest.raises(g.GoldenValidationError, match="empty"):
        g.load_golden_texts(path)


def test_load_golden_texts_accepts_bare_list(tmp_path: Path) -> None:
    path = tmp_path / "texts.json"
    path.write_text('["a", "b"]', encoding="utf-8")
    assert g.load_golden_texts(path) == ["a", "b"]


def test_build_golden_vectors_and_round_trip(tmp_path: Path) -> None:
    payload = g.build_golden_vectors(
        texts=["a", "b"],
        vectors=[[1.0, 0.0], [0.0, 1.0]],
        dim=2,
        source_revision="a" * 40,
    )
    path = tmp_path / "vectors.json"
    g.save_golden_vectors(payload, path)
    loaded = g.load_golden_vectors(path)
    assert loaded == payload
    assert loaded["dim"] == 2
    assert loaded["source_revision"] == "a" * 40


def test_build_golden_vectors_rejects_length_mismatch() -> None:
    with pytest.raises(g.GoldenValidationError, match="length mismatch"):
        g.build_golden_vectors(texts=["a", "b"], vectors=[[1.0]], dim=1, source_revision="x")


def test_build_golden_vectors_rejects_dim_mismatch() -> None:
    with pytest.raises(g.GoldenValidationError, match="dim"):
        g.build_golden_vectors(texts=["a"], vectors=[[1.0, 2.0]], dim=1, source_revision="x")


def test_load_golden_vectors_rejects_missing_keys(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"texts": [], "vectors": []}', encoding="utf-8")
    with pytest.raises(g.GoldenValidationError, match="missing required keys"):
        g.load_golden_vectors(path)
