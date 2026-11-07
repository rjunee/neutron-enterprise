# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Best-effort Atlassian Document Format (ADF) -> plain text (NE-093) — pure, no I/O.

Jira Cloud REST v3 returns rich fields (issue ``description``, comment ``body``)
as ADF: a nested ``{"type": "doc", "content": [...]}`` tree whose leaf ``text``
nodes carry the human-readable prose. :func:`adf_to_text` walks that tree and
collects the ``text`` leaves, joining top-level block nodes (paragraphs, etc.)
with newlines.

This is a documented **v1 approximation**: it recovers prose but drops rich
structure (tables, panels, media, mentions render as their fallback ``text``
only, or nothing). A plain-string value is returned unchanged (some deployments
or older fields are plain strings); ``None`` / an unrecognized shape yields
``''``. Worst case is degraded document text — never a security issue.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = ["adf_to_text"]


def _collect(node: object, out: list[str]) -> None:
    """Recursively collect ``text`` leaves under ``node`` into ``out``."""
    if isinstance(node, Mapping):
        text = node.get("text")
        if isinstance(text, str):
            out.append(text)
        content = node.get("content")
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            for child in content:
                _collect(child, out)
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for child in node:
            _collect(child, out)


def adf_to_text(value: object) -> str:
    """Return best-effort plain text for an ADF ``value`` (or a plain string)."""
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return ""
    # Join each top-level block node's text with a newline; fall back to a flat
    # collect when there is no block structure.
    content = value.get("content")
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        blocks: list[str] = []
        for block in content:
            parts: list[str] = []
            _collect(block, parts)
            joined = "".join(parts)
            if joined:
                blocks.append(joined)
        return "\n".join(blocks)
    parts2: list[str] = []
    _collect(value, parts2)
    return "".join(parts2)
