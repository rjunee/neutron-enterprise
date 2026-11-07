# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T-CON-2: idempotent re-sync, ACL-heal, and backfill prune ordering.

* a second identical run performs ZERO store writes (recording store) and reports
  all-unchanged;
* a changed row is a single property overwrite;
* a narrowed ACL on unchanged attributes takes the update_acl path on the node
  AND its asserted edges (no property rewrite — spine.py:77-82);
* a backfill prunes source-deleted entities (upsert -> delete -> ledger order);
* an incremental never prunes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from ne.brain_graph.config import GraphSourceMapping
from ne.brain_graph.construct.ledger import GraphSyncLedger
from ne.brain_graph.construct.runner import GraphSyncRunner
from ne.brain_graph.testing.fixtures import (
    DEFAULT_RECORDS,
    FixtureConnector,
    expected_key,
    make_ledger_engine,
    salesforce_contract,
    salesforce_mapping,
)
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.connect.state import ConnectorStateStore
from ne.contracts.integration import Record


class RecordingStore:
    """Delegates to an in-memory store while counting write calls."""

    def __init__(self) -> None:
        self.inner = InMemoryGraphStore()
        self.upsert_node_calls = 0
        self.upsert_edge_calls = 0
        self.update_acl_calls: list[list[str]] = []
        self.delete_calls: list[list[str]] = []
        self.delete_edges_from_source_calls: list[tuple[str, list[str]]] = []

    async def upsert_nodes(self, nodes: Sequence[Mapping[str, Any]]) -> None:
        self.upsert_node_calls += 1
        await self.inner.upsert_nodes(nodes)

    async def upsert_edges(self, edges: Sequence[Mapping[str, Any]]) -> None:
        self.upsert_edge_calls += 1
        await self.inner.upsert_edges(edges)

    async def upsert_edges_confirmed(self, edges: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        self.upsert_edge_calls += 1
        return await self.inner.upsert_edges_confirmed(edges)

    async def update_acl(self, keys: Sequence[str], acl: Sequence[str]) -> None:
        self.update_acl_calls.append(list(keys))
        await self.inner.update_acl(keys, acl)

    async def delete_where_stale(self, source_system: str, keys: Sequence[str]) -> None:
        self.delete_calls.append(list(keys))
        await self.inner.delete_where_stale(source_system, keys)

    async def delete_edges_from_source(
        self, source_system: str, from_key: str, keep_keys: Sequence[str]
    ) -> None:
        self.delete_edges_from_source_calls.append((from_key, list(keep_keys)))
        await self.inner.delete_edges_from_source(source_system, from_key, keep_keys)


class Deps:
    def __init__(self) -> None:
        self.store = RecordingStore()
        self.ledger = GraphSyncLedger(make_ledger_engine())
        self.state = ConnectorStateStore(":memory:")

    def run(
        self,
        records: Sequence[Record],
        *,
        mode: str = "backfill",
        run_id: str,
        default_acl: tuple[str, ...] = ("tenant",),
        mapping: GraphSourceMapping | None = None,
        config_hash: str = "cfg",
    ):
        runner = GraphSyncRunner(
            FixtureConnector(records=records),
            salesforce_contract(default_acl=default_acl),
            self.store,
            self.ledger,
            self.state,
            mapping or salesforce_mapping(),
            actor="tester",
            config_hash=config_hash,
            run_id=run_id,
        )
        return asyncio.run(runner.run(mode=mode))  # type: ignore[arg-type]


def test_second_identical_run_is_zero_write() -> None:
    deps = Deps()
    deps.run(DEFAULT_RECORDS, run_id="r1")
    deps.store.upsert_node_calls = 0
    deps.store.upsert_edge_calls = 0
    report = deps.run(DEFAULT_RECORDS, run_id="r2")
    assert report.upserted == 0
    assert report.unchanged == 4
    assert deps.store.upsert_node_calls == 0
    assert deps.store.upsert_edge_calls == 0
    assert deps.store.update_acl_calls == []


def test_changed_row_single_overwrite() -> None:
    deps = Deps()
    deps.run(DEFAULT_RECORDS, run_id="r1")
    deps.store.upsert_node_calls = 0
    changed = Record(
        record_id="A1",
        source_id="Account",
        payload={"Id": "A1", "Name": "Acme Renamed", "Status": "churned"},
    )
    records = (changed, *DEFAULT_RECORDS[1:])
    report = deps.run(records, run_id="r2")
    assert report.upserted == 1
    assert report.unchanged == 3
    node = deps.store.inner.raw_node(expected_key("Account", "Account", "A1"))
    assert node is not None
    assert node["display_name"] == "Acme Renamed"


def test_acl_heal_takes_update_acl_on_node_and_edges() -> None:
    deps = Deps()
    deps.run(DEFAULT_RECORDS, run_id="r1")
    deps.store.upsert_node_calls = 0
    deps.store.upsert_edge_calls = 0
    # Identical attributes, narrowed ACL -> ACL_STALE path.
    report = deps.run(DEFAULT_RECORDS, run_id="r2", default_acl=("narrow",))
    assert report.acl_healed == 4
    assert report.upserted == 0
    assert deps.store.upsert_node_calls == 0  # no property rewrite
    # The Contact (Person) heal must touch its node AND its WORKS_AT edge key.
    c1 = expected_key("Person", "Contact", "C1")
    healed_targets = [keys for keys in deps.store.update_acl_calls if c1 in keys]
    assert healed_targets, "Person node key not in any update_acl call"
    assert any(len(keys) >= 2 for keys in healed_targets), "edge keys not healed with the node"
    node = deps.store.inner.raw_node(c1)
    assert node is not None
    assert node["acl"] == ("narrow",)


def test_backfill_prunes_source_deleted_in_order() -> None:
    deps = Deps()
    deps.run(DEFAULT_RECORDS, run_id="r1")
    # Order O1 deleted at source: a backfill re-run must prune it.
    remaining = DEFAULT_RECORDS[:-1]
    report = deps.run(remaining, run_id="r2", mode="backfill")
    assert report.pruned == 1
    assert deps.store.delete_calls  # delete happened
    deleted_key = expected_key("Order", "Order", "O1")
    assert deps.store.inner.raw_node(deleted_key) is None
    assert deps.ledger.node_evidence("salesforce", deleted_key) is None


def test_incremental_never_prunes() -> None:
    deps = Deps()
    deps.run(DEFAULT_RECORDS, run_id="r1")
    remaining = DEFAULT_RECORDS[:-1]
    report = deps.run(remaining, run_id="r2", mode="incremental")
    assert report.pruned == 0
    assert deps.store.inner.raw_node(expected_key("Order", "Order", "O1")) is not None


def test_mapping_schema_and_platform_config_revisions_force_real_projection_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = Deps()
    record = Record(
        record_id="A1",
        source_id="Account",
        payload={
            "Id": "A1",
            "Name": "Acme",
            "Alias": "Acme",
            "Status": "active",
            "Domain": "acme.com",
        },
    )
    mapping = salesforce_mapping()
    account, *others = mapping.entities
    alternate_account = account.model_copy(update={"fields": {**account.fields, "name": "Alias"}})
    alternate = mapping.model_copy(update={"entities": (alternate_account, *others)})

    first = deps.run((record,), run_id="r1", mapping=mapping, config_hash="a" * 64)
    first_revision = deps.store.inner.raw_node(expected_key("Account", "Account", "A1"))
    assert first.upserted == 1
    assert first_revision is not None

    mapped = deps.run((record,), run_id="r2", mapping=alternate, config_hash="a" * 64)
    mapped_node = deps.store.inner.raw_node(expected_key("Account", "Account", "A1"))
    assert mapped.upserted == 1
    assert mapped_node is not None
    assert mapped_node["display_name"] == "Acme"
    assert mapped_node["projection_revision"] != first_revision["projection_revision"]

    configured = deps.run((record,), run_id="r3", mapping=alternate, config_hash="b" * 64)
    configured_node = deps.store.inner.raw_node(expected_key("Account", "Account", "A1"))
    assert configured.upserted == 1
    assert configured_node is not None
    assert configured_node["config_hash"] == "b" * 64
    assert configured_node["projection_revision"] != mapped_node["projection_revision"]

    monkeypatch.setattr("ne.brain_graph.construct.runner.SCHEMA_VERSION", "schema-next")
    schema = deps.run((record,), run_id="r4", mapping=alternate, config_hash="b" * 64)
    schema_node = deps.store.inner.raw_node(expected_key("Account", "Account", "A1"))
    assert schema.upserted == 1
    assert schema_node is not None
    assert schema_node["schema_version"] == "schema-next"
    assert schema_node["projection_revision"] != configured_node["projection_revision"]
