# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-164b: read-path auditing on ``EntityGraph``'s four public reads.

Mirrors the shipped T1 read-path audit discipline (NE-164's
``ne.brain.retriever.HybridRetriever._emit_read_audit``): every completed public
read (``lookup``/``neighbors``/``subgraph``/``paths``), when an ``AuditSink`` is
injected, emits exactly ONE config_hash-stamped ``AuditEvent`` carrying the query
SHAPE only (node/edge/hop counts + the adapter-side ACL-trim delta) — never an
ACL label value, entity key, display name, or cypher text (C7).

Uses the REAL trimming seam over the reference ``InMemoryGraphStore`` (which
trims fully server-side, so its adapter-side delta is always 0) PLUS a tiny local
``_PostFilterStubStore`` returning hand-built canned rows regardless of query —
modeling a store whose probed rung is ``POST_FILTER`` so EntityGraph's own
adapter-side post-filter does the trimming, to exercise a non-zero delta.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from ne.brain_graph.model import EntityKey
from ne.brain_graph.read import EntityGraph
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts import ConfigHashNotFrozenError, freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.contracts.governance import AuditVerification
from ne.contracts.types import Principal


class _DemoSettings(NeSettings):
    demo: int = 7


@pytest.fixture
def frozen_config() -> Any:
    """Freeze a deterministic config_hash for the duration of a test."""
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


class RecordingAuditSink:
    """Test-double ``AuditSink`` that records every appended event verbatim."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, *, start: Any = None, end: Any = None) -> AuditVerification:
        raise NotImplementedError


class _FailingAuditSink:
    """An ``AuditSink`` whose ``append`` always raises (fail-closed test double)."""

    async def append(self, event: Any) -> None:
        raise RuntimeError("audit store unreachable")

    async def verify(self, *, start: Any = None, end: Any = None) -> AuditVerification:
        raise NotImplementedError


class _StaticExpander:
    """Real ``GroupExpander`` returning a fixed label set — the real trimming seam."""

    def expand(self, principal: Principal | str) -> frozenset[str]:
        return frozenset({"u1", "g-eng"})


principal = Principal(id="u1", groups=("g-eng",))


def _seed_pair(store: InMemoryGraphStore) -> None:
    """Anchor 'k1' + one visible neighbour 'k2', both g-eng."""

    async def setup() -> None:
        await store.upsert_nodes(
            [
                make_node("k1", "Account", ("g-eng",)),
                make_node("k2", "Account", ("g-eng",)),
            ]
        )
        await store.upsert_edges([make_edge("LINK", "k1", "k2", ("g-eng",), key="k1_k2")])

    asyncio.run(setup())


def _node_prop(key: str, acl: tuple[str, ...]) -> dict[str, Any]:
    return {
        "key": key,
        "entity_class": "Account",
        "display_name": key,
        "source_system": "fixture",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "1.0.0",
        "synced_at": "2026-07-09T00:00:00+00:00",
    }


class _PostFilterStubStore:
    """A ``GraphReadStore`` returning HAND-BUILT canned rows regardless of query.

    Models a POST_FILTER-rung store: the ACL predicate is not honored server-side,
    so EntityGraph's adapter-side post-filter (``_node_visible``/
    ``_path_all_visible``) does all the trimming — used to exercise a non-zero
    ``acl_trim_delta``.
    """

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = list(rows)

    async def cypher(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> Sequence[Mapping[str, Any]]:
        return self._rows


def test_neighbors_emits_graph_read_audit_with_counts_and_trace_id(frozen_config: str) -> None:
    store = InMemoryGraphStore()
    _seed_pair(store)
    sink = RecordingAuditSink()
    graph = EntityGraph(store, group_expander=_StaticExpander(), audit_sink=sink)
    trace_id = "a" * 32

    async def scenario() -> None:
        sub = await graph.neighbors(
            [EntityKey(entity_class="Account", key="k1")],
            principal=principal,
            hops=1,
            trace_id=trace_id,
        )
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.action == "knowledge.graph_read"
        assert event.resource == "knowledge://graph"
        assert event.outcome == "success"
        assert event.actor == "u1"
        assert event.detail == (
            f"op=neighbors nodes={len(sub.nodes)} edges={len(sub.edges)} hops=1 acl_trim_delta=0"
        )
        assert event.trace_id == trace_id
        assert event.config_hash == frozen_config

    asyncio.run(scenario())


def test_lookup_subgraph_and_paths_each_emit_one_event(frozen_config: str) -> None:
    store = InMemoryGraphStore()
    _seed_pair(store)
    sink = RecordingAuditSink()
    graph = EntityGraph(store, group_expander=_StaticExpander(), audit_sink=sink)

    async def scenario() -> None:
        await graph.lookup([EntityKey(entity_class="Account", key="k1")], principal=principal)
        await graph.subgraph([EntityKey(entity_class="Account", key="k1")], principal=principal)
        await graph.paths(
            EntityKey(entity_class="Account", key="k1"),
            EntityKey(entity_class="Account", key="k2"),
            principal=principal,
        )
        assert len(sink.events) == 3
        assert sink.events[0].detail.startswith("op=lookup ")
        assert sink.events[1].detail.startswith("op=subgraph ")
        assert sink.events[2].detail.startswith("op=paths ")

    asyncio.run(scenario())


def test_adapter_side_trim_increments_delta_and_leaks_no_label(frozen_config: str) -> None:
    visible_row = {"path_nodes": [_node_prop("v1", ("g-eng",))], "path_rels": []}
    invisible_row = {"path_nodes": [_node_prop("v2", ("g-secret",))], "path_rels": []}
    store = _PostFilterStubStore([visible_row, invisible_row])
    sink = RecordingAuditSink()
    graph = EntityGraph(store, group_expander=_StaticExpander(), audit_sink=sink)

    async def scenario() -> None:
        records = await graph.paths(
            EntityKey(entity_class="Account", key="v1"),
            EntityKey(entity_class="Account", key="v2"),
            principal=principal,
        )
        assert len(records) == 1  # only the visible row survives
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.detail == "op=paths nodes=1 edges=0 hops=3 acl_trim_delta=1"

        for evt in sink.events:
            payload = evt.model_dump_json()
            assert "g-eng" not in payload
            assert "g-secret" not in payload

    asyncio.run(scenario())


def test_event_payload_carries_no_keys_or_cypher(frozen_config: str) -> None:
    store = InMemoryGraphStore()
    _seed_pair(store)
    sink = RecordingAuditSink()
    graph = EntityGraph(store, group_expander=_StaticExpander(), audit_sink=sink)

    async def scenario() -> None:
        await graph.neighbors([EntityKey(entity_class="Account", key="k1")], principal=principal)
        payload = sink.events[-1].model_dump_json()
        for leaked in ("k1", "k2", "MATCH"):
            assert leaked not in payload

    asyncio.run(scenario())


def test_no_audit_sink_composition_emits_nothing_and_behaves_identically() -> None:
    store = InMemoryGraphStore()
    _seed_pair(store)
    graph = EntityGraph(store, group_expander=_StaticExpander(), audit_sink=None)

    async def scenario() -> None:
        anchor = [EntityKey(entity_class="Account", key="k1")]
        sub = await graph.neighbors(anchor, principal=principal)
        assert {n.key for n in sub.nodes} == {"k1", "k2"}

    asyncio.run(scenario())


def test_audit_append_failure_fails_closed_no_result(frozen_config: str) -> None:
    store = InMemoryGraphStore()
    _seed_pair(store)
    graph = EntityGraph(store, group_expander=_StaticExpander(), audit_sink=_FailingAuditSink())

    async def scenario() -> None:
        anchor = [EntityKey(entity_class="Account", key="k1")]
        with pytest.raises(RuntimeError):
            await graph.neighbors(anchor, principal=principal)

    asyncio.run(scenario())


def test_extracted_elements_read_emits_identical_event_shape_and_no_leak(
    frozen_config: str,
) -> None:
    """NE-T2X-3: the ``knowledge.graph_read`` event grammar is UNCHANGED over
    ``ne:llm`` reads (C10/C12 re-verified) — no ``assurance``/``extracted``/
    ``ne:llm``/``evidence`` token, and no entity key, enters the event."""
    store = InMemoryGraphStore()
    n1 = make_node(
        "llm1",
        "Account",
        ("g-eng",),
        source_system="ne:llm",
        assurance="extracted",
        evidence=["sha256:p1|c1"],
    )
    n2 = make_node(
        "llm2",
        "Account",
        ("g-eng",),
        source_system="ne:llm",
        assurance="extracted",
        evidence=["sha256:p2|c2"],
    )

    async def setup() -> None:
        await store.upsert_nodes([n1, n2])
        await store.upsert_edges([make_edge("ABOUT", "llm1", "llm2", ("g-eng",), key="llm1_llm2")])

    asyncio.run(setup())
    sink = RecordingAuditSink()
    graph = EntityGraph(store, group_expander=_StaticExpander(), audit_sink=sink)

    async def scenario() -> None:
        sub = await graph.neighbors(
            [EntityKey(entity_class="Account", key="llm1")], principal=principal, hops=1
        )
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.action == "knowledge.graph_read"
        assert event.detail == (
            f"op=neighbors nodes={len(sub.nodes)} edges={len(sub.edges)} hops=1 acl_trim_delta=0"
        )
        payload = event.model_dump_json()
        for leaked in ("assurance", "extracted", "ne:llm", "evidence", "llm1", "llm2", "MATCH"):
            assert leaked not in payload

    asyncio.run(scenario())


def test_unfrozen_config_hash_fails_loud() -> None:
    store = InMemoryGraphStore()
    _seed_pair(store)
    sink = RecordingAuditSink()
    graph = EntityGraph(store, group_expander=_StaticExpander(), audit_sink=sink)

    async def scenario() -> None:
        anchor = [EntityKey(entity_class="Account", key="k1")]
        with pytest.raises(ConfigHashNotFrozenError):
            await graph.neighbors(anchor, principal=principal)
        assert sink.events == []

    asyncio.run(scenario())
