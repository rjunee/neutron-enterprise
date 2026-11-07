# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``GraphStoreContractKit`` — the C5 conformance battery for a ``GraphStore``.

Subclass and override :meth:`create_graph_store` to prove an adapter (AGE,
in-memory, later Neo4j) satisfies the frozen contract + the package-local
capabilities. Async methods are exercised via ``asyncio.run`` (pytest-asyncio is
NOT a dependency — the ChunkStore/ObjectStore kit rule). Kit-law asserted here:

* double-upsert of the same ``key`` yields ONE node (idempotent merge);
* an upsert with the same key OVERWRITES properties;
* an upsert of a node/edge with missing/empty ``acl`` is REJECTED (fail-closed);
* a canonical parameterized cypher round-trips its params unmutated (echo);
* batches larger than one internal batch upsert cleanly;
* ``purge_llm_extracted`` is DETACH-semantics: removing an ``ne:llm`` node
  also removes every edge INCIDENT to it, of ANY ``source_system`` — including
  an ``ne:er`` ``SAME_AS`` edge that anchored it into an ER cluster. This is a
  JUSTIFIED deviation (a graph store cannot leave an edge dangling off a
  deleted vertex), not a defect; pinned by the two purge DETACH tests below
  (``test_purge_llm_extracted_detach_removes_edges_of_any_source_incident_to_purged_nodes``
  and ``test_purge_llm_extracted_spares_edges_not_incident_to_any_purged_node``).
* lookup queries used by the kit itself are built at the STORE'S probed
  predicate rung (``store.predicate_style``), never a hardcoded style — the
  R1 ladder exists so a real server can reject an earlier rung, and the kit
  must exercise whichever rung the adapter under test actually selected.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import pytest

from ne.brain_graph.age.query import ACL_PARAM, PredicateStyle, TrimmedCypherBuilder
from ne.brain_graph.errors import GraphAclError, GraphStoreError


class _Store(Protocol):
    @property
    def predicate_style(self) -> PredicateStyle: ...

    async def upsert_nodes(self, nodes: Any) -> None: ...
    async def upsert_edges(self, edges: Any) -> None: ...
    async def cypher(self, query: str, params: Any = None) -> Any: ...
    async def update_acl(self, keys: Any, acl: Any) -> None: ...
    async def ensure_graph(self) -> None: ...
    async def delete_edges_from_source(
        self, source_system: str, from_key: str, keep_keys: Any
    ) -> None: ...
    async def scan_nodes(self, entity_class: str | None = None) -> Any: ...
    async def scan_edges(self) -> Any: ...
    async def fetch_nodes(self, keys: Any) -> Any: ...
    async def delete_edge(self, key: str) -> None: ...
    async def purge_llm_extracted(self, keys: Any = None) -> dict[str, int]: ...


def _node(key: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "key": key,
        "entity_class": "Account",
        "display_name": f"Acct {key}",
        "source_system": "salesforce",
        "source_ref": f"{key}:{key}",
        "acl": ("group-a",),
        "content_hash": "h0",
        "config_hash": "c0",
        "schema_version": "1.0.0",
        "synced_at": "2026-07-09T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def _edge(key: str, frm: str, to: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "key": key,
        "edge_kind": "ABOUT",
        "from_key": frm,
        "to_key": to,
        "source_system": "salesforce",
        "source_ref": f"{key}:{key}",
        "acl": ("group-a",),
        "config_hash": "c0",
        "synced_at": "2026-07-09T00:00:00+00:00",
    }
    base.update(overrides)
    return base


