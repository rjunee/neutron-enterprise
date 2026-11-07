# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Always-on golden pins for ``graphiti_age._dialect`` (per feasibility-matrix row).

Covers the label-injection guard, the never-unbounded traversal + hop ceiling, the
UNION-arm fallback text, the tsvector/pgvector DDL + search SQL shapes (undimensioned
vector, source/target endpoint columns + filters), and the fail-loud ``CALL db.*``
rewrite rule; plus driver-source structural pins for the FIX-2/FIX-3/3b changes and the
NE-083c-FU2 degraded-rung LIMIT correctness (POST_FILTER over-fetch under-return +
UNION-arms per-arm over-return, popped and enforced in-driver, fail-loud on the shapes a
degraded rung cannot make correct).
"""

from __future__ import annotations

from typing import Any

import pytest

from ._sidecar_loader import load_sidecar_module

_d = load_sidecar_module("graphiti_age/_dialect.py", "_ne083c_dialect")


# --- label / graph-name guard ------------------------------------------------


def test_valid_labels_pass() -> None:
    for label in ("Entity", "Episodic", "RELATES_TO", "A_b0"):
        assert _d.validate_label(label) == label


@pytest.mark.parametrize(
    "bad",
    ['a"b', "a`b", "a b", "a-b", "a;b", "1abc", "a)b", "", "a'b"],
)
def test_bad_labels_rejected(bad: str) -> None:
    with pytest.raises(_d.AgeDialectError):
        _d.validate_label(bad)


def test_graph_name_guard() -> None:
    assert _d.validate_graph_name("ne_memory") == "ne_memory"
    with pytest.raises(_d.AgeDialectError):
        _d.validate_graph_name('ne";DROP')


# --- traversal: bounded + never unbounded + ceiling --------------------------


def test_bounded_vle_clamps_to_ceiling() -> None:
    assert _d.bounded_vle(2) == "*1..2"
    assert _d.bounded_vle(100) == f"*1..{_d.HOP_CEILING}"


def test_bounded_vle_never_unbounded() -> None:
    for h in range(1, 12):
        text = _d.bounded_vle(h)
        assert text != "*" and ".." in text
        upper = int(text.split("..")[1])
        assert 1 <= upper <= _d.HOP_CEILING


def test_bounded_vle_rejects_bad_hops() -> None:
    with pytest.raises(_d.AgeDialectError):
        _d.bounded_vle(0)
    with pytest.raises(_d.AgeDialectError):
        _d.bounded_vle(True)  # bool is not an int here


def test_union_arms_text_pinned_hops_1_to_3() -> None:
    assert _d.union_arms(1) == "MATCH (a0)-[r1]->(m1) RETURN m1 AS node"
    assert _d.union_arms(2) == (
        "MATCH (a0)-[r1]->(m1) RETURN m1 AS node\n"
        "UNION\n"
        "MATCH (a0)-[r1]->(m1)-[r2]->(m2) RETURN m2 AS node"
    )
    assert "UNION" in _d.union_arms(3)
    assert "[*]" not in _d.union_arms(3)


# --- fulltext DDL / search SQL (row 9) ---------------------------------------


def test_ft_ddl_shape() -> None:
    ddl = _d.ft_ddl("ne_memory")
    joined = " ".join(ddl)
    assert '"ne_memory_ft"' in joined
    assert "tsvector GENERATED ALWAYS AS (to_tsvector('simple'" in joined
    assert "USING gin (tsv)" in joined
    assert "kind IN ('node','edge')" in joined
    assert "PRIMARY KEY (uuid, kind)" in joined


def test_ft_search_sql_shape() -> None:
    sql = _d.ft_search_sql("ne_memory")
    assert "websearch_to_tsquery('simple', %s)" in sql
    assert "ts_rank(tsv, q)" in sql
    assert "group_id = ANY(%s)" in sql
    assert "ORDER BY rank DESC LIMIT %s" in sql


def test_ft_upsert_on_conflict() -> None:
    assert "ON CONFLICT (uuid, kind) DO UPDATE" in _d.ft_upsert_sql("ne_memory")


# --- vector DDL / search SQL (row 10) ----------------------------------------


def test_vec_pgvector_ddl_undimensioned_with_endpoints() -> None:
    # FIX-2: the pgvector column is UNDIMENSIONED (created up front at build time,
    # before any embedding is seen) — never a fixed vector(N). FIX-3: endpoint columns.
    ddl = " ".join(_d.vec_pgvector_ddl("ne_memory"))
    assert "embedding vector," in ddl or "embedding vector " in ddl
    assert "vector(" not in ddl  # never dimensioned
    assert "source_uuid text" in ddl and "target_uuid text" in ddl
    assert "PRIMARY KEY (uuid, kind, field)" in ddl
    assert "USING btree (group_id)" in ddl


def test_vec_client_ddl_is_jsonb_with_endpoints() -> None:
    ddl = " ".join(_d.vec_client_ddl("ne_memory"))
    assert "embedding jsonb" in ddl
    assert "source_uuid text" in ddl and "target_uuid text" in ddl


def test_vec_pgvector_search_uses_cast_operator_and_endpoint_filters() -> None:
    sql = _d.vec_pgvector_search_sql("ne_memory")
    assert "embedding <=> %s::vector" in sql
    assert "ORDER BY embedding <=> %s::vector LIMIT %s" in sql
    # FIX-3: source/target endpoint filters applied IN SQL (NULL-tolerant), so a
    # filtered edge-similarity search can never under-return vs Neo4j.
    assert "(%s::text IS NULL OR source_uuid = %s)" in sql
    assert "(%s::text IS NULL OR target_uuid = %s)" in sql


def test_vec_client_search_sql_has_endpoint_filters() -> None:
    sql = _d.vec_client_search_sql("ne_memory")
    assert "(%s::text IS NULL OR source_uuid = %s)" in sql
    assert "(%s::text IS NULL OR target_uuid = %s)" in sql


def test_cosine_similarity() -> None:
    assert _d.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _d.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert _d.cosine([1.0, 2.0], []) == 0.0


# --- residual rewrites / fail-loud -------------------------------------------


def test_rewrite_strips_vecf32() -> None:
    assert _d.rewrite_cypher("RETURN vecf32([1,2,3])") == "RETURN [1,2,3]"


def test_rewrite_passthrough_plain_cypher() -> None:
    text = "MATCH (n:Entity {uuid: $uuid}) RETURN n"
    assert _d.rewrite_cypher(text) == text


@pytest.mark.parametrize(
    "call",
    [
        "CALL db.index.fulltext.queryNodes('node_name_and_summary', $query)",
        "CALL db.index.fulltext.queryRelationships('edge_name_and_fact', $query)",
        "MATCH (n) CALL db.labels() RETURN n",
    ],
)
def test_unknown_call_db_star_fails_loud(call: str) -> None:
    with pytest.raises(_d.AgeDialectError):
        _d.rewrite_cypher(call)


# --- write-path rewrites (live-verified against AGE 1.6.0) -------------------


def test_rewrite_set_map_param_to_properties() -> None:
    # AGE rejects `SET n = $param` (map); accepts `SET n = properties($param)`.
    assert _d.rewrite_cypher("SET n = $entity_data") == "SET n = properties($entity_data)"
    assert _d.rewrite_cypher("SET e = $edge_data") == "SET e = properties($edge_data)"


def test_rewrite_leaves_property_and_inline_map_sets() -> None:
    # property sets and inline-map sets (episodic save) must be untouched.
    assert _d.rewrite_cypher("SET n.name = $name") == "SET n.name = $name"
    inline = "SET n = {uuid: $uuid, name: $name}"
    assert _d.rewrite_cypher(inline) == inline


def test_rewrite_strips_set_label() -> None:
    # AGE binds the label at MERGE; `SET n:Entity` is stripped.
    out = _d.rewrite_cypher("MERGE (n:Entity {uuid: $u}) SET n:Entity SET n = $entity_data")
    assert ":Entity SET" not in out.replace("(n:Entity", "")  # the SET n:Entity is gone
    assert "SET n = properties($entity_data)" in out
    # multi-label variant
    assert "SET n:Person" not in _d.rewrite_cypher("SET n:Entity:Person SET n = $entity_data")


def test_rewrite_strips_vector_property_call_not_fail_loud() -> None:
    # The write-path embedding CALL is stripped (not raised) — embeddings go to the vec
    # side table; only genuinely unknown db.* procedures fail loud.
    q = (
        "MERGE (n:Entity {uuid: $u}) SET n = $entity_data "
        'WITH n CALL db.create.setNodeVectorProperty(n, "name_embedding", '
        "$entity_data.name_embedding) RETURN n.uuid AS uuid"
    )
    out = _d.rewrite_cypher(q)
    assert "setNodeVectorProperty" not in out
    assert "CALL" not in out
    assert "SET n = properties($entity_data)" in out
    # the relationship variant too
    edge_q = (
        "SET e = $edge_data WITH e CALL db.create.setRelationshipVectorProperty("
        'e, "fact_embedding", $edge_data.fact_embedding) RETURN e.uuid AS uuid'
    )
    assert "setRelationshipVectorProperty" not in _d.rewrite_cypher(edge_q)


# --- BULK write-path rewrites (Codex P1: /episodes → add_episode → bulk queries) ----
# The exact bulk-save query strings emitted by pinned graphiti-core 0.29.2 for the NEO4J
# provider (node_db_queries.get_entity_node_save_bulk_query / edge_db_queries.
# get_entity_edge_save_bulk_query, has_aoss=False). These are the queries the sidecar
# /episodes service actually runs; every construct below is live-verified translatable
# on apache/age:release_PG17_1.6.0.
_BULK_NODE_Q = (
    "UNWIND $nodes AS node\n"
    "MERGE (n:Entity {uuid: node.uuid})\n"
    "SET n:$(node.labels)\n"
    "SET n = node\n"
    'WITH n, node CALL db.create.setNodeVectorProperty(n, "name_embedding", '
    "node.name_embedding)\n"
    "RETURN n.uuid AS uuid"
)
_BULK_EDGE_Q = (
    "UNWIND $entity_edges AS edge\n"
    "MATCH (source:Entity {uuid: edge.source_node_uuid})\n"
    "MATCH (target:Entity {uuid: edge.target_node_uuid})\n"
    "MERGE (source)-[e:RELATES_TO {uuid: edge.uuid}]->(target)\n"
    "SET e = edge\n"
    'WITH e, edge CALL db.create.setRelationshipVectorProperty(e, "fact_embedding", '
    "edge.fact_embedding)\n"
    "RETURN edge.uuid AS uuid"
)


def test_rewrite_strips_multivar_bulk_vector_property_call() -> None:
    # Codex P1: the bulk form carries the UNWIND row alongside the node/edge var
    # (`WITH n, node CALL …`). The single-var regex missed it, so the residual CALL
    # tripped the fail-loud guard and blocked ALL /episodes ingestion. Both variants
    # must now strip WITHOUT raising.
    node_out = _d.rewrite_cypher(_BULK_NODE_Q)
    assert "CALL" not in node_out
    assert "setNodeVectorProperty" not in node_out
    edge_out = _d.rewrite_cypher(_BULK_EDGE_Q)
    assert "CALL" not in edge_out
    assert "setRelationshipVectorProperty" not in edge_out


def test_rewrite_strips_bulk_dynamic_label_set() -> None:
    # The bulk node query uses the Neo4j-5 dynamic-label form `SET n:$(node.labels)`,
    # which is a hard AGE syntax error; it must be stripped (label bound at MERGE).
    out = _d.rewrite_cypher(_BULK_NODE_Q)
    assert "$(node.labels)" not in out
    assert "SET n:" not in out


def test_rewrite_bulk_node_query_survives_core_clauses() -> None:
    # After stripping the dynamic label + vector CALL, the surviving form is exactly the
    # UNWIND/MERGE/`SET n = node` shape proven to execute on AGE 1.6.0.
    out = _d.rewrite_cypher(_BULK_NODE_Q)
    assert "UNWIND $nodes AS node" in out
    assert "MERGE (n:Entity {uuid: node.uuid})" in out
    assert "SET n = node" in out  # map-from-UNWIND-row set left untouched (works on AGE)
    assert "RETURN n.uuid AS uuid" in out


def test_rewrite_bulk_edge_query_survives_core_clauses() -> None:
    out = _d.rewrite_cypher(_BULK_EDGE_Q)
    assert "UNWIND $entity_edges AS edge" in out
    assert "MERGE (source)-[e:RELATES_TO {uuid: edge.uuid}]->(target)" in out
    assert "SET e = edge" in out
    assert "RETURN edge.uuid AS uuid" in out


def test_rewrite_leaves_bulk_map_set_from_unwind_row() -> None:
    # `SET n = node` / `SET e = edge` (map from the UNWIND row, no `$param`) execute on
    # AGE directly and must NOT be rewritten to properties(...) — only `= $param` is.
    assert _d.rewrite_cypher("SET n = node") == "SET n = node"
    assert _d.rewrite_cypher("SET e = edge") == "SET e = edge"


# --- RETURN alias extraction (record keys, never SQL) ------------------------


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("RETURN 1 AS ok", ["ok"]),
        ("RETURN e.name AS name", ["name"]),
        ("RETURN count(*) AS count", ["count"]),
        ("RETURN n.uuid AS uuid, n.name AS name", ["uuid", "name"]),
        ("MATCH (n) RETURN n.a AS a, collect(n.b) AS b ORDER BY a LIMIT 5", ["a", "b"]),
        ("RETURN 1", ["col0"]),
    ],
)
def test_return_aliases(cypher: str, expected: list[str]) -> None:
    assert _d.return_aliases(cypher) == expected


def test_return_aliases_fallback_when_no_return() -> None:
    assert _d.return_aliases("CREATE (n:Entity {uuid: $u})") == ["result"]


# --- side-table population plan ----------------------------------------------


def test_plan_side_tables_entity_shape() -> None:
    clean = {
        "entity_data": {
            "uuid": "e1",
            "name": "Alice",
            "summary": "an engineer",
            "group_id": "agent-a",
            "name_embedding": [0.1, 0.2],
        }
    }
    out, ft, vec = _d.plan_side_tables(clean)
    assert ft == [("e1", "node", "agent-a", "Alice an engineer")]
    # node rows carry None/None endpoints (7-tuple)
    assert vec == [("e1", "node", "name_embedding", "agent-a", None, None, [0.1, 0.2])]
    # the embedding is stripped out of agtype (feasibility row 10)
    assert "name_embedding" not in out["entity_data"]
    # the input dict is not mutated
    assert "name_embedding" in clean["entity_data"]


def test_plan_side_tables_edge_shape() -> None:
    clean = {
        "edge_data": {
            "uuid": "r1",
            "fact": "Alice works at Acme",
            "group_id": "agent-a",
            "source_uuid": "alice-uuid",
            "target_uuid": "acme-uuid",
            "fact_embedding": [0.3],
        }
    }
    out, ft, vec = _d.plan_side_tables(clean)
    assert ft == [("r1", "edge", "agent-a", "Alice works at Acme")]
    # FIX-3: edge rows carry the source/target endpoints extracted from edge_data
    assert vec == [("r1", "edge", "fact_embedding", "agent-a", "alice-uuid", "acme-uuid", [0.3])]
    assert "fact_embedding" not in out["edge_data"]


def test_plan_side_tables_episodic_shape() -> None:
    clean = {"uuid": "u1", "content": "Alice joined Acme", "group_id": "agent-a"}
    _out, ft, vec = _d.plan_side_tables(clean)
    assert ft == [("u1", "node", "agent-a", "Alice joined Acme")]
    assert vec == []


def test_plan_side_tables_single_episodic_with_entity_edges_list_not_bulk() -> None:
    # The single EpisodicNode.save passes `entity_edges` as a list of edge-uuid STRINGS
    # (not dicts). It must NOT be mistaken for the bulk entity-edge shape — the episode's
    # content must still yield its fulltext node row (regression guard).
    clean = {
        "uuid": "ep1",
        "content": "Alice was promoted to lead",
        "group_id": "agent-a",
        "entity_edges": ["edge-uuid-1", "edge-uuid-2"],
    }
    _out, ft, vec = _d.plan_side_tables(clean)
    assert ft == [("ep1", "node", "agent-a", "Alice was promoted to lead")]
    assert vec == []


def test_plan_side_tables_unrecognised_passes_through() -> None:
    clean = {"uuids": ["a", "b"]}
    out, ft, vec = _d.plan_side_tables(clean)
    assert out == clean and ft == [] and vec == []


# --- BULK side-table population (Codex P1: /episodes ingestion must be searchable) ---


def test_plan_side_tables_bulk_nodes_shape() -> None:
    clean = {
        "nodes": [
            {
                "uuid": "n1",
                "name": "Alice",
                "summary": "eng",
                "group_id": "g",
                "labels": ["Entity"],
                "name_embedding": [0.1, 0.2],
            },
            {
                "uuid": "n2",
                "name": "Acme",
                "group_id": "g",
                "labels": ["Entity"],
                "name_embedding": [0.3],
            },
        ]
    }
    out, ft, vec = _d.plan_side_tables(clean)
    assert ft == [("n1", "node", "g", "Alice eng"), ("n2", "node", "g", "Acme")]
    assert vec == [
        ("n1", "node", "name_embedding", "g", None, None, [0.1, 0.2]),
        ("n2", "node", "name_embedding", "g", None, None, [0.3]),
    ]
    # embeddings stripped out of agtype in every element; caller list not mutated
    assert all("name_embedding" not in n for n in out["nodes"])
    assert "name_embedding" in clean["nodes"][0]


def test_plan_side_tables_bulk_entity_edges_shape() -> None:
    # BULK edge shape uses source_node_uuid / target_node_uuid (not source_uuid/…).
    clean = {
        "entity_edges": [
            {
                "uuid": "r1",
                "fact": "Alice works at Acme",
                "group_id": "g",
                "source_node_uuid": "n1",
                "target_node_uuid": "n2",
                "fact_embedding": [0.4],
            },
        ]
    }
    out, ft, vec = _d.plan_side_tables(clean)
    assert ft == [("r1", "edge", "g", "Alice works at Acme")]
    assert vec == [("r1", "edge", "fact_embedding", "g", "n1", "n2", [0.4])]
    assert "fact_embedding" not in out["entity_edges"][0]


def test_plan_side_tables_bulk_episodes_shape() -> None:
    clean = {
        "episodes": [
            {"uuid": "e1", "content": "Alice joined Acme", "group_id": "g"},
            {"uuid": "e2", "content": "Bob likes tea", "group_id": "g"},
        ]
    }
    _out, ft, vec = _d.plan_side_tables(clean)
    assert ft == [("e1", "node", "g", "Alice joined Acme"), ("e2", "node", "g", "Bob likes tea")]
    assert vec == []


def test_plan_side_tables_bulk_episodic_edges_yield_no_rows() -> None:
    # MENTIONS edges carry no fact/embedding and are not recall targets.
    clean = {"episodic_edges": [{"uuid": "m1", "source_node_uuid": "e1", "target_node_uuid": "n1"}]}
    _out, ft, vec = _d.plan_side_tables(clean)
    assert ft == [] and vec == []


# --- vec-table existence probe (fresh-graph search must not UndefinedTable) --


def test_vec_table_exists_sql_pinned() -> None:
    assert _d.vec_table_exists_sql() == "SELECT to_regclass(%s)"


# --- edge similarity source/target post-filter (fail-closed rung) ------------


class _Edge:
    def __init__(self, uuid: str, source: str, target: str) -> None:
        self.uuid = uuid
        self.source_node_uuid = source
        self.target_node_uuid = target


def test_filter_edge_candidates_none_filters_pass_through() -> None:
    edges = [_Edge("e1", "a", "b"), _Edge("e2", "c", "d")]
    assert _d.filter_edge_candidates(edges, None, None) == edges


def test_filter_edge_candidates_source_filter() -> None:
    edges = [_Edge("e1", "a", "b"), _Edge("e2", "c", "b")]
    out = _d.filter_edge_candidates(edges, "a", None)
    assert [e.uuid for e in out] == ["e1"]


def test_filter_edge_candidates_target_filter() -> None:
    edges = [_Edge("e1", "a", "b"), _Edge("e2", "a", "d")]
    out = _d.filter_edge_candidates(edges, None, "d")
    assert [e.uuid for e in out] == ["e2"]


def test_filter_edge_candidates_both_filters_never_wrong_pair() -> None:
    edges = [_Edge("e1", "a", "b"), _Edge("e2", "a", "d"), _Edge("e3", "c", "b")]
    out = _d.filter_edge_candidates(edges, "a", "b")
    assert [e.uuid for e in out] == ["e1"]


def test_edge_filter_fetch_limit_helper_removed() -> None:
    # FIX-3: the widened-fetch helper is superseded by the in-SQL endpoint filter
    # (which cannot under-return) and must be gone.
    assert not hasattr(_d, "edge_filter_fetch_limit")
    assert not hasattr(_d, "EDGE_FILTER_FETCH_MIN")


# --- driver-source structural pins (FIX-2/FIX-3/3b, no venv needed) -----------


def _driver_source() -> str:
    from ._sidecar_loader import graphiti_age_dir

    return (graphiti_age_dir() / "driver.py").read_text(encoding="utf-8")


def test_driver_creates_pgvector_table_at_build_time() -> None:
    # FIX-2: the pgvector {graph}_vec table is created in _probe_vector_backend (up
    # front at build-indices time), and the lazy _vec_dim path is gone entirely.
    src = _driver_source()
    probe = src.split("async def _probe_vector_backend", 1)[1].split("async def ", 1)[0]
    assert "vec_pgvector_ddl(self._graph_name)" in probe
    assert "_vec_dim" not in src


def test_driver_maps_null_result_cell_to_none() -> None:
    # A SQL NULL result cell (a cypher projection of an absent property, e.g. an edge's
    # unset expired_at/invalid_at) must decode to None, not raise AgtypeDecodeError and
    # drop the whole row on reconstruction (parity with Neo4j null semantics).
    src = _driver_source()
    exec_q = src.split("async def execute_query", 1)[1].split("async def ", 1)[0]
    assert "if raw is None:" in exec_q
    assert "record[alias] = None" in exec_q


def test_driver_applies_min_score_in_both_similarity_methods() -> None:
    # FIX-3b: min_score parity applied (strict >) in node AND edge similarity.
    src = _driver_source()
    node = src.split("async def node_similarity_search", 1)[1].split("async def ", 1)[0]
    edge = src.split("async def edge_similarity_search", 1)[1].split("async def ", 1)[0]
    assert "min_score" in node and "> min_score" in node
    assert "min_score" in edge and "> min_score" in edge
    # FIX-3: edge similarity threads the source/target endpoints into the vec search.
    assert "source_uuid=source_node_uuid" in edge
    assert "target_uuid=target_node_uuid" in edge


# =====================================================================================
# NE-083c-FOLLOWUP: forced-rung battery for the wired predicate + traversal ladders.
# Each rung is FORCED (no live AGE needed) and its exact translation output is pinned;
# the driver-source structural pins prove execute_query/build_indices consult the styles.
# =====================================================================================

_LABEL_PRED = "MATCH (n) WHERE any(l IN n.labels WHERE l IN $labels) RETURN n"


# --- predicate ladder: ANY / SIZE_COMPREHENSION / POST_FILTER -----------------


def test_apply_predicate_any_passthrough() -> None:
    # ANY (the direct rung selected on AGE 1.6.0) is byte-identical passthrough, no specs.
    out, specs = _d.apply_predicate_style(_LABEL_PRED, _d.PredicateStyle.ANY)
    assert out == _LABEL_PRED
    assert specs == []


def test_apply_predicate_size_comprehension_rewrite() -> None:
    out, specs = _d.apply_predicate_style(_LABEL_PRED, _d.PredicateStyle.SIZE_COMPREHENSION)
    assert out == "MATCH (n) WHERE size([l IN n.labels WHERE l IN $labels]) > 0 RETURN n"
    assert specs == []


def test_apply_predicate_post_filter_strips_and_specs() -> None:
    out, specs = _d.apply_predicate_style(_LABEL_PRED, _d.PredicateStyle.POST_FILTER)
    assert out == "MATCH (n) WHERE true RETURN n"
    assert specs == [("n", "labels")]


def test_apply_predicate_post_filter_alias_projection() -> None:
    q = "MATCH (n) WHERE any(l IN n.labels WHERE l IN $labels) RETURN n AS node"
    out, specs = _d.apply_predicate_style(q, _d.PredicateStyle.POST_FILTER)
    assert specs == [("node", "labels")]
    assert "WHERE true RETURN n AS node" in out


def test_apply_predicate_post_filter_var_not_projected_fails_loud() -> None:
    # POST_FILTER needs the predicate's node var projected WHOLE; a property projection
    # (RETURN n.uuid AS uuid) leaves the driver unable to filter → fail-loud, never
    # silently-unfiltered rows.
    q = "MATCH (n) WHERE any(l IN n.labels WHERE l IN $labels) RETURN n.uuid AS uuid"
    with pytest.raises(_d.AgeDialectError):
        _d.apply_predicate_style(q, _d.PredicateStyle.POST_FILTER)


def test_apply_predicate_noncanonical_label_any_fails_loud_on_degraded_rungs() -> None:
    # A non-canonical label-any (not the `l IN $param` shape) passes through on ANY but a
    # degraded rung cannot express it → AgeDialectError on SIZE_COMPREHENSION + POST_FILTER.
    q = "MATCH (n) WHERE any(l IN n.labels WHERE l STARTS WITH 'X') RETURN n"
    out, specs = _d.apply_predicate_style(q, _d.PredicateStyle.ANY)
    assert out == q and specs == []
    for style in (_d.PredicateStyle.SIZE_COMPREHENSION, _d.PredicateStyle.POST_FILTER):
        with pytest.raises(_d.AgeDialectError):
            _d.apply_predicate_style(q, style)


def test_apply_predicate_non_label_any_untouched() -> None:
    # any(...) over a non-label collection (e.g. e.episodes) is untouched on every rung.
    q = "MATCH (e) WHERE any(x IN e.episodes WHERE x IN $eps) RETURN e"
    for style in (
        _d.PredicateStyle.ANY,
        _d.PredicateStyle.SIZE_COMPREHENSION,
        _d.PredicateStyle.POST_FILTER,
    ):
        out, specs = _d.apply_predicate_style(q, style)
        assert out == q and specs == []


def test_passes_label_post_filter_truth_table() -> None:
    spec = ("n", "labels")
    node = {"n": {"properties": {"labels": ["Entity", "Person"]}}}
    assert _d.passes_label_post_filter(node, spec, {"labels": ["Person"]}) is True
    assert _d.passes_label_post_filter(node, spec, {"labels": ["Org"]}) is False
    # missing labels property, non-dict record value, non-list param → drop the row
    assert (
        _d.passes_label_post_filter({"n": {"properties": {}}}, spec, {"labels": ["Person"]})
        is False
    )
    assert _d.passes_label_post_filter({"n": "x"}, spec, {"labels": ["Person"]}) is False
    assert _d.passes_label_post_filter(node, spec, {"labels": "Person"}) is False


# --- traversal ladder: DIRECT_VLE / UNION_ARMS --------------------------------

_NODE_BFS = (
    "UNWIND $bfs_origin_node_uuids AS origin_uuid\n"
    "MATCH (origin {uuid: origin_uuid})-[:RELATES_TO|MENTIONS*1..2]->(n:Entity)\n"
    "WHERE n.group_id = origin.group_id\n"
    "RETURN n.uuid AS uuid"
)
_EDGE_BFS = (
    "MATCH path = (origin {uuid: origin_uuid})-[:RELATES_TO|MENTIONS*1..2]->(:Entity)\n"
    "UNWIND relationships(path) AS rel\n"
    "MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]-(m:Entity)\n"
    "RETURN DISTINCT e.uuid AS uuid"
)


def test_traversal_canary_pinned() -> None:
    assert _d.TRAVERSAL_CANARY == "MATCH (a)-[*1..2]->(b) RETURN 1 AS probe LIMIT 1"


def test_apply_traversal_direct_passthrough_and_clamp() -> None:
    t1 = "MATCH (a)-[:RELATES_TO|MENTIONS*1..3]->(b) RETURN b"
    assert _d.apply_traversal_style(t1, _d.TraversalStyle.DIRECT_VLE) == t1  # <=6: passthrough
    t2 = "MATCH (a)-[:RELATES_TO|MENTIONS*1..12]->(b) RETURN b"
    out = _d.apply_traversal_style(t2, _d.TraversalStyle.DIRECT_VLE)
    assert "*1..6" in out and "*1..12" not in out  # clamped to HOP_CEILING


def test_apply_traversal_unbounded_fails_loud_both_rungs() -> None:
    for q in ("MATCH (a)-[*]->(b) RETURN b", "MATCH (a)-[:RELATES_TO*1..]->(b) RETURN b"):
        for style in (_d.TraversalStyle.DIRECT_VLE, _d.TraversalStyle.UNION_ARMS):
            with pytest.raises(_d.AgeDialectError):
                _d.apply_traversal_style(q, style)


def test_apply_traversal_union_arms_node_bfs_shape() -> None:
    out = _d.apply_traversal_style(_NODE_BFS, _d.TraversalStyle.UNION_ARMS)
    arm1 = _NODE_BFS.replace("[:RELATES_TO|MENTIONS*1..2]", "[:RELATES_TO|MENTIONS]")
    arm2 = _NODE_BFS.replace(
        "[:RELATES_TO|MENTIONS*1..2]",
        "[:RELATES_TO|MENTIONS]->()-[:RELATES_TO|MENTIONS]",
    )
    assert out == arm1 + "\nUNION\n" + arm2
    assert "[*]" not in out and "*1..2" not in out


def test_apply_traversal_union_arms_edge_bfs_path_shape() -> None:
    out = _d.apply_traversal_style(_EDGE_BFS, _d.TraversalStyle.UNION_ARMS)
    arms = out.split("\nUNION\n")
    assert len(arms) == 2
    # arm 1 (depth 1): one named chain rel, relationships(path) → [__vle_r1], path stripped
    assert "MATCH (origin {uuid: origin_uuid})-[__vle_r1:RELATES_TO|MENTIONS]->(:Entity)" in arms[0]
    assert "UNWIND [__vle_r1] AS rel" in arms[0]
    # arm 2 (depth 2): two named chain rels + list substitution
    assert (
        "-[__vle_r1:RELATES_TO|MENTIONS]->()-[__vle_r2:RELATES_TO|MENTIONS]->(:Entity)" in arms[1]
    )
    assert "UNWIND [__vle_r1, __vle_r2] AS rel" in arms[1]
    # `path = ` stripped and NO residual `path` token anywhere in the output
    assert "path = " not in out
    assert "path" not in out


def test_apply_traversal_union_arms_rejects_unsupported() -> None:
    two_vle = "MATCH (a)-[:T*1..2]->(b)-[:T*1..2]->(c) RETURN c"
    bound_var = "MATCH (a)-[e:RELATES_TO*1..2]->(b) RETURN b"
    residual = (
        "MATCH path = (a)-[:T*1..2]->(b) UNWIND relationships(path) AS rel RETURN nodes(path)"
    )
    for q in (two_vle, bound_var, residual):
        with pytest.raises(_d.AgeDialectError):
            _d.apply_traversal_style(q, _d.TraversalStyle.UNION_ARMS)


def test_apply_traversal_no_vle_passthrough() -> None:
    q = "MATCH (n:Entity {uuid: $u})-[e:RELATES_TO {uuid: $r}]->(m:Entity) RETURN n"
    for style in (_d.TraversalStyle.DIRECT_VLE, _d.TraversalStyle.UNION_ARMS):
        assert _d.apply_traversal_style(q, style) == q


def test_apply_traversal_union_arm_count_clamped() -> None:
    out = _d.apply_traversal_style(
        "MATCH (a)-[:RELATES_TO|MENTIONS*1..100]->(b) RETURN b", _d.TraversalStyle.UNION_ARMS
    )
    assert out.count("\nUNION\n") == _d.HOP_CEILING - 1  # exactly HOP_CEILING (6) arms
    assert "[*]" not in out


# --- driver-source structural pins (wiring proof, no venv needed) -------------


def test_driver_probes_traversal_rung_in_build_indices() -> None:
    src = _driver_source()
    build = src.split("async def build_indices_and_constraints", 1)[1].split("async def ", 1)[0]
    assert "self._traversal_style = await self._probe_traversal_rung(conn)" in build
    probe = src.split("async def _probe_traversal_rung", 1)[1].split("async def ", 1)[0]
    assert "TRAVERSAL_CANARY" in probe


def test_driver_consults_both_styles_and_post_filters() -> None:
    src = _driver_source()
    exec_q = src.split("async def execute_query", 1)[1].split("async def ", 1)[0]
    assert "apply_predicate_style(text, self._predicate_style)" in exec_q
    assert "apply_traversal_style(text, self._traversal_style)" in exec_q
    assert "passes_label_post_filter(" in exec_q


def test_driver_default_traversal_style_fail_closed() -> None:
    src = _driver_source()
    init = src.split("def __init__(self, dsn: str", 1)[1].split("async def ", 1)[0]
    assert "self._traversal_style = TraversalStyle.UNION_ARMS" in init


# =====================================================================================
# NE-083c-FU2: degraded-rung LIMIT correctness (POST_FILTER over-fetch + UNION global limit)
# The degraded rungs are dead code on the shipping AGE 1.6.0 (the direct rungs are probed
# there); these forced-rung pins prove the graceful-degradation is CORRECT when activated.
# =====================================================================================


def test_has_vle_segment() -> None:
    # The driver-side trigger for popping a trailing LIMIT on the UNION_ARMS rung.
    assert _d.has_vle_segment(_NODE_BFS) is True
    assert _d.has_vle_segment(_EDGE_BFS) is True
    assert _d.has_vle_segment("MATCH (n:Entity {uuid: $u}) RETURN n") is False


def test_pop_trailing_limit_literal() -> None:
    assert _d.pop_trailing_limit("MATCH (n) RETURN n LIMIT 10", {}) == ("MATCH (n) RETURN n", 10)


def test_pop_trailing_limit_param_resolved() -> None:
    text, limit = _d.pop_trailing_limit("MATCH (n) RETURN n\nLIMIT $limit\n", {"limit": 7})
    assert limit == 7
    assert text == "MATCH (n) RETURN n"  # trailing whitespace rstripped


def test_pop_trailing_limit_absent_returns_none() -> None:
    assert _d.pop_trailing_limit("MATCH (n) RETURN n", {}) == ("MATCH (n) RETURN n", None)


@pytest.mark.parametrize(
    ("query", "params"),
    [
        ("MATCH (n) RETURN n LIMIT $limit", {}),  # $limit missing from params
        ("MATCH (n) RETURN n LIMIT $limit", {"limit": "ten"}),  # non-int
        ("MATCH (n) RETURN n LIMIT $limit", {"limit": True}),  # bool is not an int here
        ("MATCH (n) RETURN n LIMIT $limit", {"limit": -1}),  # negative
        ("MATCH (n) WITH n LIMIT 5 RETURN n LIMIT 10", {}),  # residual non-trailing LIMIT
        ("MATCH (n) RETURN n SKIP 5 LIMIT 10", {}),  # residual SKIP after popping LIMIT
        ("MATCH (n) RETURN n SKIP 5", {}),  # a bare SKIP cannot be made correct
    ],
)
def test_pop_trailing_limit_fail_loud(query: str, params: dict[str, Any]) -> None:
    # A degraded rung must never silently mis-count: missing/non-int/bool/negative $limit,
    # a residual non-trailing LIMIT, and any SKIP all fail loud.
    with pytest.raises(_d.AgeDialectError):
        _d.pop_trailing_limit(query, params)


def test_post_filter_with_limit_returns_requested_matching_count() -> None:
    # FIX-1 SPEC regression: a POST_FILTER query with LIMIT n over a candidate set whose
    # first n AGE-ordered rows include a NON-match must return n MATCHING rows (not < n),
    # byte-equal to the ANY-rung / Neo4j result — proven by filtering the FULL candidate
    # set THEN applying the popped limit, and contrast-pinned against the old bug.
    q = "MATCH (n) WHERE any(l IN n.labels WHERE l IN $labels) RETURN n LIMIT 2"
    params: dict[str, Any] = {"labels": ["Person"]}
    text, specs = _d.apply_predicate_style(q, _d.PredicateStyle.POST_FILTER)
    text, limit = _d.pop_trailing_limit(text, params)
    assert "LIMIT" not in text  # the AGE-side LIMIT is gone — the full candidate set fetches
    assert limit == 2
    assert specs == [("n", "labels")]
    # candidate set: the SECOND AGE-ordered row (idx 1) is a non-match (Org).
    candidates: list[dict[str, Any]] = [
        {"n": {"properties": {"labels": ["Person"]}}},
        {"n": {"properties": {"labels": ["Org"]}}},
        {"n": {"properties": {"labels": ["Person"]}}},
        {"n": {"properties": {"labels": ["Person"]}}},
    ]
    # in-driver: filter the FULL set, THEN slice to the popped limit (the fixed pipeline).
    filtered = [r for r in candidates if _d.passes_label_post_filter(r, specs[0], params)]
    result = filtered[:limit]
    assert len(result) == 2  # exactly n matching rows
    assert result == [candidates[0], candidates[2]]  # the ANY-rung / Neo4j result
    # contrast-pin the PRE-FIX bug: an AGE-side LIMIT on the UNFILTERED set (candidates[:2])
    # would filter to only 1 matching row (< 2) — the under-return the fix eliminates.
    old_bug = [r for r in candidates[:2] if _d.passes_label_post_filter(r, specs[0], params)]
    assert len(old_bug) == 1


def test_union_arms_rejects_in_query_limit_skip_and_order_by() -> None:
    # The UNION_ARMS fail-loud guards: a per-arm LIMIT/SKIP would over-return and a per-arm
    # ORDER BY != global order, so an un-popped LIMIT/SKIP or an ORDER BY raises.
    with_limit = _NODE_BFS + "\nLIMIT 10"
    with_skip = _NODE_BFS + "\nSKIP 5"
    with_order = _NODE_BFS.replace("RETURN n.uuid AS uuid", "RETURN n.uuid AS uuid ORDER BY n.uuid")
    for q in (with_limit, with_skip, with_order):
        with pytest.raises(_d.AgeDialectError):
            _d.apply_traversal_style(q, _d.TraversalStyle.UNION_ARMS)


def test_union_arms_with_limit_global_bound_pipeline() -> None:
    # FIX-2 SPEC regression: a *1..6 traversal with LIMIT $limit=10 pops to 6 arms carrying
    # ZERO per-arm LIMIT tokens, and the popped global bound caps the union at 10 (not 60).
    q = (
        "UNWIND $bfs_origin_node_uuids AS origin_uuid\n"
        "MATCH (origin {uuid: origin_uuid})-[:RELATES_TO|MENTIONS*1..6]->(n:Entity)\n"
        "WHERE n.group_id = origin.group_id\n"
        "RETURN n.uuid AS uuid\n"
        "LIMIT $limit"
    )
    params: dict[str, Any] = {"limit": 10}
    text, limit = _d.pop_trailing_limit(q, params)
    assert limit == 10
    out = _d.apply_traversal_style(text, _d.TraversalStyle.UNION_ARMS)
    assert len(out.split("\nUNION\n")) == 6  # 6 arms for *1..6
    assert "LIMIT" not in out  # no per-arm limit anywhere
    # simulate the 60 unioned rows AGE could return (6 arms x 10) and enforce the bound.
    rows = list(range(60))
    assert len(rows[:limit]) == 10  # AT MOST 10 globally, not 6x10


def test_union_arms_edge_bfs_with_limit_pipeline() -> None:
    # The real graphiti-core 0.29.2 path-bound edge-BFS shape + trailing LIMIT pops then
    # expands limit-free to 2 arms (*1..2).
    q = _EDGE_BFS + "\nLIMIT $limit"
    params: dict[str, Any] = {"limit": 10}
    text, limit = _d.pop_trailing_limit(q, params)
    assert limit == 10
    out = _d.apply_traversal_style(text, _d.TraversalStyle.UNION_ARMS)
    assert len(out.split("\nUNION\n")) == 2  # *1..2 → 2 arms
    assert "LIMIT" not in out  # zero per-arm LIMIT tokens


def test_driver_pops_trailing_limit_and_slices_after_post_filter() -> None:
    # Driver-source structural pin (driver.py imports graphiti_core, sidecar-venv-only):
    # execute_query consults pop_trailing_limit under the label_specs-or-UNION_ARMS
    # condition and applies records[:in_driver_limit] AFTER the passes_label_post_filter
    # drop (the bound applies to the ACTUALLY-matching rows).
    src = _driver_source()
    exec_q = src.split("async def execute_query", 1)[1].split("async def ", 1)[0]
    assert "pop_trailing_limit(text, clean)" in exec_q
    assert "TraversalStyle.UNION_ARMS and has_vle_segment(text)" in exec_q
    assert "if in_driver_limit is not None:" in exec_q
    assert exec_q.index("passes_label_post_filter(r, s, clean)") < exec_q.index(
        "records[:in_driver_limit]"
    )
