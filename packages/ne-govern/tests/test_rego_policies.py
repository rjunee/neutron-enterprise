# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Validate the shipped Rego with the real ``opa`` binary (skip-if-unavailable).

LIVE-PARKABLE: the build host has no ``opa`` binary, so this module skips there.
Wherever ``opa`` is on PATH it runs ``opa test`` (the native unit tests) and
``opa eval`` per decision vector, so the Rego is validated end-to-end.
CI installs a SHA256-pinned opa 1.18.2 (see .github/workflows/ci.yml "Install OPA"
step), so these tests execute on every PR; the module-level skip applies only to
hosts without the binary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from .conftest import load_vectors

_OPA = shutil.which("opa")

if _OPA is None:
    pytest.skip(
        "LIVE-PARKABLE: opa binary not on PATH — install OPA to validate the Rego "
        "(`opa test packages/ne-govern/policies -v`).",
        allow_module_level=True,
    )

# Narrowed for the type checker; unreachable when the module skips above.
_OPA_BIN: str = _OPA or "opa"
_POLICIES = str(Path(__file__).parents[1] / "policies")
_VECTORS = load_vectors()


def test_opa_test_suite_passes() -> None:
    result = subprocess.run(
        [_OPA_BIN, "test", _POLICIES, "-v"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_opa_eval_matches_vector(vector: dict[str, Any]) -> None:
    result = subprocess.run(
        [
            _OPA_BIN,
            "eval",
            "-d",
            _POLICIES,
            "--stdin-input",
            "--format=json",
            "data.ne.govern.decision",
        ],
        input=json.dumps(vector["input"]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)["result"][0]["expressions"][0]["value"]
    expected = vector["expected"]
    assert value["effect"] == expected["effect"]
    assert value["tier"] == expected["tier"]
    assert value["obligations"] == expected["obligations"]
    assert value["policy_version"] == "1.5.0"
