# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the `ne schema` CLI group (generate / check)."""

from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

from ne.cli import main

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_schema_help_lists_subcommands() -> None:
    result = CliRunner().invoke(main, ["schema", "--help"])
    assert result.exit_code == 0
    assert "generate" in result.output
    assert "check" in result.output
    assert "bootstrap" in result.output


def test_schema_check_clean_on_repo() -> None:
    cwd = Path.cwd()
    try:
        os.chdir(REPO_ROOT)
        result = CliRunner().invoke(main, ["schema", "check"])
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_schema_check_reports_stale(monkeypatch) -> None:
    from ne.schema import codegen

    monkeypatch.setattr(codegen, "check_freshness", lambda root: ["operational.py"])
    result = CliRunner().invoke(main, ["schema", "check"])
    assert result.exit_code == 1
    assert "operational.py" in result.output
    assert "ne schema generate" in result.output
