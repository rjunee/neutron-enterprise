# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""Dual-backend temporal-graph conformance harness (runs INSIDE the sidecar venv).

Drives a fixed, deterministic scenario against EITHER the Apache-AGE driver or the
Neo4j-CE driver and emits ONE canonical JSON document to stdout. Two backends are
behaviorally equivalent iff their canonical JSON is identical — this is the binding
AGE == Neo4j-CE proof (the ``test_dual_backend_conformance.py`` live-parked test runs
both and asserts equality).

NO LLM / embedder service is used: nodes/edges are constructed directly with fixed
literal embeddings, so the scenario is reproducible and offline (C2).

Usage (inside the venv)::

    python conformance.py --backend age    # NE_MEMORY_GRAPH_AGE_DSN / _AGE_GRAPH
    python conformance.py --backend neo4j  # NE_MEMORY_GRAPH_NEO4J_URI/USER/PASSWORD

Rank ORDER is excluded from the canonical fulltext/vector output by design (ts_rank is
not BM25; only SET membership of the top-k is compared — feasibility row 9).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from typing import Any

# Fixed scenario constants -----------------------------------------------------
GROUP_A = "agent-a"
GROUP_B = "agent-b"
GROUP_C = "agent-c"
#: FIX (Codex P1) BULK-ingestion probe group. The single-save seed above drives
#: EntityNode/EntityEdge/EpisodicNode.save(); the sidecar ``/episodes`` service instead
#: drives ``Graphiti.add_episode`` → ``add_nodes_and_edges_bulk_tx`` → the UNWIND-based
#: BULK save queries (``WITH n, node CALL …`` vector-property writes + dynamic labels +
#: bulk side-table population). GROUP_D exercises that path with the EXACT pinned bulk
#: queries so AGE==Neo4j is proven on it too (it was previously uncovered).
GROUP_D = "agent-d"
#: FIX-1 (silent-corruption) probe: a property VALUE whose text contains the literal
#: AGE agtype cast tokens. It must round-trip through the AGE backend byte-identical
#: (Neo4j returns the same ground truth), proving the codec strips only real type
#: suffixes, never substrings inside stored user data.
TOKEN_TEXT = "user data with literal ::vertex, ::edge, ::path and ::numeric tokens"
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 1, 3, 0, 0, 0, tzinfo=UTC)
_EMBED_DIM = 8


def _vec(seed: int) -> list[float]:
    """A fixed unit-ish embedding derived from a seed (deterministic, no service)."""
    return [((seed * (i + 1)) % 7) / 7.0 for i in range(_EMBED_DIM)]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _build_driver(backend: str) -> Any:
    if backend == "age":
        from graphiti_age import AgeDriver

        return AgeDriver(
            dsn=_env("NE_MEMORY_GRAPH_AGE_DSN"),
            graph_name=_env("NE_MEMORY_GRAPH_AGE_GRAPH", "ne_memory"),
        )
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    return Neo4jDriver(
        uri=_env("NE_MEMORY_GRAPH_NEO4J_URI", "bolt://127.0.0.1:7687"),
        user=_env("NE_MEMORY_GRAPH_NEO4J_USER", "neo4j"),
        password=_env("NE_MEMORY_GRAPH_NEO4J_PASSWORD", "neo4j"),
    )


