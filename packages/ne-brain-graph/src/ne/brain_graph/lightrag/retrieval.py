# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``GraphRetriever`` — the ``KnowledgeRead``-shaped graph-aware retrieval surface.

Composition: T1 hybrid search (already trimmed) anchors the query → entity linking
via ``EntityGraph.lookup`` (source-ref join to the T1 citations) → expansion via
``EntityGraph.neighbors`` → graph paths via ``EntityGraph.paths`` → a merged
``RetrievalResult`` carrying ADR-0005 ``graph_paths``.

C7 (fail-closed, trim-before-return): every graph read goes through the
principal-scoped ``EntityGraph`` (server-side trimmed) — anchors, expansion, and
paths are ALL principal-scoped, so a hop can never cite a node/edge the caller
could not see. ``graph_paths`` are therefore trimmed BY CONSTRUCTION (ADR-0005).
The ``retrieve`` fetch-by-id delegates to the T1 retriever and carries no graph
paths (citation/evidence replay is chunk-scoped).

Backend selection is a config flip (``settings.graph_expansion_backend``): the
native path is the default; the LightRAG sidecar branch (NE-082 STEP 6) re-validates
every sidecar candidate against the SAME trimmed surfaces before returning.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.model import EntityKey, EntityNode, GraphPathRecord
from ne.brain_graph.read import EntityGraph
from ne.contracts.knowledge import KnowledgeRead
from ne.contracts.types import (
    GraphEntityRef,
    GraphHop,
    GraphPath,
    Principal,
    RetrievalResult,
)

__all__ = ["GraphExpansionSidecar", "GraphRetriever", "paths_to_graph_paths"]


class GraphExpansionSidecar(Protocol):
    """The typed seam for the out-of-process graph-expansion sidecar (C5).

    Candidates are UNTRUSTED: ``query`` returns raw entity keys / chunk ids that
    the caller MUST re-validate against the principal-scoped surfaces before any
    of it becomes a citation (trim-before-return, C7).

    BOTH methods are BLOCKING (``ensure`` may (re)build a hash-pinned venv;
    ``query`` runs a subprocess) so an async caller MUST dispatch them off the
    event loop (``asyncio.to_thread``) — never call them directly from a coroutine.
    """

    def ensure(self) -> None: ...

    def query(self, query_text: str, *, top_k: int = 10) -> dict[str, list[str]]: ...


def _ref(node: EntityNode) -> GraphEntityRef:
    """Build the wire-facing entity pointer for one trimmed node.

    ``source_ref`` (NE-T2X-5-ENDPOINT-EVIDENCE-REF, ADR-0015) now rides the
    contract alongside ``assurance`` — the entity's own evidencing ref
    (``node.source_ref``) survives past this point instead of being dropped,
    so a downstream console can show ENTITY evidence when the ENDPOINT,
    not the asserting edge, is the provisional element in a hop (design
    §6.1's "evidencing chunk one click away" property).
    """
    return GraphEntityRef(
        entity_class=node.entity_class,
        entity_key=node.key,
        display=node.display_name,
        assurance=node.assurance,
        source_ref=node.source_ref,
    )


def paths_to_graph_paths(records: Sequence[GraphPathRecord]) -> tuple[GraphPath, ...]:
    """Map trimmed ``GraphPathRecord``s to ADR-0005 ``GraphPath``s.

    Hop ``i`` is ``GraphHop(subject=nodes[i], edge_kind, obj=nodes[i+1],
    source_system/source_ref/assurance from edges[i])``. Every record came
    from ``EntityGraph`` trimmed output, so the citations are trimmed by
    construction. ``assurance`` on both the hop and each ``GraphEntityRef`` is
    set EXPLICITLY from the trimmed element (NE-T2X-3, design §5.2's
    correction) — ``GraphEntityRef``/``GraphHop`` are frozen but NOT
    ``extra="forbid"``, so an omitted/misspelled key on a payload built any
    other way silently defaults to ``"source"`` (fails OPEN toward the
    trusted-sounding value); explicit assignment here is the defense.
    """
    out: list[GraphPath] = []
    for record in records:
        if not record.edges:
            continue
        hops: list[GraphHop] = []
        for i, edge in enumerate(record.edges):
            hops.append(
                GraphHop(
                    subject=_ref(record.nodes[i]),
                    edge_kind=edge.edge_kind,
                    obj=_ref(record.nodes[i + 1]),
                    source_system=edge.source_system,
                    source_ref=edge.source_ref,
                    assurance=edge.assurance,
                )
            )
        out.append(GraphPath(hops=tuple(hops)))
    return tuple(out)


