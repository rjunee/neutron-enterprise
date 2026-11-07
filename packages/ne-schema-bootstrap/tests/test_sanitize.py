# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``ne.schema_bootstrap.sanitize`` — ports the ``diffs.py`` behaviors."""

from __future__ import annotations

from ne.schema_bootstrap import sanitize


def test_safe_inline_collapses_whitespace_kills_line_start_markdown() -> None:
    hostile = "normal text\n# Fake Heading\n\n```danger\nrm -rf /\n```"
    result = sanitize.safe_inline(hostile)
    assert "\n" not in result
    # The '#' survived the whitespace collapse but is escaped, so it can never
    # start a new line as a heading once re-embedded in a rendered document.
    assert "\\#" in result
    assert "Fake Heading" in result


def test_safe_inline_escape_set_exact() -> None:
    hostile = r"\`*_{}[]()<>#!|~@&" + "safe-chars.,:+-"
    result = sanitize.safe_inline(hostile)
    for ch in "\\`*_{}[]()<>#!|~@&":
        assert f"\\{ch}" in result
    # Deliberately-unescaped characters (line-start-significant only, already
    # neutralized by the whitespace collapse) render byte-identical.
    for ch in "-.,:+":
        assert ch in result
        assert f"\\{ch}" not in result


def test_safe_inline_drops_non_printables() -> None:
    hostile = "before\x00\x1b[31mred\x1b[0mafter"
    result = sanitize.safe_inline(hostile)
    assert "\x00" not in result
    assert "\x1b" not in result
    assert "before" in result
    assert "after" in result


def test_safe_inline_benign_text_unchanged_modulo_escapes() -> None:
    benign = "a normal exemplar value, with a comma and a dash-42."
    assert sanitize.safe_inline(benign) == benign


def test_fence_length_max_3_or_longest_run_plus_1() -> None:
    assert sanitize.fence("no backticks here") == "```"
    # A single backtick (run length 1) still yields the floor of 3 (`max(3, 2)`).
    assert sanitize.fence("one ` backtick") == "```"
    five_run = "a" + "`" * 5 + "b"
    assert sanitize.fence(five_run) == "`" * 6


def test_fenced_wraps_with_dynamic_fence_and_optional_lang() -> None:
    text = "```danger\nrm -rf /\n```"
    result = sanitize.fenced(text, lang="yaml")
    f = sanitize.fence(text)
    assert result == f"{f}yaml\n{text}\n{f}"
    # The fence (4 backticks) is strictly longer than the longest run inside
    # `text` (3 backticks) — the hostile embedded fence can never break out.
    assert len(f) == 4
    assert "```" in text


def test_fenced_no_lang_defaults_to_empty() -> None:
    text = "plain multi-line\ntext"
    result = sanitize.fenced(text)
    assert result.startswith("```\n")
    assert result.endswith("\n```")