async def _seed(driver: Any) -> dict[str, Any]:
    """Save the fixed episodic/entity/edge scenario; return the created identities."""
    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

    ep_a0 = EpisodicNode(
        name="ep-a0",
        group_id=GROUP_A,
        source=EpisodeType.text,
        source_description="seed",
        content="Alice joined Acme as an engineer",
        created_at=T0,
        valid_at=T0,
    )
    ep_a1 = EpisodicNode(
        name="ep-a1",
        group_id=GROUP_A,
        source=EpisodeType.text,
        source_description="seed",
        content="Alice was promoted to lead",
        created_at=T1,
        valid_at=T1,
    )
    ep_b0 = EpisodicNode(
        name="ep-b0",
        group_id=GROUP_B,
        source=EpisodeType.text,
        source_description="seed",
        content="Bob prefers tea over coffee",
        created_at=T0,
        valid_at=T0,
    )
    for ep in (ep_a0, ep_a1, ep_b0):
        await ep.save(driver)

    alice = EntityNode(name="Alice", group_id=GROUP_A, labels=["Entity"], created_at=T0)
    acme = EntityNode(name="Acme", group_id=GROUP_A, labels=["Entity"], created_at=T0)
    lead = EntityNode(name="Lead", group_id=GROUP_A, labels=["Entity"], created_at=T1)
    alice.name_embedding = _vec(1)
    acme.name_embedding = _vec(2)
    lead.name_embedding = _vec(3)
    for node in (alice, acme, lead):
        await node.save(driver)

    edge_works = EntityEdge(
        source_node_uuid=alice.uuid,
        target_node_uuid=acme.uuid,
        group_id=GROUP_A,
        name="WORKS_AT",
        fact="Alice works at Acme",
        episodes=[ep_a0.uuid],
        created_at=T0,
        valid_at=T0,
    )
    edge_role = EntityEdge(
        source_node_uuid=alice.uuid,
        target_node_uuid=lead.uuid,
        group_id=GROUP_A,
        name="HAS_ROLE",
        fact="Alice has role engineer",
        episodes=[ep_a0.uuid],
        created_at=T0,
        valid_at=T0,
        # invalidated at T2 — a temporal as-of before T2 sees it, after T2 does not.
        invalid_at=T2,
        expired_at=T2,
    )
    edge_works.fact_embedding = _vec(4)
    edge_role.fact_embedding = _vec(5)
    for edge in (edge_works, edge_role):
        await edge.save(driver)

    # FIX-1 scenario (silent-corruption regression): a node summary and an edge fact
    # whose VALUES contain literal agtype cast tokens (::vertex/::edge/::path/::numeric).
    # These must survive write→read byte-identical on AGE (Neo4j is the ground truth).
    tok1 = EntityNode(name="Tok1", group_id=GROUP_C, labels=["Entity"], created_at=T0)
    tok1.summary = TOKEN_TEXT
    tok1.name_embedding = _vec(6)
    tok2 = EntityNode(name="Tok2", group_id=GROUP_C, labels=["Entity"], created_at=T0)
    tok2.name_embedding = _vec(7)
    for node in (tok1, tok2):
        await node.save(driver)

    edge_tok = EntityEdge(
        source_node_uuid=tok1.uuid,
        target_node_uuid=tok2.uuid,
        group_id=GROUP_C,
        name="MENTIONS_TOKENS",
        fact=TOKEN_TEXT,
        episodes=[ep_a0.uuid],
        created_at=T0,
        valid_at=T0,
    )
    edge_tok.fact_embedding = _vec(8)
    await edge_tok.save(driver)

    return {
        "episodes": {"a0": ep_a0.uuid, "a1": ep_a1.uuid, "b0": ep_b0.uuid},
        "nodes": {
            "alice": alice.uuid,
            "acme": acme.uuid,
            "lead": lead.uuid,
            "tok1": tok1.uuid,
            "tok2": tok2.uuid,
        },
        "edges": {"works": edge_works.uuid, "role": edge_role.uuid, "tok": edge_tok.uuid},
    }


async def _qlist(driver: Any, cypher: str, key: str, **params: Any) -> list[Any]:
    """Run backend-agnostic Neo4j cypher and return the ``key`` column of every row.

    Works across both backends: ``AgeDriver.execute_query`` returns
    ``(records, aliases, None)`` and Neo4j's ``EagerResult`` is a 3-tuple whose first
    element is the record list; both record types support ``rec[key]`` and return
    native python scalars for scalar projections.
    """
    result = await driver.execute_query(cypher, params=params)
    records = result[0]
    return [rec[key] for rec in records]


async def _one(driver: Any, cypher: str, key: str, **params: Any) -> Any:
    rows = await _qlist(driver, cypher, key, **params)
    return rows[0] if rows else None


