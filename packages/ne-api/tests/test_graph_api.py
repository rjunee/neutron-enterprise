# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-133 — Graph read routes (POST /v1/graph/query, GET /v1/graph/neighborhood,
POST /v1/graph/paths) over the ONE composed Tier-2 ``EntityGraph``.

The governed spine is REAL; only the network edges + the graph store are faked
(the trimming-faithful ``InMemoryGraphStore``). No pytest-asyncio (repo policy):
async is driven via the conftest ``run`` helper. C7 is the load-bearing bar — every
hidden-artifact assertion is paired with a visible-artifact assertion in the SAME
scenario (non-vacuous). Wire shapes carry NO ``acl`` field (exact-field-set +
recursive no-acl-key walk).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ne.api import create_app
from ne.api.platform import build_platform
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.read import ENTITY_DISCOVERY_CURSOR_CAP
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts import reset_config_hash_for_testing
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.schema import ENTITY_CLASSES

from .conftest import admit_current_source, chunk, get_state, run
from .test_graph_wiring import (
    GraphAppHarness,
    _api_settings,
    _assert_no_acl_key,
    _ExplodingGraphStore,
    _seams,
    _seed_c7_graph,
)
from .test_ontology_api import _audit_rows

_C1 = (chunk("c1", acl=["g-eng"], text="a"),)


def _bind_current_graph(state: Any, store: InMemoryGraphStore) -> None:
    for domain, rows in (("node", store._nodes), ("edge", store._edges)):
        for row_key, properties in rows.items():
            if properties.get("source_status") == "no_source" or properties.get(
                "source_authority_stamps"
            ):
                continue
            _receipt, stamp = admit_current_source(
                state.source_authority_reader,
                key=f"graph:{domain}:{row_key}",
                labels=tuple(properties["acl"]),
                content=json.dumps(properties, sort_keys=True, default=str).encode(),
            )
            properties["source_authority_stamps"] = [stamp.model_dump()]


@pytest.fixture
def make_graph_app(tmp_path: Path) -> Iterator[Any]:
    """Replicated (~30-line) create_app harness — the make_graph_app precedent.

    ``graph_store=None`` builds the graph tier OFF (default seams); any store builds
    the tier ON. Every enabled/disabled app seeds u-ada → g-child ⊂ g-eng on the
    composed perm store so the C7 label algebra is exercised for real.
    """
    apps: list[Any] = []

    def _make(*, graph_store: Any | None = None, chunks: tuple[Any, ...] = ()) -> GraphAppHarness:
        reset_config_hash_for_testing()
        enabled = graph_store is not None
        seams, verifier, _perms_url, _store = _seams(
            tmp_path,
            key=uuid.uuid4().hex,
            chunks=chunks,
            brain_graph_settings=BrainGraphSettings(enabled=True) if enabled else None,
            graph_store=graph_store,
        )
        app = create_app(_api_settings(), seams=seams)
        apps.append(app)
        state = get_state(app)
        if isinstance(graph_store, InMemoryGraphStore):
            _bind_current_graph(state, graph_store)
        state.perm_store.upsert_principals(
            [SyncedPrincipal(principal_id="u-ada", kind=PrincipalKind.USER)],
            source="test",
            now=datetime.now(UTC),
        )
        now = datetime.now(UTC)
        state.perm_store.set_member_groups("u-ada", ["g-child"], source="test", now=now)
        state.perm_store.set_member_groups("g-child", ["g-eng"], source="test", now=now)
        return GraphAppHarness(app=app, verifier=verifier)

    yield _make
    for app in apps:
        for engine in get_state(app).engines:
            engine.dispose()


# --- topology helpers -------------------------------------------------------

_NODE_FIELDS = {
    "key",
    "entity_class",
    "display_name",
    "source_system",
    "source_ref",
    "properties",
    "assurance",
}
_EDGE_FIELDS = {
    "key",
    "edge_kind",
    "from_key",
    "to_key",
    "source_system",
    "source_ref",
    "assurance",
}
_ENTITY_SUMMARY_FIELDS = {
    "key",
    "entity_class",
    "display_name",
    "source_system",
    "assurance",
}


def _seed_star(store: InMemoryGraphStore, *, spokes: int = 4) -> None:
    """Anchor ``a`` + ``spokes`` visible neighbours (all g-eng), one edge each."""

    async def setup() -> None:
        await store.upsert_nodes(
            [make_node("a", "Account", ("g-eng",))]
            + [make_node(f"n{i}", "Account", ("g-eng",)) for i in range(1, spokes + 1)]
        )
        await store.upsert_edges(
            [
                make_edge("LINK", "a", f"n{i}", ("g-eng",), key=f"a_n{i}")
                for i in range(1, spokes + 1)
            ]
        )

    run(setup())


