# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the release NOTICE aggregator (`ne release notice`)."""

from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

from ne.cli import main
from ne.cli.release import _member_dirs, aggregate_notice

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_aggregates_all_member_notices() -> None:
    content = aggregate_notice(REPO_ROOT)
    members = _member_dirs(REPO_ROOT)
    sections = [line for line in content.splitlines() if line.startswith("=== ")]
    # One section per workspace member, deterministic (sorted) order.
    assert len(sections) == len(members) >= 19
    assert "=== ne-cli ===" in sections
    assert "=== ne-contracts ===" in sections
    # Deterministic order: regenerating yields byte-identical output.
    assert aggregate_notice(REPO_ROOT) == content


def test_missing_notice_fails(tmp_path: Path) -> None:
    # A synthetic workspace: root pyproject + one member lacking a NOTICE.
    (tmp_path / "pyproject.toml").write_text("[tool.uv.workspace]\n", encoding="utf-8")
    member = tmp_path / "packages" / "ne-x"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text('[project]\nname = "ne-x"\n', encoding="utf-8")
    # No NOTICE written on purpose.
    cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        result = CliRunner().invoke(
            main, ["release", "notice", "--out", str(tmp_path / "dist" / "NOTICE")]
        )
    finally:
        os.chdir(cwd)
    assert result.exit_code != 0
    assert "ne-x" in result.output


def test_output_not_repo_root() -> None:
    cwd = Path.cwd()
    try:
        os.chdir(REPO_ROOT)
        result = CliRunner().invoke(main, ["release", "notice", "--out", "NOTICE"])
    finally:
        os.chdir(cwd)
    assert result.exit_code != 0
    assert "root" in result.output.lower()
    assert not (REPO_ROOT / "NOTICE").exists()
