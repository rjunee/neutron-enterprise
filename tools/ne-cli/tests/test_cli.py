# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the `ne` CLI: version and header checks."""

from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

from ne.cli import main

REPO_ROOT = Path(__file__).resolve().parents[3]

HEADER = "# Copyright (c) 2026 Ryan Junee.\n# SPDX-License-Identifier: Apache-2.0\n"


def test_version_command() -> None:
    result = CliRunner().invoke(main, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == "0.1.0"


def test_check_headers_passes_with_header(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text(HEADER + '"""ok."""\n')
    result = CliRunner().invoke(main, ["check", "headers", str(good)])
    assert result.exit_code == 0


def test_check_headers_fails_without_header(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text('"""no header here."""\n')
    result = CliRunner().invoke(main, ["check", "headers", str(bad)])
    assert result.exit_code == 1
    assert "bad.py" in result.output


def test_check_headers_whole_repo() -> None:
    cwd = Path.cwd()
    try:
        os.chdir(REPO_ROOT)
        result = CliRunner().invoke(main, ["check", "headers"])
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.output
