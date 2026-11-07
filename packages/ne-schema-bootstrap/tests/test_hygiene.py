# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``ne.schema_bootstrap.hygiene`` (local credential-scrub copy)."""

from __future__ import annotations

import ast
from pathlib import Path

from ne.schema_bootstrap.hygiene import scrub_text

_REDACTED = "[REDACTED]"


def _extract_patterns_and_redacted(source: str) -> tuple[list[str], str | None]:
    """AST-extract the ordered ``_PATTERNS`` regex string literals (each a
    ``re.compile(...)`` call argument) and the ``_REDACTED`` string constant
    from a hygiene-module source file."""
    tree = ast.parse(source)
    patterns: list[str] = []
    redacted: str | None = None
    for node in ast.walk(tree):
        # _PATTERNS and _REDACTED are both annotated assignments
        # (``ast.AnnAssign``: ``_PATTERNS: tuple[...] = (...)``), not plain
        # ``ast.Assign`` — handle both target shapes.
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        else:
            continue

        if "_PATTERNS" in targets and isinstance(value, ast.Tuple):
            for element in value.elts:
                if (
                    isinstance(element, ast.Call)
                    and isinstance(element.func, ast.Attribute)
                    and element.func.attr == "compile"
                    and element.args
                    and isinstance(element.args[0], ast.Constant)
                    and isinstance(element.args[0].value, str)
                ):
                    patterns.append(element.args[0].value)
        if (
            "_REDACTED" in targets
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            redacted = value.value
    return patterns, redacted


def test_aws_access_key_redacted() -> None:
    text = "creds: AKIAABCDEFGHIJKLMNOP end"
    assert _REDACTED in scrub_text(text, max_chars=200)
    assert "AKIAABCDEFGHIJKLMNOP" not in scrub_text(text, max_chars=200)


def test_github_token_redacted() -> None:
    text = "token ghp_abcdefghijklmnopqrstuvwxyz012345 in the clear"
    scrubbed = scrub_text(text, max_chars=200)
    assert _REDACTED in scrubbed
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in scrubbed


def test_slack_token_redacted() -> None:
    text = "slack xoxb-1234567890-abcdefghijklmno leaked"
    scrubbed = scrub_text(text, max_chars=200)
    assert _REDACTED in scrubbed
    assert "xoxb-1234567890-abcdefghijklmno" not in scrubbed


def test_bearer_header_redacted() -> None:
    text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789"
    scrubbed = scrub_text(text, max_chars=200)
    assert _REDACTED in scrubbed
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in scrubbed


def test_jwt_redacted() -> None:
    text = "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQ-abc123"
    scrubbed = scrub_text(text, max_chars=200)
    assert _REDACTED in scrubbed
    assert "eyJhbGciOiJIUzI1NiJ9" not in scrubbed


def test_pem_block_redacted() -> None:
    text = "-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkq\n-----END PRIVATE KEY-----"
    scrubbed = scrub_text(text, max_chars=200)
    assert _REDACTED in scrubbed
    assert "MIIBVQIBADANBgkq" not in scrubbed


def test_dangling_pem_begin_with_no_end_redacted() -> None:
    text = "-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkq (truncated export)"
    scrubbed = scrub_text(text, max_chars=200)
    assert _REDACTED in scrubbed
    assert "MIIBVQIBADANBgkq" not in scrubbed


def test_key_value_credential_assignment_redacted() -> None:
    text = "config line: password=hunter2superSecret"
    scrubbed = scrub_text(text, max_chars=200)
    assert _REDACTED in scrubbed
    assert "hunter2superSecret" not in scrubbed


def test_negative_control_ordinary_sentence_passes_through_byte_identical() -> None:
    text = "The quarterly report is due next Tuesday at noon."
    assert scrub_text(text, max_chars=200) == text


def test_negative_control_email_shaped_string_passes_through_byte_identical() -> None:
    text = "contact ada@example.com for details"
    assert scrub_text(text, max_chars=200) == text


def test_pattern_set_matches_flywheel_copy() -> None:
    # This package's hygiene.py is a DELIBERATE LOCAL COPY of
    # ne-flywheel's pattern set (design §3.3), not an import — so the two
    # copies can silently drift. This test converts that normative claim
    # from a docstring into a hard, non-skippable assertion: both files are
    # workspace-resident, so there is no reason to skip. AST-extracts the
    # ordered _PATTERNS regex literals + _REDACTED from both copies and
    # asserts exact ordered equality.
    local_source = (
        Path(__file__).resolve().parents[1] / "src" / "ne" / "schema_bootstrap" / "hygiene.py"
    )
    flywheel_source = (
        Path(__file__).resolve().parents[2]
        / "ne-flywheel"
        / "src"
        / "ne"
        / "flywheel"
        / "hygiene.py"
    )
    assert local_source.is_file(), f"local hygiene module not found at {local_source}"
    assert flywheel_source.is_file(), f"ne-flywheel hygiene module not found at {flywheel_source}"

    local_patterns, local_redacted = _extract_patterns_and_redacted(local_source.read_text())
    flywheel_patterns, flywheel_redacted = _extract_patterns_and_redacted(
        flywheel_source.read_text()
    )

    # Non-vacuity control: the extractor actually found something to compare.
    assert len(local_patterns) >= 5

    assert local_patterns == flywheel_patterns, (
        "ne-schema-bootstrap's hygiene._PATTERNS has drifted from ne-flywheel's copy"
    )
    assert local_redacted == flywheel_redacted == _REDACTED


def test_truncation_happens_after_redaction_not_mid_secret() -> None:
    # A long AWS key positioned so that a naive truncate-first-then-scrub
    # would leave half the raw key exposed in the output.
    secret_key = "AKIA" + "B" * 16
    text = "prefix " + secret_key + " suffix-padding-that-pushes-past-the-cut"
    max_chars = 20  # shorter than the raw secret itself

    scrubbed = scrub_text(text, max_chars=max_chars)

    assert secret_key not in scrubbed
    # No half-leaked fragment of the raw key survives truncation either.
    assert "AKIABBBBBBBBBBBBBBBB"[:10] not in scrubbed
    assert len(scrubbed) <= max_chars
