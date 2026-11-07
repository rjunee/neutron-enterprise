# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The D-9/R-19 + C3 guardian: ne.skills_cc imports no substrate/provider library.

Mirrors ``packages/ne-skills/tests/test_import_boundary.py``. Runs in a FRESH
subprocess so a sibling test's dev-dep import cannot pollute the observation.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import importlib
for mod in (
    "ne.skills_cc",
    "ne.skills_cc.substrate",
    "ne.skills_cc.store",
    "ne.skills_cc.journal",
    "ne.skills_cc.native",
    "ne.skills_cc.subprocess_dispatch",
    "ne.skills_cc.errors",
):
    importlib.import_module(mod)
import sys
forbidden = [
    m
    for m in ("pydantic_ai", "dbos", "ne.runtime", "anthropic", "litellm", "openai")
    if m in sys.modules
]
assert not forbidden, f"ne.skills_cc leaked forbidden imports: {forbidden}"
print("OK")
"""


def test_cc_binding_imports_no_substrate_or_provider_library() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
