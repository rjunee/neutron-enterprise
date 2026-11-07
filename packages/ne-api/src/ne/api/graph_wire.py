# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Graph read wire shapes (NE-133) — the ACL-stripped Tier-2 HTTP contract.

The no-leak invariant: :class:`GraphNodeView`/:class:`GraphEdgeView`/
:class:`SubgraphView`/:class:`GraphPathRecordView`/:class:`GraphPathsView` mirror
the ``EntityNode``/``EntityEdge`` shapes that :mod:`ne.brain_graph.read`
constructs acl-free and governance-hash-free — the read layer strips ``acl`` /
``content_hash`` / ``config_hash`` before these models are ever built. The view
converters here are field-by-field (the ``retrieval_result_view`` precedent),
NEVER ``model_dump``'ed off the source model, so a future field added to the read
model can never silently ride onto the wire. ``properties`` is additionally
filtered through :data:`_PROPERTY_DENYLIST` (defense-in-depth; the read layer
already strips those keys).

A separate wire module (the ``turn_wire.py`` precedent) keeps concurrent
NE-131/NE-135 ``wire.py`` edits conflict-free.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from ne.brain_graph.model import EntityEdge, EntityNode, GraphPathRecord
    from ne.brain_graph.read import EntityFacets

# Server-max caps (the fetch layer enforces these; the request bounds clamp to them).
GRAPH_NODE_CAP = 500
GRAPH_EDGE_CAP = 2000
QUERY_HOPS_CAP = 2
NEIGHBORHOOD_HOPS_CAP = 4
PATHS_HOPS_CAP = 4
PATHS_LIMIT_CAP = 20
ANCHOR_CAP = 100
ENTITY_DISCOVERY_LIMIT_DEFAULT = 25
ENTITY_DISCOVERY_LIMIT_CAP = 100
#: Longest ``entity_class`` / ``source`` string GET /v1/graph/entities accepts.
#: GET /v1/graph/facets drops any facet value longer than this so every option
#: it offers is one the discovery route will actually accept — an offered
#: option that 422s is the same dead-control defect as an option with no rows.
ENTITY_FILTER_MAX_LENGTH = 128

#: Property keys that must NEVER surface in a node's ``properties`` map. The read
#: layer already strips these (ne.brain_graph.read._GOVERNANCE); this is a
#: belt-and-braces wire-side filter so a leak is structurally impossible.
_PROPERTY_DENYLIST = frozenset({"acl", "content_hash", "config_hash"})


# --- request models ---------------------------------------------------------


class GraphQueryRequest(BaseModel):
    """POST /v1/graph/query — an anchored subgraph query."""

    anchor_keys: list[str] = Field(default_factory=list, max_length=ANCHOR_CAP)
    anchor_citations: list[str] = Field(default_factory=list, max_length=ANCHOR_CAP)
    hops: int = Field(default=2, ge=1, le=QUERY_HOPS_CAP)
    limit_nodes: int = Field(default=GRAPH_NODE_CAP, ge=1, le=GRAPH_NODE_CAP)
    limit_edges: int = Field(default=GRAPH_EDGE_CAP, ge=1, le=GRAPH_EDGE_CAP)

    @model_validator(mode="after")
    def _require_an_anchor(self) -> GraphQueryRequest:
        if not self.anchor_keys and not self.anchor_citations:
            raise ValueError("at least one of anchor_keys/anchor_citations is required")
        return self


class GraphPathsRequest(BaseModel):
    """POST /v1/graph/paths — paths between two entities (design §5.1 {from,to})."""

    model_config = ConfigDict(populate_by_name=True)

    # 'from' is a Python keyword, hence the alias (design §5.1 uses {from,to}).
    from_key: str = Field(alias="from", min_length=1)
    to_key: str = Field(alias="to", min_length=1)
    max_hops: int = Field(default=4, ge=1, le=PATHS_HOPS_CAP)
    limit: int = Field(default=20, ge=1, le=PATHS_LIMIT_CAP)


# --- view models (ACL-stripped by construction) -----------------------------


class GraphNodeView(BaseModel):
    """A trimmed node on the wire — EXACTLY seven fields, no ``acl``.

    ``assurance`` (NE-T2X-4, design §5.2 correction 2) is REQUIRED, no
    default — the same anti-silent-drop stance as ``wire.py``'s
    ``GraphEntityRefView``: this module maps field-by-field from
    ``EntityNode`` (never ``model_dump``), so a construction site that
    forgets ``assurance`` fails LOUD (``pydantic.ValidationError``) rather
    than silently defaulting toward the trusted-sounding ``"source"`` value.
    The SAME silent-drop defect NE-T2X-4 fixed on the ``/v1/brain`` wire
    exists here on the ``/v1/graph`` explorer surface; NE-T2X-5's console
    chip needs it threaded on this surface too.
    """

    key: str
    entity_class: str
    display_name: str
    source_system: str
    source_ref: str
    properties: dict[str, object]
    assurance: str


