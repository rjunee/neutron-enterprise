# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""``AgeDriver`` — Graphiti's ``GraphDriver`` seam implemented over Apache AGE.

SEAM DECISIONS (recorded from the pinned graphiti-core==0.29.2 installed source read
at build time — the STEP-2 hard prerequisite):

* **provider base = ``GraphProvider.NEO4J``.** The feasibility memo predicted a
  FalkorDB base (row 2), but inspecting the pinned source, riding NEO4J yields FEWER
  residual rewrites: the generic node/edge write path in ``nodes.py`` / ``edges.py``
  falls through to ``execute_query(get_<x>_query(driver.provider), **args)``, and
  ``graph_queries.py`` shows the FALKORDB branch injects RediSearch fulltext
  procedures + ``vecf32()`` vector wrappers + string-interpolated labels, whereas the
  NEO4J (default ``return``) branch emits plain openCypher closest to what AGE's
  ``cypher()`` accepts. We therefore ride NEO4J and translate the small residue in
  :meth:`execute_query` (:func:`graphiti_age._dialect.rewrite_cypher`).

* **``graph_operations_interface`` = ``None``.** ``nodes.py``/``edges.py`` consult
  ``driver.graph_operations_interface`` first and fall back (``except
  NotImplementedError``) to the generic provider-branched cypher via
  ``driver.execute_query``. Leaving it ``None`` routes ALL writes/gets/deletes through
  :meth:`execute_query`, where the datetime codec + embedding extraction + dialect
  rewrite live — one chokepoint, the Neptune ``_sanitize_parameters`` precedent.

* **``search_interface`` = :class:`AgeSearchInterface`.** ``search/search_utils.py``
  dispatches fulltext/vector search on ``driver.provider`` with hardcoded
  KUZU/NEPTUNE/FALKORDB branches (default = Neo4j ``CALL db.index.fulltext.*`` +
  ``vector.similarity.cosine`` — constructs AGE CANNOT execute). But EACH search
  function first checks ``if driver.search_interface: return await
  driver.search_interface.<op>(...)`` (e.g. search_utils.py:586). Setting
  ``search_interface`` therefore makes fulltext/vector search NEVER reach cypher — it
  runs against the ``{graph}_ft`` / ``{graph}_vec`` side tables instead. This is the
  clean delegation seam; Neptune itself does not use it (it relies on its hardcoded
  provider branch), so a vendored driver cannot copy Neptune literally — the seam it
  DOES expose is ``search_interface``, which we use.

* **``build_indices_and_constraints``** is OVERRIDDEN completely — it never calls
  ``get_range_indices`` / ``get_fulltext_indices`` (those emit Neo4j-only
  ``CREATE FULLTEXT INDEX`` DDL). It creates the AGE graph, per-label btree indexes,
  the ft/vec side tables, and runs the vector + predicate probes.

LADDER WIRING STATUS (stated plainly — all three wired): the three R1 fail-closed
ladders are ALL runtime-probed in :meth:`build_indices_and_constraints` AND applied in
the query path — **vector** (pgvector → client-cosine, drives ``_vec_upsert``/
``_vec_search``), **predicate** (``self._predicate_style``, applied by
:func:`~graphiti_age._dialect.apply_predicate_style` in :meth:`execute_query`: ANY
passthrough → SIZE_COMPREHENSION rewrite → predicate-stripped POST_FILTER + an in-driver
:func:`~graphiti_age._dialect.passes_label_post_filter`), and **traversal**
(``self._traversal_style``, probed via ``_probe_traversal_rung`` and applied by
:func:`~graphiti_age._dialect.apply_traversal_style`: clamped DIRECT_VLE →
bounded-UNION-arms for the two Graphiti BFS shapes). On the shipping AGE 1.6.0 image all
three probes select the DIRECT rungs (ANY predicate, direct VLE, pgvector-or-client
vector), so behavior — and the dual-backend AGE==Neo4j conformance — is UNCHANGED; on a
future AGE build that rejects ``any()`` / ``*1..n`` the probed rung auto-degrades
(SIZE_COMPREHENSION / POST_FILTER; UNION-arms). A query shape a degraded rung cannot
express (POST_FILTER without a whole-node projection, a non-canonical label-``any()``, an
unbounded VLE, or an unsupported UNION-arms shape) still raises
``AgeDialectError``/``AgeQueryError`` — fail-loud, never silently wrong. On a degraded
rung a trailing ``LIMIT`` is popped from the cypher and enforced in-driver after the
label filter / union (never an under-count from limiting the unfiltered set, never
``arms x limit`` over-count; a non-trailing ``LIMIT``/``SKIP`` or an ``ORDER BY`` across
UNION arms fails loud). The degraded rungs cover the canonical Graphiti 0.29.2 shapes and
are proven by forced-rung unit tests (a live AGE build that actually rejects the direct
forms is not available here).

