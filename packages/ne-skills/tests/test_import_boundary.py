# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The D-10 guardian: ne.skills core imports no substrate/runtime library.

Importing every ne.skills module must NOT drag ``pydantic_ai`` / ``dbos`` /
``ne.runtime`` into ``sys.modules`` — the stack-agnostic-core rule (R-19). This
runs in a FRESH subprocess so a sibling test's dev-dep import cannot pollute the
observation.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import importlib
for mod in (
    "ne.skills",
    "ne.skills.registry",
    "ne.skills.standard",
    "ne.skills.packio",
    "ne.skills.reconcile",
    "ne.skills.visibility",
    "ne.skills.active_set",
    "ne.skills.tools",
    "ne.skills.substrate",
    "ne.skills.config",
    "ne.skills.errors",
    "ne.skills.signature",
    "ne.skills.authoring",
    "ne.skills.worthiness",
    "ne.skills.suppression",
    "ne.skills.evals",
    "ne.skills.near_dup",
    "ne.skills.forge",
    "ne.skills.governed_forge",
    "ne.skills.apply",
):
    importlib.import_module(mod)
import sys
forbidden = [m for m in ("pydantic_ai", "dbos", "ne.runtime") if m in sys.modules]
assert not forbidden, f"ne.skills leaked forbidden imports: {forbidden}"
print("OK")
"""


def test_core_imports_no_substrate_library() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
