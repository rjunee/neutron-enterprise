# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal-scoped entity discovery: stable filters, cursors, and post-filter scans."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from ne.brain_graph.errors import GraphStoreError
from ne.brain_graph.read import (
    ENTITY_DISCOVERY_CURSOR_CAP,
    EntityGraph,
    InvalidDiscoveryCursor,
)
from ne.brain_graph.testing.fixtures import make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts.types import Principal

_PRINCIPAL = Principal(id="u", groups=("visible",))


def _graph(store: Any, *, health: Any = None) -> EntityGraph:
    return EntityGraph(
        store,
        group_expander=None,
        audit_sink=None,
        source_health=health,
    )


def test_discovery_orders_by_human_name_class_key_and_pages_from_last_visible() -> None:
    store = InMemoryGraphStore()

    async def scenario() -> None:
        await store.upsert_nodes(
            [
                make_node("k3", "Person", ("visible",), display_name="Beta"),
                make_node("k2", "Person", ("visible",), display_name="Alpha"),
                make_node("k1", "Account", ("visible",), display_name="Alpha"),
            ]
        )
        graph = _graph(store)
        first = await graph.discover(principal=_PRINCIPAL, limit=2)
        assert [(n.display_name, n.entity_class, n.key) for n in first.entities] == [
            ("Alpha", "Account", "k1"),
            ("Alpha", "Person", "k2"),
        ]
        assert first.next_cursor is not None
        second = await graph.discover(principal=_PRINCIPAL, cursor=first.next_cursor, limit=2)
        assert [n.key for n in second.entities] == ["k3"]
        assert second.next_cursor is None

    asyncio.run(scenario())


def test_discovery_order_is_case_normalized_across_page_boundaries() -> None:
    store = InMemoryGraphStore()

    async def scenario() -> None:
        await store.upsert_nodes(
            [
                make_node("k3", "Person", ("visible",), display_name="delta lowercase"),
                make_node("k1", "Account", ("visible",), display_name="Alpha uppercase"),
                make_node("k2", "Person", ("visible",), display_name="beta lowercase"),
                make_node("k4", "Person", ("visible",), display_name="Echo uppercase"),
            ]
        )
        graph = _graph(store)
        first = await graph.discover(principal=_PRINCIPAL, limit=2)
        assert [node.key for node in first.entities] == ["k1", "k2"]
        assert first.next_cursor is not None
        second = await graph.discover(principal=_PRINCIPAL, cursor=first.next_cursor, limit=2)
        assert [node.key for node in second.entities] == ["k3", "k4"]

    asyncio.run(scenario())


def test_discovery_q_is_trimmed_exact_key_or_case_insensitive_partial_name() -> None:
    store = InMemoryGraphStore()

    async def scenario() -> None:
        await store.upsert_nodes(
            [
                make_node("Exact-Key", "Account", ("visible",), display_name="Unrelated"),
                make_node("other", "Account", ("visible",), display_name="Meridian Auto"),
                make_node("hidden", "Account", ("other",), display_name="Meridian Secret"),
            ]
        )
        graph = _graph(store)
        exact = await graph.discover(principal=_PRINCIPAL, q="  Exact-Key  ")
        assert [n.key for n in exact.entities] == ["Exact-Key"]
        partial = await graph.discover(principal=_PRINCIPAL, q="mErIdIaN")
        assert [n.key for n in partial.entities] == ["other"]

    asyncio.run(scenario())


class _CountingExpander:
    def __init__(self) -> None:
        self.calls = 0

    def expand(self, principal: Principal | str) -> frozenset[str]:
        self.calls += 1
        return frozenset({"visible"})


def test_discovery_rechecks_acl_after_store_and_expands_labels_once() -> None:
    expander = _CountingExpander()
    store = _BrokenOrderStore(
        [
            {"node": make_node("hidden", "Account", ("other",), display_name="Alpha")},
            {"node": make_node("shown", "Account", ("visible",), display_name="Beta")},
        ]
    )

    async def scenario() -> None:
        graph = EntityGraph(store, group_expander=expander, audit_sink=None)
        page = await graph.discover(principal=_PRINCIPAL)
        assert [node.key for node in page.entities] == ["shown"]
        assert expander.calls == 1

    asyncio.run(scenario())