class GraphEdgeView(BaseModel):
    """A trimmed edge on the wire — EXACTLY seven fields, no ``acl``.

    ``assurance`` (NE-T2X-4) — see :class:`GraphNodeView`'s docstring for the
    no-default anti-silent-drop rationale, which applies identically here.
    """

    key: str
    edge_kind: str
    from_key: str
    to_key: str
    source_system: str
    source_ref: str
    assurance: str


class SubgraphView(BaseModel):
    """A trimmed subgraph result (query / neighborhood) with a ``truncated`` flag."""

    nodes: list[GraphNodeView]
    edges: list[GraphEdgeView]
    truncated: bool


class GraphPathRecordView(BaseModel):
    """One ordered trimmed path. (Distinct from wire.py's /v1/brain GraphPathView.)"""

    nodes: list[GraphNodeView]
    edges: list[GraphEdgeView]


class GraphPathsView(BaseModel):
    """A set of trimmed paths (query result) with a ``truncated`` flag."""

    paths: list[GraphPathRecordView]
    truncated: bool


class GraphEntitySummaryView(BaseModel):
    """One discovery row: human identity plus server-owned source assurance."""

    key: str
    entity_class: str
    display_name: str
    source_system: str
    assurance: str


class GraphEntityListView(BaseModel):
    """A strict keyset page; totals are deliberately not inferred.

    (Facets are NOT folded onto this page either — a page is a page. They are a
    separate additive read, :class:`GraphEntityFacetsView` below, so this wire
    shape is unchanged.)
    """

    entities: list[GraphEntitySummaryView]
    next_cursor: str | None


class GraphEntityFacetsView(BaseModel):
    """GET /v1/graph/facets — the vocabulary PRESENT in the principal's slice.

    Both lists name only values carried by at least one entity this principal
    can actually see (``ne.brain_graph.read.EntityGraph.facets`` folds the same
    trimmed scan discovery serves its rows from), sorted case-insensitively.
    Empty lists are the honest empty state for an empty or entirely-invisible
    graph — never the compile-time ``ne.schema`` class registry.

    Deliberately NO counts: the scan is bounded, so a count would be a
    possibly-wrong number presented as a fact. ``truncated`` says when the
    bounded scan stopped early and the lists may be incomplete.
    """

    entity_classes: list[str]
    source_systems: list[str]
    truncated: bool


# --- field-by-field converters ----------------------------------------------


def graph_node_view(node: EntityNode) -> GraphNodeView:
    return GraphNodeView(
        key=node.key,
        entity_class=node.entity_class,
        display_name=node.display_name,
        source_system=node.source_system,
        source_ref=node.source_ref,
        properties={k: v for k, v in node.properties.items() if k not in _PROPERTY_DENYLIST},
        assurance=node.assurance,
    )


def graph_entity_summary_view(node: EntityNode) -> GraphEntitySummaryView:
    return GraphEntitySummaryView(
        key=node.key,
        entity_class=node.entity_class,
        display_name=node.display_name,
        source_system=node.source_system,
        assurance=node.assurance,
    )


def graph_entity_facets_view(
    facets: EntityFacets, *, entity_classes: Collection[str]
) -> GraphEntityFacetsView:
    """Field-by-field, narrowing-only conversion of the read layer's facets.

    ``entity_classes`` is the set of classes the discovery route will accept as
    an ``entity_class`` filter (``ne.schema.ENTITY_CLASSES``): a present class
    outside it is dropped rather than offered as a control that 422s. Values
    longer than :data:`ENTITY_FILTER_MAX_LENGTH` are dropped for the same
    reason. Both filters only ever REMOVE — nothing is added here, so the view
    can never name a class or source the principal cannot see.
    """
    return GraphEntityFacetsView(
        entity_classes=[
            value
            for value in facets.entity_classes
            if value in entity_classes and len(value) <= ENTITY_FILTER_MAX_LENGTH
        ],
        source_systems=[
            value for value in facets.source_systems if len(value) <= ENTITY_FILTER_MAX_LENGTH
        ],
        truncated=facets.truncated,
    )


def graph_edge_view(edge: EntityEdge) -> GraphEdgeView:
    return GraphEdgeView(
        key=edge.key,
        edge_kind=edge.edge_kind,
        from_key=edge.from_key,
        to_key=edge.to_key,
        source_system=edge.source_system,
        source_ref=edge.source_ref,
        assurance=edge.assurance,
    )


def path_record_view(record: GraphPathRecord) -> GraphPathRecordView:
    return GraphPathRecordView(
        nodes=[graph_node_view(n) for n in record.nodes],
        edges=[graph_edge_view(e) for e in record.edges],
    )
