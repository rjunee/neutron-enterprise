# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Connector-health withholding on every principal-scoped Tier-2 read shape."""

from __future__ import annotations

import asyncio

import pytest

from ne.brain_graph.model import EntityKey
from ne.brain_graph.read import EntityGraph
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts.types import Principal


class _Health:
    def __init__(self, degraded: frozenset[str]) -> None:
        self.degraded = degraded
        self.calls = 0

    async def __call__(self) -> frozenset[str]:
        self.calls += 1
        return self.degraded


_PRINCIPAL = Principal(id="u1", groups=("eng",))


def test_backbone_and_unattributable_derived_nodes_withhold_then_recover() -> None:
    store = InMemoryGraphStore()
    health = _Health(frozenset({"fixture"}))
    graph = EntityGraph(
        store,
        group_expander=None,
        audit_sink=None,
        source_health=health,
    )

    async def scenario() -> None:
        await store.upsert_nodes(
            [
                make_node("connector", "Account", ("eng",), source_system="fixture"),
                make_node("manual", "Account", ("eng",), source_system="manual"),
                make_node("derived", "Account", ("eng",), source_system="ne:llm"),
            ]
        )
        keys = [
            EntityKey(entity_class="Account", key="connector"),
            EntityKey(entity_class="Account", key="manual"),
            EntityKey(entity_class="Account", key="derived"),
        ]
        degraded = await graph.lookup(keys=keys, principal=_PRINCIPAL)
        assert [node.key for node in degraded] == ["manual"]

        health.degraded = frozenset()
        recovered = await graph.lookup(keys=keys, principal=_PRINCIPAL)
        assert {node.key for node in recovered} == {"connector", "manual", "derived"}
        assert health.calls == 2

    asyncio.run(scenario())


def test_path_is_dropped_whole_when_its_asserting_connector_is_degraded() -> None:
    store = InMemoryGraphStore()
    graph = EntityGraph(
        store,
        group_expander=None,
        audit_sink=None,
        source_health=_Health(frozenset({"fixture"})),
    )

    async def scenario() -> None:
        await store.upsert_nodes(
            [
                make_node("a", "Account", ("eng",), source_system="manual"),
                make_node("b", "Account", ("eng",), source_system="manual"),
            ]
        )
        edge = make_edge("LINK", "a", "b", ("eng",), key="a_b")
        edge["source_system"] = "fixture"
        await store.upsert_edges([edge])

        paths = await graph.paths(
            EntityKey(entity_class="Account", key="a"),
            EntityKey(entity_class="Account", key="b"),
            principal=_PRINCIPAL,
        )
        assert paths == ()

    asyncio.run(scenario())


def test_neighbors_and_subgraph_drop_connector_asserted_rows_then_recover() -> None:
    store = InMemoryGraphStore()
    health = _Health(frozenset({"fixture"}))
    graph = EntityGraph(
        store,
        group_expander=None,
        audit_sink=None,
        source_health=health,
    )

    async def scenario() -> None:
        await store.upsert_nodes(
            [
                make_node("a", "Account", ("eng",), source_system="manual"),
                make_node("b", "Account", ("eng",), source_system="manual"),
            ]
        )
        edge = make_edge("LINK", "a", "b", ("eng",), key="a_b")
        edge["source_system"] = "fixture"
        await store.upsert_edges([edge])
        anchor = [EntityKey(entity_class="Account", key="a")]

        neighbors = await graph.neighbors(anchor, principal=_PRINCIPAL)
        subgraph = await graph.subgraph(anchor, principal=_PRINCIPAL)
        assert neighbors.nodes == neighbors.edges == ()
        assert {node.key for node in subgraph.nodes} == {"a"}
        assert subgraph.edges == ()

        health.degraded = frozenset()
        neighbors = await graph.neighbors(anchor, principal=_PRINCIPAL)
        subgraph = await graph.subgraph(anchor, principal=_PRINCIPAL)
        assert {node.key for node in neighbors.nodes} == {"a", "b"}
        assert {node.key for node in subgraph.nodes} == {"a", "b"}

    asyncio.run(scenario())


def test_graph_health_provider_failure_propagates_without_returning_rows() -> None:
    store = InMemoryGraphStore()

    async def failing_health() -> frozenset[str]:
        raise RuntimeError("health store unavailable")

    graph = EntityGraph(
        store,
        group_expander=None,
        audit_sink=None,
        source_health=failing_health,
    )

    async def scenario() -> None:
        await store.upsert_nodes([make_node("a", "Account", ("eng",))])
        with pytest.raises(RuntimeError, match="health store unavailable"):
            await graph.lookup(
                keys=[EntityKey(entity_class="Account", key="a")],
                principal=_PRINCIPAL,
            )

    asyncio.run(scenario())
