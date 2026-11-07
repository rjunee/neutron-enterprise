# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""D2 provable trimming: REAL ne-permissions expander composed with EntityGraph.

Real ``PrincipalStore``/``PrincipalExpander`` over sqlite + ``EntityGraph`` over
the in-memory store (always-on), proving transitive groups, unknown principals,
disjoint ACLs, empty-ACL nodes, ``TENANT_ALL``, query-time revocation, and the
headline property: a path whose ONLY route crosses an invisible node OR edge is
absent even when both endpoints are visible.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.brain_graph.model import EntityKey
from ne.brain_graph.read import EntityGraph
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts.types import Principal
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import TENANT_ALL, PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore, ensure_permissions_schema

NOW = datetime(2026, 7, 9, tzinfo=UTC)

P_ADA = Principal(id="u-ada")  # transitive g-child ⊂ g-eng
P_BOB = Principal(id="u-bob")  # store-known, groupless
P_GHOST = Principal(id="u-ghost")  # never synced -> unknown


def _key(k: str) -> EntityKey:
    return EntityKey(entity_class="Account", key=k)


@dataclass
class _Env:
    graph: EntityGraph
    store: InMemoryGraphStore
    principals: PrincipalStore
    expander: PrincipalExpander


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'principals.db'}")
    ensure_permissions_schema(engine)
    pstore = PrincipalStore(engine)
    pstore.upsert_principals(
        [
            SyncedPrincipal(principal_id="u-ada", kind=PrincipalKind.USER),
            SyncedPrincipal(principal_id="u-bob", kind=PrincipalKind.USER),
        ],
        source="test",
        now=NOW,
    )
    pstore.set_member_groups("u-ada", ["g-child"], source="test", now=NOW)
    pstore.set_member_groups("g-child", ["g-eng"], source="test", now=NOW)

    expander = PrincipalExpander(pstore)
    store = InMemoryGraphStore()

    async def setup() -> None:
        await store.upsert_nodes(
            [
                make_node("a", "Account", ("g-eng",)),
                make_node("b", "Account", ("u-ada",)),
                make_node("x", "Account", ("u-other",)),  # invisible intermediate NODE
                make_node("p", "Account", ("u-ada",)),
                make_node("q", "Account", ("u-ada",)),
                make_node("t", "Account", (TENANT_ALL,)),
                make_node("ghost", "Account", ("u-ghost",)),
            ]
        )
        # An empty-acl node cannot be UPSERT (write-boundary fail-closed), so it is
        # injected directly to prove the READ-time defense-in-depth: a node with no
        # acl matches NOTHING for every principal.
        store._nodes["e"] = make_node("e", "Account", ())

        # -- NE-T2X-3: D2 extension over extracted (ne:llm) nodes/edges. New
        # elements connect ONLY to new keys (ex1/ex2/llm_x/llm_v/llm_e) so no
        # existing neighbor/path assertion above can shift.
        await store.upsert_nodes(
            [
                make_node("ex1", "Account", ("u-ada",)),
                make_node("ex2", "Account", ("u-ada",)),
                make_node(
                    "llm_x",
                    "Account",
                    ("u-other",),  # invisible extracted middle
                    source_system="ne:llm",
                    assurance="extracted",
                    evidence=["sha256:p|c"],
                ),
                make_node(
                    "llm_v",
                    "Account",
                    ("g-eng",),
                    source_system="ne:llm",
                    assurance="extracted",
                ),
            ]
        )
        # Empty-ACL ne:llm node (mirrors node "e"'s read-time defense-in-depth).
        store._nodes["llm_e"] = make_node("llm_e", "Account", (), source_system="ne:llm")

        ex1_llmv = make_edge("ABOUT", "ex1", "llm_v", ("u-ada",), key="ex1_llmv")
        ex1_llmv["source_system"] = "ne:llm"
        ex1_llmv["assurance"] = "extracted"

        await store.upsert_edges(
            [
                # a -> x -> b : the only a..b route crosses the invisible node x.
                make_edge("ABOUT", "a", "x", ("u-ada",), key="a_x"),
                make_edge("ABOUT", "x", "b", ("u-ada",), key="x_b"),
                # p -> q via an INVISIBLE edge (endpoints visible to ada).
                make_edge("ABOUT", "p", "q", ("u-other",), key="p_q"),
                # a -> t : a plainly-visible 1-hop for ada.
                make_edge("ABOUT", "a", "t", ("u-ada",), key="a_t"),
                # ex1 -> llm_x -> ex2 : the only ex1..ex2 route crosses the
                # invisible EXTRACTED node llm_x.
                make_edge("ABOUT", "ex1", "llm_x", ("u-ada",), key="ex1_llmx"),
                make_edge("ABOUT", "llm_x", "ex2", ("u-ada",), key="llmx_ex2"),
                # ex1 -> llm_v : an ne:llm-SOURCED edge (headline: reaches a
                # visible extracted node with explicit assurance).
                ex1_llmv,
            ]
        )

    asyncio.run(setup())
    graph = EntityGraph(store, group_expander=expander, audit_sink=None)
    return _Env(graph=graph, store=store, principals=pstore, expander=expander)


