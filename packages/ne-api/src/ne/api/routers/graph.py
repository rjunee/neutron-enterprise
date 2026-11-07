# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Graph read routes (NE-133, C7 surface) — Tier-2 EntityGraph over governed HTTP.

Every route reads ONLY ``state.entity_graph`` — the ONE ``EntityGraph`` instance
``platform.py`` composed with ``group_expander=`` the platform expander (the
guardian-enforced single construction; never a second, differently-trimmed
graph). Per-principal trimming IS the authorization (the /v1/brain precedent): no
``require_role`` gate — reads are trimmed at source.

NE-164b: 'no audit-sink append' above is NO LONGER true — reads are still
trimmed at source AND (NE-164b) the composed ``EntityGraph`` now emits one
``knowledge.graph_read`` counts/delta-only event per completed read through the
platform's ONE audit sink (wired in ``platform.py``). Every call below passes
``trace_id=current_run_trace_id()`` (the NE-166 run-scoped correlation id,
``None`` outside any run scope) down AS DATA — this ne-api layer reads the
ContextVar; ``ne.brain_graph`` never imports ``ne.runtime``. ``graph_query``
(``POST /v1/graph/query``) may emit up to TWO events (one for the ``lookup``
anchor-citation resolution, one for the ``subgraph`` fetch) — one per actual
store read, matching the one-event-per-read rule.

Honest degrade: when the graph tier is disabled ``state.entity_graph`` is ``None``
and every route answers ``404 "graph tier not enabled"`` (not an error — the CDC
no-mount precedent). Caps are enforced at the fetch layer via the cap+1 discipline
(fetch one past the requested limit, slice, flag ``truncated`` exactly). A backend
fault becomes a ``502`` whose detail is the exception CLASS NAME only — a DSN/URL
never reaches the wire (C2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from ne.api.deps import (
    ApiState,
    api_state,
    bind_authenticated_request_context,
    current_principal,
)
from ne.api.errors import _BACKEND_ERRORS
from ne.api.graph_wire import (
    ANCHOR_CAP,
    ENTITY_DISCOVERY_LIMIT_CAP,
    ENTITY_DISCOVERY_LIMIT_DEFAULT,
    ENTITY_FILTER_MAX_LENGTH,
    GRAPH_EDGE_CAP,
    GRAPH_NODE_CAP,
    NEIGHBORHOOD_HOPS_CAP,
    GraphEntityFacetsView,
    GraphEntityListView,
    GraphPathsRequest,
    GraphPathsView,
    GraphQueryRequest,
    SubgraphView,
    graph_edge_view,
    graph_entity_facets_view,
    graph_entity_summary_view,
    graph_node_view,
    path_record_view,
)
from ne.brain_graph.model import EntityEdge, EntityKey, EntityNode, Subgraph
from ne.brain_graph.read import ENTITY_DISCOVERY_CURSOR_CAP, InvalidDiscoveryCursor
from ne.contracts.types import Principal
from ne.runtime.correlation import current_run_trace_id
from ne.schema import ENTITY_CLASSES

if TYPE_CHECKING:
    from ne.brain_graph.read import EntityGraph

router = APIRouter(dependencies=[Depends(bind_authenticated_request_context)])

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]


def _require_graph(state: ApiState) -> EntityGraph:
    """Return the composed graph, or answer the honest 404 when the tier is off."""
    graph = state.entity_graph
    if graph is None:
        raise HTTPException(status_code=404, detail="graph tier not enabled")
    return graph


def _apply_caps(
    sub: Subgraph, limit_nodes: int, limit_edges: int
) -> tuple[list[EntityNode], list[EntityEdge], bool]:
    """Exact cap enforcement: slice nodes, drop dangling edges, flag ``truncated``.

    The fetch requested one past each limit (cap+1), so a ``len > limit`` here is a
    genuine overflow, never a false positive at exactly-cap. Edges are filtered to
    endpoints that survive the node slice — no dangling endpoint reaches the wire.
    """
    nodes = list(sub.nodes)
    edges = list(sub.edges)
    truncated = len(nodes) > limit_nodes or len(edges) > limit_edges
    nodes = nodes[:limit_nodes]
    kept = {n.key for n in nodes}
    edges = [e for e in edges if e.from_key in kept and e.to_key in kept][:limit_edges]
    return nodes, edges, truncated