WRITE-PATH / SEARCH-PATH BEHAVIORAL EQUIVALENCE — LIVE-VALIDATED at build time:
the full episodic append → AGE graph facts → temporal/traversal/group/fulltext/vector
recall round-trip was run end-to-end against ``apache/age:release_PG17_1.6.0`` (and the
AGE==Neo4j-CE canonical-JSON equality against ``neo4j:5.26-community``) via
``conformance.py`` + ``tests/test_{age_driver_server,dual_backend_conformance}.py``.
Those live gates skip cleanly on a docker-less host (the always-on suite pins the
codec/dialect/pg/flip constants + every dialect rewrite), but they are the standing
behavioral proof and DID pass live. See ``docs/architecture/ne083c-graphiti-age-upstream.md``
(Live-validation section) for the AGE ``cypher()`` invocation-form correction and the
three write-path rewrites (``SET x = properties($p)``, ``SET n:Label`` strip, vector-
property ``CALL`` strip) that running it live forced.
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

from graphiti_core.driver.driver import GraphDriver, GraphDriverSession, GraphProvider
from graphiti_core.driver.search_interface.search_interface import SearchInterface

from graphiti_age import _codec, _pg
from graphiti_age._dialect import (
    PREDICATE_CANARIES,
    TRAVERSAL_CANARY,
    PredicateStyle,
    TraversalStyle,
    VectorBackend,
    apply_predicate_style,
    apply_traversal_style,
    cosine,
    filter_edge_candidates,
    ft_ddl,
    ft_search_sql,
    ft_upsert_sql,
    has_vle_segment,
    passes_label_post_filter,
    plan_side_tables,
    pop_trailing_limit,
    return_aliases,
    rewrite_cypher,
    validate_graph_name,
    validate_label,
    vec_client_ddl,
    vec_client_search_sql,
    vec_pgvector_ddl,
    vec_pgvector_search_sql,
    vec_table_exists_sql,
)

__all__ = ["AgeDriver", "AgeDriverSession", "AgeSearchInterface"]

DEFAULT_GRAPH = "ne_memory"

# Graphiti node/edge labels this driver creates btree indexes for (from
# graph_queries.py get_range_indices, NEO4J branch).
_INDEX_LABELS = ("Entity", "Episodic", "Community", "Saga", "RELATES_TO", "MENTIONS")