def _seed_two_routes(store: InMemoryGraphStore) -> None:
    """Two distinct visible a→t routes: the direct a_t edge and a→v→t (v visible)."""

    async def setup() -> None:
        await store.upsert_nodes(
            [
                make_node("a", "Account", ("g-eng",)),
                make_node("t", "Account", ("g-eng",)),
                make_node("v", "Account", ("g-eng",)),
            ]
        )
        await store.upsert_edges(
            [
                make_edge("LINK", "a", "t", ("g-eng",), key="a_t"),
                make_edge("LINK", "a", "v", ("g-eng",), key="a_v"),
                make_edge("LINK", "v", "t", ("g-eng",), key="v_t"),
            ]
        )

    run(setup())


def _assert_subgraph_shape(body: dict[str, Any]) -> None:
    assert set(body) == {"nodes", "edges", "truncated"}
    for node in body["nodes"]:
        assert set(node) == _NODE_FIELDS
    for edge in body["edges"]:
        assert set(edge) == _EDGE_FIELDS
    _assert_no_acl_key(body)


# --- GET /v1/graph/entities -------------------------------------------------


def test_graph_entities_returns_exact_acl_trimmed_wire_and_stable_cursor(
    make_graph_app: Any,
) -> None:
    store = InMemoryGraphStore()

    async def setup() -> None:
        await store.upsert_nodes(
            [
                make_node(
                    "a2", "Account", ("g-eng",), display_name="Acme", source_system="salesforce"
                ),
                make_node(
                    "a1", "Account", ("g-eng",), display_name="Acme", source_system="salesforce"
                ),
                make_node(
                    "hidden",
                    "Account",
                    ("g-secret",),
                    display_name="Acme Hidden",
                    source_system="salesforce",
                ),
                make_node("p1", "Person", ("g-eng",), display_name="Beta", source_system="m365"),
            ]
        )

    run(setup())
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("u-ada", groups=["g-child"])
            first = await client.get(
                "/api/v1/graph/entities",
                params={"q": "  acme  ", "source": "salesforce", "limit": 1},
                headers=headers,
            )
            assert first.status_code == 200
            body = first.json()
            assert set(body) == {"entities", "next_cursor"}
            assert len(body["entities"]) == 1
            assert set(body["entities"][0]) == _ENTITY_SUMMARY_FIELDS
            assert body["entities"][0] == {
                "key": "a1",
                "entity_class": "Account",
                "display_name": "Acme",
                "source_system": "salesforce",
                "assurance": "source",
            }
            assert "hidden" not in json.dumps(body)
            assert body["next_cursor"] is not None
            assert len(body["next_cursor"]) <= ENTITY_DISCOVERY_CURSOR_CAP
            second = await client.get(
                "/api/v1/graph/entities",
                params={
                    "q": "acme",
                    "source": "salesforce",
                    "limit": 1,
                    "cursor": body["next_cursor"],
                },
                headers=headers,
            )
            assert second.status_code == 200
            assert [row["key"] for row in second.json()["entities"]] == ["a2"]
            assert second.json()["next_cursor"] is None
            _assert_no_acl_key(second.json())

    run(_t())


