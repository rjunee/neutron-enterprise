# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Sovereignty guard (NE-LG-DROPIN): the langgraph drop-in leaks NOTHING into the lock.

The headline invariant of this unit. The customer's langgraph + langchain closure
lives ONLY in the isolated hash-pinned sidecar venv (``langgraph_env/`` +
git-ignored ``.langgraph-venv``) and NEVER enters the platform ``uv.lock``, SBOM, or
C4 license gate.

The load-bearing NEW assertion is ``test_uv_lock_has_no_langgraph``: langgraph
depends on langchain-core, and ``langgraph`` / ``langgraph-checkpoint`` /
``langgraph-sdk`` / ``langgraph-prebuilt`` would ALL slip past the existing
``langchain*``-only ragas guard. The ``langchain*`` assertion here is DELIBERATELY
redundant with ``packages/ne-eval/tests/test_ragas_isolation.py`` — two packages now
depend on this invariant, and each guard must fail loudly on its own.

Structural sibling of the ragas guard; the ``_repo_root`` / ``_NAME_RE`` helpers are
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


def test_uv_lock_has_no_langgraph() -> None:
    """NEW coverage: langgraph* (checkpoint/sdk/prebuilt) would slip past langchain*."""
    names = _lock_names()
    langgraph = sorted(n for n in names if n.startswith("langgraph"))
    assert langgraph == [], f"langgraph* leaked into the platform uv.lock: {langgraph}"


def test_uv_lock_has_no_langchain() -> None:
    """Deliberately redundant with ne-eval's guard — each must fail loudly alone."""
    names = _lock_names()
    langchain = sorted(n for n in names if n.startswith("langchain"))
    assert langchain == [], f"langchain* leaked into the platform uv.lock: {langchain}"


def test_ne_mcp_pyproject_declares_no_langgraph_or_langchain() -> None:
    pyproject = _repo_root() / "packages" / "ne-mcp" / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dependencies = data["project"].get("dependencies", [])
    offenders = [
        dep for dep in dependencies if "langgraph" in dep.lower() or "langchain" in dep.lower()
    ]
    assert offenders == [], offenders


def test_langgraph_env_carries_the_langchain_core_security_floor() -> None:
    """The isolated closure must keep the CVE-2026-34070/26013 langchain-core floor."""
    req_in = (_repo_root() / "packages" / "ne-mcp" / "langgraph_env" / "requirements.in").read_text(
        encoding="utf-8"
    )
    assert "langchain-core>=1.2.22,<2" in req_in, (
        "the langchain-core security floor (CVE-2026-34070 / CVE-2026-26013) must be "
        "carried in langgraph_env/requirements.in"
    )


def test_guardian_has_no_skip_markers() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    prefix = "pytest.mark."
    for suffix in ("skip", "skipif", "xfail"):
        marker = prefix + suffix
        assert marker not in source, f"guardian file must not contain {marker}"
