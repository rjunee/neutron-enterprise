# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``AgeGraphStore`` — the frozen ``GraphStore`` adapter over Apache AGE.

Implements the frozen contract (upsert_nodes / upsert_edges / cypher) plus the
package-local capabilities (update_acl / delete_where_stale / ensure_graph — the
``FilteredChunkFetch`` precedent). Raw cypher stays confined here; callers get
:class:`~ne.brain_graph.read.EntityGraph`.

``ensure_graph`` creates the graph + label tables + btree/GIN indexes on
``key``/``acl`` (idempotent, versioned DDL), then PROBES the R1 predicate ladder
(ANY → SIZE_COMPREHENSION → POST_FILTER) so the selected rung is runtime-
determined, documented (README), and test-pinnable. Upserts are batched
``MERGE`` on ``key`` (property overwrite); a node/edge with missing/empty ``acl``
is rejected with :class:`GraphAclError` (write-boundary fail-closed). Every
``entity_class``/``edge_kind`` is allowlist-validated against ``ENTITY_CLASSES`` /
``EDGE_KINDS | {SAME_AS}`` BEFORE it is interpolated into cypher, so a
caller/payload-derived label can never become executed cypher (injection guard).

LIVE-PARKED: exercised by the server-marked conformance tests against a real AGE
container (``NE_AGE_DSN``); the always-on path uses the in-memory reference store.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from ne.brain_graph import _pg
from ne.brain_graph.age.agtype import decode_agtype
from ne.brain_graph.age.query import PredicateStyle, describe_query
from ne.brain_graph.errors import GraphAclError, GraphStoreError
from ne.schema import EDGE_KINDS, ENTITY_CLASSES

__all__ = ["AgeGraphStore"]

_UPSERT_BATCH_SIZE = 64

_REQUIRED_NODE_PROPS = (
    "key",
    "entity_class",
    "display_name",
    "source_system",
    "source_ref",
    "acl",
    "content_hash",
    "schema_version",
    "config_hash",
    "synced_at",
)
_REQUIRED_EDGE_PROPS = (
    "key",
    "edge_kind",
    "source_system",
    "source_ref",
    "acl",
    "config_hash",
    "synced_at",
    "from_key",
    "to_key",
)

#: The edge-kind allowlist for cypher interpolation: the schema ``EDGE_KINDS`` plus
#: the ER lane's non-destructive merge edge ``SAME_AS`` (the literal
#: ``ne.brain_graph.er.merge.SAME_AS_EDGE_KIND`` — duplicated here, NOT imported, to
#: keep ``age/`` free of an ``er/`` import edge).
_EDGE_KIND_ALLOWLIST: frozenset[str] = frozenset(EDGE_KINDS) | {"SAME_AS"}


def _validate_label(label: str, allowed: Collection[str], what: str) -> str:
    """Allowlist a cypher identifier before it is interpolated (injection guard)."""
    if label not in allowed:
        raise GraphStoreError(
            f"unknown {what} {label!r}: not in the {what} allowlist "
            "(cypher-identifier injection guard)"
        )
    return label


def _require_props(props: Mapping[str, Any], required: Sequence[str], what: str) -> None:
    missing = [p for p in required if p not in props]
    if missing:
        raise GraphStoreError(f"{what} {props.get('key')!r} missing required props {missing}")
    if not props.get("acl"):
        raise GraphAclError(
            f"{what} {props.get('key')!r} upsert rejected: missing/empty acl (fail-closed)"
        )
    # Allowlist the cypher identifier (node label / edge kind) BEFORE any batch
    # reaches the database — a payload-derived identifier can never become executed
    # cypher (the frozen GraphStore chokepoint's documented injection guard).
    if what == "node":
        _validate_label(str(props["entity_class"]), ENTITY_CLASSES, "entity_class")
    elif what == "edge":
        _validate_label(str(props["edge_kind"]), _EDGE_KIND_ALLOWLIST, "edge_kind")


