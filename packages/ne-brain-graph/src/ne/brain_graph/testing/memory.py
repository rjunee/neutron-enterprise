# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``InMemoryGraphStore`` — the networkx-free reference ``GraphStore`` adapter.

Plain dicts, no deps. It implements the frozen ``GraphStore`` (upsert_nodes /
upsert_edges / cypher) plus the package-local capabilities (update_acl /
delete_where_stale / ensure_graph) so the conformance kit, the k-hop battery, and
both C7 guardians run WITHOUT Postgres.

Its ``cypher`` recognises ONLY the canonical :class:`~ne.brain_graph.age.query`
templates — it recovers the :class:`QueryShape` via :func:`describe_query` and
executes the traversal natively with EXACT trimming semantics (a hop
``(a)-[r]->(b)`` is visible iff the caller's label set intersects ``a.acl`` AND
``r.acl`` AND ``b.acl``; empty/missing ``acl`` matches NOTHING). Unknown cypher
raises ``ValueError`` (fail-closed) so this can never masquerade as a general
cypher engine.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ne.brain_graph.age.query import (
    PredicateStyle,
    QueryShape,
    acl_labels_from_params,
    describe_query,
    discovery_order_key,
)
from ne.brain_graph.errors import GraphAclError, GraphStoreError

_REQUIRED_NODE_KEY = "key"
_REQUIRED_EDGE_ENDPOINTS = ("from_key", "to_key")


def _matches_endpoint(node: Mapping[str, Any], selector: str) -> bool:
    """True when ``selector`` names this node by key, source-record id, or class."""
    return (
        str(node.get("key", "")) == selector
        or str(node.get("id", "")) == selector
        or str(node.get("entity_class", "")) == selector
    )


def _visible(acl: Any, labels: frozenset[str]) -> bool:
    """A node/edge is visible iff its non-empty acl intersects the label set."""
    if not acl:
        return False
    return bool(set(acl) & labels)