async def _recall_core(driver: Any, ids: dict[str, Any]) -> dict[str, Any]:
    """The BINDING cross-backend recall scenario — deterministic, scalar, identical on
    AGE and Neo4j (every value derives from stored graph facts, none from ``ids``).

    Covers: uuid lookup (row 1/16), group_id-filtered search + isolation (row 12),
    temporal as-of before/after edge invalidation (rows 5/6), 1-/2-hop traversal
    (rows 7/8), count + collect plumbing (row 13), and content-token recall (row 9 at
    the equivalence level — tsvector RANKING is an AGE-side detail asserted separately;
    only token membership is compared, per feasibility row 9)."""
    alice = ids["nodes"]["alice"]
    return {
        "entity_by_uuid_name": await _one(
            driver, "MATCH (n:Entity {uuid: $uuid}) RETURN n.name AS name", "name", uuid=alice
        ),
        "entities_in_group_a": sorted(
            await _qlist(
                driver,
                "MATCH (n:Entity) WHERE n.group_id = $g RETURN n.name AS name",
                "name",
                g=GROUP_A,
            )
        ),
        "entities_in_group_b": sorted(
            await _qlist(
                driver,
                "MATCH (n:Entity) WHERE n.group_id = $g RETURN n.name AS name",
                "name",
                g=GROUP_B,
            )
        ),
        "episodes_in_group_a": sorted(
            await _qlist(
                driver,
                "MATCH (n:Episodic) WHERE n.group_id = $g RETURN n.name AS name",
                "name",
                g=GROUP_A,
            )
        ),
        "episodes_in_group_b": sorted(
            await _qlist(
                driver,
                "MATCH (n:Episodic) WHERE n.group_id = $g RETURN n.name AS name",
                "name",
                g=GROUP_B,
            )
        ),
        "alice_targets_asof_T1": sorted(
            await _qlist(
                driver,
                "MATCH (a:Entity {uuid: $u})-[e:RELATES_TO]->(t:Entity) "
                "WHERE e.valid_at <= $as_of AND (e.invalid_at IS NULL OR e.invalid_at > $as_of) "
                "RETURN t.name AS name",
                "name",
                u=alice,
                as_of=T1,
            )
        ),
        "alice_targets_asof_T2": sorted(
            await _qlist(
                driver,
                "MATCH (a:Entity {uuid: $u})-[e:RELATES_TO]->(t:Entity) "
                "WHERE e.valid_at <= $as_of AND (e.invalid_at IS NULL OR e.invalid_at > $as_of) "
                "RETURN t.name AS name",
                "name",
                u=alice,
                as_of=T2,
            )
        ),
        "alice_1hop_targets": sorted(
            await _qlist(
                driver,
                "MATCH (a:Entity {uuid: $u})-[:RELATES_TO]->(t:Entity) RETURN t.name AS name",
                "name",
                u=alice,
            )
        ),
        "alice_2hop_names": sorted(
            set(
                await _qlist(
                    driver,
                    "MATCH (a:Entity {uuid: $u})-[*1..2]->(t:Entity) RETURN t.name AS name",
                    "name",
                    u=alice,
                )
            )
        ),
        "entity_count_group_a": await _one(
            driver,
            "MATCH (n:Entity) WHERE n.group_id = $g RETURN count(*) AS cnt",
            "cnt",
            g=GROUP_A,
        ),
        "entity_names_collected_group_a": sorted(
            await _one(
                driver,
                "MATCH (n:Entity) WHERE n.group_id = $g RETURN collect(n.name) AS names",
                "names",
                g=GROUP_A,
            )
            or []
        ),
        "content_token_promoted_group_a": sorted(
            await _qlist(
                driver,
                "MATCH (n:Episodic) WHERE n.group_id = $g AND n.content CONTAINS $tok "
                "RETURN n.name AS name",
                "name",
                g=GROUP_A,
                tok="promoted",
            )
        ),
        "content_token_tea_group_a": sorted(
            await _qlist(
                driver,
                "MATCH (n:Episodic) WHERE n.group_id = $g AND n.content CONTAINS $tok "
                "RETURN n.name AS name",
                "name",
                g=GROUP_A,
                tok="tea",
            )
        ),
        "content_token_tea_group_b": sorted(
            await _qlist(
                driver,
                "MATCH (n:Episodic) WHERE n.group_id = $g AND n.content CONTAINS $tok "
                "RETURN n.name AS name",
                "name",
                g=GROUP_B,
                tok="tea",
            )
        ),
        # FIX-1: a stored property VALUE containing literal ::vertex/::edge/::path/
        # ::numeric tokens must round-trip UNCHANGED. Neo4j returns TOKEN_TEXT verbatim,
        # so cross-backend equality here is a byte-identical AGE round-trip proof.
        "token_node_summary": await _one(
            driver,
            "MATCH (n:Entity {uuid: $u}) RETURN n.summary AS s",
            "s",
            u=ids["nodes"]["tok1"],
        ),
        "token_edge_fact": await _one(
            driver,
            "MATCH ()-[e:RELATES_TO {uuid: $u}]->() RETURN e.fact AS f",
            "f",
            u=ids["edges"]["tok"],
        ),
    }