class AgeGraphStore:
    """Apache AGE-backed graph store (psycopg via the ``_pg`` chokepoint)."""

    def __init__(self, dsn: str, graph_name: str = "ne_graph") -> None:
        self._dsn = dsn
        self._graph_name = graph_name
        self._conn: Any = None
        self._predicate_style = PredicateStyle.POST_FILTER

    @property
    def predicate_style(self) -> PredicateStyle:
        return self._predicate_style

    async def _connection(self) -> Any:
        if self._conn is None:
            self._conn = await _pg.connect(self._dsn)
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # -- ensure_graph + probe -------------------------------------------------

    async def ensure_graph(self) -> None:
        conn = await self._connection()
        await _pg.execute_sql(
            conn,
            "SELECT create_graph(%s) WHERE NOT EXISTS "
            "(SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s)",
            (self._graph_name, self._graph_name),
        )
        await self._ensure_indexes(conn)
        self._predicate_style = await self._probe_predicate_rung(conn)

    async def _ensure_indexes(self, conn: Any) -> None:
        # Label tables live under the graph's schema; btree(key)/GIN(acl) indexes
        # are created idempotently per label. Best-effort — a missing label table
        # simply means no node of that class exists yet.
        for label in ("Account", "Person", "Document", "Ticket", "Order"):
            table = f'"{self._graph_name}"."{label}"'
            try:
                await _pg.execute_sql(
                    conn,
                    f'CREATE INDEX IF NOT EXISTS "idx_{label}_key" ON {table} '
                    "USING btree ((properties->>'key'))",
                )
                await _pg.execute_sql(
                    conn,
                    f'CREATE INDEX IF NOT EXISTS "idx_{label}_acl" ON {table} '
                    "USING gin (properties)",
                )
            except GraphStoreError:  # pragma: no cover - label table not yet created
                continue

    async def _probe_predicate_rung(self, conn: Any) -> PredicateStyle:
        canaries = {
            PredicateStyle.ANY: (
                "MATCH (n) WHERE any(l IN n.acl WHERE l IN $acl_labels) RETURN 1 AS probe LIMIT 1"
            ),
            PredicateStyle.SIZE_COMPREHENSION: (
                "MATCH (n) WHERE size([l IN n.acl WHERE l IN $acl_labels]) > 0 "
                "RETURN 1 AS probe LIMIT 1"
            ),
        }
        for style, text in canaries.items():
            try:
                await _pg.execute_cypher(
                    conn,
                    self._graph_name,
                    text,
                    {"acl_labels": ["__probe__"]},
                    result_columns=("probe",),
                )
                return style
            except Exception:
                continue
        return PredicateStyle.POST_FILTER

    # -- frozen GraphStore ----------------------------------------------------

    async def upsert_nodes(self, nodes: Sequence[Mapping[str, Any]]) -> None:
        items = list(nodes)
        for node in items:
            _require_props(node, _REQUIRED_NODE_PROPS, "node")
        conn = await self._connection()
        for start in range(0, len(items), _UPSERT_BATCH_SIZE):
            batch = items[start : start + _UPSERT_BATCH_SIZE]
            by_label: dict[str, list[dict[str, Any]]] = {}
            for node in batch:
                by_label.setdefault(str(node["entity_class"]), []).append(dict(node))
            for label, rows in by_label.items():
                text = f"UNWIND $rows AS row MERGE (n:{label} {{key: row.key}}) SET n = row"
                await _pg.execute_cypher(
                    conn, self._graph_name, text, {"rows": rows}, result_columns=("result",)
                )

    async def upsert_edges(self, edges: Sequence[Mapping[str, Any]]) -> None:
        await self.upsert_edges_confirmed(edges)

    async def upsert_edges_confirmed(self, edges: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        items = list(edges)
        for edge in items:
            _require_props(edge, _REQUIRED_EDGE_PROPS, "edge")
        expected = tuple(str(edge["key"]) for edge in items)
        if len(set(expected)) != len(expected):
            raise GraphStoreError(
                "edge upsert requires unique keys for exact persisted edge acknowledgment"
            )
        if not items:
            return ()
        conn = await self._connection()
        acknowledged: list[str] = []
        for start in range(0, len(items), _UPSERT_BATCH_SIZE):
            batch = items[start : start + _UPSERT_BATCH_SIZE]
            by_kind: dict[str, list[dict[str, Any]]] = {}
            for edge in batch:
                by_kind.setdefault(str(edge["edge_kind"]), []).append(dict(edge))
            for kind, rows in by_kind.items():
                text = (
                    "UNWIND $rows AS row "
                    "MATCH (a {key: row.from_key}), (b {key: row.to_key}) "
                    f"MERGE (a)-[e:{kind} {{key: row.key}}]->(b) SET e = row "
                    "RETURN e.key AS result"
                )
                cells = await _pg.execute_cypher(
                    conn, self._graph_name, text, {"rows": rows}, result_columns=("result",)
                )
                acknowledged.extend(str(decode_agtype(cell[0])) for cell in cells)
        if (
            len(acknowledged) != len(expected)
            or len(set(acknowledged)) != len(acknowledged)
            or set(acknowledged) != set(expected)
        ):
            raise GraphStoreError("AGE did not return the exact persisted edge acknowledgment")
        return tuple(acknowledged)

    async def cypher(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> Sequence[Mapping[str, Any]]:
        conn = await self._connection()
        shape = describe_query(query)
        if shape is not None and shape.kind in {"lookup", "discover"}:
            cells = await _pg.execute_cypher(
                conn,
                self._graph_name,
                query,
                params,
                result_columns=("node",),
            )
            return [{"node": decode_agtype(cell[0])} for cell in cells]
        cells = await _pg.execute_cypher(
            conn,
            self._graph_name,
            query,
            params,
            result_columns=("path_nodes", "path_rels"),
        )
        return [
            {"path_nodes": decode_agtype(cell[0]), "path_rels": decode_agtype(cell[1])}
            for cell in cells
        ]

    # -- capabilities ---------------------------------------------------------

    async def update_acl(self, keys: Sequence[str], acl: Sequence[str]) -> None:
        if not acl:
            raise GraphAclError("update_acl rejected empty acl (fail-closed)")
        conn = await self._connection()
        text = "MATCH (n {key: $key}) SET n.acl = $acl RETURN 1 AS result"
        edge_text = "MATCH ()-[e {key: $key}]-() SET e.acl = $acl RETURN 1 AS result"
        for key in keys:
            await _pg.execute_cypher(
                conn,
                self._graph_name,
                text,
                {"key": key, "acl": list(acl)},
                result_columns=("result",),
            )
            await _pg.execute_cypher(
                conn,
                self._graph_name,
                edge_text,
                {"key": key, "acl": list(acl)},
                result_columns=("result",),
            )

    async def delete_edges_from_source(
        self, source_system: str, from_key: str, keep_keys: Sequence[str]
    ) -> None:
        """Delete outgoing edges of ``source_system`` from ``from_key`` not kept.

        Fetch-then-per-key-delete (sidesteps AGE list-predicate uncertainty, the
        ``delete_where_stale`` per-key style): one internal cypher lists the
        candidate edge keys, we client-filter against ``keep_keys``, then delete
        per-key with a parameterized ``source_system`` guard. The guard is what
        protects foreign-source edges (e.g. 'ne:er' SAME_AS) from a connector
        re-sync.
        """
        conn = await self._connection()
        keep = set(keep_keys)
        list_text = (
            "MATCH ({key: $from_key})-[e]->() WHERE e.source_system = $source "
            "RETURN e.key AS result"
        )
        cells = await _pg.execute_cypher(
            conn,
            self._graph_name,
            list_text,
            {"from_key": from_key, "source": source_system},
            result_columns=("result",),
        )
        edge_keys = [str(decode_agtype(cell[0])) for cell in cells]
        delete_text = (
            "MATCH ({key: $from_key})-[e {key: $key}]->() "
            "WHERE e.source_system = $source DELETE e RETURN 1 AS result"
        )
        for edge_key in edge_keys:
            if edge_key in keep:
                continue
            await _pg.execute_cypher(
                conn,
                self._graph_name,
                delete_text,
                {"from_key": from_key, "key": edge_key, "source": source_system},
                result_columns=("result",),
            )

    async def delete_where_stale(self, source_system: str, keys: Sequence[str]) -> None:
        conn = await self._connection()
        node_text = (
            "MATCH (n {key: $key}) WHERE n.source_system = $source "
            "DETACH DELETE n RETURN 1 AS result"
        )
        for key in keys:
            await _pg.execute_cypher(
                conn,
                self._graph_name,
                node_text,
                {"key": key, "source": source_system},
                result_columns=("result",),
            )

    async def scan_nodes(self, entity_class: str | None = None) -> Sequence[Mapping[str, Any]]:
        """All node property maps (optionally one class) — a platform batch read."""
        conn = await self._connection()
        if entity_class is not None:
            text = (
                f"MATCH (n:{_validate_label(entity_class, ENTITY_CLASSES, 'entity_class')}) "
                "RETURN properties(n) AS result"
            )
        else:
            text = "MATCH (n) RETURN properties(n) AS result"
        cells = await _pg.execute_cypher(
            conn, self._graph_name, text, {}, result_columns=("result",)
        )
        return [decode_agtype(cell[0]) for cell in cells]

    async def scan_edges(self) -> Sequence[Mapping[str, Any]]:
        """All edge property maps — a platform batch read."""
        conn = await self._connection()
        cells = await _pg.execute_cypher(
            conn,
            self._graph_name,
            "MATCH ()-[e]->() RETURN properties(e) AS result",
            {},
            result_columns=("result",),
        )
        return [decode_agtype(cell[0]) for cell in cells]

    async def fetch_nodes(self, keys: Sequence[str]) -> Sequence[Mapping[str, Any]]:
        """Node property maps for ``keys`` (missing keys silently omitted)."""
        conn = await self._connection()
        out: list[Mapping[str, Any]] = []
        for key in keys:
            cells = await _pg.execute_cypher(
                conn,
                self._graph_name,
                "MATCH (n {key: $key}) RETURN properties(n) AS result",
                {"key": key},
                result_columns=("result",),
            )
            if cells:
                out.append(decode_agtype(cells[0][0]))
        return out

    async def delete_edge(self, key: str) -> None:
        """Delete ONE edge by key — guarded to source_system 'ne:er' (unmerge-only).

        A non-'ne:er' edge is never deletable through this path; an absent edge is
        a no-op (idempotent unmerge).
        """
        conn = await self._connection()
        cells = await _pg.execute_cypher(
            conn,
            self._graph_name,
            "MATCH ()-[e {key: $key}]->() RETURN e.source_system AS result",
            {"key": key},
            result_columns=("result",),
        )
        if not cells:
            return
        source_system = str(decode_agtype(cells[0][0]))
        if source_system != "ne:er":
            raise GraphStoreError(
                f"delete_edge refused for edge {key!r}: source_system "
                f"{source_system!r} is not 'ne:er' (unmerge-only guard)"
            )
        await _pg.execute_cypher(
            conn,
            self._graph_name,
            "MATCH ()-[e {key: $key}]->() WHERE e.source_system = 'ne:er' DELETE e "
            "RETURN 1 AS result",
            {"key": key},
            result_columns=("result",),
        )

    async def purge_llm_extracted(self, keys: Sequence[str] | None = None) -> dict[str, int]:
        """Delete every (or the given) ``source_system='ne:llm'`` node and edge (F5).

        Hardwired to ``ne:llm`` (the ``delete_edge`` 'ne:er' guard precedent):
        a backbone/foreign-source key passed in ``keys`` is silently
        untouched (source-scoped, mirroring ``delete_where_stale``).

        **DETACH semantics (deliberate, graph-DB-forced — Argus P1).** AGE
        refuses to delete a vertex that still has edges attached; a plain
        (non-``DETACH``) node delete against a node with ANY incident edge
        fails loudly. Reversibility (F5) requires the purged node to
        actually be gone, so this method ``DETACH DELETE``s purged nodes:
        EVERY edge incident to a purged node — including a foreign-source
        one such as an ``ne:er`` ``SAME_AS`` anchoring the extracted node
        into an ER cluster (design §0 A-P1-4) — is removed as unavoidable
        collateral; a graph store cannot leave an edge pointing at a vertex
        that no longer exists. An edge that is ``ne:llm``-sourced but NOT
        incident to any purged node (a dangling extracted edge between two
        untouched backbone nodes) is still explicitly purged via a plain
        (non-``DETACH``) delete, since no node delete is involved. An edge
        that is NEITHER incident to a purged node NOR itself
        ``ne:llm``-sourced is NEVER reached by this method — mirrors
        ``testing.memory.InMemoryGraphStore.purge_llm_extracted`` exactly
        (kit-enforced parity is over THESE semantics, not the earlier plain-
        delete draft the kit test caught as unrepresentable on AGE).

        Returned counts are ACTUAL rows removed — a before/after ``scan``
        diff, not the pre-scan's intended-to-delete set — so a guard
        mismatch, a race, or a partial failure is never silently
        over-reported into the ``graph.extraction_revert``/
        ``graph.extraction_purge`` audit detail (Argus P2).

        Materializes the full graph exactly twice (once before the deletes,
        once after) — not four times (Argus P3 efficiency finding: the
        earlier draft scanned nodes/edges once for the ``_before`` sets and
        again, redundantly, for the delete-candidate computation).
        """
        all_nodes = await self.scan_nodes()
        all_edges = await self.scan_edges()
        nodes_before = {str(n["key"]) for n in all_nodes}
        edges_before = {str(e["key"]) for e in all_edges}
        target = set(keys) if keys is not None else None

        llm_node_keys = {
            str(n["key"])
            for n in all_nodes
            if n.get("source_system") == "ne:llm" and (target is None or str(n["key"]) in target)
        }
        incident_edge_keys = {
            str(e["key"])
            for e in all_edges
            if str(e.get("from_key", "")) in llm_node_keys
            or str(e.get("to_key", "")) in llm_node_keys
        }
        explicit_llm_edge_keys = {
            str(e["key"])
            for e in all_edges
            if e.get("source_system") == "ne:llm"
            and str(e["key"]) not in incident_edge_keys
            and (target is None or str(e["key"]) in target)
        }

        conn = await self._connection()
        edge_delete_text = (
            "MATCH ()-[e {key: $key}]->() WHERE e.source_system = 'ne:llm' DELETE e "
            "RETURN 1 AS result"
        )
        for key in explicit_llm_edge_keys:
            await _pg.execute_cypher(
                conn, self._graph_name, edge_delete_text, {"key": key}, result_columns=("result",)
            )
        node_delete_text = (
            "MATCH (n {key: $key}) WHERE n.source_system = 'ne:llm' DETACH DELETE n "
            "RETURN 1 AS result"
        )
        for key in llm_node_keys:
            await _pg.execute_cypher(
                conn, self._graph_name, node_delete_text, {"key": key}, result_columns=("result",)
            )

        nodes_after = {str(n["key"]) for n in await self.scan_nodes()}
        edges_after = {str(e["key"]) for e in await self.scan_edges()}
        return {
            "nodes": len(nodes_before - nodes_after),
            "edges": len(edges_before - edges_after),
        }

    async def stats(self) -> dict[str, int]:
        conn = await self._connection()
        node_cells = await _pg.execute_cypher(
            conn,
            self._graph_name,
            "MATCH (n) RETURN count(n) AS result",
            result_columns=("result",),
        )
        edge_cells = await _pg.execute_cypher(
            conn,
            self._graph_name,
            "MATCH ()-[r]-() RETURN count(r) AS result",
            result_columns=("result",),
        )
        nodes = int(decode_agtype(node_cells[0][0])) if node_cells else 0
        edges = int(decode_agtype(edge_cells[0][0])) if edge_cells else 0
        return {"nodes": nodes, "edges": edges}
