# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C4/C11 textual guards on the isolated codegen env pin files.

The codegen env must be fully ==-pinned and hashed (C11), must contain the linkml
pin, and must NOT contain the GPLv3+ `rfc3987` distribution (C4) — that GPL dep is
eliminated via the jsonschema[format-nongpl] override.
"""

from __future__ import annotations

import re
from pathlib import Path

_CODEGEN = Path(__file__).resolve().parents[1] / "codegen"
_REQUIREMENTS = _CODEGEN / "requirements.txt"
_REQUIREMENTS_IN = _CODEGEN / "requirements.in"
_OVERRIDES = _CODEGEN / "overrides.txt"


def _requirement_lines(text: str) -> list[str]:
    """Top-level requirement lines (name==version), ignoring hash/comment/continuation."""
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("--hash"):
            continue
        # A requirement line starts at column 0 (hashes/continuations are indented).
        if raw[:1].isspace():
            continue
        lines.append(stripped)
    return lines


def test_gpl_rfc3987_absent() -> None:
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    # The GPLv3+ package is exactly `rfc3987`; `rfc3987-syntax` (MIT) is fine.
    assert not re.search(r"(?m)^rfc3987==", text), "GPL rfc3987 must not be in the codegen env"


def test_linkml_pinned() -> None:
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    assert re.search(r"(?m)^linkml==1\.11\.1", text), "linkml==1.11.1 must be pinned"
    assert re.search(r"(?m)^linkml==1\.11\.1", _REQUIREMENTS_IN.read_text(encoding="utf-8"))


def test_every_requirement_is_pinned_and_hashed() -> None:
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    reqs = _requirement_lines(text)
    assert reqs, "requirements.txt should list distributions"
    for req in reqs:
        # strip trailing continuation backslash
        spec = req.rstrip("\\").strip()
        assert "==" in spec, f"requirement not ==-pinned: {spec!r}"
    # Every top-level requirement must carry at least one --hash line.
    assert text.count("--hash=sha256:") >= len(reqs), "every requirement must be hashed"


def test_overrides_use_format_nongpl() -> None:
    text = _OVERRIDES.read_text(encoding="utf-8")
    assert "jsonschema[format-nongpl]" in text
