# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Sidecar-isolation guard (C4): ``graphiti_age`` never leaks platform-side.

No ``src/**/*.py`` under any package may import ``graphiti_age`` (it lives only in the
sidecar venv, like graphiti_core). Complements the UNCHANGED
``test_no_graph_closure_in_lock.py`` — psycopg is deliberately NOT asserted absent
from uv.lock (it is legitimately in the lock via ne-brain-graph).
"""

from __future__ import annotations

import re
from pathlib import Path


def _repo_root() -> Path:
    for candidate in (Path(__file__).resolve(), *Path(__file__).resolve().parents):
        if (candidate / "uv.lock").is_file():
            return candidate
    raise AssertionError("could not locate repo-root uv.lock")


_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+graphiti_age\b", re.MULTILINE)


def test_no_platform_side_graphiti_age_import() -> None:
    root = _repo_root()
    offenders: list[str] = []
    for path in root.glob("packages/*/src/**/*.py"):
        if _IMPORT_RE.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"graphiti_age imported platform-side: {offenders}"


def test_graphiti_age_is_sidecar_only_location() -> None:
    # graphiti_age lives under graphiti_env (the sidecar venv app-dir), never under src.
    root = _repo_root()
    assert (root / "packages/ne-memory-graph/graphiti_env/graphiti_age/driver.py").is_file()
    assert not list(root.glob("packages/*/src/**/graphiti_age"))
