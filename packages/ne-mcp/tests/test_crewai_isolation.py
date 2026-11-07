# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Sovereignty guard (NE-CREWAI-DROPIN): the crewai drop-in leaks NOTHING into the lock.

The headline invariant of this unit — the exact structural twin of
``test_langgraph_isolation.py``. The customer's crewai closure lives ONLY in the
isolated hash-pinned sidecar venv (``crewai_env/`` + git-ignored ``.crewai-venv``) and
NEVER enters the platform ``uv.lock``, SBOM, or C4 license gate.

Three independent reasons the closure can never enter the lock, each guarded here:
  * crewai's base deps pull chromadb~=1.1.0, whose base deps pull ``posthog`` — which is
    PERMANENTLY barred by ``packages/ne-memory-graph/tests/test_no_graph_closure_in_lock.py``;
  * crewai also pulls ``lancedb`` and ``instructor`` (its own base deps);
  * four hard pin conflicts (pydantic<2.13 vs locked 2.13.4, etc.) mean the closure
    cannot even resolve into the workspace lock.

The ``posthog`` assertion below is DELIBERATELY redundant with the ne-memory-graph
guard — two packages now depend on that invariant, and each guard must fail loudly on
its own (the langchain-redundancy precedent from ``test_langgraph_isolation.py`` /
``test_ragas_isolation.py``).

Structural sibling of the langgraph guard; the ``_repo_root`` / ``_NAME_RE`` helpers are
duplicated (cross-package test imports are banned by repo rule).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_NAME_RE = re.compile(r'^name = "([^"]+)"', re.MULTILINE)


def _repo_root() -> Path:
    for candidate in (Path(__file__).resolve(), *Path(__file__).resolve().parents):
        if (candidate / "uv.lock").is_file():
            return candidate
    raise AssertionError("could not locate repo-root uv.lock")


def _lock_names() -> set[str]:
    lock_text = (_repo_root() / "uv.lock").read_text(encoding="utf-8")
    return set(_NAME_RE.findall(lock_text))


def test_uv_lock_has_no_crewai() -> None:
    """No name starting with ``crewai`` (crewai / crewai-core / crewai-cli / crewai-tools)."""
    names = _lock_names()
    crewai = sorted(n for n in names if n.startswith("crewai"))
    assert crewai == [], f"crewai* leaked into the platform uv.lock: {crewai}"


def test_uv_lock_has_no_crewai_closure() -> None:
    """The load-bearing closure names: chromadb* / lancedb* / posthog / instructor.

    ``posthog`` is DELIBERATELY redundant with
    ``packages/ne-memory-graph/tests/test_no_graph_closure_in_lock.py`` — two packages
    now depend on the posthog ban, and each guard must fail loudly on its own.
    """
    names = _lock_names()
    chromadb = sorted(n for n in names if n.startswith("chromadb"))
    lancedb = sorted(n for n in names if n.startswith("lancedb"))
    assert chromadb == [], f"chromadb* leaked into the platform uv.lock: {chromadb}"
    assert lancedb == [], f"lancedb* leaked into the platform uv.lock: {lancedb}"
    assert "posthog" not in names, "posthog (via crewai->chromadb) leaked into the platform uv.lock"
    assert "instructor" not in names, "instructor (via crewai) leaked into the platform uv.lock"


def test_ne_mcp_pyproject_declares_no_crewai_closure() -> None:
    pyproject = _repo_root() / "packages" / "ne-mcp" / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dependencies = data["project"].get("dependencies", [])
    barred = ("crewai", "chromadb", "posthog", "lancedb", "instructor")
    offenders = [dep for dep in dependencies if any(name in dep.lower() for name in barred)]
    assert offenders == [], offenders


def test_crewai_env_declares_no_crewai_tools() -> None:
    """crewai-tools ships pymupdf (AGPL-3.0) in its BASE deps — never add it (load-bearing)."""
    req_in = (_repo_root() / "packages" / "ne-mcp" / "crewai_env" / "requirements.in").read_text(
        encoding="utf-8"
    )
    # Match the actual pin token, not the HARD-RULE comment that names it to forbid it.
    offenders = [
        line
        for line in req_in.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "crewai-tools" in line
    ]
    assert offenders == [], (
        "crewai-tools must NOT be a pin in crewai_env/requirements.in "
        f"(AGPL-3.0 pymupdf in its base deps): {offenders}"
    )


def test_guardian_has_no_skip_markers() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    prefix = "pytest.mark."
    for suffix in ("skip", "skipif", "xfail"):
        marker = prefix + suffix
        assert marker not in source, f"guardian file must not contain {marker}"