class AgeSearchInterface(SearchInterface):
    """Fulltext + vector search over the AGE side tables (never reaches cypher).

    LIVE-PARKED behavioral surface: the methods below hit ``{graph}_ft`` /
    ``{graph}_vec`` and reconstruct the entity/edge objects by uuid lookups through
    the driver. Exercised by the LIVE-PARKED conformance run, not the always-on suite.
    """

    async def node_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        hits = await driver._ft_search(query, "node", group_ids, limit)
        return await driver._entity_nodes_by_uuid([u for u, _ in hits])

    async def edge_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        hits = await driver._ft_search(query, "edge", group_ids, limit)
        return await driver._entity_edges_by_uuid([u for u, _ in hits])

    async def node_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
        min_score: float = 0.7,
    ) -> list[Any]:
        hits = await driver._vec_search(search_vector, "node", "name_embedding", group_ids, limit)
        # min_score parity with Graphiti's Neo4j path (search_utils.py: WHERE score >
        # $min_score, strict): drop sub-threshold hits before reconstruction so AGE
        # returns the same set Neo4j does at the default threshold, not a superset.
        return await driver._entity_nodes_by_uuid([u for u, s in hits if s > min_score])

    async def edge_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        source_node_uuid: str | None,
        target_node_uuid: str | None,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
        min_score: float = 0.7,
    ) -> list[Any]:
        # source/target constraints are applied IN the vec-search SQL (via the vec
        # table's source_uuid / target_uuid endpoint columns), matching the in-query
        # filter of Graphiti's Neo4j edge-similarity path — so this cannot under-return
        # the way a bounded post-filter fetch window could. min_score parity is applied
        # before reconstruction (strict >, upstream semantics). filter_edge_candidates
        # is kept as cheap fail-closed defense-in-depth over the reconstructed edges.
        hits = await driver._vec_search(
            search_vector,
            "edge",
            "fact_embedding",
            group_ids,
            limit,
            source_uuid=source_node_uuid,
            target_uuid=target_node_uuid,
        )
        uuids = [u for u, s in hits if s > min_score]
        edges = await driver._entity_edges_by_uuid(uuids)
        return filter_edge_candidates(edges, source_node_uuid, target_node_uuid)

    async def episode_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        hits = await driver._ft_search(query, "node", group_ids, limit)
        return await driver._episodic_nodes_by_uuid([u for u, _ in hits])

    def build_node_search_filters(self, search_filters: Any) -> Any:
        return search_filters

    def build_edge_search_filters(self, search_filters: Any) -> Any:
        return search_filters


