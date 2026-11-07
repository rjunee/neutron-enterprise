# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Structural ratchet tests for the monorepo scaffold (SPEC section 3, plan section 1.4)."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# dist name -> import module segment (hyphen packages map to underscore modules).
SPEC_PACKAGES: dict[str, str] = {
    "ne-contracts": "contracts",
    "ne-schema": "schema",
    "ne-gateway": "gateway",
    "ne-serve": "serve",
    "ne-brain": "brain",
    "ne-brain-graph": "brain_graph",
    "ne-brain-ontology": "brain_ontology",
    "ne-permissions": "permissions",
    "ne-runtime": "runtime",
    "ne-govern": "govern",
    "ne-audit": "audit",
    "ne-connect": "connect",
    "ne-mcp": "mcp",
    "ne-eval": "eval",
    "ne-obs": "obs",
    "ne-flywheel": "flywheel",
    "ne-identity": "identity",
    "ne-secrets": "secrets",
}


def test_all_spec_packages_present() -> None:
    assert len(SPEC_PACKAGES) == 18
    for dist, mod in SPEC_PACKAGES.items():
        base = REPO_ROOT / "packages" / dist
        assert (base / "pyproject.toml").is_file(), f"{dist} missing pyproject.toml"
        assert (base / "README.md").is_file(), f"{dist} missing README.md"
        assert (base / "NOTICE").is_file(), f"{dist} missing NOTICE"
        pkg = base / "src" / "ne" / mod
        assert (pkg / "__init__.py").is_file(), f"{dist} missing __init__.py"
        assert (pkg / "py.typed").is_file(), f"{dist} missing py.typed"


def test_workspace_members_are_globs() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    workspace = data["tool"]["uv"]["workspace"]
    assert workspace["members"] == ["packages/*", "apps/*", "tools/*"]
    assert workspace["exclude"] == ["apps/console"]


def test_no_root_notice_file() -> None:
    assert not (REPO_ROOT / "NOTICE").exists()


def test_ne_namespace_has_no_init() -> None:
    for dist in SPEC_PACKAGES:
        stray = REPO_ROOT / "packages" / dist / "src" / "ne" / "__init__.py"
        assert not stray.exists(), f"stray namespace __init__.py in {dist}"
    assert not (REPO_ROOT / "tools" / "ne-cli" / "src" / "ne" / "__init__.py").exists()