@router.get("/v1/graph/entities")
async def graph_entities(
    principal: _Principal,
    state: _State,
    q: Annotated[str | None, Query(max_length=200)] = None,
    entity_class: str | None = None,
    source: Annotated[str | None, Query(max_length=ENTITY_FILTER_MAX_LENGTH)] = None,
    assurance: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=ENTITY_DISCOVERY_LIMIT_CAP)] = (
        ENTITY_DISCOVERY_LIMIT_DEFAULT
    ),
) -> GraphEntityListView:
    """Principal-visible entity discovery for the Graph/Record entry rail."""
    graph = _require_graph(state)
    if entity_class is not None and (
        len(entity_class) > ENTITY_FILTER_MAX_LENGTH or entity_class not in ENTITY_CLASSES
    ):
        raise HTTPException(status_code=422, detail="invalid-entity-class")
    if assurance is not None and assurance not in {"source", "confirmed", "extracted"}:
        raise HTTPException(status_code=422, detail="invalid-assurance")
    if cursor is not None and len(cursor) > ENTITY_DISCOVERY_CURSOR_CAP:
        raise HTTPException(status_code=422, detail="invalid-cursor")
    try:
        page = await graph.discover(
            principal=principal,
            q=q,
            entity_class=entity_class,
            source=source,
            assurance=assurance,
            cursor=cursor,
            limit=limit,
            trace_id=current_run_trace_id(),
        )
    except InvalidDiscoveryCursor as exc:
        raise HTTPException(status_code=422, detail="invalid-cursor") from exc
    except _BACKEND_ERRORS as exc:
        raise HTTPException(status_code=502, detail=type(exc).__name__) from exc
    return GraphEntityListView(
        entities=[graph_entity_summary_view(entity) for entity in page.entities],
        next_cursor=page.next_cursor,
    )


@router.get("/v1/graph/facets")
async def graph_facets(
    principal: _Principal,
    state: _State,
) -> GraphEntityFacetsView:
    """The entity classes / source systems PRESENT in the principal's own slice.

    Additive (NE-GFACET): nothing about ``GET /v1/graph/entities`` changes. This
    route exists so the console's Class and Source controls can offer values
    that are actually in the data instead of the compile-time ``ne.schema``
    registry — four of whose five names matched nothing in the live graph, while
    three real classes could not be filtered at all.

    The list is derived by ``EntityGraph.facets``, which folds the SAME trimmed
    scan ``discover`` serves its rows from. It is therefore ACL-correct by
    construction: a class or source with no principal-visible instance never
    appears, and one visible only to another principal never appears — the
    facet list cannot disclose the existence of data the row list withholds.

    Honest degrade matches its siblings: ``404`` when the tier is off (not an
    error), the principal's own trimmed-to-empty view returns EMPTY LISTS (not a
    404 and not an invented vocabulary), and a backend fault is a ``502`` whose
    detail is the exception CLASS NAME only.

    Cost/caching: one bounded scan per call — a single store round trip for any
    graph holding fewer than ``FACET_SCAN_BATCH`` (200) rows, which the Meridian
    demo graph at ~45 nodes is, and at most ``FACET_SCAN_CAP / FACET_SCAN_BATCH``
    = 10 round trips for any graph at all, because the scan is capped on RAW
    rows examined as well as on visible ones. Deliberately UNCACHED: the answer
    is a function of the caller's expanded label set, so any cache not keyed on
    that exact set would serve one principal's vocabulary to another. If this
    ever needs caching, the key is the label set, not the route.
    """
    graph = _require_graph(state)
    try:
        facets = await graph.facets(principal=principal, trace_id=current_run_trace_id())
    except _BACKEND_ERRORS as exc:
        raise HTTPException(status_code=502, detail=type(exc).__name__) from exc
    return graph_entity_facets_view(facets, entity_classes=ENTITY_CLASSES)


