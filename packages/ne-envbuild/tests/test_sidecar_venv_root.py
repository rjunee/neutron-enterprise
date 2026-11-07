# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SVENV: pin the writable-root ``sidecar_venv_dir`` two-branch contract.

Hardened/non-root/read-only-rootfs deploys install into a root-owned, read-only
site-packages tree, so a runtime (re)build of a missing/stale sidecar venv at the
in-package default fails. ``sidecar_venv_dir`` resolves the venv to
``$NE_SIDECAR_VENV_ROOT/<leaf>`` when that env var is set-and-nonempty, else the
historical in-package sibling ``env_dir.parent/<leaf>`` (default byte-identical).

This suite pins BOTH resolution branches, the empty-string-is-unset rule, the
rmtree-surviving lock-sibling invariant AT the resolved path, and the
PRE-STAGING↔RUNTIME contract: a venv pre-built where the runtime resolves it makes
``ensure_sidecar_venv`` a silent no-op. It also mechanically pins that all five
pre-staging READMEs document the env var. Every test toggles the env var ONLY via
pytest monkeypatch (hermetic; never mutates ``os.environ`` directly).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ne.envbuild import (
    SIDECAR_VENV_ROOT_VAR,
    ensure_sidecar_venv,
    env_lock_path,
    requirements_sha256,
    sidecar_venv_dir,
    venv_python,
    write_marker_atomic,
)
from ne.envbuild.sidecar import marker_path


def test_env_unset_resolves_in_package_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SIDECAR_VENV_ROOT_VAR, raising=False)
    env_dir = tmp_path / "pkg" / "x_env"
    assert sidecar_venv_dir(env_dir, ".x-venv") == env_dir.parent / ".x-venv"


def test_env_empty_resolves_in_package_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SIDECAR_VENV_ROOT_VAR, "")
    env_dir = tmp_path / "pkg" / "x_env"
    assert sidecar_venv_dir(env_dir, ".x-venv") == env_dir.parent / ".x-venv"


def test_env_set_resolves_under_writable_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "writable"
    monkeypatch.setenv(SIDECAR_VENV_ROOT_VAR, str(root))
    env_dir = tmp_path / "pkg" / "x_env"
    a = sidecar_venv_dir(env_dir, ".a-venv")
    b = sidecar_venv_dir(env_dir, ".b-venv")
    assert a == root / ".a-venv"
    assert b == root / ".b-venv"
    assert a != b  # distinct leaves resolve to distinct dirs under one root


def test_lock_path_is_sibling_inside_writable_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "writable"
    monkeypatch.setenv(SIDECAR_VENV_ROOT_VAR, str(root))
    env_dir = tmp_path / "pkg" / "x_env"
    venv_dir = sidecar_venv_dir(env_dir, ".x-venv")
    # The lock lands beside the venv INSIDE the root (survives rmtree(venv_dir)).
    assert env_lock_path(venv_dir) == root / ".x-venv.lock"


def test_prebuilt_venv_at_writable_root_is_fast_path_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A venv pre-built where the runtime resolves it makes ensure a silent no-op.

    This is the pre-staging↔runtime contract: build at the SAME resolved path the
    runtime computes and no ``uv`` egress ever happens.
    """
    root = tmp_path / "writable"
    monkeypatch.setenv(SIDECAR_VENV_ROOT_VAR, str(root))
    env_dir = tmp_path / "pkg" / "x_env"
    env_dir.mkdir(parents=True)
    (env_dir / "requirements.txt").write_text("some-pkg==1.2.3\n", encoding="utf-8")

    venv_dir = sidecar_venv_dir(env_dir, ".x-venv")
    assert venv_dir == root / ".x-venv"
    # Pre-stage a current venv at the resolved path: bin/python + matching marker.
    venv_python(venv_dir).parent.mkdir(parents=True)
    venv_python(venv_dir).write_text("", encoding="utf-8")
    want = hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()
    write_marker_atomic(marker_path(venv_dir), want)
    assert want == requirements_sha256(env_dir)

    calls: list[list[str]] = []
    ensure_sidecar_venv(
        env_dir=env_dir, venv_dir=venv_dir, run=lambda cmd, env=None: calls.append(cmd)
    )
    assert calls == []  # already-current pre-built venv → zero build side effects


def test_readmes_document_writable_root() -> None:
    """All five pre-staging READMEs must document NE_SIDECAR_VENV_ROOT."""
    current = Path(__file__).resolve()
    repo_root = next(c for c in (current, *current.parents) if (c / "uv.lock").is_file())
    readmes = [
        repo_root / "packages/ne-brain-graph/splink_env/README.md",
        repo_root / "packages/ne-brain-graph/lightrag_env/README.md",
        repo_root / "packages/ne-govern/presidio_env/README.md",
        repo_root / "packages/ne-memory-graph/graphiti_env/README.md",
        repo_root / "packages/ne-brain/parser_env/README.md",
    ]
    for readme in readmes:
        assert readme.is_file(), f"missing pre-staging README: {readme}"
        assert SIDECAR_VENV_ROOT_VAR in readme.read_text(encoding="utf-8"), (
            f"{readme} does not document {SIDECAR_VENV_ROOT_VAR}"
        )