def test_discovery_applies_exact_class_source_and_derived_assurance() -> None:
    store = InMemoryGraphStore()

    async def scenario() -> None:
        await store.upsert_nodes(
            [
                make_node(
                    "confirmed",
                    "Account",
                    ("visible",),
                    display_name="Confirmed",
                    source_system="ne:llm",
                    assurance="confirmed",
                ),
                make_node(
                    "extracted",
                    "Account",
                    ("visible",),
                    display_name="Extracted",
                    source_system="ne:llm",
                    assurance="forged-source",
                ),
                make_node(
                    "source",
                    "Account",
                    ("visible",),
                    display_name="Source",
                    source_system="salesforce",
                    assurance="confirmed",
                ),
                make_node("person", "Person", ("visible",), source_system="salesforce"),
            ]
        )
        graph = _graph(store)
        confirmed = await graph.discover(
            principal=_PRINCIPAL,
            entity_class="Account",
            source="ne:llm",
            assurance="confirmed",
        )
        assert [n.key for n in confirmed.entities] == ["confirmed"]
        extracted = await graph.discover(principal=_PRINCIPAL, assurance="extracted")
        assert [n.key for n in extracted.entities] == ["extracted"]
        source = await graph.discover(principal=_PRINCIPAL, assurance="source")
        assert {n.key for n in source.entities} == {"person", "source"}

    asyncio.run(scenario())


def test_discovery_repeats_raw_batches_after_assurance_filter_and_reads_health_once() -> None:
    store = InMemoryGraphStore()
    health_calls = 0

    async def health() -> frozenset[str]:
        nonlocal health_calls
        health_calls += 1
        return frozenset()

    async def scenario() -> None:
        await store.upsert_nodes(
            [
                *[
                    make_node(
                        f"x{i}",
                        "Account",
                        ("visible",),
                        display_name=f"A{i}",
                        source_system="ne:llm",
                    )
                    for i in range(3)
                ],
                make_node("s1", "Account", ("visible",), display_name="Z1"),
                make_node("s2", "Account", ("visible",), display_name="Z2"),
                make_node("s3", "Account", ("visible",), display_name="Z3"),
            ]
        )
        page = await _graph(store, health=health).discover(
            principal=_PRINCIPAL, assurance="source", limit=2
        )
        assert [n.key for n in page.entities] == ["s1", "s2"]
        assert page.next_cursor is not None
        assert store.cypher_calls == 2
        assert health_calls == 1

    asyncio.run(scenario())


def test_discovery_withholds_degraded_source_and_unattributable_derived_nodes() -> None:
    store = InMemoryGraphStore()
    health_calls = 0

    async def health() -> frozenset[str]:
        nonlocal health_calls
        health_calls += 1
        return frozenset({"salesforce"})

    async def scenario() -> None:
        await store.upsert_nodes(
            [
                make_node("connector", "Account", ("visible",), source_system="salesforce"),
                make_node("derived", "Account", ("visible",), source_system="ne:llm"),
                make_node("manual", "Account", ("visible",), source_system="manual"),
            ]
        )
        page = await _graph(store, health=health).discover(principal=_PRINCIPAL)
        assert [node.key for node in page.entities] == ["manual"]
        assert health_calls == 1

    asyncio.run(scenario())


def test_discovery_cursor_is_canonical_and_bound_to_exact_normalized_filters() -> None:
    store = InMemoryGraphStore()

    async def scenario() -> None:
        await store.upsert_nodes(
            [
                make_node("a", "Account", ("visible",), display_name="Acme A"),
                make_node("b", "Account", ("visible",), display_name="Acme B"),
            ]
        )
        graph = _graph(store)
        filters = {
            "q": "Acme",
            "entity_class": "Account",
            "source": "fixture",
            "assurance": "source",
        }
        first = await graph.discover(
            principal=_PRINCIPAL,
            q="  Acme ",
            entity_class="Account",
            source="fixture",
            assurance="source",
            limit=1,
        )
        assert first.next_cursor is not None
        assert len(first.next_cursor) <= ENTITY_DISCOVERY_CURSOR_CAP
        continued = await graph.discover(
            principal=_PRINCIPAL, cursor=first.next_cursor, limit=100, **filters
        )
        assert [n.key for n in continued.entities] == ["b"]
        for changed in (
            {**filters, "q": "acme"},
            {**filters, "entity_class": "Person"},
            {**filters, "source": "other"},
            {**filters, "assurance": "confirmed"},
        ):
            with pytest.raises(InvalidDiscoveryCursor):
                await graph.discover(
                    principal=_PRINCIPAL, cursor=first.next_cursor, limit=1, **changed
                )
        with pytest.raises(InvalidDiscoveryCursor):
            await graph.discover(principal=_PRINCIPAL, cursor="not+urlsafe", limit=1)

    asyncio.run(scenario())


def test_discovery_fails_closed_before_returning_an_oversize_cursor() -> None:
    store = InMemoryGraphStore()

    async def scenario() -> None:
        await store.upsert_nodes(
            [
                make_node("boundary", "Document", ("visible",), display_name="x" * 700),
                make_node("lookahead", "Document", ("visible",), display_name="z" * 700),
            ]
        )
        with pytest.raises(GraphStoreError, match="cursor exceeded its wire cap"):
            await _graph(store).discover(principal=_PRINCIPAL, limit=1)

    asyncio.run(scenario())


