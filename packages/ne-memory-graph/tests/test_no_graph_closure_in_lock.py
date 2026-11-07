# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Sovereignty guard (C4): the Graphiti/neo4j/posthog closure never enters uv.lock.

THE SPEC-MANDATED grep test (mirrors ``test_no_gpl_sidecar_leak.py``). ``ne check
licenses`` scans ``uv.lock`` only, so it stays green. openai / numpy are
deliberately NOT asserted absent — openai is legitimately in the platform lock via
litellm, numpy via fastembed (the ragas-test precedent).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_NAME_RE = re.compile(r'^name = "([^"]+)"', re.MULTILINE)

# The out-of-process Graphiti sidecar surface that must never appear in uv.lock.
_FORBIDDEN = {
    "graphiti-core",
    "graphiti",
    "neo4j",
    "posthog",
}


def _repo_root() -> Path:
    for candidate in (Path(__file__).resolve(), *Path(__file__).resolve().parents):
        if (candidate / "uv.lock").is_file():
            return candidate
    raise AssertionError("could not locate repo-root uv.lock")


def test_uv_lock_has_no_graph_closure() -> None:
    lock_text = (_repo_root() / "uv.lock").read_text(encoding="utf-8")
    names = set(_NAME_RE.findall(lock_text))
    leaked = sorted(names & _FORBIDDEN)
    assert leaked == [], f"Graphiti sidecar closure leaked into the platform uv.lock: {leaked}"


def test_ne_memory_graph_pyproject_declares_no_sidecar_deps() -> None:
    pyproject = _repo_root() / "packages" / "ne-memory-graph" / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = data["project"].get("dependencies", [])
    for dep in deps:
        assert not any(bad in dep.lower() for bad in _FORBIDDEN), dep
