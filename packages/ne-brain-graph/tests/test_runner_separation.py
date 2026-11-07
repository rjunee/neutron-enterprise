# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The tap-starvation proof (tier2-spec §4.2).

A T1 ``SyncRunner`` content-hash-dedups unchanged records, so a mapper/writer tap
on it sees ZERO records on an already-synced deployment — it would build an EMPTY
graph. The SEPARATE ``GraphSyncRunner``, with its own ``graph:`` state namespace
and own ledger, fully populates the graph from the SAME connector.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from ne.brain_graph.construct.ledger import GraphSyncLedger
from ne.brain_graph.construct.runner import GraphSyncRunner
from ne.brain_graph.testing.fixtures import (
    FixtureConnector,
    make_ledger_engine,
    salesforce_contract,
    salesforce_mapping,
)
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.connect.bridge import SyncRunner
from ne.connect.state import ConnectorStateStore
from ne.contracts.knowledge import KnowledgeItem, UpsertReceipt


class RecordingWriter:
    """A minimal ``KnowledgeWrite`` that records how many items it ever sees."""

    def __init__(self) -> None:
        self.total_items = 0

    async def upsert(self, items: Sequence[KnowledgeItem]) -> UpsertReceipt:
        self.total_items += len(list(items))
        return UpsertReceipt(accepted=tuple(i.item_id for i in items), quarantined=())

    async def curate(
        self, item_id: str, patch: object, *, provenance: str
    ) -> None:  # pragma: no cover
        return None

    async def quarantine(self, item_id: str, reason: str) -> None:  # pragma: no cover
        return None


def test_t1_tap_starves_while_graph_runner_populates() -> None:
    connector = FixtureConnector()
    contract = salesforce_contract()
    t1_state = ConnectorStateStore(":memory:")
    writer = RecordingWriter()

    t1 = SyncRunner(connector, contract, writer, t1_state, actor="t1")

    # First T1 backfill: the writer (a would-be tap) sees all 4 records.
    asyncio.run(t1.run(mode="backfill"))
    assert writer.total_items == 4

    # A SECOND T1 run: content-hash dedup starves the tap — zero deliveries.
    writer.total_items = 0
    asyncio.run(t1.run(mode="backfill"))
    assert writer.total_items == 0, "T1 dedup should starve a tap on an already-synced source"

    # The SEPARATE GraphSyncRunner (own graph: namespace + own ledger) populates
    # the graph fully from the SAME connector — proving it is not a starved tap.
    store = InMemoryGraphStore()
    graph_state = ConnectorStateStore(":memory:")
    graph_runner = GraphSyncRunner(
        connector,
        contract,
        store,
        GraphSyncLedger(make_ledger_engine()),
        graph_state,
        salesforce_mapping(),
        actor="graph",
        config_hash="cfg",
        run_id="g1",
    )
    report = asyncio.run(graph_runner.run(mode="backfill"))
    assert report.upserted == 4
    assert store.node_count() == 4
    assert store.edge_count() == 5

    t1_state.close()
    graph_state.close()


def test_graph_namespace_is_separate_from_t1() -> None:
    # Even sharing ONE ConnectorStateStore, the graph: namespace is independent,
    # so the graph runner is never gated by T1's dedup entries.
    connector = FixtureConnector()
    contract = salesforce_contract()
    shared_state = ConnectorStateStore(":memory:")

    writer = RecordingWriter()
    t1 = SyncRunner(connector, contract, writer, shared_state, actor="t1")
    asyncio.run(t1.run(mode="backfill"))

    store = InMemoryGraphStore()
    graph_runner = GraphSyncRunner(
        connector,
        contract,
        store,
        GraphSyncLedger(make_ledger_engine()),
        shared_state,
        salesforce_mapping(),
        actor="graph",
        config_hash="cfg",
        run_id="g1",
    )
    report = asyncio.run(graph_runner.run(mode="backfill"))
    assert report.upserted == 4
    assert store.node_count() == 4
    shared_state.close()
