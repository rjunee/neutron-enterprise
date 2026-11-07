# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Brain T2: property-graph knowledge tier (v1.1).

Two-layer privacy (the ChunkStore/HybridRetriever mirror): the frozen, unscoped
``GraphStore`` (implemented by :class:`~ne.brain_graph.age.store.AgeGraphStore`)
sits beneath the ONLY caller-facing surface :class:`~ne.brain_graph.read.EntityGraph`,
which trims every read through ``ne.brain.trimming.acl_match_labels``. Raw cypher
is never exported to callers.

``AgeGraphStore`` is lazy so importing this package does not drag psycopg unless
the AGE adapter is actually used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ne.brain_graph.config import (
    BrainGraphSettings,
    GraphEdgeMapping,
    GraphEntityMapping,
    GraphSourceMapping,
)
from ne.brain_graph.construct.runner import GraphSyncReport, GraphSyncRunner
from ne.brain_graph.errors import GraphAclError, GraphStoreError
from ne.brain_graph.model import (
    EntityEdge,
    EntityKey,
    EntityNode,
    GraphPathRecord,
    Subgraph,
)
from ne.brain_graph.read import EntityGraph

__version__ = "0.2.0"

_LAZY: dict[str, tuple[str, str]] = {
    "AgeGraphStore": ("ne.brain_graph.age.store", "AgeGraphStore"),
    "GraphRetriever": ("ne.brain_graph.lightrag.retrieval", "GraphRetriever"),
    "LightRagProjector": ("ne.brain_graph.lightrag.projector", "LightRagProjector"),
    "LightRagSidecar": ("ne.brain_graph.lightrag._lightrag", "LightRagSidecar"),
    "SplinkResolutionJob": ("ne.brain_graph.er.job", "SplinkResolutionJob"),
    "apply_merge": ("ne.brain_graph.er.merge", "apply_merge"),
    "unmerge": ("ne.brain_graph.er.merge", "unmerge"),
    "ExtractionJob": ("ne.brain_graph.extraction.job", "ExtractionJob"),
    "ExtractionWriter": ("ne.brain_graph.extraction.writer", "ExtractionWriter"),
    "apply_extraction": ("ne.brain_graph.extraction.apply", "apply_extraction"),
    "revert_extraction": ("ne.brain_graph.extraction.apply", "revert_extraction"),
}

__all__ = [
    "AgeGraphStore",
    "BrainGraphSettings",
    "EntityEdge",
    "EntityGraph",
    "EntityKey",
    "EntityNode",
    "ExtractionJob",
    "ExtractionWriter",
    "GraphAclError",
    "GraphEdgeMapping",
    "GraphEntityMapping",
    "GraphPathRecord",
    "GraphRetriever",
    "GraphSourceMapping",
    "GraphStoreError",
    "GraphSyncReport",
    "GraphSyncRunner",
    "LightRagProjector",
    "LightRagSidecar",
    "SplinkResolutionJob",
    "Subgraph",
    "__version__",
    "apply_extraction",
    "apply_merge",
    "revert_extraction",
    "unmerge",
]


def __getattr__(name: str) -> object:
    try:
        module_name, symbol = _LAZY[name]
    except KeyError as exc:  # pragma: no cover - standard attribute error path
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    import importlib

    return getattr(importlib.import_module(module_name), symbol)


if TYPE_CHECKING:
    from ne.brain_graph.age.store import AgeGraphStore as AgeGraphStore
    from ne.brain_graph.er.job import SplinkResolutionJob as SplinkResolutionJob
    from ne.brain_graph.er.merge import apply_merge as apply_merge
    from ne.brain_graph.er.merge import unmerge as unmerge
    from ne.brain_graph.extraction.apply import apply_extraction as apply_extraction
    from ne.brain_graph.extraction.apply import revert_extraction as revert_extraction
    from ne.brain_graph.extraction.job import ExtractionJob as ExtractionJob
    from ne.brain_graph.extraction.writer import ExtractionWriter as ExtractionWriter
    from ne.brain_graph.lightrag._lightrag import LightRagSidecar as LightRagSidecar
    from ne.brain_graph.lightrag.projector import LightRagProjector as LightRagProjector
    from ne.brain_graph.lightrag.retrieval import GraphRetriever as GraphRetriever