class GraphStoreContractKit:
    """Subclass and override :meth:`create_graph_store` to prove conformance."""

    def create_graph_store(self) -> _Store:
        raise NotImplementedError

    def test_ensure_graph_idempotent(self) -> None:
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            await store.ensure_graph()

        asyncio.run(scenario())

    def test_double_upsert_yields_one_node(self) -> None:
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            await store.upsert_nodes([_node("a1")])
            await store.upsert_nodes([_node("a1")])
            builder = TrimmedCypherBuilder(store.predicate_style)
            compiled = builder.build_lookup(["group-a"], keys=["a1"], entity_class="Account")
            rows = await store.cypher(compiled.text, compiled.params)
            assert len(list(rows)) == 1

        asyncio.run(scenario())

    def test_upsert_overwrites_properties(self) -> None:
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            await store.upsert_nodes([_node("a1", display_name="First")])
            await store.upsert_nodes([_node("a1", display_name="Second")])
            builder = TrimmedCypherBuilder(store.predicate_style)
            compiled = builder.build_lookup(["group-a"], keys=["a1"])
            rows = list(await store.cypher(compiled.text, compiled.params))
            assert len(rows) == 1
            assert rows[0]["node"]["display_name"] == "Second"

        asyncio.run(scenario())

    def test_empty_acl_node_upsert_rejected(self) -> None:
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            with pytest.raises(GraphAclError):
                await store.upsert_nodes([_node("a1", acl=())])

        asyncio.run(scenario())

    def test_empty_acl_edge_upsert_rejected(self) -> None:
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            await store.upsert_nodes([_node("a1"), _node("a2")])
            with pytest.raises(GraphAclError):
                await store.upsert_edges([_edge("e1", "a1", "a2", acl=())])

        asyncio.run(scenario())

    def test_cypher_params_echo_unmutated(self) -> None:
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            await store.upsert_nodes([_node("a1")])
            builder = TrimmedCypherBuilder(store.predicate_style)
            compiled = builder.build_lookup(["group-a"], keys=["a1"])
            before = dict(compiled.params)
            await store.cypher(compiled.text, compiled.params)
            assert compiled.params == before
            assert compiled.params[ACL_PARAM] == ["group-a"]

        asyncio.run(scenario())

    def test_delete_edges_from_source_keeps_foreign_and_kept(self) -> None:
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            await store.upsert_nodes([_node("a1"), _node("a2"), _node("a3")])
            # Two connector edges out of a1, plus one foreign-source 'ne:er' edge.
            await store.upsert_edges(
                [
                    _edge("e_keep", "a1", "a2"),
                    _edge("e_stale", "a1", "a3"),
                    _edge("e_er", "a1", "a2", source_system="ne:er"),
                ]
            )
            await store.delete_edges_from_source("salesforce", "a1", ["e_keep"])
            edges = {e["key"] for e in await store.scan_edges()}
            assert "e_keep" in edges  # kept (in keep_keys)
            assert "e_stale" not in edges  # deleted (connector-source, not kept)
            assert "e_er" in edges  # foreign source_system survives

        asyncio.run(scenario())

    def test_scan_and_fetch_nodes(self) -> None:
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            await store.upsert_nodes([_node("a1"), _node("a2", entity_class="Person")])
            all_nodes = {n["key"] for n in await store.scan_nodes()}
            assert all_nodes == {"a1", "a2"}
            accounts = [n["key"] for n in await store.scan_nodes("Account")]
            assert accounts == ["a1"]
            fetched = {n["key"] for n in await store.fetch_nodes(["a1", "missing"])}
            assert fetched == {"a1"}

        asyncio.run(scenario())

    def test_delete_edge_guards_non_er_source(self) -> None:
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            await store.upsert_nodes([_node("a1"), _node("a2")])
            await store.upsert_edges(
                [
                    _edge("e_conn", "a1", "a2"),
                    _edge("e_er", "a1", "a2", edge_kind="SAME_AS", source_system="ne:er"),
                ]
            )
            with pytest.raises(GraphStoreError):
                await store.delete_edge("e_conn")  # non-'ne:er' guarded
            await store.delete_edge("e_er")  # unmerge-only path deletes
            await store.delete_edge("e_er")  # idempotent no-op on absent
            edges = {e["key"] for e in await store.scan_edges()}
            assert edges == {"e_conn"}

        asyncio.run(scenario())

    def test_purge_llm_extracted_wholesale_spares_backbone(self) -> None:
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            await store.upsert_nodes(
                [
                    _node("backbone1"),
                    _node("llm1", entity_class="Person", source_system="ne:llm"),
                    _node("llm2", entity_class="Person", source_system="ne:llm"),
                ]
            )
            counts = await store.purge_llm_extracted()
            assert counts == {"nodes": 2, "edges": 0}
            remaining = {n["key"] for n in await store.scan_nodes()}
            assert remaining == {"backbone1"}

        asyncio.run(scenario())

    def test_purge_llm_extracted_targeted_keys_are_source_scoped(self) -> None:
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            await store.upsert_nodes(
                [
                    _node("backbone1"),
                    _node("llm1", entity_class="Person", source_system="ne:llm"),
                    _node("llm2", entity_class="Person", source_system="ne:llm"),
                ]
            )
            # A backbone key passed in keys= is silently untouched (source-scoped).
            counts = await store.purge_llm_extracted(keys=["llm1", "backbone1"])
            assert counts == {"nodes": 1, "edges": 0}
            remaining = {n["key"] for n in await store.scan_nodes()}
            assert remaining == {"backbone1", "llm2"}

        asyncio.run(scenario())

    def test_purge_llm_extracted_detach_removes_edges_of_any_source_incident_to_purged_nodes(
        self,
    ) -> None:
        """DETACH semantics (Argus P1): a graph DB cannot delete a vertex while
        edges still reference it, so purging a node ALSO removes every edge
        incident to it — of ANY ``source_system``, not just ``ne:llm`` — as
        unavoidable collateral (design §0 A-P1-4's ``ne:er`` ``SAME_AS``
        case: an extracted node anchored into an ER cluster loses that anchor
        edge too when the node is purged)."""
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            await store.upsert_nodes(
                [
                    _node("backbone1"),
                    _node("llm1", entity_class="Person", source_system="ne:llm"),
                ]
            )
            await store.upsert_edges(
                [
                    # ne:llm edge incident to llm1 -> deleted alongside llm1.
                    _edge("e_llm", "llm1", "backbone1", source_system="ne:llm"),
                    # foreign-source edge incident to llm1 -> also removed:
                    # a graph store cannot leave an edge dangling off a
                    # deleted vertex (DETACH collateral, not a targeted
                    # foreign-element delete).
                    _edge("e_er", "llm1", "backbone1", edge_kind="SAME_AS", source_system="ne:er"),
                ]
            )
            counts = await store.purge_llm_extracted()
            assert counts == {"nodes": 1, "edges": 2}
            remaining_nodes = {n["key"] for n in await store.scan_nodes()}
            assert remaining_nodes == {"backbone1"}
            remaining_edges = {e["key"] for e in await store.scan_edges()}
            assert remaining_edges == set()

        asyncio.run(scenario())

    def test_purge_llm_extracted_spares_edges_not_incident_to_any_purged_node(self) -> None:
        """The invariant DETACH collateral does NOT swallow: an edge whose
        endpoints are BOTH surviving (non-purged) nodes is never touched by
        purge, even if it is a foreign-source (``ne:er``) edge — purge only
        ever reaches an element that is itself ``ne:llm`` or collateral of an
        ``ne:llm`` node it removed, never an unrelated backbone/ER edge."""
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            await store.upsert_nodes(
                [
                    _node("backbone1"),
                    _node("backbone2"),
                    _node("llm1", entity_class="Person", source_system="ne:llm"),
                ]
            )
            await store.upsert_edges(
                [
                    _edge(
                        "e_er", "backbone1", "backbone2", edge_kind="SAME_AS", source_system="ne:er"
                    ),
                ]
            )
            counts = await store.purge_llm_extracted()
            assert counts == {"nodes": 1, "edges": 0}
            remaining_edges = {e["key"] for e in await store.scan_edges()}
            assert remaining_edges == {"e_er"}

        asyncio.run(scenario())

    def test_batch_upsert_all_present(self) -> None:
        store = self.create_graph_store()

        async def scenario() -> None:
            await store.ensure_graph()
            nodes = [_node(f"a{i}") for i in range(150)]
            await store.upsert_nodes(nodes)
            builder = TrimmedCypherBuilder(store.predicate_style)
            compiled = builder.build_lookup(["group-a"], entity_class="Account", limit=1000)
            rows = list(await store.cypher(compiled.text, compiled.params))
            assert len(rows) == 150

        asyncio.run(scenario())
