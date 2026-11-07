# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CLI generation/check behavior and checkout-first fixture resolution."""

from __future__ import annotations

from pathlib import Path

from ne.meridian import cli

from .conftest import FIXTURES


def test_default_fixtures_prefers_checkout() -> None:
    assert cli.default_fixtures_dir() == FIXTURES


def test_generate_then_check_round_trip(tmp_path: Path) -> None:
    assert cli.main(["generate", "--out", str(tmp_path)]) == 0
    assert cli.main(["check", "--fixtures", str(tmp_path)]) == 0
    assert (tmp_path / "assets" / "debit-card-dispute-handling-sop-v4.docx").is_file()
