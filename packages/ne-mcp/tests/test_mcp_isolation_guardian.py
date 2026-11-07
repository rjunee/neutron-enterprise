# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance (c), part 1: the grep guardian for the mcp SDK chokepoint (C3).

The ONLY place in the repository permitted to import ``mcp`` is
``packages/ne-mcp/src/ne/mcp/_sdk.py``. This guardian scans BOTH ``packages/*/src``
AND ``packages/*/tests`` — deliberately stricter than the litellm guardian, because
v2 migration containment must cover test code too (a stray ``from mcp import`` in a
test would silently couple the suite to the pre-v2 API). It must contain ZERO skip
markers (self-checked below), and it guards against an accidental v2 jump past the
lock via :func:`test_sdk_version_is_pinned_line`.
"""

from __future__ import annotations

import re
from pathlib import Path

import ne.mcp._sdk

_IMPORT_RE = re.compile(r"^\s*(import mcp\b|from mcp\b)")

# tests/ -> ne-mcp/ -> packages/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHOKEPOINT = Path("packages/ne-mcp/src/ne/mcp/_sdk.py")


def test_only_sdk_module_imports_mcp() -> None:
    hits: list[Path] = []
    for glob in ("*/src/**/*.py", "*/tests/**/*.py"):
        for path in sorted((_REPO_ROOT / "packages").glob(glob)):
            text = path.read_text(encoding="utf-8")
            if any(_IMPORT_RE.match(line) for line in text.splitlines()):
                hits.append(path.relative_to(_REPO_ROOT))
    assert hits == [_CHOKEPOINT], f"unexpected mcp importers: {hits}"


def test_sdk_version_is_pinned_line() -> None:
    # Guards an accidental v2 jump past the lock (pinned mcp>=1.30.0,<2).
    assert ne.mcp._sdk.MCP_SDK_VERSION.startswith("1.")


def test_guardian_has_no_skip_markers() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    prefix = "pytest.mark."
    for suffix in ("skip", "skipif", "xfail"):
        marker = prefix + suffix
        assert marker not in source, f"guardian file must not contain {marker}"
