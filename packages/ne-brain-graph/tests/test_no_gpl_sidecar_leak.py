# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Sovereignty guard (C4): NO Tier-2 sidecar closure leaks into the platform lock.

Proves the grep-0 invariant — the Splink (GPL igraph) and LightRAG closures live
ONLY in their hash-pinned sidecar venvs and never enter the platform ``uv.lock``.
``ne check licenses`` scans ``uv.lock`` only, so it stays green. Mirrors the
``test_ragas_isolation`` precedent verbatim.
"""

from __future__ import annotations

import re
from pathlib import Path

_NAME_RE = re.compile(r'^name = "([^"]+)"', re.MULTILINE)

# The whole out-of-process sidecar surface that must never appear in uv.lock.
_FORBIDDEN = {
    "splink",
    "igraph",
    "python-igraph",
    "duckdb",
    "lightrag",
    "lightrag-hku",
    "pipmaster",
}


def _repo_root() -> Path:
    for candidate in (Path(__file__).resolve(), *Path(__file__).resolve().parents):
        if (candidate / "uv.lock").is_file():
            return candidate
    raise AssertionError("could not locate repo-root uv.lock")


def test_uv_lock_has_no_sidecar_gpl_closure() -> None:
    lock_text = (_repo_root() / "uv.lock").read_text(encoding="utf-8")
    names = set(_NAME_RE.findall(lock_text))
    leaked = sorted(names & _FORBIDDEN)
    assert leaked == [], f"Tier-2 sidecar closure leaked into the platform uv.lock: {leaked}"


def test_ne_brain_graph_pyproject_declares_no_sidecar_deps() -> None:
    import tomllib

    pyproject = _repo_root() / "packages" / "ne-brain-graph" / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = data["project"].get("dependencies", [])
    for dep in deps:
        assert not any(bad in dep.lower() for bad in _FORBIDDEN), dep