class InMemoryGraphStore:
    """A dict-backed ``GraphStore`` with native, exactly-trimmed cypher execution."""

    def __init__(self, *, predicate_style: PredicateStyle = PredicateStyle.ANY) -> None:
        self._nodes: dict[str, dict[str, Any]] = {}
        self._edges: dict[str, dict[str, Any]] = {}
        self._predicate_style = predicate_style
        self.cypher_calls = 0

    # -- capabilities ---------------------------------------------------------

    @property
    def predicate_style(self) -> PredicateStyle:
        return self._predicate_style

    async def ensure_graph(self) -> None:
        return None

    async def stats(self) -> dict[str, int]:
        return {"nodes": len(self._nodes), "edges": len(self._edges)}

    # -- frozen GraphStore ----------------------------------------------------

    async def upsert_nodes(self, nodes: Sequence[Mapping[str, Any]]) -> None:
        for node in nodes:
            props = dict(node)
            if _REQUIRED_NODE_KEY not in props:
                raise GraphAclError("node upsert missing 'key'")
            if not props.get("acl"):
                raise GraphAclError(
                    f"node {props.get('key')!r} upsert rejected: missing/empty acl (fail-closed)"
                )
            self._nodes[props["key"]] = props

    async def upsert_edges(self, edges: Sequence[Mapping[str, Any]]) -> None:
        await self.upsert_edges_confirmed(edges)

    async def upsert_edges_confirmed(self, edges: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        persisted: list[str] = []
        for edge in edges:
            props = dict(edge)
            if _REQUIRED_NODE_KEY not in props:
                raise GraphAclError("edge upsert missing 'key'")
            for endpoint in _REQUIRED_EDGE_ENDPOINTS:
                if endpoint not in props:
                    raise GraphAclError(f"edge {props.get('key')!r} upsert missing {endpoint!r}")
            if not props.get("acl"):
                raise GraphAclError(
                    f"edge {props.get('key')!r} upsert rejected: missing/empty acl (fail-closed)"
                )
            self._edges[props["key"]] = props
            persisted.append(str(props["key"]))
        return tuple(persisted)

    async def cypher(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> Sequence[Mapping[str, Any]]:
        self.cypher_calls += 1
        shape = describe_query(query)
        if shape is None:
            raise ValueError(
                "InMemoryGraphStore.cypher only executes canonical TrimmedCypherBuilder "
                "templates (fail-closed); unrecognised cypher was refused"
            )
        labels = frozenset(acl_labels_from_params(params))
        params = params or {}
        if shape.kind == "discover":
            return self._exec_discover(shape, params, labels)
        if shape.kind == "lookup":
            return self._exec_lookup(shape, params, labels)
        return self._exec_traverse(shape, params, labels)

    async def update_acl(self, keys: Sequence[str], acl: Sequence[str]) -> None:
        if not acl:
            raise GraphAclError("update_acl rejected empty acl (fail-closed)")
        acl_t = tuple(acl)
        for key in keys:
            if key in self._nodes:
                self._nodes[key]["acl"] = acl_t
            if key in self._edges:
                self._edges[key]["acl"] = acl_t

    async def delete_edges_from_source(
        self, source_system: str, from_key: str, keep_keys: Sequence[str]
    ) -> None:
        """Delete outgoing edges of ``source_system`` from ``from_key`` not kept.

        The ``source_system`` filter is what protects foreign-source edges (e.g.
        an 'ne:er' SAME_AS edge) from a connector re-sync: an edge whose
        ``source_system`` differs is never a candidate for deletion here.
        """
        keep = set(keep_keys)
        for key in list(self._edges):
            edge = self._edges[key]
            if (
                str(edge.get("from_key", "")) == from_key
                and edge.get("source_system") == source_system
                and key not in keep
            ):
                del self._edges[key]

    async def delete_where_stale(self, source_system: str, keys: Sequence[str]) -> None:
        stale = set(keys)
        for key in list(self._nodes):
            node = self._nodes[key]
            if key in stale and node.get("source_system") == source_system:
                del self._nodes[key]
        for key in list(self._edges):
            edge = self._edges[key]
            if (
                key in stale
                or str(edge.get("from_key", "")) in stale
                or str(edge.get("to_key", "")) in stale
            ):
                del self._edges[key]

    # -- batch capabilities (non-principal; ER export + LightRAG projector) ---

    async def scan_nodes(self, entity_class: str | None = None) -> Sequence[Mapping[str, Any]]:
        """All node property maps (optionally one class) — a platform batch read."""
        return [
            dict(self._nodes[k])
            for k in sorted(self._nodes)
            if entity_class is None or self._nodes[k].get("entity_class") == entity_class
        ]

    async def scan_edges(self) -> Sequence[Mapping[str, Any]]:
        """All edge property maps — a platform batch read."""
        return [dict(self._edges[k]) for k in sorted(self._edges)]

    async def fetch_nodes(self, keys: Sequence[str]) -> Sequence[Mapping[str, Any]]:
        """Node property maps for ``keys`` (missing keys silently omitted)."""
        return [dict(self._nodes[k]) for k in keys if k in self._nodes]

    async def delete_edge(self, key: str) -> None:
        """Delete ONE edge by key — guarded to source_system 'ne:er' (unmerge-only).

        A non-'ne:er' edge is never deletable through this path (it belongs to a
        connector re-sync); an absent edge is a no-op (idempotent unmerge).
        """
        edge = self._edges.get(key)
        if edge is None:
            return
        if edge.get("source_system") != "ne:er":
            raise GraphStoreError(
                f"delete_edge refused for edge {key!r}: source_system "
                f"{edge.get('source_system')!r} is not 'ne:er' (unmerge-only guard)"
            )
        del self._edges[key]

    async def purge_llm_extracted(self, keys: Sequence[str] | None = None) -> dict[str, int]:
        """Delete every (or the given) ``source_system='ne:llm'`` node and edge (F5).

        Hardwired to ``ne:llm`` (the ``delete_edge`` 'ne:er' guard precedent) —
        a non-``ne:llm`` element, INCLUDING one explicitly named in ``keys``, is
        never DIRECTLY a delete candidate (a backbone/``ne:er`` key passed in
        ``keys`` is silently untouched — source-scoped, mirroring
        ``delete_where_stale``).

        **DETACH semantics (mirrors ``age.store.AgeGraphStore``, which forces
        this: a real graph DB cannot delete a vertex while edges still
        reference it).** Purging a node ALSO removes every edge INCIDENT to
        it, of ANY ``source_system`` — including a foreign one such as an
        ``ne:er`` ``SAME_AS`` anchoring the extracted node into an ER cluster
        (design §0 A-P1-4). An edge that is itself ``ne:llm``-sourced but NOT
        incident to any purged node (a dangling extracted edge whose
        endpoints are untouched backbone nodes) is still explicitly purged —
        it is an ``ne:llm`` element in its own right. An edge that is
        NEITHER incident to a purged node NOR itself ``ne:llm``-sourced is
        NEVER touched (the invariant this scopes to: purge never reaches a
        backbone/foreign element that isn't collateral of an ``ne:llm`` node
        it removed). Returned counts are the ACTUAL number of keys removed
        (a before/after diff), never a pre-scan's intended-to-delete size.
        """
        nodes_before = len(self._nodes)
        edges_before = len(self._edges)
        target = set(keys) if keys is not None else None

        deleted_nodes: set[str] = set()
        for key in list(self._nodes):
            node = self._nodes[key]
            if node.get("source_system") != "ne:llm":
                continue
            if target is not None and key not in target:
                continue
            del self._nodes[key]
            deleted_nodes.add(key)

        for key in list(self._edges):
            edge = self._edges[key]
            incident = (
                str(edge.get("from_key", "")) in deleted_nodes
                or str(edge.get("to_key", "")) in deleted_nodes
            )
            is_llm = edge.get("source_system") == "ne:llm"
            requested = target is not None and key in target and is_llm
            wholesale_llm = target is None and is_llm
            if incident or requested or wholesale_llm:
                del self._edges[key]

        return {
            "nodes": nodes_before - len(self._nodes),
            "edges": edges_before - len(self._edges),
        }

    # -- inspection helpers (tests only) -------------------------------------

    def node_count(self) -> int:
        return len(self._nodes)

    def edge_count(self) -> int:
        return len(self._edges)

    def raw_node(self, key: str) -> dict[str, Any] | None:
        return self._nodes.get(key)

    def raw_edge(self, key: str) -> dict[str, Any] | None:
        return self._edges.get(key)

    # -- execution ------------------------------------------------------------

    def _exec_lookup(
        self, shape: QueryShape, params: Mapping[str, Any], labels: frozenset[str]
    ) -> list[dict[str, Any]]:
        keys = set(params.get("keys", [])) if shape.has_keys else None
        name_prefix = params.get("name_prefix") if shape.has_name else None
        source_refs = set(params.get("source_refs", [])) if shape.has_source_refs else None
        rows: list[dict[str, Any]] = []
        for key in sorted(self._nodes):
            node = self._nodes[key]
            if shape.entity_class is not None and node.get("entity_class") != shape.entity_class:
                continue
            if keys is not None and key not in keys:
                continue
            if name_prefix is not None and not str(node.get("display_name", "")).startswith(
                name_prefix
            ):
                continue
            if source_refs is not None and str(node.get("source_ref", "")) not in source_refs:
                continue
            if not _visible(node.get("acl"), labels):
                continue
            rows.append({"node": dict(node)})
            if len(rows) >= shape.limit:
                break
        return rows

    def _exec_discover(
        self, shape: QueryShape, params: Mapping[str, Any], labels: frozenset[str]
    ) -> list[dict[str, Any]]:
        query = str(params["query"]) if shape.has_query else None
        source_system = str(params["source_system"]) if shape.has_source_system else None
        after = (
            (
                str(params["after_display_name"]),
                str(params["after_entity_class"]),
                str(params["after_key"]),
            )
            if shape.has_after
            else None
        )
        ordered = sorted(
            self._nodes.values(),
            key=lambda node: discovery_order_key(
                (
                    str(node.get("display_name", "")),
                    str(node.get("entity_class", "")),
                    str(node.get("key", "")),
                )
            ),
        )
        rows: list[dict[str, Any]] = []
        for node in ordered:
            keyset = (
                str(node.get("display_name", "")),
                str(node.get("entity_class", "")),
                str(node.get("key", "")),
            )
            if shape.entity_class is not None and node.get("entity_class") != shape.entity_class:
                continue
            if query is not None and not (
                str(node.get("key", "")) == query
                or query.lower() in str(node.get("display_name", "")).lower()
            ):
                continue
            if source_system is not None and node.get("source_system") != source_system:
                continue
            if after is not None and discovery_order_key(keyset) <= discovery_order_key(after):
                continue
            if not _visible(node.get("acl"), labels):
                continue
            rows.append({"node": dict(node)})
            if len(rows) >= shape.limit:
                break
        return rows

    def _neighbors(self, node_key: str, direction: str, edge_kinds: tuple[str, ...]):
        """Yield (edge_props, neighbor_key) for one hop from ``node_key``."""
        for ekey in sorted(self._edges):
            edge = self._edges[ekey]
            kind = edge.get("edge_kind")
            if edge_kinds and kind not in edge_kinds:
                continue
            frm = str(edge.get("from_key", ""))
            to = str(edge.get("to_key", ""))
            if direction in ("out", "both") and frm == node_key and to:
                yield edge, to
            if direction in ("in", "both") and to == node_key and frm:
                yield edge, frm

    def _exec_traverse(
        self, shape: QueryShape, params: Mapping[str, Any], labels: frozenset[str]
    ) -> list[dict[str, Any]]:
        if shape.kind == "paths":
            # NE-GRAPH-ANCHOR (mirrors build_paths): an endpoint selector
            # matches a node's UUID key, its source-record ``id``, or its
            # ``entity_class``. Selection only; visibility stays downstream.
            from_selector = str(params["from_key"])
            anchors = [
                key
                for key in sorted(self._nodes)
                if _matches_endpoint(self._nodes[key], from_selector)
            ]
            target = params.get("to_key")
        else:
            anchors = list(params.get("keys", []))
            target = None

        rows: list[dict[str, Any]] = []
        seen_signatures: set[tuple[str, ...]] = set()

        for anchor in anchors:
            node = self._nodes.get(anchor)
            if node is None or not _visible(node.get("acl"), labels):
                continue
            # DFS enumerating simple visible paths of length 1..hops.
            stack: list[tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]] = [
                ([node], [], {anchor})
            ]
            while stack:
                path_nodes, path_edges, visited = stack.pop()
                depth = len(path_edges)
                current = path_nodes[-1]["key"]
                if depth >= 1:
                    is_result = target is None or _matches_endpoint(path_nodes[-1], str(target))
                    if is_result:
                        signature = tuple(
                            [n["key"] for n in path_nodes] + [e["key"] for e in path_edges]
                        )
                        if signature not in seen_signatures:
                            seen_signatures.add(signature)
                            rows.append(
                                {
                                    "path_nodes": [dict(n) for n in path_nodes],
                                    "path_rels": [dict(e) for e in path_edges],
                                }
                            )
                if depth >= shape.hops:
                    continue
                for edge, neighbor_key in self._neighbors(
                    current, shape.direction, shape.edge_kinds
                ):
                    if neighbor_key in visited:
                        continue
                    neighbor = self._nodes.get(neighbor_key)
                    if neighbor is None:
                        continue
                    if not _visible(edge.get("acl"), labels):
                        continue
                    if not _visible(neighbor.get("acl"), labels):
                        continue
                    stack.append(
                        (
                            [*path_nodes, neighbor],
                            [*path_edges, edge],
                            visited | {neighbor_key},
                        )
                    )

        rows.sort(key=lambda r: (len(r["path_rels"]), [n["key"] for n in r["path_nodes"]]))
        return rows[: shape.limit]
