# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Packaging-metadata regression test (Codex P2-B, NE-069 continuation).

The `ne release fingerprint`/`ne release bundle` commands must be importable
on a NORMAL (non-dev) install — so ne-contracts/ne-runtime/ne-govern must live
in ne-cli's `[project].dependencies`, not only in a dev dependency-group.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _dep_names(deps: list[str]) -> set[str]:
    return {re.split(r"[\s\[<>=!~;]", d.strip(), maxsplit=1)[0].lower() for d in deps}


def test_release_command_deps_are_runtime_dependencies() -> None:
    data = tomllib.loads(PYPROJECT.read_text())
    runtime = _dep_names(data["project"]["dependencies"])
    assert {"ne-contracts", "ne-runtime", "ne-govern"} <= runtime
