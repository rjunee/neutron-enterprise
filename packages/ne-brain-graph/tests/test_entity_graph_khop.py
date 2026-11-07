# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T-KHOP: k-hop correctness through the REAL EntityGraph over the in-memory store.

Known topology (all visible to the querying principal):
    C1 -WORKS_AT-> A1 ; K1 -BELONGS_TO-> A1 ; K1 -REQUESTER-> C1 ;
    O1 -ACCOUNT-> A1 ; O1 -PLACED_BY-> C1
"""

from __future__ import annotations

import asyncio

import pytest

from ne.brain_graph.model import EntityKey
from ne.brain_graph.read import EntityGraph
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts.types import Principal

_ACL = ("g",)
_PRINCIPAL = Principal(id="u", groups=("g",))


def _key(k: str) -> EntityKey:
    return EntityKey(entity_class="X", key=k)


def _graph() -> EntityGraph:
    store = InMemoryGraphStore()

    async def build() -> None:
        await store.upsert_nodes(
            [
                make_node("A1", "Account", _ACL, display_name="Acme"),
                make_node("C1", "Person", _ACL, display_name="Alice Ng"),
                make_node("K1", "Ticket", _ACL, display_name="Login issue"),
                make_node("O1", "Order", _ACL, display_name="ORD-0001"),
                make_node("I1", "Account", _ACL, display_name="Isolated"),
            ]
        )
        await store.upsert_edges(
            [
                make_edge("WORKS_AT", "C1", "A1", _ACL),
                make_edge("BELONGS_TO", "K1", "A1", _ACL),
                make_edge("REQUESTER", "K1", "C1", _ACL),
                make_edge("ACCOUNT", "O1", "A1", _ACL),
                make_edge("PLACED_BY", "O1", "C1", _ACL),
            ]
        )

    asyncio.run(build())
    return EntityGraph(store, group_expander=None, audit_sink=None)


def test_lookup_by_key() -> None:
    graph = _graph()
    nodes = asyncio.run(graph.lookup([_key("A1")], principal=_PRINCIPAL))
    assert [n.key for n in nodes] == ["A1"]
    assert nodes[0].display_name == "Acme"
    # No ACL leak in the returned payload.
    assert "acl" not in nodes[0].properties


def test_lookup_name_prefix() -> None:
    graph = _graph()
    nodes = asyncio.run(
        graph.lookup(principal=_PRINCIPAL, entity_class="Person", name_query="Alice")
    )
    assert [n.key for n in nodes] == ["C1"]


def test_neighbors_both_hop1() -> None:
    graph = _graph()
    sub = asyncio.run(graph.neighbors([_key("C1")], principal=_PRINCIPAL, hops=1))
    assert {n.key for n in sub.nodes} == {"C1", "A1", "K1", "O1"}
    assert {e.edge_kind for e in sub.edges} == {"WORKS_AT", "REQUESTER", "PLACED_BY"}


def test_neighbors_out_direction() -> None:
    graph = _graph()
    sub = asyncio.run(graph.neighbors([_key("C1")], principal=_PRINCIPAL, hops=1, direction="out"))
    assert {n.key for n in sub.nodes} == {"C1", "A1"}
    assert {e.edge_kind for e in sub.edges} == {"WORKS_AT"}


def test_neighbors_edge_kind_filter() -> None:
    graph = _graph()
    sub = asyncio.run(
        graph.neighbors([_key("K1")], principal=_PRINCIPAL, hops=1, edge_kinds=["BELONGS_TO"])
    )
    assert {n.key for n in sub.nodes} == {"K1", "A1"}
    assert {e.edge_kind for e in sub.edges} == {"BELONGS_TO"}


def test_paths_direct_and_indirect() -> None:
    graph = _graph()
    records = asyncio.run(graph.paths(_key("K1"), _key("A1"), principal=_PRINCIPAL, max_hops=2))
    lengths = sorted(len(r.edges) for r in records)
    assert lengths == [1, 2]  # K1->A1 direct; K1->C1->A1 indirect


def test_paths_anchor_by_record_id_and_entity_class() -> None:
    """NE-GRAPH-ANCHOR: endpoints resolve by source-record id and entity class.

    A caller holds record ids (e.g. ``mer-loan-001``) and class names, never
    the store's UUIDv5 node keys — the S5 demo hop "loan -> its Customer" is
    exactly ``paths(id, class)``.
    """
    store = InMemoryGraphStore()

    async def build() -> None:
        await store.upsert_nodes(
            [
                make_node(
                    "uuid-loan-1",
                    "AutoLoan",
                    _ACL,
                    id="mer-loan-001",
                    display_name="mer-loan-001",
                ),
                make_node(
                    "uuid-cus-1",
                    "Customer",
                    _ACL,
                    id="mer-cus-001",
                    display_name="Avery Arbor",
                ),
            ]
        )
        await store.upsert_edges([make_edge("HAS_AUTO_LOAN", "uuid-cus-1", "uuid-loan-1", _ACL)])

    asyncio.run(build())
    graph = EntityGraph(store, group_expander=None, audit_sink=None)

    # record id -> entity class ("the nearest Customer").
    records = asyncio.run(
        graph.paths(
            EntityKey(entity_class="", key="mer-loan-001"),
            EntityKey(entity_class="", key="Customer"),
            principal=_PRINCIPAL,
            max_hops=2,
        )
    )
    assert records
    assert records[0].nodes[0].key == "uuid-loan-1"
    assert records[0].nodes[-1].entity_class == "Customer"
    assert records[0].nodes[-1].display_name == "Avery Arbor"

    # record id -> record id still works, and UUID keys are still accepted.
    for subject, obj in (
        ("mer-loan-001", "mer-cus-001"),
        ("uuid-loan-1", "uuid-cus-1"),
    ):
        records = asyncio.run(
            graph.paths(
                EntityKey(entity_class="", key=subject),
                EntityKey(entity_class="", key=obj),
                principal=_PRINCIPAL,
                max_hops=2,
            )
        )
        assert records, (subject, obj)

    # an invisible principal still sees nothing through the widened anchor.
    stranger = Principal(id="stranger")
    assert (
        asyncio.run(
            graph.paths(
                EntityKey(entity_class="", key="mer-loan-001"),
                EntityKey(entity_class="", key="Customer"),
                principal=stranger,
                max_hops=2,
            )
        )
        == ()
    )


def test_hop_cap_clamped() -> None:
    graph = _graph()
    # Even asking for hops=10, the settings max_hops (default 3) clamps it.
    sub = asyncio.run(graph.neighbors([_key("C1")], principal=_PRINCIPAL, hops=10))
    assert sub.nodes  # still returns; no crash, just clamped depth


def test_subgraph_two_hops() -> None:
    graph = _graph()
    sub = asyncio.run(graph.subgraph([_key("A1")], principal=_PRINCIPAL, hops=2))
    assert {n.key for n in sub.nodes} == {"A1", "C1", "K1", "O1"}


def test_subgraph_retains_authorized_isolated_anchor() -> None:
    graph = _graph()

    sub = asyncio.run(graph.subgraph([_key("A1"), _key("I1")], principal=_PRINCIPAL, hops=2))

    assert {n.key for n in sub.nodes} == {"A1", "C1", "K1", "O1", "I1"}
    assert all(edge.from_key != "I1" and edge.to_key != "I1" for edge in sub.edges)


def test_invisible_principal_sees_nothing() -> None:
    graph = _graph()
    stranger = Principal(id="stranger")  # labels = {"stranger"}, no intersection
    assert asyncio.run(graph.lookup([_key("A1")], principal=stranger)) == ()
    sub = asyncio.run(graph.neighbors([_key("C1")], principal=stranger, hops=2))
    assert sub.nodes == () and sub.edges == ()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
