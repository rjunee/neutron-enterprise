# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Sovereignty guard: the RAGAS dev venv leaks NOTHING into the platform lock.

Proves the "grep-0" invariant — the heavy ragas closure (ragas / scipy /
langchain*) lives ONLY in the isolated hash-pinned dev venv and never enters the
platform ``uv.lock`` or ne-eval's declared dependencies. Deliberately silent on
``openai`` / ``numpy`` / ``datasets``, which are legitimately present in the
platform lock via litellm / fastembed and must NOT be asserted against.
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


def test_uv_lock_has_no_ragas_scipy_or_langchain() -> None:
    lock_text = (_repo_root() / "uv.lock").read_text(encoding="utf-8")
    names = set(_NAME_RE.findall(lock_text))
    assert "ragas" not in names, "ragas leaked into the platform uv.lock"
    assert "scipy" not in names, "scipy leaked into the platform uv.lock"
    langchain = sorted(n for n in names if n.startswith("langchain"))
    assert langchain == [], f"langchain* leaked into the platform uv.lock: {langchain}"


def test_uv_lock_has_no_langgraph() -> None:
    """Close the langgraph* gap in the original sovereignty guard (NE-LG-DROPIN).

    ``langgraph`` depends on ``langchain-core``, so it is the same sovereignty class
    — but ``langgraph`` / ``langgraph-sdk`` / ``langgraph-checkpoint`` /
    ``langgraph-prebuilt`` all start with ``langgraph``, NOT ``langchain``, and would
    slip past the ``langchain*`` assertion above. NE-LG-DROPIN's out-of-process
    LangGraph drop-in keeps that whole closure in an isolated sidecar venv; this
    asserts none of it reached the platform lock.
    """
    lock_text = (_repo_root() / "uv.lock").read_text(encoding="utf-8")
    names = set(_NAME_RE.findall(lock_text))
    langgraph = sorted(n for n in names if n.startswith("langgraph"))
    assert langgraph == [], f"langgraph* leaked into the platform uv.lock: {langgraph}"


def test_ne_eval_pyproject_declares_no_ragas() -> None:
    pyproject = _repo_root() / "packages" / "ne-eval" / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dependencies = data["project"].get("dependencies", [])
    assert all("ragas" not in dep.lower() for dep in dependencies), dependencies