@router.post("/v1/graph/query")
async def graph_query(
    body: GraphQueryRequest,
    principal: _Principal,
    state: _State,
) -> SubgraphView:
    graph = _require_graph(state)
    # Bare-key anchors (retrieval.py:212 precedent — build_subgraph consumes .key).
    keys = [EntityKey(entity_class="", key=k) for k in body.anchor_keys]
    try:
        if body.anchor_citations:
            # Resolve T1 citation source_refs to anchors through the TRIMMED lookup —
            # an invisible source_ref resolves to nothing (never over-returns).
            anchors = await graph.lookup(
                source_refs=body.anchor_citations,
                principal=principal,
                limit=ANCHOR_CAP,
                trace_id=current_run_trace_id(),
            )
            keys.extend(EntityKey(entity_class=a.entity_class, key=a.key) for a in anchors)
        if not keys:
            # An unresolvable/invisible anchor is indistinguishable from absent —
            # fail-closed empty, NEVER an existence-leaking 404.
            return SubgraphView(nodes=[], edges=[], truncated=False)
        sub = await graph.subgraph(
            keys,
            principal=principal,
            hops=body.hops,
            limit_nodes=body.limit_nodes + 1,
            limit_edges=body.limit_edges + 1,
            trace_id=current_run_trace_id(),
        )
    except _BACKEND_ERRORS as exc:
        raise HTTPException(status_code=502, detail=type(exc).__name__) from exc
    nodes, edges, truncated = _apply_caps(sub, body.limit_nodes, body.limit_edges)
    return SubgraphView(
        nodes=[graph_node_view(n) for n in nodes],
        edges=[graph_edge_view(e) for e in edges],
        truncated=truncated,
    )


@router.get("/v1/graph/neighborhood")
async def graph_neighborhood(
    principal: _Principal,
    state: _State,
    entity: Annotated[str, Query(min_length=1)],
    hops: Annotated[int, Query(ge=1, le=NEIGHBORHOOD_HOPS_CAP)] = 1,
    limit_nodes: Annotated[int, Query(ge=1, le=GRAPH_NODE_CAP)] = GRAPH_NODE_CAP,
    limit_edges: Annotated[int, Query(ge=1, le=GRAPH_EDGE_CAP)] = GRAPH_EDGE_CAP,
) -> SubgraphView:
    graph = _require_graph(state)
    try:
        sub = await graph.neighbors(
            [EntityKey(entity_class="", key=entity)],
            principal=principal,
            hops=hops,
            limit=limit_edges + 1,
            trace_id=current_run_trace_id(),
        )
    except _BACKEND_ERRORS as exc:
        raise HTTPException(status_code=502, detail=type(exc).__name__) from exc
    nodes, edges, truncated = _apply_caps(sub, limit_nodes, limit_edges)
    return SubgraphView(
        nodes=[graph_node_view(n) for n in nodes],
        edges=[graph_edge_view(e) for e in edges],
        truncated=truncated,
    )


@router.post("/v1/graph/paths")
async def graph_paths(
    body: GraphPathsRequest,
    principal: _Principal,
    state: _State,
) -> GraphPathsView:
    graph = _require_graph(state)
    try:
        records = await graph.paths(
            EntityKey(entity_class="", key=body.from_key),
            EntityKey(entity_class="", key=body.to_key),
            principal=principal,
            max_hops=body.max_hops,
            limit=body.limit + 1,
            trace_id=current_run_trace_id(),
        )
    except _BACKEND_ERRORS as exc:
        raise HTTPException(status_code=502, detail=type(exc).__name__) from exc
    kept = list(records)
    truncated = len(kept) > body.limit
    return GraphPathsView(
        paths=[path_record_view(r) for r in kept[: body.limit]],
        truncated=truncated,
    )