def _lookup(env: _Env, principal: Principal, key: str) -> set[str]:
    nodes = asyncio.run(env.graph.lookup([_key(key)], principal=principal))
    return {n.key for n in nodes}


def test_transitive_group_member_sees_group_node(env: _Env) -> None:
    assert _lookup(env, P_ADA, "a") == {"a"}  # g-eng via g-child ⊂ g-eng


def test_groupless_known_principal_denied_group_node_but_sees_tenant_all(env: _Env) -> None:
    assert _lookup(env, P_BOB, "a") == set()
    assert _lookup(env, P_BOB, "t") == {"t"}


def test_unknown_principal_only_self_never_tenant_all(env: _Env) -> None:
    assert _lookup(env, P_GHOST, "ghost") == {"ghost"}
    assert _lookup(env, P_GHOST, "t") == set()  # TENANT_ALL only for known principals


def test_empty_acl_node_invisible_to_all(env: _Env) -> None:
    assert _lookup(env, P_ADA, "e") == set()
    assert _lookup(env, P_BOB, "e") == set()
    assert _lookup(env, P_GHOST, "e") == set()


def test_disjoint_acl_hidden(env: _Env) -> None:
    assert _lookup(env, P_ADA, "x") == set()  # u-other, disjoint from ada


def test_headline_path_through_invisible_node_is_absent(env: _Env) -> None:
    # a and b are BOTH visible to ada, but the only route crosses invisible x.
    assert _lookup(env, P_ADA, "a") == {"a"}
    assert _lookup(env, P_ADA, "b") == {"b"}
    records = asyncio.run(env.graph.paths(_key("a"), _key("b"), principal=P_ADA, max_hops=3))
    assert records == ()
    # Neighbors of a at 2 hops must NOT reach b through x.
    sub = asyncio.run(env.graph.neighbors([_key("a")], principal=P_ADA, hops=2))
    assert "b" not in {n.key for n in sub.nodes}
    assert "t" in {n.key for n in sub.nodes}  # the plainly-visible hop survives


def test_path_through_invisible_edge_is_absent(env: _Env) -> None:
    assert _lookup(env, P_ADA, "p") == {"p"}
    assert _lookup(env, P_ADA, "q") == {"q"}
    records = asyncio.run(env.graph.paths(_key("p"), _key("q"), principal=P_ADA, max_hops=2))
    assert records == ()
    sub = asyncio.run(env.graph.neighbors([_key("p")], principal=P_ADA, hops=1))
    assert "q" not in {n.key for n in sub.nodes}


def test_query_time_revocation(env: _Env) -> None:
    assert _lookup(env, P_ADA, "a") == {"a"}
    # Revoke ada's membership: next expansion no longer includes g-eng.
    env.principals.set_member_groups("u-ada", [], source="test", now=NOW)
    assert _lookup(env, P_ADA, "a") == set()


# -- NE-T2X-3: D2 real-expander battery over extracted (ne:llm) elements -------
# Extension, not exemption — every assertion above stays verbatim; these prove
# extracted nodes/edges trim through the IDENTICAL acl_match_labels algebra.


def test_extracted_node_visible_via_transitive_group_carries_assurance(env: _Env) -> None:
    nodes = asyncio.run(env.graph.lookup([_key("llm_v")], principal=P_ADA))
    assert {n.key for n in nodes} == {"llm_v"}
    assert nodes[0].assurance == "extracted"


def test_extracted_node_disjoint_acl_hidden(env: _Env) -> None:
    assert _lookup(env, P_ADA, "llm_x") == set()  # u-other, disjoint from ada


def test_extracted_empty_acl_node_invisible_to_all(env: _Env) -> None:
    assert _lookup(env, P_ADA, "llm_e") == set()
    assert _lookup(env, P_BOB, "llm_e") == set()
    assert _lookup(env, P_GHOST, "llm_e") == set()


def test_headline_path_through_invisible_extracted_node_is_absent(env: _Env) -> None:
    # ex1 and ex2 are BOTH visible to ada, but the only route crosses the
    # invisible EXTRACTED node llm_x.
    assert _lookup(env, P_ADA, "ex1") == {"ex1"}
    assert _lookup(env, P_ADA, "ex2") == {"ex2"}
    records = asyncio.run(env.graph.paths(_key("ex1"), _key("ex2"), principal=P_ADA, max_hops=3))
    assert records == ()

    sub = asyncio.run(env.graph.neighbors([_key("ex1")], principal=P_ADA, hops=2))
    node_keys = {n.key for n in sub.nodes}
    assert "ex2" not in node_keys  # blocked by the invisible extracted middle
    assert "llm_v" in node_keys  # the plainly-visible ne:llm hop survives
    llm_v_node = next(n for n in sub.nodes if n.key == "llm_v")
    assert llm_v_node.assurance == "extracted"
    llm_v_edge = next(e for e in sub.edges if e.key == "ex1_llmv")
    assert llm_v_edge.assurance == "extracted"


def test_query_time_revocation_applies_to_extracted(env: _Env) -> None:
    assert _lookup(env, P_ADA, "llm_v") == {"llm_v"}
    env.principals.set_member_groups("u-ada", [], source="test", now=NOW)
    assert _lookup(env, P_ADA, "llm_v") == set()
