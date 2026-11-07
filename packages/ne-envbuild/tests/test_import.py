# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Leaf guard for ne-envbuild (C3/C4): import pulls no heavy / first-party deps."""

from __future__ import annotations

import subprocess
import sys


def test_import_and_version() -> None:
    import ne.envbuild

    assert ne.envbuild.__version__ == "0.1.0"


def test_public_exports_present() -> None:
    import ne.envbuild

    expected = {
        "DEFAULT_LOCK_TIMEOUT_S",
        "DEFAULT_PYTHON_VERSION",
        "ENV_LOCK_SUFFIX",
        "ENV_MARKER",
        "SIDECAR_VENV_ROOT_VAR",
        "UV_CACHE_DIR_LEAF",
        "UV_CACHE_DIR_VAR",
        "UV_PYTHON_DIR_LEAF",
        "UV_PYTHON_INSTALL_DIR_VAR",
        "UvRunner",
        "check_exec_capable",
        "ensure_sidecar_venv",
        "env_lock_path",
        "marker_path",
        "read_marker",
        "requirements_sha256",
        "sidecar_venv_dir",
        "uv_cache_dir",
        "uv_python_install_dir",
        "venv_python",
        "write_marker_atomic",
    }
    assert expected == set(ne.envbuild.__all__)
    for name in expected:
        assert hasattr(ne.envbuild, name), name


def test_import_pulls_no_provider_or_firstparty_sdk() -> None:
    """C3/C4: a fresh ``import ne.envbuild`` must stay a filelock-only leaf.

    Checked in a clean subprocess so an unrelated test importing a heavy module
    cannot pollute the assertion.
    """
    banned = (
        "litellm",
        "openai",
        "httpx",
        "ne.gateway",
        "ne.brain",
        "ne.govern",
        "ne.schema",
        "ne.net",
    )
    code = (
        "import ne.envbuild, ne.envbuild.sidecar, sys; "
        f"present = [m for m in {banned!r} if m in sys.modules]; "
        "print(present)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout
