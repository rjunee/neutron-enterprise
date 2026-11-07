# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``LightRagProjector`` — export the entity graph as a LightRAG custom-KG payload.

Reads the graph via the package-local ``scan_nodes``/``scan_edges`` capabilities (a
PLATFORM batch read — running as the platform, NOT as a user) and shapes a
``{entities, relationships, chunks}`` payload for ``lightrag_env/project.py``'s
``ainsert_custom_kg``. The projection is fully REBUILDABLE from scratch
(``ne-brain-graph rebuild-lightrag``); it is a derived index, not a system of
record.

The exported ``description``/``content`` are the node ``display_name`` + class only
— governance fields (``acl``/hashes) NEVER leave the graph through this path, and
the sidecar's candidates are re-validated trim-before-return regardless (C7).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

__all__ = ["LightRagProjector", "LightRagScanStore"]


class LightRagScanStore(Protocol):
    """The SCAN-ONLY store surface the projector needs (no upsert/delete reachable)."""

    async def scan_nodes(self, entity_class: str | None = None) -> Sequence[Mapping[str, Any]]: ...
    async def scan_edges(self) -> Sequence[Mapping[str, Any]]: ...


class LightRagProjector:
    """Project the graph into a LightRAG custom-KG payload (rebuildable)."""

    def __init__(self, store: LightRagScanStore) -> None:
        self._store = store

    async def build_payload(self) -> dict[str, list[dict[str, Any]]]:
        entities: list[dict[str, Any]] = []
        chunks: list[dict[str, Any]] = []
        seen_chunks: set[str] = set()
        for node in await self._store.scan_nodes():
            key = str(node["key"])
            entity_class = str(node.get("entity_class", ""))
            display = str(node.get("display_name", "") or key)
            source_ref = str(node.get("source_ref", "") or key)
            description = f"{display} ({entity_class})" if entity_class else display
            entities.append(
                {
                    "entity_name": key,
                    "entity_type": entity_class or "Entity",
                    "description": description,
                    "source_id": source_ref,
                }
            )
            if source_ref not in seen_chunks:
                seen_chunks.add(source_ref)
                chunks.append({"content": description, "source_id": source_ref})

        relationships: list[dict[str, Any]] = []
        for edge in await self._store.scan_edges():
            edge_kind = str(edge.get("edge_kind", ""))
            source_ref = str(edge.get("source_ref", "") or edge.get("key", ""))
            relationships.append(
                {
                    "src_id": str(edge["from_key"]),
                    "tgt_id": str(edge["to_key"]),
                    "description": edge_kind,
                    "keywords": edge_kind,
                    "weight": 1.0,
                    "source_id": source_ref,
                }
            )
            if source_ref and source_ref not in seen_chunks:
                seen_chunks.add(source_ref)
                chunks.append({"content": edge_kind, "source_id": source_ref})

        # Deterministic ordering so a rebuild is byte-stable.
        entities.sort(key=lambda e: str(e["entity_name"]))
        relationships.sort(key=lambda r: (str(r["src_id"]), str(r["tgt_id"]), str(r["source_id"])))
        chunks.sort(key=lambda c: str(c["source_id"]))
        return {"entities": entities, "relationships": relationships, "chunks": chunks}