class _BrokenOrderStore:
    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.rows = rows

    async def cypher(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> Sequence[Mapping[str, Any]]:
        return self.rows


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"node": make_node("b", "Account", ("visible",), display_name="Zulu")},
            {"node": make_node("a", "Account", ("visible",), display_name="Alpha")},
        ],
        [
            {"node": make_node("a", "Account", ("visible",), display_name="Alpha")},
            {"node": make_node("a", "Account", ("visible",), display_name="Alpha")},
        ],
    ],
)
def test_discovery_fails_closed_on_unsorted_or_duplicate_backend_rows(
    rows: Sequence[Mapping[str, Any]],
) -> None:
    async def scenario() -> None:
        with pytest.raises(GraphStoreError, match="order did not advance"):
            await _graph(_BrokenOrderStore(rows)).discover(principal=_PRINCIPAL, limit=2)

    asyncio.run(scenario())


def test_discovery_fails_closed_when_a_later_raw_page_does_not_advance() -> None:
    repeated = [
        {"node": make_node("a", "Account", ("other",), display_name="Alpha")},
        {"node": make_node("b", "Account", ("other",), display_name="Beta")},
    ]

    async def scenario() -> None:
        with pytest.raises(GraphStoreError, match="order did not advance"):
            await _graph(_BrokenOrderStore(repeated)).discover(principal=_PRINCIPAL, limit=1)

    asyncio.run(scenario())


# --- facets: the served Class/Source vocabulary (NE-GFACET) -----------------


def test_facets_fold_only_visible_entities_and_report_completeness() -> None:
    """Present-and-visible appears; present-but-invisible does not; complete
    scans say ``truncated is False`` (so the flag is not a constant)."""
    store = InMemoryGraphStore()

    async def scenario() -> None:
        await store.upsert_nodes(
            [
                make_node("k1", "Customer", ("visible",), source_system="m365-sharepoint"),
                make_node("k2", "Document", ("visible",), source_system="m365-sharepoint"),
                make_node("k3", "Ticket", ("hidden",), source_system="zendesk"),
            ]
        )
        facets = await _graph(store).facets(principal=_PRINCIPAL)
        assert facets.entity_classes == ("Customer", "Document")
        assert facets.source_systems == ("m365-sharepoint",)
        assert facets.truncated is False

    asyncio.run(scenario())


def test_facets_report_truncated_when_the_visible_cap_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is honest, not decorative: a capped scan says so."""
    store = InMemoryGraphStore()
    monkeypatch.setattr("ne.brain_graph.read.FACET_SCAN_CAP", 2)
    monkeypatch.setattr("ne.brain_graph.read.FACET_SCAN_BATCH", 2)

    async def scenario() -> None:
        await store.upsert_nodes(
            [make_node(f"k{i}", "Customer", ("visible",), display_name=f"n{i}") for i in range(5)]
        )
        facets = await _graph(store).facets(principal=_PRINCIPAL)
        assert facets.truncated is True
        assert facets.entity_classes == ("Customer",)

    asyncio.run(scenario())


class _DegradedSources:
    def __init__(self, degraded: frozenset[str]) -> None:
        self._degraded = degraded

    async def __call__(self) -> frozenset[str]:
        return self._degraded


def test_facets_spend_a_raw_row_budget_rather_than_scanning_a_whole_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A principal whose rows are nearly all trimmed AFTER the store returns them
    must not walk the whole graph looking for a vocabulary. The raw-row budget
    stops the walk, and the short result is reported as incomplete rather than
    as "this graph is empty"."""
    store = InMemoryGraphStore()
    monkeypatch.setattr("ne.brain_graph.read.FACET_SCAN_CAP", 4)
    monkeypatch.setattr("ne.brain_graph.read.FACET_SCAN_BATCH", 2)

    async def scenario() -> None:
        # 20 rows the store hands back but source health trims adapter-side,
        # then the one row that would actually contribute a facet.
        await store.upsert_nodes(
            [
                make_node(
                    f"d{i:02d}",
                    "Ticket",
                    ("visible",),
                    display_name=f"a{i:02d}",
                    source_system="degraded",
                )
                for i in range(20)
            ]
            + [
                make_node(
                    "v1", "Customer", ("visible",), display_name="zzz", source_system="healthy"
                )
            ]
        )
        graph = _graph(store, health=_DegradedSources(frozenset({"degraded"})))
        store.cypher_calls = 0
        facets = await graph.facets(principal=_PRINCIPAL)
        # Budget 4 raw rows at 2 per page: the scan stopped two pages in, long
        # before the 21st row, so the Customer was never reached.
        assert facets.entity_classes == ()
        assert facets.source_systems == ()
        # ...and that is stated as incomplete, never as an empty graph.
        assert facets.truncated is True
        assert store.cypher_calls == 3

    asyncio.run(scenario())