def test_graph_entities_cursor_overflow_is_safe_502_without_success_audit(
    tmp_path: Path,
) -> None:
    reset_config_hash_for_testing()
    store = InMemoryGraphStore()

    async def setup() -> None:
        await store.upsert_nodes(
            [
                make_node(
                    "boundary",
                    "Document",
                    ("g-eng",),
                    display_name="x" * 700,
                    source_system="fixture",
                ),
                make_node(
                    "lookahead",
                    "Document",
                    ("g-eng",),
                    display_name="z" * 700,
                    source_system="fixture",
                ),
            ]
        )

    run(setup())
    seams, verifier, _p, _s = _seams(
        tmp_path,
        key="discover-cursor-overflow",
        chunks=_C1,
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=store,
    )
    app = create_app(_api_settings(), seams=seams)
    state = get_state(app)
    _bind_current_graph(state, store)
    state.perm_store.upsert_principals(
        [SyncedPrincipal(principal_id="u-ada", kind=PrincipalKind.USER)],
        source="test",
        now=datetime.now(UTC),
    )
    now = datetime.now(UTC)
    state.perm_store.set_member_groups("u-ada", ["g-child"], source="test", now=now)
    state.perm_store.set_member_groups("g-child", ["g-eng"], source="test", now=now)
    h = GraphAppHarness(app=app, verifier=verifier)
    try:

        async def _t() -> None:
            async with h.client() as client:
                response = await client.get(
                    "/api/v1/graph/entities",
                    params={"limit": 1},
                    headers=h.bearer("u-ada", groups=["g-child"]),
                )
                assert response.status_code == 502
                assert response.json() == {"detail": "GraphStoreError"}
                assert "boundary" not in response.text
                assert "lookahead" not in response.text

        run(_t())
        rows = _audit_rows(f"sqlite:///{tmp_path}/audit-discover-cursor-overflow.db")
        assert not [row for row in rows if row["action"] == "knowledge.graph_read"]
    finally:
        for engine in state.engines:
            engine.dispose()


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"entity_class": "NotAClass"}, "invalid-entity-class"),
        ({"assurance": "trusted"}, "invalid-assurance"),
        ({"cursor": "not+canonical"}, "invalid-cursor"),
        ({"cursor": "x" * (ENTITY_DISCOVERY_CURSOR_CAP + 1)}, "invalid-cursor"),
    ],
)
def test_graph_entities_rejects_invalid_filters_with_constant_422(
    make_graph_app: Any, params: dict[str, str], detail: str
) -> None:
    h = make_graph_app(graph_store=InMemoryGraphStore(), chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            response = await client.get(
                "/api/v1/graph/entities",
                params=params,
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert response.status_code == 422
            assert response.json() == {"detail": detail}

    run(_t())


def test_graph_entities_backend_failure_is_safe_502(make_graph_app: Any) -> None:
    h = make_graph_app(graph_store=_ExplodingGraphStore(), chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            response = await client.get(
                "/api/v1/graph/entities",
                params={"q": "secret-query-token"},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert response.status_code == 502
            assert response.json() == {"detail": "GraphStoreError"}
            assert "secret-query-token" not in response.text

    run(_t())


# --- GET /v1/graph/facets ---------------------------------------------------
#
# NE-GFACET. The owner-reported defect this closes: the console's Class dropdown
# offered the compile-time ne-schema registry (Account/Document/Order/Person/
# Ticket) while the live graph held Customer/Document/AutoLoan/ServicingNote —
# four of five options matched nothing and three real classes were unfilterable.
# The bar for the fix is three PAIRED arms per half (classes AND sources):
#   (1) a value PRESENT in the principal's visible data appears;
#   (2) a value in the schema with NO visible instance does NOT appear;
#   (3) a value visible to ANOTHER principal but not this one does NOT appear.
# Arm (3) is the one that matters, and it is non-vacuous here because the same
# scenario proves the other principal DOES see it.

_FACETS_FIELDS = {"entity_classes", "source_systems", "truncated"}


def _seed_two_principal_graph(store: InMemoryGraphStore) -> None:
    """g-eng sees Customer+Document over m365-sharepoint; g-secret sees Ticket
    over zendesk. Account/Person/Order are in ENTITY_CLASSES with no instance."""

    async def setup() -> None:
        await store.upsert_nodes(
            [
                make_node(
                    "cus-1",
                    "Customer",
                    ("g-eng",),
                    display_name="Ada Lovelace",
                    source_system="m365-sharepoint",
                ),
                make_node(
                    "doc-1",
                    "Document",
                    ("g-eng",),
                    display_name="Servicing policy",
                    source_system="m365-sharepoint",
                ),
                make_node(
                    "tkt-1",
                    "Ticket",
                    ("g-secret",),
                    display_name="Escalation",
                    source_system="zendesk",
                ),
            ]
        )

    run(setup())


def _grant_second_principal(app: Any, principal_id: str, groups: list[str]) -> None:
    state = get_state(app)
    now = datetime.now(UTC)
    state.perm_store.upsert_principals(
        [SyncedPrincipal(principal_id=principal_id, kind=PrincipalKind.USER)],
        source="test",
        now=now,
    )
    state.perm_store.set_member_groups(principal_id, groups, source="test", now=now)


def test_graph_facets_name_only_present_principal_visible_classes_and_sources(
    make_graph_app: Any,
) -> None:
    """All three arms, both halves, in ONE scenario over ONE seeded graph."""
    store = InMemoryGraphStore()
    _seed_two_principal_graph(store)
    h = make_graph_app(graph_store=store, chunks=_C1)
    _grant_second_principal(h.app, "u-bob", ["g-secret"])

    async def _t() -> None:
        async with h.client() as client:
            ada = await client.get(
                "/api/v1/graph/facets", headers=h.bearer("u-ada", groups=["g-child"])
            )
            assert ada.status_code == 200
            body = ada.json()
            assert set(body) == _FACETS_FIELDS
            assert body["truncated"] is False

            # (1) PRESENT in u-ada's visible data -> offered, both halves.
            assert body["entity_classes"] == ["Customer", "Document"]
            assert body["source_systems"] == ["m365-sharepoint"]

            # (2) In ne-schema ENTITY_CLASSES, but NO visible instance anywhere.
            for absent in ("Account", "Person", "Order", "AutoLoan"):
                assert absent in ENTITY_CLASSES
                assert absent not in body["entity_classes"]

            # (3) THE ACL ARM — visible only to u-bob, so absent for u-ada, on
            # BOTH halves. Never a substring of the payload either.
            assert "Ticket" not in body["entity_classes"]
            assert "zendesk" not in body["source_systems"]
            assert "zendesk" not in ada.text

            # ...and non-vacuous: u-bob really can see exactly that class and
            # source, and cannot see u-ada's.
            bob = await client.get(
                "/api/v1/graph/facets", headers=h.bearer("u-bob", groups=["g-secret"])
            )
            assert bob.status_code == 200
            bob_body = bob.json()
            assert bob_body["entity_classes"] == ["Ticket"]
            assert bob_body["source_systems"] == ["zendesk"]
            assert "Customer" not in bob_body["entity_classes"]
            assert "m365-sharepoint" not in bob_body["source_systems"]
            assert "m365-sharepoint" not in bob.text

    run(_t())


def test_graph_facets_offered_values_all_filter_to_rows_and_omitted_ones_do_not(
    make_graph_app: Any,
) -> None:
    """The dead-control regression: every offered option must return rows, and a
    schema class the facets omit must return NONE (not a 422, not a guess)."""
    store = InMemoryGraphStore()
    _seed_two_principal_graph(store)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("u-ada", groups=["g-child"])
            body = (await client.get("/api/v1/graph/facets", headers=headers)).json()
            assert body["entity_classes"] and body["source_systems"], "vacuous scenario"

            for entity_class in body["entity_classes"]:
                rows = await client.get(
                    "/api/v1/graph/entities",
                    params={"entity_class": entity_class},
                    headers=headers,
                )
                assert rows.status_code == 200, entity_class
                assert rows.json()["entities"], f"offered class {entity_class} matched nothing"
            for source in body["source_systems"]:
                rows = await client.get(
                    "/api/v1/graph/entities", params={"source": source}, headers=headers
                )
                assert rows.status_code == 200, source
                assert rows.json()["entities"], f"offered source {source} matched nothing"

            for omitted in ("Account", "Ticket"):
                rows = await client.get(
                    "/api/v1/graph/entities", params={"entity_class": omitted}, headers=headers
                )
                assert rows.status_code == 200
                assert rows.json()["entities"] == []

    run(_t())


def test_graph_facets_on_an_empty_graph_is_an_honest_empty_not_the_registry(
    make_graph_app: Any,
) -> None:
    """No fabrication: nothing visible -> empty lists, never the class registry."""
    h = make_graph_app(graph_store=InMemoryGraphStore(), chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/graph/facets", headers=h.bearer("u-ada", groups=["g-child"])
            )
            assert r.status_code == 200
            assert r.json() == {"entity_classes": [], "source_systems": [], "truncated": False}
            for never in ENTITY_CLASSES:
                assert never not in r.text

    run(_t())


def test_graph_facets_trims_a_principal_with_no_visible_slice_to_empty(
    make_graph_app: Any,
) -> None:
    """Trimmed-to-empty is EMPTY, not a 404 and not another principal's list."""
    store = InMemoryGraphStore()
    _seed_two_principal_graph(store)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/graph/facets", headers=h.bearer("u-lonely", groups=[]))
            assert r.status_code == 200
            assert r.json() == {"entity_classes": [], "source_systems": [], "truncated": False}

    run(_t())


def test_graph_facets_backend_failure_is_safe_502(make_graph_app: Any) -> None:
    h = make_graph_app(graph_store=_ExplodingGraphStore(), chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/graph/facets", headers=h.bearer("u-ada", groups=["g-child"])
            )
            assert r.status_code == 502
            assert r.json() == {"detail": "GraphStoreError"}
            assert "postgresql" not in r.text

    run(_t())


def test_graph_facets_emits_one_counts_only_audit_without_class_or_source_tokens(
    tmp_path: Path,
) -> None:
    reset_config_hash_for_testing()
    store = InMemoryGraphStore()

    async def setup() -> None:
        await store.upsert_nodes(
            [
                make_node(
                    "facet-secret",
                    "Customer",
                    ("g-eng",),
                    display_name="Name Secret",
                    source_system="source-secret-token",
                )
            ]
        )

    run(setup())
    seams, verifier, _p, _s = _seams(
        tmp_path,
        key="facets-audit",
        chunks=_C1,
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=store,
    )
    app = create_app(_api_settings(), seams=seams)
    state = get_state(app)
    _bind_current_graph(state, store)
    state.perm_store.upsert_principals(
        [SyncedPrincipal(principal_id="u-ada", kind=PrincipalKind.USER)],
        source="test",
        now=datetime.now(UTC),
    )
    now = datetime.now(UTC)
    state.perm_store.set_member_groups("u-ada", ["g-child"], source="test", now=now)
    state.perm_store.set_member_groups("g-child", ["g-eng"], source="test", now=now)
    h = GraphAppHarness(app=app, verifier=verifier)
    try:

        async def _t() -> None:
            async with h.client() as client:
                r = await client.get(
                    "/api/v1/graph/facets", headers=h.bearer("u-ada", groups=["g-child"])
                )
                assert r.status_code == 200
                assert r.json()["source_systems"] == ["source-secret-token"]

        run(_t())
        rows = _audit_rows(f"sqlite:///{tmp_path}/audit-facets-audit.db")
        events = [row for row in rows if row["action"] == "knowledge.graph_read"]
        assert len(events) == 1
        assert events[0]["detail"].startswith("op=facets nodes=1 edges=0 hops=0 ")
        serialized = json.dumps(events[0])
        for secret in ("Name Secret", "facet-secret", "source-secret-token", "g-eng", "g-child"):
            assert secret not in serialized
    finally:
        for engine in state.engines:
            engine.dispose()


# --- platform-level: the ONE composed graph, honest default -----------------


def test_platform_default_entity_graph_is_none(tmp_path: Path) -> None:
    reset_config_hash_for_testing()
    seams, _v, _p, _s = _seams(tmp_path, key=uuid.uuid4().hex)
    platform = build_platform(_api_settings(), seams)
    try:
        assert platform.entity_graph is None
    finally:
        for engine in platform.engines:
            engine.dispose()

    reset_config_hash_for_testing()
    seams2, _v2, _p2, _s2 = _seams(tmp_path, key=uuid.uuid4().hex)
    app = create_app(_api_settings(), seams=seams2)
    try:
        assert get_state(app).entity_graph is None
    finally:
        for engine in get_state(app).engines:
            engine.dispose()


def test_platform_exposes_the_one_composed_entity_graph(tmp_path: Path) -> None:
    reset_config_hash_for_testing()
    seams, _v, _p, _s = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=InMemoryGraphStore(),
    )
    platform = build_platform(_api_settings(), seams)
    try:
        retriever: Any = platform.retriever
        # The exposed handle IS the graph inside the composed GraphRetriever — no
        # second, differently-trimmed graph.
        assert platform.entity_graph is retriever._graph
        # C7 by expander identity: the F2 guardian extended to the exposed handle.
        assert retriever._graph._group_expander is platform.expander
    finally:
        for engine in platform.engines:
            engine.dispose()


# --- POST /v1/graph/query ---------------------------------------------------


def test_graph_query_returns_acl_stripped_subgraph_shape(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_c7_graph(store)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/graph/query",
                json={"anchor_keys": ["a"]},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 200
            body = r.json()
            _assert_subgraph_shape(body)

    run(_t())


def test_graph_query_retains_visible_isolated_anchor(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()

    async def setup() -> None:
        await store.upsert_nodes(
            [
                make_node("a", "Account", ("g-eng",)),
                make_node("p", "Person", ("g-eng",)),
                make_node("isolated", "Account", ("g-eng",)),
            ]
        )
        await store.upsert_edges([make_edge("WORKS_AT", "p", "a", ("g-eng",), key="p_a")])

    run(setup())
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            response = await client.post(
                "/api/v1/graph/query",
                json={"anchor_keys": ["a", "isolated"]},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )

        assert response.status_code == 200
        body = response.json()
        assert {node["key"] for node in body["nodes"]} == {"a", "p", "isolated"}
        assert {edge["key"] for edge in body["edges"]} == {"p_a"}
        assert body["truncated"] is False

    run(_t())


def test_graph_query_assurance_distinguishes_extracted_from_source(
    make_graph_app: Any,
) -> None:
    """Argus P1: the ``/v1/graph`` explorer surface's ``assurance`` threading
    (``graph_wire.py``'s ``graph_node_view``/``graph_edge_view``) was ONLY
    covered by a key-set membership check (``_NODE_FIELDS``/``_EDGE_FIELDS``
    contain the string ``"assurance"``), which a hardcoded
    ``assurance="source"`` on either converter satisfies — a defeatable
    guard. This asserts VALUES: a backbone node/edge reads ``"source"``, an
    ``ne:llm`` (extraction-sourced) node/edge reads ``"extracted"``, through
    the REAL ``/api/v1/graph/query`` response body."""
    store = InMemoryGraphStore()

    async def setup() -> None:
        await store.upsert_nodes(
            [
                make_node("a", "Account", ("g-eng",)),
                # source_system="ne:llm" with no stored 'assurance' prop reads
                # as "extracted" (read.py's fail-closed truth table) — the
                # extraction-writer-shaped case, not a hand-forged one.
                make_node("p1", "Person", ("g-eng",), source_system="ne:llm"),
            ]
        )
        await store.upsert_edges(
            [
                make_edge("RELATED_TO", "a", "p1", ("g-eng",), key="a_p1_backbone"),
            ]
        )
        # A second edge, itself ne:llm-sourced, between the same two visible
        # nodes — edges carry their OWN assurance independent of either
        # endpoint's.
        edge = make_edge("MENTIONS", "p1", "a", ("g-eng",), key="p1_a_extracted")
        edge["source_system"] = "ne:llm"
        await store.upsert_edges([edge])

    run(setup())
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/graph/query",
                json={"anchor_keys": ["a"]},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 200
            body = r.json()
            node_assurance = {n["key"]: n["assurance"] for n in body["nodes"]}
            assert node_assurance["a"] == "source"
            assert node_assurance["p1"] == "extracted"
            edge_assurance = {e["key"]: e["assurance"] for e in body["edges"]}
            assert edge_assurance["a_p1_backbone"] == "source"
            assert edge_assurance["p1_a_extracted"] == "extracted"

    run(_t())


def test_graph_query_c7_hidden_node_never_appears(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_c7_graph(store)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/graph/query",
                json={"anchor_keys": ["a"]},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 200
            body = r.json()
            node_keys = {n["key"] for n in body["nodes"]}
            # NON-VACUOUS: the visible 1-hop survives.
            assert "a" in node_keys and "t" in node_keys
            # C7 bar: the invisible intermediate x and the x-only-reachable b never appear.
            assert "x" not in node_keys and "b" not in node_keys
            endpoints = {e["from_key"] for e in body["edges"]} | {
                e["to_key"] for e in body["edges"]
            }
            assert "x" not in endpoints and "b" not in endpoints

    run(_t())


def test_graph_query_groupless_principal_gets_empty_subgraph(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_c7_graph(store)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/graph/query",
                json={"anchor_keys": ["a"]},
                headers=h.bearer("u-lonely", groups=[]),
            )
            assert r.status_code == 200
            assert r.json() == {"nodes": [], "edges": [], "truncated": False}

    run(_t())


def test_graph_query_anchor_citations_resolve_via_trimmed_lookup(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_c7_graph(store)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("u-ada", groups=["g-child"])
            # 'ref-c1' is node a's source_ref — resolves anchor a via the trimmed lookup.
            r = await client.post(
                "/api/v1/graph/query", json={"anchor_citations": ["ref-c1"]}, headers=headers
            )
            assert r.status_code == 200
            node_keys = {n["key"] for n in r.json()["nodes"]}
            assert "a" in node_keys and "t" in node_keys
            assert "x" not in node_keys and "b" not in node_keys
            # 'ref-x' is ONLY the invisible node x's source_ref ⇒ resolves to nothing.
            r2 = await client.post(
                "/api/v1/graph/query", json={"anchor_citations": ["ref-x"]}, headers=headers
            )
            assert r2.status_code == 200
            assert r2.json() == {"nodes": [], "edges": [], "truncated": False}

    run(_t())


def test_graph_query_unresolvable_anchor_returns_empty_not_404(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_c7_graph(store)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/graph/query",
                json={"anchor_citations": ["ref-nope"]},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 200
            assert r.json() == {"nodes": [], "edges": [], "truncated": False}

    run(_t())


def test_graph_query_node_cap_truncates(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_star(store, spokes=4)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/graph/query",
                json={"anchor_keys": ["a"], "limit_nodes": 3},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 200
            body = r.json()
            assert body["truncated"] is True
            assert len(body["nodes"]) == 3
            keys = {n["key"] for n in body["nodes"]}
            for edge in body["edges"]:
                assert edge["from_key"] in keys and edge["to_key"] in keys

    run(_t())


def test_graph_query_edge_cap_truncates(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_star(store, spokes=4)  # 5 nodes, 4 edges visible
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("u-ada", groups=["g-child"])
            r = await client.post(
                "/api/v1/graph/query",
                json={"anchor_keys": ["a"], "limit_edges": 2},
                headers=headers,
            )
            assert r.status_code == 200
            body = r.json()
            assert body["truncated"] is True
            assert len(body["edges"]) == 2
            # Exactness: caps EXACTLY equal to the visible counts ⇒ no false positive.
            r2 = await client.post(
                "/api/v1/graph/query",
                json={"anchor_keys": ["a"], "limit_nodes": 5, "limit_edges": 4},
                headers=headers,
            )
            assert r2.status_code == 200
            body2 = r2.json()
            assert body2["truncated"] is False
            assert len(body2["nodes"]) == 5
            assert len(body2["edges"]) == 4

    run(_t())


@pytest.mark.parametrize(
    "payload",
    [
        {},  # no anchors at all ⇒ the model_validator fires
        {"anchor_keys": ["a"], "hops": 3},  # le=2
        {"anchor_keys": ["a"], "limit_nodes": 501},  # le=500
    ],
)
def test_graph_query_validation_422(make_graph_app: Any, payload: dict[str, Any]) -> None:
    h = make_graph_app(graph_store=InMemoryGraphStore(), chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/graph/query",
                json=payload,
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 422

    run(_t())


# --- GET /v1/graph/neighborhood ---------------------------------------------


def test_graph_neighborhood_shape_and_c7(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_c7_graph(store)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/graph/neighborhood",
                params={"entity": "a", "hops": 2},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 200
            body = r.json()
            _assert_subgraph_shape(body)
            node_keys = {n["key"] for n in body["nodes"]}
            assert "t" in node_keys  # non-vacuous
            assert "x" not in node_keys and "b" not in node_keys

    run(_t())


def test_graph_neighborhood_caps_truncated(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_star(store, spokes=4)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/graph/neighborhood",
                params={"entity": "a", "limit_nodes": 3},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 200
            body = r.json()
            assert body["truncated"] is True
            assert len(body["nodes"]) == 3

    run(_t())


# --- POST /v1/graph/paths ---------------------------------------------------


def test_graph_paths_visible_path_shape(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_c7_graph(store)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/graph/paths",
                json={"from": "a", "to": "t"},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 200
            body = r.json()
            assert set(body) == {"paths", "truncated"}
            assert body["truncated"] is False
            assert len(body["paths"]) == 1
            record = body["paths"][0]
            assert set(record) == {"nodes", "edges"}
            assert [n["key"] for n in record["nodes"]] == ["a", "t"]
            assert [e["key"] for e in record["edges"]] == ["a_t"]
            for node in record["nodes"]:
                assert set(node) == _NODE_FIELDS
            for edge in record["edges"]:
                assert set(edge) == _EDGE_FIELDS
            _assert_no_acl_key(body)

    run(_t())


def test_graph_paths_c7_invisible_intermediate_yields_no_path(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_c7_graph(store)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            # The ONLY a→b route crosses the invisible node x ⇒ no path on the wire.
            r = await client.post(
                "/api/v1/graph/paths",
                json={"from": "a", "to": "b"},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 200
            assert r.json() == {"paths": [], "truncated": False}

    run(_t())


def test_graph_paths_limit_truncated(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_two_routes(store)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/graph/paths",
                json={"from": "a", "to": "t", "limit": 1},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 200
            body = r.json()
            assert len(body["paths"]) == 1
            assert body["truncated"] is True

    run(_t())


def test_graph_paths_accepts_from_to_aliases(make_graph_app: Any) -> None:
    store = InMemoryGraphStore()
    _seed_c7_graph(store)
    h = make_graph_app(graph_store=store, chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("u-ada", groups=["g-child"])
            r_alias = await client.post(
                "/api/v1/graph/paths", json={"from": "a", "to": "t"}, headers=headers
            )
            r_field = await client.post(
                "/api/v1/graph/paths", json={"from_key": "a", "to_key": "t"}, headers=headers
            )
            assert r_alias.status_code == 200
            assert r_field.status_code == 200

    run(_t())


# --- honest degrade / auth / backend-fault ----------------------------------


@pytest.mark.parametrize(
    ("method", "path", "json_body", "params"),
    [
        ("post", "/api/v1/graph/query", {"anchor_keys": ["a"]}, None),
        ("get", "/api/v1/graph/entities", None, None),
        ("get", "/api/v1/graph/facets", None, None),
        ("get", "/api/v1/graph/neighborhood", None, {"entity": "a"}),
        ("post", "/api/v1/graph/paths", {"from": "a", "to": "t"}, None),
    ],
)
def test_graph_disabled_routes_return_honest_404(
    make_graph_app: Any,
    method: str,
    path: str,
    json_body: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> None:
    h = make_graph_app(graph_store=None)  # graph tier OFF (default seams)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("u-ada", groups=["g-child"])
            r = await client.request(
                method.upper(), path, json=json_body, params=params, headers=headers
            )
            assert r.status_code == 404
            assert r.json()["detail"] == "graph tier not enabled"

    run(_t())


@pytest.mark.parametrize(
    ("method", "path", "json_body", "params"),
    [
        ("post", "/api/v1/graph/query", {"anchor_keys": ["a"]}, None),
        ("get", "/api/v1/graph/entities", None, None),
        ("get", "/api/v1/graph/facets", None, None),
        ("get", "/api/v1/graph/neighborhood", None, {"entity": "a"}),
        ("post", "/api/v1/graph/paths", {"from": "a", "to": "t"}, None),
    ],
)
def test_graph_routes_require_bearer_401(
    make_graph_app: Any,
    method: str,
    path: str,
    json_body: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> None:
    h = make_graph_app(graph_store=InMemoryGraphStore(), chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.request(method.upper(), path, json=json_body, params=params)
            assert r.status_code == 401

    run(_t())


def test_graph_store_failure_maps_to_502_class_name_only(make_graph_app: Any) -> None:
    h = make_graph_app(graph_store=_ExplodingGraphStore(), chunks=_C1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/graph/query",
                json={"anchor_keys": ["a"]},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 502
            assert r.json() == {"detail": "GraphStoreError"}
            assert "postgresql" not in r.text  # no DSN oracle

    run(_t())


# --- NE-164b: read-path auditing on the composed GET /v1/graph/neighborhood ---


def test_neighborhood_route_emits_graph_read_audit_event(tmp_path: Path) -> None:
    reset_config_hash_for_testing()
    store = InMemoryGraphStore()

    async def setup() -> None:
        await store.upsert_nodes(
            [
                make_node("a", "Account", ("g-eng",)),
                make_node("n1", "Account", ("g-eng",)),
            ]
        )
        await store.upsert_edges([make_edge("LINK", "a", "n1", ("g-eng",), key="a_n1")])

    run(setup())

    seams, verifier, _p, _s = _seams(
        tmp_path,
        key="ne164b",
        chunks=_C1,
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=store,
    )
    app = create_app(_api_settings(), seams=seams)
    state = get_state(app)
    _bind_current_graph(state, store)
    state.perm_store.upsert_principals(
        [SyncedPrincipal(principal_id="u-ada", kind=PrincipalKind.USER)],
        source="test",
        now=datetime.now(UTC),
    )
    now = datetime.now(UTC)
    state.perm_store.set_member_groups("u-ada", ["g-child"], source="test", now=now)
    state.perm_store.set_member_groups("g-child", ["g-eng"], source="test", now=now)
    h = GraphAppHarness(app=app, verifier=verifier)

    try:

        async def _t() -> None:
            async with h.client() as client:
                r = await client.get(
                    "/api/v1/graph/neighborhood",
                    params={"entity": "a"},
                    headers=h.bearer("u-ada", groups=["g-child"]),
                )
                assert r.status_code == 200

        run(_t())

        rows = _audit_rows(f"sqlite:///{tmp_path}/audit-ne164b.db")
        events = [r for r in rows if r["action"] == "knowledge.graph_read"]
        assert len(events) == 1
        event = events[0]
        assert event["outcome"] == "success"
        detail = event["detail"]
        assert detail.startswith("op=neighbors ")
        assert " acl_trim_delta=0" in detail
        # bare HTTP call outside any run scope -> no ambient trace_id.
        assert event["payload"].get("trace_id") is None
        row_text = json.dumps(event)
        assert "g-eng" not in row_text
        assert "g-child" not in row_text
        assert "n1" not in row_text
    finally:
        for engine in state.engines:
            engine.dispose()


def test_entity_discovery_emits_one_counts_only_audit_without_filter_tokens(
    tmp_path: Path,
) -> None:
    reset_config_hash_for_testing()
    store = InMemoryGraphStore()

    async def setup() -> None:
        await store.upsert_nodes(
            [
                make_node(
                    "entity-secret",
                    "Account",
                    ("g-eng",),
                    display_name="Name Secret",
                    source_system="source-secret-token",
                )
            ]
        )

    run(setup())
    seams, verifier, _p, _s = _seams(
        tmp_path,
        key="discover-audit",
        chunks=_C1,
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=store,
    )
    app = create_app(_api_settings(), seams=seams)
    state = get_state(app)
    _bind_current_graph(state, store)
    state.perm_store.upsert_principals(
        [SyncedPrincipal(principal_id="u-ada", kind=PrincipalKind.USER)],
        source="test",
        now=datetime.now(UTC),
    )
    now = datetime.now(UTC)
    state.perm_store.set_member_groups("u-ada", ["g-child"], source="test", now=now)
    state.perm_store.set_member_groups("g-child", ["g-eng"], source="test", now=now)
    h = GraphAppHarness(app=app, verifier=verifier)
    try:

        async def _t() -> None:
            async with h.client() as client:
                response = await client.get(
                    "/api/v1/graph/entities",
                    params={
                        "q": "Name Secret",
                        "source": "source-secret-token",
                        "assurance": "source",
                    },
                    headers=h.bearer("u-ada", groups=["g-child"]),
                )
                assert response.status_code == 200

        run(_t())
        rows = _audit_rows(f"sqlite:///{tmp_path}/audit-discover-audit.db")
        events = [row for row in rows if row["action"] == "knowledge.graph_read"]
        assert len(events) == 1
        event = events[0]
        assert event["detail"].startswith("op=discover nodes=1 edges=0 hops=0 ")
        serialized = json.dumps(event)
        for secret in (
            "Name Secret",
            "entity-secret",
            "source-secret-token",
            "g-eng",
            "g-child",
        ):
            assert secret not in serialized
    finally:
        for engine in state.engines:
            engine.dispose()
