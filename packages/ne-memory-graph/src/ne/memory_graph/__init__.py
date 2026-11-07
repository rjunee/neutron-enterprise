# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-memory-graph — temporal-graph agent memory behind the frozen MemoryProvider.

NE-083b. A :class:`~ne.memory_graph.facade.TemporalAgentMemory` implements the
FROZEN ``ne.contracts.runtime.MemoryProvider`` Protocol byte-unchanged:

* ``append`` = the unchanged NE-037 episodic INSERT (the TRUTH, the transaction)
  + a best-effort queued Graphiti episode ingestion (sidecar down never fails an
  append);
* ``recall`` = graph hybrid-search hits UNIONed over the retained v1 keyword
  floor, projected to the frozen ``MemoryEvent`` shape.

The NE-037 episodic log stays the source of truth; the temporal graph is a
DERIVED, rebuildable index. Graphiti (openai-SDK closure) runs OUT-OF-PROCESS in
a hash-pinned sidecar venv (``graphiti_env/``, the splink/ragas isolation
pattern) — nothing here imports ``graphiti_core`` / ``neo4j`` / ``posthog`` and
none of them enter the platform ``uv.lock`` (grep-provable).

Richer temporal ops (e.g. :meth:`TemporalAgentMemory.recall_as_of`) live on the
concrete class per the NE-037 ``recall_shared`` / ``get`` precedent — the frozen
Protocol is untouched.
"""

from __future__ import annotations

from ne.memory_graph.client import GraphHit, MemoryGraphClient
from ne.memory_graph.config import MemoryGraphSettings
from ne.memory_graph.facade import TemporalAgentMemory, create_temporal_agent_memory

__all__ = [
    "GraphHit",
    "MemoryGraphClient",
    "MemoryGraphSettings",
    "TemporalAgentMemory",
    "create_temporal_agent_memory",
]