async def _age_side_recall(driver: Any, ids: dict[str, Any]) -> dict[str, Any]:
    """AGE-only: exercise the tsvector fulltext + vector side-table shims (rows 9/10).

    Excluded from cross-backend equality (Neo4j uses BM25 / native vector — different
    mechanism); asserted for correctness in ``test_age_driver_server.py``. Hits are
    mapped back to stable scenario labels so the output is reproducible."""
    label_of = {v: k for k, v in ids["nodes"].items()} | {v: k for k, v in ids["episodes"].items()}
    ft = await driver._ft_search("promoted", "node", [GROUP_A], 10)
    vec = await driver._vec_search(_vec(1), "node", "name_embedding", [GROUP_A], 2)
    # FIX (Codex P1) finding-2 proof: the BULK /episodes rows must land in the ft/vec side
    # tables (a bulk save that did not populate them would leave /search blind to
    # episode-ingested data). These probe the GROUP_D bulk rows written by _bulk_seed.
    bulk_ft_node = await driver._ft_search("shipped", "node", [GROUP_D], 10)
    bulk_ft_edge = await driver._ft_search("shipped", "edge", [GROUP_D], 10)
    bulk_vec_node = await driver._vec_search(_vec(11), "node", "name_embedding", [GROUP_D], 5)
    bulk_vec_edge = await driver._vec_search(_vec(13), "edge", "fact_embedding", [GROUP_D], 5)
    return {
        "ft_promoted_group_a": sorted(label_of.get(u, u) for u, _ in ft),
        "vec_top2_for_alice_vector": sorted(label_of.get(u, u) for u, _ in vec),
        "vector_backend": str(getattr(driver, "_vector_backend", "") or ""),
        # bulk side-table population (booleans — the specific hit set is backend-agnostic
        # graph-fact recall, already compared in bulk_recall):
        "bulk_ft_node_populated": bool(bulk_ft_node),
        "bulk_ft_edge_populated": bool(bulk_ft_edge),
        "bulk_vec_node_populated": bool(bulk_vec_node),
        "bulk_vec_edge_populated": bool(bulk_vec_edge),
    }


async def _fresh_graph(driver: Any) -> dict[str, Any]:
    """FIX-2: search a freshly-built graph (NO nodes/edges/embeddings yet).

    On AGE this hits the just-created {graph}_ft / {graph}_vec side tables — the
    pgvector rung's table now exists up front, so a similarity search returns an empty
    result instead of raising ``UndefinedTable``. Every list MUST be ``[]`` on BOTH
    backends (Neo4j's native empty-graph path returns empty too)."""
    from graphiti_core.search.search_filters import SearchFilters
    from graphiti_core.search.search_utils import (
        edge_similarity_search,
        node_fulltext_search,
        node_similarity_search,
    )

    sf = SearchFilters()
    nodes = await node_similarity_search(driver, _vec(1), sf, group_ids=[GROUP_A], limit=5)
    edges = await edge_similarity_search(
        driver, _vec(4), None, None, sf, group_ids=[GROUP_A], limit=5
    )
    ft = await node_fulltext_search(driver, "anything", sf, [GROUP_A], 5)
    return {
        "node_similarity": [getattr(n, "uuid", n) for n in nodes],
        "edge_similarity": [getattr(e, "uuid", e) for e in edges],
        "node_fulltext": [getattr(n, "uuid", n) for n in ft],
    }


