# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tier-2 graph-aware retrieval — native EntityGraph expansion (default) + the
LightRAG library-only sidecar backend (config flip).

The native path is built FIRST and is the default (``graph_expansion_backend =
"native"``) so the pre-authorized native-methodology fallback (NE-024 precedent)
is a config flip, not new code. Both backends are FAIL-CLOSED trim-before-return
(C7): every entity/chunk is re-validated against the principal-scoped
``EntityGraph`` / T1 retriever before anything leaves the retrieval layer.
"""

from __future__ import annotations

from ne.brain_graph.lightrag._lightrag import LightRagSidecar, default_lightrag_env_dir
from ne.brain_graph.lightrag.projector import LightRagProjector
from ne.brain_graph.lightrag.retrieval import (
    GraphExpansionSidecar,
    GraphRetriever,
    paths_to_graph_paths,
)

__all__ = [
    "GraphExpansionSidecar",
    "GraphRetriever",
    "LightRagProjector",
    "LightRagSidecar",
    "default_lightrag_env_dir",
    "paths_to_graph_paths",
]
