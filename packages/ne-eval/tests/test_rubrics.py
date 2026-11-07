# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Rubric loading, packaging, content-hash, and rendering tests."""

from __future__ import annotations

import hashlib
from importlib.resources import files

import pytest

from ne.eval.errors import RubricError
from ne.eval.rubrics import get_rubric, load_rubrics, render


def test_all_four_rubrics_packaged() -> None:
    rubrics = load_rubrics()
    ids = {r.id for r in rubrics}
    assert ids == {
        "faithfulness_statements",
        "faithfulness_verdicts",
        "context_precision",
        "context_recall",
    }
    for r in rubrics:
        assert r.version == "v1"


def test_content_hash_is_sha256_of_file_bytes() -> None:
    anchor = files("ne.eval").joinpath("rubrics")
    raw = anchor.joinpath("faithfulness_statements_v1.md").read_bytes()
    expected = hashlib.sha256(raw).hexdigest()
    assert get_rubric("faithfulness_statements").content_hash == expected


def test_content_hash_differs_for_different_text() -> None:
    r = get_rubric("faithfulness_statements")
    other = hashlib.sha256((r.template + " edited").encode("utf-8")).hexdigest()
    assert other != r.content_hash


def test_get_rubric_unknown_raises() -> None:
    with pytest.raises(RubricError, match="unknown rubric"):
        get_rubric("does_not_exist")


def test_render_fills_placeholders() -> None:
    r = get_rubric("faithfulness_statements")
    out = render(r, question="Q?", answer="A.")
    assert "Q?" in out
    assert "A." in out


def test_render_missing_placeholder_raises() -> None:
    r = get_rubric("faithfulness_statements")
    with pytest.raises(RubricError, match="missing placeholder"):
        render(r, question="Q?")