async def _edge_similarity(driver: Any, ids: dict[str, Any]) -> dict[str, Any]:
    """FIX-3 / FIX-3b: source/target-filtered edge-similarity parity (AGE == Neo4j).

    NOTE (upstream quirk, recorded in the memo): graphiti-core applies the source/target
    filters ONLY inside the ``group_ids is not None`` branch, so we always pass
    ``group_ids=[GROUP_A]``. Results canonicalise to sorted scenario labels."""
    from graphiti_core.search.search_filters import SearchFilters
    from graphiti_core.search.search_utils import edge_similarity_search

    sf = SearchFilters()
    label_of = {v: k for k, v in ids["edges"].items()}
    alice = ids["nodes"]["alice"]
    acme = ids["nodes"]["acme"]
    lead = ids["nodes"]["lead"]

    async def run(src: str | None, tgt: str | None, min_score: float | None) -> list[str]:
        kwargs: dict[str, Any] = {"group_ids": [GROUP_A], "limit": 5}
        if min_score is not None:
            kwargs["min_score"] = min_score
        edges = await edge_similarity_search(driver, _vec(4), src, tgt, sf, **kwargs)
        return sorted(label_of.get(getattr(e, "uuid", e), getattr(e, "uuid", e)) for e in edges)

    return {
        "filtered_alice_acme": await run(alice, acme, -1.0),
        "wrong_pair_acme_lead": await run(acme, lead, -1.0),
        "unfiltered": await run(None, None, -1.0),
        "default_min_score_unfiltered": await run(None, None, None),
    }


async def _bulk_seed(driver: Any) -> dict[str, Any]:
    """Ingest a fixed scenario via Graphiti's BULK save queries (the /episodes path).

    Uses the EXACT pinned graphiti-core 0.29.2 bulk-save query builders (NEO4J provider)
    with the bulk param shapes ``add_nodes_and_edges_bulk_tx`` constructs — so the AGE
    driver must translate ``UNWIND $nodes``, dynamic labels ``SET n:$(node.labels)``, the
    multi-var ``WITH n, node CALL db.create.setNodeVectorProperty(…)`` write, and populate
    the ft/vec side tables for bulk rows. Neo4j runs the same queries natively; the recall
    below derives only from graph facts, so equality is a true parity proof."""
    from graphiti_core.driver.driver import GraphProvider
    from graphiti_core.models.edges.edge_db_queries import (
        get_entity_edge_save_bulk_query,
        get_episodic_edge_save_bulk_query,
    )
    from graphiti_core.models.nodes.node_db_queries import (
        get_entity_node_save_bulk_query,
        get_episode_node_save_bulk_query,
    )

    ep = {
        "uuid": "bulk-ep0",
        "name": "bulk-ep0",
        "group_id": GROUP_D,
        "source_description": "seed",
        "source": "text",
        "content": "Dana shipped the bulk ingestion feature",
        "entity_edges": [],
        "created_at": T0,
        "valid_at": T0,
    }
    nodes = [
        {
            "uuid": "bulk-dana",
            "name": "Dana",
            "group_id": GROUP_D,
            "summary": "a shipper",
            "created_at": T0,
            "labels": ["Entity", "Person"],
            "name_embedding": _vec(11),
        },
        {
            "uuid": "bulk-feat",
            "name": "Feature",
            "group_id": GROUP_D,
            "summary": "",
            "created_at": T0,
            "labels": ["Entity"],
            "name_embedding": _vec(12),
        },
    ]
    edges = [
        {
            "uuid": "bulk-ships",
            "source_node_uuid": "bulk-dana",
            "target_node_uuid": "bulk-feat",
            "name": "SHIPPED",
            "fact": "Dana shipped the feature",
            "group_id": GROUP_D,
            "episodes": ["bulk-ep0"],
            "created_at": T0,
            "expired_at": None,
            "valid_at": T0,
            "invalid_at": None,
            "reference_time": T0,
            "fact_embedding": _vec(13),
        }
    ]
    episodic_edges = [
        {
            "uuid": "bulk-mentions",
            "source_node_uuid": "bulk-ep0",
            "target_node_uuid": "bulk-dana",
            "group_id": GROUP_D,
            "created_at": T0,
        }
    ]

    await driver.execute_query(
        get_episode_node_save_bulk_query(GraphProvider.NEO4J), params={"episodes": [ep]}
    )
    await driver.execute_query(
        get_entity_node_save_bulk_query(GraphProvider.NEO4J, nodes), params={"nodes": nodes}
    )
    await driver.execute_query(
        get_entity_edge_save_bulk_query(GraphProvider.NEO4J), params={"entity_edges": edges}
    )
    await driver.execute_query(
        get_episodic_edge_save_bulk_query(GraphProvider.NEO4J),
        params={"episodic_edges": episodic_edges},
    )
    return {"nodes": {"dana": "bulk-dana", "feat": "bulk-feat"}, "edges": {"ships": "bulk-ships"}}


