# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Attribute-frame export for the Splink ER job (non-principal batch read, R8).

``export_frames`` reads all nodes of one class via the package-local ``scan_nodes``
capability — a PLATFORM batch read over the whole graph (running as the platform,
NOT as a user; its OUTPUT is what the NE-066 gate controls). Only the configured
compare/blocking fields (plus the key + display) are exported, minimizing the
attribute exposure carried into a proposal payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class ErScanStore(Protocol):
    """The SCAN-ONLY store surface the ER export needs (no upsert/delete reachable)."""

    async def scan_nodes(self, entity_class: str | None = None) -> Sequence[Mapping[str, Any]]: ...


async def export_frames(
    store: ErScanStore, entity_class: str, fields: Sequence[str]
) -> list[dict[str, Any]]:
    """Export ``entity_class`` nodes as flat frames restricted to ``fields``.

    Each frame carries ``key`` + ``display`` + the configured field values only —
    governance fields (``acl``/hashes) and unrelated attributes never leave the
    graph through this path.
    """
    allowed = tuple(dict.fromkeys(fields))  # dedup, preserve order
    frames: list[dict[str, Any]] = []
    for node in await store.scan_nodes(entity_class):
        frame: dict[str, Any] = {
            "key": str(node["key"]),
            "display": str(node.get("display_name", "")),
        }
        for field_name in allowed:
            frame[field_name] = node.get(field_name)
        frames.append(frame)
    frames.sort(key=lambda f: f["key"])
    return frames
