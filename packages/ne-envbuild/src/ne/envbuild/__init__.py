# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-envbuild — the ONE consolidated sidecar-venv env-builder leaf (NE-CKC8).

Three platform-side drivers each build a heavy dependency closure into an
isolated hash-pinned ``uv`` virtualenv and skip the rebuild via a stored
``.requirements.sha256`` marker: ne-brain's docling parser, ne-govern's presidio
backend, and ne-schema's linkml codegen. The fast-path marker probe used to be a
TOCTOU (a non-atomic ``write_text`` observed mid-flight by an ``is_file ->
read_text`` probe could yield a false "up-to-date" skip against OLD requirements,
or a redundant rebuild; codegen had no lock at all). This leaf fixes it once:
atomic ``os.replace`` marker write, fail-safe marker read, and an authoritative
under-lock re-check of the skip decision.

It is a LEAF: its only third-party dependency is ``filelock`` (already shared by
ne-brain and ne-govern), so it adds NO heavy-dependency drag (constraint C3) and
NO new third-party component (C4). Leaf-ness is enforced by the
``ne-envbuild-leaf`` import-linter contract and the subprocess import guard test.
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.envbuild.sidecar import (
    DEFAULT_LOCK_TIMEOUT_S,
    DEFAULT_PYTHON_VERSION,
    ENV_LOCK_SUFFIX,
    ENV_MARKER,
    SIDECAR_VENV_ROOT_VAR,
    UV_CACHE_DIR_LEAF,
    UV_CACHE_DIR_VAR,
    UV_PYTHON_DIR_LEAF,
    UV_PYTHON_INSTALL_DIR_VAR,
    UvRunner,
    check_exec_capable,
    ensure_sidecar_venv,
    env_lock_path,
    marker_path,
    read_marker,
    requirements_sha256,
    sidecar_venv_dir,
    uv_cache_dir,
    uv_python_install_dir,
    venv_python,
    write_marker_atomic,
)

__all__ = [
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
]
