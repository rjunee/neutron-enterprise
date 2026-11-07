# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T-CON-3: two from-empty runs over identical fixtures produce byte-identical
canonical exports (volatile ``synced_at`` excluded)."""

from __future__ import annotations

import asyncio
import json

from ne.brain_graph.construct.ledger import GraphSyncLedger
from ne.brain_graph.construct.runner import GraphSyncRunner
from ne.brain_graph.testing.fixtures import (
    FixtureConnector,
    make_ledger_engine,
    salesforce_contract,
    salesforce_mapping,
)
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.connect.state import ConnectorStateStore

_VOLATILE = {"synced_at"}


def _canonical_export(store: InMemoryGraphStore) -> str:
    def _clean(props: dict) -> dict:
        return {k: v for k, v in props.items() if k not in _VOLATILE}

    nodes = sorted(
        (_clean(props) for props in store._nodes.values()),
        key=lambda p: p["key"],
    )
    edges = sorted(
        (_clean(props) for props in store._edges.values()),
        key=lambda p: p["key"],
    )
    return json.dumps({"nodes": nodes, "edges": edges}, sort_keys=True, default=str)


def _fresh_run() -> InMemoryGraphStore:
    store = InMemoryGraphStore()
    state = ConnectorStateStore(":memory:")
    runner = GraphSyncRunner(
        FixtureConnector(),
        salesforce_contract(),
        store,
        GraphSyncLedger(make_ledger_engine()),
        state,
        salesforce_mapping(),
        actor="tester",
        config_hash="cfg",
        run_id="run",
    )
    asyncio.run(runner.run(mode="backfill"))
    state.close()
    return store


def test_two_from_empty_runs_are_byte_identical() -> None:
    first = _canonical_export(_fresh_run())
    second = _canonical_export(_fresh_run())
    assert first == second