class AgeDriverSession(GraphDriverSession):
    """Immediate-execution session (AGE autocommit — the FalkorDB/Neptune posture)."""

    provider = GraphProvider.NEO4J

    def __init__(self, driver: AgeDriver) -> None:
        self._driver = driver

    async def __aenter__(self) -> AgeDriverSession:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute_write(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return await func(self, *args, **kwargs)

    async def run(self, query: str | list[Any], **kwargs: Any) -> Any:
        if isinstance(query, list):
            result: Any = None
            for cypher, params in query:
                result = await self._driver.execute_query(cypher, **dict(params or {}))
            return result
        return await self._driver.execute_query(str(query), **kwargs)


class AgeDriver(GraphDriver):
    """Apache AGE-backed Graphiti driver (psycopg via the ``_pg`` chokepoint)."""

    provider = GraphProvider.NEO4J
    default_group_id: str = ""
    fulltext_syntax: str = ""
    graph_operations_interface = None
    aoss_client: None = None

    def __init__(self, dsn: str, graph_name: str = DEFAULT_GRAPH) -> None:
        super().__init__()
        self._dsn = dsn
        self._graph_name = validate_graph_name(graph_name)
        self._database = graph_name
        self._conn: Any = None
        self._predicate_style = PredicateStyle.POST_FILTER
        self._traversal_style = TraversalStyle.UNION_ARMS
        self._vector_backend = VectorBackend.CLIENT
        self.search_interface = AgeSearchInterface()

    # -- connection -----------------------------------------------------------

    async def _connection(self) -> Any:
        if self._conn is None:
            self._conn = await _pg.connect(self._dsn)
        return self._conn

    def session(self, database: str | None = None) -> GraphDriverSession:
        return AgeDriverSession(self)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # -- execute_query (the write/read chokepoint) ----------------------------

    async def execute_query(self, cypher_query_: str, **kwargs: Any) -> Any:
        """Translate Graphiti Neo4j-cypher → AGE and return ``(records, header, None)``.

        Pipeline: datetimes are ISO-encoded and ``*_embedding`` params extracted
        (:func:`_codec.sanitize_params`); :func:`plan_side_tables` detects the Graphiti
        save shapes (``entity_data`` / ``edge_data`` / episodic-flat), strips nested
        embeddings out of agtype (feasibility row 10) and yields the ft/vec side-table
        rows; the cypher text passes through :func:`rewrite_cypher` (``SET n = $p`` →
        ``SET n = properties($p)``, fail-loud on ``CALL db.*``), then the two probed R1
        rungs are applied — :func:`apply_predicate_style` (ANY passthrough / SIZE-comp
        rewrite / predicate-stripped POST_FILTER, emitting the in-driver label specs) and
        :func:`apply_traversal_style` (clamped direct VLE / UNION-arms) — and executes via
        the ``_pg`` chokepoint with generated ``col{i}`` result columns; POST_FILTER label
        specs are then applied over the returned records (:func:`passes_label_post_filter`)
        and the ft/vec side tables are populated so the SearchInterface can recall. On a
        degraded rung (POST_FILTER or UNION_ARMS) a trailing ``LIMIT`` is popped via
        :func:`pop_trailing_limit` before the query runs and enforced in-driver AFTER the
        label filter / union — so the driver never under-counts from limiting the
        unfiltered set nor over-counts ``arms x limit`` (fail-loud on a non-trailing
        ``LIMIT``/``SKIP`` and on an ``ORDER BY`` across UNION arms).
        """
        params = kwargs.pop("params", None) or {}
        kwargs.pop("routing_", None)
        kwargs.pop("database_", None)
        merged = {**dict(params), **kwargs}
        clean, top_embeddings = _codec.sanitize_params(merged)
        clean, ft_rows, vec_rows = plan_side_tables(clean)

        text = rewrite_cypher(str(cypher_query_))
        text, label_specs = apply_predicate_style(text, self._predicate_style)
        # Degraded-rung LIMIT correctness (NE-083c-FU2): an in-query trailing LIMIT would
        # bound the UNFILTERED candidate set on the POST_FILTER rung (under-return) and be
        # duplicated per arm by UNION_ARMS (arms x limit over-return), so on those rungs it
        # is popped here and enforced in-driver after the label filter / union. On the
        # shipping AGE 1.6.0 direct rungs label_specs is [] and the UNION_ARMS condition is
        # false, so the pop is unreachable and the query text is unchanged.
        in_driver_limit: int | None = None
        if label_specs or (
            self._traversal_style is TraversalStyle.UNION_ARMS and has_vle_segment(text)
        ):
            text, in_driver_limit = pop_trailing_limit(text, clean)
        text = apply_traversal_style(text, self._traversal_style)
        aliases = return_aliases(text)
        conn = await self._connection()
        cells = await _pg.execute_cypher(
            conn,
            self._graph_name,
            text,
            clean,
            column_count=len(aliases),
        )

        await self._populate_side_tables(conn, clean, ft_rows, vec_rows, top_embeddings)

        records: list[dict[str, Any]] = []
        for row in cells:
            record: dict[str, Any] = {}
            for alias, raw in zip(aliases, row, strict=False):
                # A None result cell is a SQL NULL — a cypher projection of an absent /
                # null property (e.g. an edge's unset expired_at/invalid_at). That is a
                # legitimate null value (Neo4j returns None too), NOT malformed agtype,
                # so map it straight to None rather than letting the strict codec raise
                # (which would otherwise drop an entire row on reconstruction).
                if raw is None:
                    record[alias] = None
                    continue
                decoded = _codec.decode_agtype(raw)
                if isinstance(decoded, dict) and isinstance(decoded.get("properties"), dict):
                    decoded["properties"] = _codec.decode_temporal_props(decoded["properties"])
                record[alias] = decoded
            records.append(record)
        if label_specs:
            # POST_FILTER rung: the label predicate was stripped to `true` in-query, so
            # enforce it in-driver here — parity with Neo4j's any(l IN n.labels …) (a
            # node with null/absent labels is dropped, never silently returned).
            records = [
                r
                for r in records
                if all(passes_label_post_filter(r, s, clean) for s in label_specs)
            ]
        if in_driver_limit is not None:
            # Enforce the popped trailing LIMIT over the ACTUALLY-matching / unioned rows —
            # Neo4j filter-then-limit parity (`is not None` so LIMIT 0 still means 0 rows).
            records = records[:in_driver_limit]
        return records, aliases, None

    async def _populate_side_tables(
        self,
        conn: Any,
        clean: dict[str, Any],
        ft_rows: list[tuple[str, str, str | None, str]],
        vec_rows: list[tuple[str, str, str, str | None, str | None, str | None, list[float]]],
        top_embeddings: dict[str, list[float]],
    ) -> None:
        """Upsert the ft/vec side-table rows planned for this write (best-effort).

        Side-table population never fails a graph write: the tables may not exist for a
        read-only query, and recall is a defense-in-depth surface over the graph facts.
        """
        try:
            for uuid, kind, group_id, content in ft_rows:
                if uuid:
                    await self._ft_upsert(conn, str(uuid), kind, group_id, content)
            for uuid, kind, field, group_id, source_uuid, target_uuid, vector in vec_rows:
                if uuid and vector:
                    await self._vec_upsert(
                        conn, str(uuid), kind, field, group_id, source_uuid, target_uuid, vector
                    )
            # Legacy path: a top-level *_embedding param with a sibling top-level uuid.
            # Endpoints are unknown here, so such rows carry NULL source/target and are
            # therefore excluded by any endpoint-filtered edge-similarity search
            # (fail-closed — the primary write path threads the real endpoints).
            uuid = clean.get("uuid")
            if top_embeddings and uuid:
                group_id = clean.get("group_id")
                kind = "edge" if any(k.startswith("fact") for k in top_embeddings) else "node"
                for field, vector in top_embeddings.items():
                    await self._vec_upsert(
                        conn, str(uuid), kind, field, group_id, None, None, vector
                    )
        except _pg.AgeQueryError:
            # Side tables not materialised yet (write before build_indices) — the graph
            # facts are already persisted; recall is defense-in-depth. Best-effort.
            return

    # -- indices / side tables / probes --------------------------------------

    async def build_indices_and_constraints(self, delete_existing: bool = False) -> None:
        conn = await self._connection()
        await _pg.execute_sql(
            conn,
            "SELECT create_graph(%s) WHERE NOT EXISTS "
            "(SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s)",
            (self._graph_name, self._graph_name),
        )
        await self._ensure_label_indexes(conn)
        for stmt in ft_ddl(self._graph_name):
            await _pg.execute_sql(conn, stmt)
        self._vector_backend = await self._probe_vector_backend(conn)
        self._predicate_style = await self._probe_predicate_rung(conn)
        self._traversal_style = await self._probe_traversal_rung(conn)

    async def _ensure_label_indexes(self, conn: Any) -> None:
        for label in _INDEX_LABELS:
            validate_label(label)
            table = f'"{self._graph_name}"."{label}"'
            for prop in ("uuid", "group_id"):
                try:
                    await _pg.execute_sql(
                        conn,
                        f'CREATE INDEX IF NOT EXISTS "idx_{self._graph_name}_{label}_{prop}" '
                        f"ON {table} USING btree ((properties->>'{prop}'))",
                    )
                except _pg.AgeQueryError:
                    continue  # label table not materialised yet — best-effort

    async def _probe_vector_backend(self, conn: Any) -> VectorBackend:
        try:
            await _pg.execute_sql(conn, "CREATE EXTENSION IF NOT EXISTS vector")
            rows = await _pg.execute_sql(
                conn, "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            )
            if rows:
                # Create the {graph}_vec table (undimensioned) up front — symmetric with
                # the CLIENT branch below — so a similarity search against a fresh /
                # episode-only graph returns empty like Neo4j, never UndefinedTable.
                for stmt in vec_pgvector_ddl(self._graph_name):
                    await _pg.execute_sql(conn, stmt)
                return VectorBackend.PGVECTOR
        except _pg.AgeQueryError:
            pass
        for stmt in vec_client_ddl(self._graph_name):
            await _pg.execute_sql(conn, stmt)
        return VectorBackend.CLIENT

    async def _probe_predicate_rung(self, conn: Any) -> PredicateStyle:
        for style, text in PREDICATE_CANARIES.items():
            try:
                await _pg.execute_cypher(
                    conn,
                    self._graph_name,
                    text,
                    {"probe_labels": ["__probe__"]},
                    column_count=1,
                )
                return style
            except Exception:
                continue
        return PredicateStyle.POST_FILTER

    async def _probe_traversal_rung(self, conn: Any) -> TraversalStyle:
        """Probe whether AGE accepts a variable-length ``-[*1..n]->`` (mirrors the
        vector/predicate probes). DIRECT_VLE if the canary runs, else the fail-closed
        UNION_ARMS rewrite (the always-safe most-degraded rung)."""
        try:
            await _pg.execute_cypher(conn, self._graph_name, TRAVERSAL_CANARY, {}, column_count=1)
            return TraversalStyle.DIRECT_VLE
        except Exception:
            return TraversalStyle.UNION_ARMS

    async def delete_all_indexes(self) -> None:
        conn = await self._connection()
        for table in (f'"{self._graph_name}_ft"', f'"{self._graph_name}_vec"'):
            try:
                await _pg.execute_sql(conn, f"DROP TABLE IF EXISTS {table}")
            except _pg.AgeQueryError:
                continue

    # -- fulltext / vector side-table ops ------------------------------------

    async def _ft_upsert(
        self, conn: Any, uuid: str, kind: str, group_id: str | None, content: str
    ) -> None:
        await _pg.execute_sql(
            conn, ft_upsert_sql(self._graph_name), (uuid, kind, group_id, content)
        )

    async def _ft_search(
        self, query: str, kind: str, group_ids: list[str] | None, limit: int
    ) -> list[tuple[str, float]]:
        conn = await self._connection()
        gids = list(group_ids) if group_ids else None
        rows = await _pg.execute_sql(
            conn, ft_search_sql(self._graph_name), (query, gids, gids, kind, limit)
        )
        return [(str(r[0]), float(r[2])) for r in rows]

    async def _vec_upsert(
        self,
        conn: Any,
        uuid: str,
        kind: str,
        field: str,
        group_id: str | None,
        source_uuid: str | None,
        target_uuid: str | None,
        vector: list[float],
    ) -> None:
        # The {graph}_vec table is created up front in build_indices_and_constraints
        # (both rungs), so no lazy DDL is needed here.
        if self._vector_backend is VectorBackend.PGVECTOR:
            literal = "[" + ",".join(str(x) for x in vector) + "]"
            await _pg.execute_sql(
                conn,
                f'INSERT INTO "{self._graph_name}_vec" '
                "(uuid, kind, field, group_id, source_uuid, target_uuid, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::vector) "
                "ON CONFLICT (uuid, kind, field) DO UPDATE SET "
                "group_id = EXCLUDED.group_id, source_uuid = EXCLUDED.source_uuid, "
                "target_uuid = EXCLUDED.target_uuid, embedding = EXCLUDED.embedding",
                (uuid, kind, field, group_id, source_uuid, target_uuid, literal),
            )
        else:
            import json

            await _pg.execute_sql(
                conn,
                f'INSERT INTO "{self._graph_name}_vec" '
                "(uuid, kind, field, group_id, source_uuid, target_uuid, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (uuid, kind, field) DO UPDATE SET "
                "group_id = EXCLUDED.group_id, source_uuid = EXCLUDED.source_uuid, "
                "target_uuid = EXCLUDED.target_uuid, embedding = EXCLUDED.embedding",
                (uuid, kind, field, group_id, source_uuid, target_uuid, json.dumps(vector)),
            )

    async def _vec_search(
        self,
        vector: list[float],
        kind: str,
        field: str,
        group_ids: list[str] | None,
        limit: int,
        source_uuid: str | None = None,
        target_uuid: str | None = None,
    ) -> list[tuple[str, float]]:
        conn = await self._connection()
        gids = list(group_ids) if group_ids else None
        if self._vector_backend is VectorBackend.PGVECTOR:
            # The {graph}_vec table is created up front in build_indices_and_constraints;
            # this existence probe is retained as defense-in-depth for a driver used
            # before build (a fresh-graph search then still yields [] not UndefinedTable).
            exists = await _pg.execute_sql(
                conn, vec_table_exists_sql(), (f'"{self._graph_name}_vec"',)
            )
            if not exists or exists[0][0] is None:
                return []
            literal = "[" + ",".join(str(x) for x in vector) + "]"
            rows = await _pg.execute_sql(
                conn,
                vec_pgvector_search_sql(self._graph_name),
                (
                    literal,
                    field,
                    kind,
                    gids,
                    gids,
                    source_uuid,
                    source_uuid,
                    target_uuid,
                    target_uuid,
                    literal,
                    limit,
                ),
            )
            return [(str(r[0]), float(r[2])) for r in rows]
        import json

        rows = await _pg.execute_sql(
            conn,
            vec_client_search_sql(self._graph_name),
            (field, kind, gids, gids, source_uuid, source_uuid, target_uuid, target_uuid),
        )
        scored = [
            (str(r[0]), cosine(vector, list(json.loads(r[2]) if isinstance(r[2], str) else r[2])))
            for r in rows
        ]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:limit]

    # -- entity reconstruction (LIVE-PARKED; uuid graph lookups) -------------

    async def _entity_nodes_by_uuid(self, uuids: list[str]) -> list[Any]:
        from graphiti_core.nodes import EntityNode

        return await self._load_by_uuid(EntityNode, uuids)

    async def _episodic_nodes_by_uuid(self, uuids: list[str]) -> list[Any]:
        from graphiti_core.nodes import EpisodicNode

        return await self._load_by_uuid(EpisodicNode, uuids)

    async def _entity_edges_by_uuid(self, uuids: list[str]) -> list[Any]:
        from graphiti_core.edges import EntityEdge

        out: list[Any] = []
        for uuid in uuids:
            try:
                out.append(await EntityEdge.get_by_uuid(self, uuid))
            except Exception:
                continue
        return out

    async def _load_by_uuid(self, cls: Any, uuids: list[str]) -> list[Any]:
        out: list[Any] = []
        for uuid in uuids:
            try:
                out.append(await cls.get_by_uuid(self, uuid))
            except Exception:
                continue
        return out

    # -- fulltext query passthrough ------------------------------------------

    def build_fulltext_query(
        self, query: str, group_ids: list[str] | None = None, max_query_length: int = 128
    ) -> str:
        """AGE fulltext runs via the side table + ``websearch_to_tsquery``; the raw
        query string is passed through (tsquery parsing happens in the side table)."""
        trimmed = " ".join(query.split())
        if len(trimmed) > max_query_length:
            trimmed = trimmed[:max_query_length]
        return trimmed

    def delete_all_indexes_impl(self) -> Coroutine[Any, Any, None]:
        return self.delete_all_indexes()