async def _bulk_recall(driver: Any, _ids: dict[str, Any]) -> dict[str, Any]:
    """FIX (Codex P1): recall over BULK-ingested data — backend-agnostic (graph facts).

    Every value derives from stored graph facts (names/facts/traversal/content), so it is
    byte-identical on AGE and Neo4j — proving the bulk ``/episodes`` write path ingests
    equivalently. (The AGE side-table population for these rows is asserted separately in
    ``test_age_driver_server.py``; Neo4j has no side-table analogue.)"""
    return {
        "entities_in_group_d": sorted(
            await _qlist(
                driver,
                "MATCH (n:Entity) WHERE n.group_id = $g RETURN n.name AS name",
                "name",
                g=GROUP_D,
            )
        ),
        "episodes_in_group_d": sorted(
            await _qlist(
                driver,
                "MATCH (n:Episodic) WHERE n.group_id = $g RETURN n.name AS name",
                "name",
                g=GROUP_D,
            )
        ),
        "edge_facts_in_group_d": sorted(
            await _qlist(
                driver,
                "MATCH (:Entity)-[e:RELATES_TO]->(:Entity) WHERE e.group_id = $g "
                "RETURN e.fact AS fact",
                "fact",
                g=GROUP_D,
            )
        ),
        "dana_1hop_targets": sorted(
            await _qlist(
                driver,
                "MATCH (a:Entity {uuid: $u})-[:RELATES_TO]->(t:Entity) RETURN t.name AS name",
                "name",
                u="bulk-dana",
            )
        ),
        "dana_mentioned_by_episode": sorted(
            await _qlist(
                driver,
                "MATCH (ep:Episodic)-[:MENTIONS]->(n:Entity {uuid: $u}) RETURN ep.name AS name",
                "name",
                u="bulk-dana",
            )
        ),
    }


async def _run(backend: str) -> dict[str, Any]:
    driver = _build_driver(backend)
    try:
        await driver.build_indices_and_constraints()
        report: dict[str, Any] = {"backend": backend}
        # FIX-2: fresh-graph search must return empty (never UndefinedTable) — run
        # BEFORE seeding so the graph is genuinely empty.
        report["fresh_graph"] = await _fresh_graph(driver)
        ids = await _seed(driver)
        report.update(await _recall_core(driver, ids))
        # FIX-3 / FIX-3b: filtered edge-similarity parity.
        report["edge_similarity"] = await _edge_similarity(driver, ids)
        # FIX (Codex P1): BULK /episodes ingestion parity — recall over data written via
        # the UNWIND-based bulk save queries (backend-agnostic graph facts).
        bulk_ids = await _bulk_seed(driver)
        report["bulk_recall"] = await _bulk_recall(driver, bulk_ids)
        report["probe_rungs"] = {
            "predicate": str(getattr(driver, "_predicate_style", "") or ""),
            "vector_backend": str(getattr(driver, "_vector_backend", "") or ""),
            "traversal": str(getattr(driver, "_traversal_style", "") or ""),
        }
        if hasattr(driver, "_ft_search"):
            report["age_side_table_recall"] = await _age_side_recall(driver, ids)
        return report
    finally:
        await driver.close()


def _canonical(doc: dict[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, indent=2, default=str)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("age", "neo4j"), required=True)
    args = parser.parse_args()
    report = asyncio.run(_run(args.backend))
    print(_canonical(report))


if __name__ == "__main__":
    main()