class GraphRetriever:
    """Graph-aware, principal-scoped, fail-closed retrieval (C7)."""

    def __init__(
        self,
        entity_graph: EntityGraph,
        t1_retriever: KnowledgeRead,
        settings: BrainGraphSettings,
        *,
        sidecar: GraphExpansionSidecar | None = None,
    ) -> None:
        self._graph = entity_graph
        self._t1 = t1_retriever
        self._settings = settings
        self._sidecar = sidecar

    async def startup(self) -> None:
        """Front-load the sidecar's (potential venv-rebuild) ``ensure`` off-loop.

        Composition roots MUST call this ONCE at startup so the blocking venv
        (re)build never lands on a per-request retrieval path. A no-op for the
        native backend (or when no sidecar is wired); the ``ensure`` is dispatched
        via ``asyncio.to_thread`` so the event loop is never blocked.
        """
        if self._settings.graph_expansion_backend == "lightrag" and self._sidecar is not None:
            await asyncio.to_thread(self._sidecar.ensure)

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        t1 = await self._t1.search(query, principal=principal, top_k=top_k, filters=filters)
        if self._settings.graph_expansion_backend == "lightrag":
            graph_paths = await self._lightrag_paths(query, t1, principal=principal)
        else:
            graph_paths = await self._native_paths(t1, principal=principal)
        return RetrievalResult(chunks=t1.chunks, citations=t1.citations, graph_paths=graph_paths)

    async def retrieve(self, chunk_ids: Sequence[str], *, principal: Principal) -> RetrievalResult:
        t1 = await self._t1.retrieve(chunk_ids, principal=principal)
        return RetrievalResult(chunks=t1.chunks, citations=t1.citations, graph_paths=())

    async def _native_paths(
        self, t1: RetrievalResult, *, principal: Principal
    ) -> tuple[GraphPath, ...]:
        source_refs = [c.source_ref for c in t1.citations if c.source_ref]
        if not source_refs:
            # No T1 citations -> no anchor possible; short-circuit before graph I/O.
            return ()
        anchors = list(await self._graph.lookup(source_refs=source_refs, principal=principal))
        if not anchors:
            return ()
        anchor_keys = [EntityKey(entity_class=a.entity_class, key=a.key) for a in anchors]
        expansion = await self._graph.neighbors(
            anchor_keys,
            principal=principal,
            hops=self._settings.retrieval_expansion_hops,
        )
        # Endpoints: the anchors plus their trimmed neighborhood (dedup by key).
        endpoints: dict[str, EntityNode] = {a.key: a for a in anchors}
        for node in expansion.nodes:
            endpoints.setdefault(node.key, node)
        ordered = [endpoints[k] for k in sorted(endpoints)]

        records: list[GraphPathRecord] = []
        max_paths = self._settings.retrieval_max_paths
        for left, right in itertools.islice(itertools.combinations(ordered, 2), max_paths):
            recs = await self._graph.paths(
                EntityKey(entity_class=left.entity_class, key=left.key),
                EntityKey(entity_class=right.entity_class, key=right.key),
                principal=principal,
                max_hops=self._settings.retrieval_expansion_hops + 1,
            )
            records.extend(recs)
        return paths_to_graph_paths(records)

    async def _lightrag_paths(
        self, query: str, t1: RetrievalResult, *, principal: Principal
    ) -> tuple[GraphPath, ...]:
        """LightRAG sidecar backend — TRIM-BEFORE-RETURN (C7, NE-082 STEP 6).

        The sidecar is UNTRUSTED: every candidate entity/chunk it returns is
        re-validated against the SAME principal-scoped surfaces the native path
        uses BEFORE any of it becomes a citation:

        * candidate entity keys → ``EntityGraph.lookup(keys=..., principal)`` — a
          key the principal cannot see is simply never returned (dropped);
        * candidate chunk ids → ``t1_retriever.retrieve(chunk_ids, principal)`` —
          the T1 fail-closed read; its citations' source_refs seed extra anchors;
        * ANY verification error PROPAGATES (deny-by-erroring — NO silent fallback
          to the native path);
        * ``graph_paths`` are then built ONLY from ``EntityGraph.paths`` over the
          VERIFIED anchors, so every hop is trimmed by construction.

        The sidecar ``query`` is BLOCKING (subprocess) so it is dispatched via
        ``asyncio.to_thread`` — the event loop is never blocked (P3-3).
        """
        if self._sidecar is None:
            raise RuntimeError(
                "graph_expansion_backend='lightrag' requires a LightRagSidecar; "
                "none was provided to GraphRetriever (fail-closed)."
            )
        sidecar = self._sidecar  # pyright narrowing after the fail-closed None check
        candidates = await asyncio.to_thread(
            sidecar.query, query, top_k=self._settings.retrieval_max_paths or 10
        )
        candidate_keys = [str(k) for k in candidates.get("entity_keys", [])]
        candidate_chunk_ids = [str(c) for c in candidates.get("chunk_ids", [])]

        # (1) Re-validate candidate entity keys against the trimmed graph.
        anchors: dict[str, EntityNode] = {}
        if candidate_keys:
            verified = await self._graph.lookup(
                keys=[EntityKey(entity_class="", key=k) for k in candidate_keys],
                principal=principal,
            )
            for node in verified:
                anchors.setdefault(node.key, node)

        # (2) Re-validate candidate chunk ids through the T1 fail-closed retriever;
        # a verification error here PROPAGATES (no silent degradation). The verified
        # chunks' source_refs seed additional trimmed anchors.
        source_refs = [c.source_ref for c in t1.citations if c.source_ref]
        if candidate_chunk_ids:
            verified_chunks = await self._t1.retrieve(candidate_chunk_ids, principal=principal)
            source_refs.extend(c.source_ref for c in verified_chunks.citations if c.source_ref)
        if source_refs:
            for node in await self._graph.lookup(source_refs=source_refs, principal=principal):
                anchors.setdefault(node.key, node)

        if not anchors:
            return ()
        return await self._paths_over(anchors, principal=principal)

    async def _paths_over(
        self, anchors: Mapping[str, EntityNode], *, principal: Principal
    ) -> tuple[GraphPath, ...]:
        """Build trimmed paths between the first ``retrieval_max_paths`` anchor pairs."""
        ordered = [anchors[k] for k in sorted(anchors)]
        records: list[GraphPathRecord] = []
        max_paths = self._settings.retrieval_max_paths
        for left, right in itertools.islice(itertools.combinations(ordered, 2), max_paths):
            recs = await self._graph.paths(
                EntityKey(entity_class=left.entity_class, key=left.key),
                EntityKey(entity_class=right.entity_class, key=right.key),
                principal=principal,
                max_hops=self._settings.retrieval_expansion_hops + 1,
            )
            records.extend(recs)
        return paths_to_graph_paths(records)
