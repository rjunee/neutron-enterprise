# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T-CON-1: construction correctness over the recorded salesforce fixtures.

Exact nodes/edges/ACLs/keys/display_name; a dangling FK becomes a pending edge
(never a placeholder node); an invalid payload is quarantined and the batch
continues.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from ne.brain.lineage import GraphEvidenceBatch, GraphEvidenceBindResult
from ne.brain_graph.config import GraphEdgeMapping, GraphEntityMapping, GraphSourceMapping
from ne.brain_graph.construct.ledger import GraphSyncLedger
from ne.brain_graph.construct.mappers import MappedEntity, map_record
from ne.brain_graph.construct.runner import GraphSyncReport, GraphSyncRunner, M365GraphSyncHost
from ne.brain_graph.errors import GraphStoreError
from ne.brain_graph.testing.fixtures import (
    FixtureConnector,
    expected_key,
    make_ledger_engine,
    salesforce_contract,
    salesforce_mapping,
)
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.contract import m365_admitted_target_digest
from ne.connect.state import ConnectorStateStore
from ne.contracts.integration import Record
from ne.contracts.source_authority import SourceAuthorityStamp

_CONFIG_HASH = "a" * 64


class _AuditSink:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.events: dict[str, Any] = {}

    async def append(self, event: Any) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("audit unavailable")
        self.events[event.event_id] = event

    async def verify(self, **_kwargs: Any) -> Any:  # pragma: no cover - Protocol completeness
        raise NotImplementedError


class _Recorder:
    def __init__(self, *, fail_once: bool = False, unbound_once: bool = False) -> None:
        self.fail_once = fail_once
        self.unbound_once = unbound_once
        self.batches: list[GraphEvidenceBatch] = []

    def bind_graph_evidence(self, batch: GraphEvidenceBatch) -> GraphEvidenceBindResult:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("lineage unavailable")
        self.batches.append(batch)
        if self.unbound_once:
            self.unbound_once = False
            return GraphEvidenceBindResult(0, 0, len(batch.entities), len(batch.relationships))
        return GraphEvidenceBindResult(len(batch.entities), len(batch.relationships), 0, 0)


class _SourceAuthority:
    async def stamps_for(
        self, record: Record, *, acl: tuple[str, ...]
    ) -> tuple[SourceAuthorityStamp, ...]:
        del record, acl
        return ()


class _CrashAfterFirstNodeWrite:
    """Delegate one durable node write, then simulate process loss."""

    def __init__(self, inner: InMemoryGraphStore) -> None:
        self.inner = inner
        self.crashed = False

    async def upsert_nodes(self, nodes: Sequence[Mapping[str, Any]]) -> None:
        await self.inner.upsert_nodes(nodes)
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("crash after graph write")

    async def upsert_edges(self, edges: Sequence[Mapping[str, Any]]) -> None:
        await self.inner.upsert_edges(edges)

    async def upsert_edges_confirmed(self, edges: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        return await self.inner.upsert_edges_confirmed(edges)

    async def update_acl(self, keys: Sequence[str], acl: Sequence[str]) -> None:
        await self.inner.update_acl(keys, acl)

    async def delete_where_stale(self, source_system: str, keys: Sequence[str]) -> None:
        await self.inner.delete_where_stale(source_system, keys)

    async def delete_edges_from_source(
        self, source_system: str, from_key: str, keep_keys: Sequence[str]
    ) -> None:
        await self.inner.delete_edges_from_source(source_system, from_key, keep_keys)


class _FailFirstEdgeWrite(_CrashAfterFirstNodeWrite):
    def __init__(self, inner: InMemoryGraphStore) -> None:
        super().__init__(inner)
        self.crashed = True
        self.failed_edge = False

    async def upsert_edges(self, edges: Sequence[Mapping[str, Any]]) -> None:
        if not self.failed_edge:
            self.failed_edge = True
            raise RuntimeError("edge write unavailable")
        await self.inner.upsert_edges(edges)

    async def upsert_edges_confirmed(self, edges: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        if not self.failed_edge:
            self.failed_edge = True
            raise RuntimeError("edge write unavailable")
        return await self.inner.upsert_edges_confirmed(edges)


class _SilentSuccessEdgeWrite(_CrashAfterFirstNodeWrite):
    def __init__(self, inner: InMemoryGraphStore, *, ack_mode: str = "missing") -> None:
        super().__init__(inner)
        self.crashed = True
        self.ack_mode = ack_mode

    async def upsert_edges(self, edges: Sequence[Mapping[str, Any]]) -> None:
        del edges
        return None

    async def upsert_edges_confirmed(self, edges: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        expected = tuple(str(edge["key"]) for edge in edges)
        if self.ack_mode == "partial":
            return expected[:-1]
        if self.ack_mode == "duplicate":
            return (*expected[:-1], expected[0])
        if self.ack_mode == "extra":
            return (*expected, "unexpected")
        if self.ack_mode == "wrong-set":
            return tuple(f"wrong-{index}" for index, _key in enumerate(expected))
        return ()


def _run(connector: FixtureConnector, store: InMemoryGraphStore, mode: str = "backfill"):
    ledger = GraphSyncLedger(make_ledger_engine())
    state = ConnectorStateStore(":memory:")
    runner = GraphSyncRunner(
        connector,
        salesforce_contract(),
        store,
        ledger,
        state,
        salesforce_mapping(),
        actor="tester",
        config_hash="cfg-1",
        run_id="run-1",
    )
    report = asyncio.run(runner.run(mode=mode))  # type: ignore[arg-type]
    state.close()
    return report, ledger


def test_exact_nodes_and_edges() -> None:
    store = InMemoryGraphStore()
    report, _ = _run(FixtureConnector(), store)

    assert report.seen == 4
    assert report.upserted == 4
    assert report.unmapped == 0
    assert report.quarantined == 0
    assert store.node_count() == 4
    assert store.edge_count() == 5

    a1 = expected_key("Account", "Account", "A1")
    c1 = expected_key("Person", "Contact", "C1")
    node = store.raw_node(a1)
    assert node is not None
    assert node["display_name"] == "Acme"
    assert node["entity_class"] == "Account"
    assert node["acl"] == ["tenant"]
    assert node["source_ref"] == "Account:A1"
    assert node["synced_at"]  # stamped on write

    person = store.raw_node(c1)
    assert person is not None
    assert person["display_name"] == "Alice Ng"

    kinds = {
        e["edge_kind"] for e in (store.raw_edge(k) for k in _edge_keys(store)) if e is not None
    }
    assert kinds == {"WORKS_AT", "BELONGS_TO", "REQUESTER", "ACCOUNT", "PLACED_BY"}


def _edge_keys(store: InMemoryGraphStore) -> list[str]:
    # Access the internal edge map for assertion convenience.
    return list(store._edges)


def test_edge_acl_is_asserting_record_acl() -> None:
    store = InMemoryGraphStore()
    _run(FixtureConnector(), store)
    for key in _edge_keys(store):
        edge = store.raw_edge(key)
        assert edge is not None
        assert edge["acl"] == ["tenant"]


def test_dangling_fk_becomes_pending_never_placeholder() -> None:
    # A lone Contact whose AccountId points at a never-synced Account.
    ghost_contact = Record(
        record_id="C9",
        source_id="Contact",
        payload={
            "Id": "C9",
            "FirstName": "Bob",
            "LastName": "Lone",
            "Email": "bob@ghost.test",
            "AccountId": "GHOST",
        },
    )
    store = InMemoryGraphStore()
    report, _ = _run(FixtureConnector(records=(ghost_contact,)), store)
    assert report.upserted == 1
    assert store.node_count() == 1  # only the contact — no placeholder Account
    assert store.edge_count() == 0
    assert report.pending_edges == 1


def test_invalid_payload_quarantined_batch_continues() -> None:
    bad_account = Record(
        record_id="A2",
        source_id="Account",
        payload={"Id": "A2"},  # missing required Name -> ValidationError
    )
    good_account = Record(
        record_id="A3",
        source_id="Account",
        payload={"Id": "A3", "Name": "Good Co", "Status": "active"},
    )
    store = InMemoryGraphStore()
    report, _ = _run(FixtureConnector(records=(bad_account, good_account)), store)
    assert report.quarantined == 1
    assert report.upserted == 1
    assert store.node_count() == 1
    assert store.raw_node(expected_key("Account", "Account", "A3")) is not None


def test_unmapped_record_skipped() -> None:
    stray = Record(record_id="X1", source_id="Unknown", payload={"Id": "X1"})
    store = InMemoryGraphStore()
    report, _ = _run(FixtureConnector(records=(stray,)), store)
    assert report.unmapped == 1
    assert report.upserted == 0
    assert store.node_count() == 0


def test_audit_failure_leaves_recoverable_prepared_mutation_intent() -> None:
    store = InMemoryGraphStore()
    ledger = GraphSyncLedger(make_ledger_engine())
    state = ConnectorStateStore(":memory:")
    sink = _AuditSink(fail_once=True)
    recorder = _Recorder()

    def runner(run_id: str) -> GraphSyncRunner:
        return GraphSyncRunner(
            FixtureConnector(),
            salesforce_contract(),
            store,
            ledger,
            state,
            salesforce_mapping(),
            actor="tester",
            audit_sink=sink,
            config_hash=_CONFIG_HASH,
            run_id=run_id,
            evidence_recorder=recorder,
        )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        asyncio.run(runner("run-1").run(mode="backfill"))
    assert ledger.pending_mutation_count() == 1
    assert ledger.pending_evidence_count() == 0
    assert store.node_count() == 0
    assert recorder.batches == []

    report = asyncio.run(runner("run-2").run(mode="backfill"))
    assert report.evidence_pending == 0
    assert len(recorder.batches) == 1
    completion_events = {
        event_id for event_id, event in sink.events.items() if event.action == "graph.sync"
    }
    assert {batch.audit_event_id for batch in recorder.batches} == completion_events
    state.close()


def test_crash_after_first_graph_write_recovers_prepared_intent_before_receipt() -> None:
    graph = InMemoryGraphStore()
    crashing_store = _CrashAfterFirstNodeWrite(graph)
    ledger = GraphSyncLedger(make_ledger_engine())
    state = ConnectorStateStore(":memory:")
    sink = _AuditSink()
    recorder = _Recorder()

    def runner(run_id: str) -> GraphSyncRunner:
        return GraphSyncRunner(
            FixtureConnector(),
            salesforce_contract(),
            crashing_store,
            ledger,
            state,
            salesforce_mapping(),
            actor="tester",
            audit_sink=sink,
            config_hash=_CONFIG_HASH,
            run_id=run_id,
            evidence_recorder=recorder,
        )

    with pytest.raises(RuntimeError, match="crash after graph write"):
        asyncio.run(runner("run-crash").run(mode="backfill"))
    assert graph.node_count() > 0
    assert ledger.pending_mutation_count() == 1
    assert ledger.node_evidence("salesforce", expected_key("Account", "Account", "A1")) is None
    assert state.load_state("graph:salesforce") == {}
    assert recorder.batches == []

    recovered = asyncio.run(runner("run-recovery").run(mode="backfill"))
    assert recovered.evidence_pending == 0
    assert ledger.pending_mutation_count() == 0
    assert graph.node_count() == 4
    assert graph.edge_count() == 5
    assert len(recorder.batches) == 1
    assert len(recorder.batches[0].relationships) == 5
    state.close()


def test_edge_failure_cannot_clean_ledger_or_emit_inferred_relationship_receipt() -> None:
    graph = InMemoryGraphStore()
    failing_store = _FailFirstEdgeWrite(graph)
    ledger = GraphSyncLedger(make_ledger_engine())
    state = ConnectorStateStore(":memory:")
    sink = _AuditSink()
    recorder = _Recorder()

    def runner(run_id: str) -> GraphSyncRunner:
        return GraphSyncRunner(
            FixtureConnector(),
            salesforce_contract(),
            failing_store,
            ledger,
            state,
            salesforce_mapping(),
            actor="tester",
            audit_sink=sink,
            config_hash=_CONFIG_HASH,
            run_id=run_id,
            evidence_recorder=recorder,
        )

    with pytest.raises(RuntimeError, match="edge write unavailable"):
        asyncio.run(runner("run-edge-fail").run(mode="backfill"))
    assert graph.node_count() == 4
    assert graph.edge_count() == 0
    assert recorder.batches == []
    assert ledger.pending_mutation_count() == 1
    assert ledger.node_evidence("salesforce", expected_key("Account", "Account", "A1")) is None
    assert (
        ledger.edge_evidence_for_nodes("salesforce", [expected_key("Account", "Account", "A1")])
        == []
    )
    assert state.load_state("graph:salesforce") == {}

    recovered = asyncio.run(runner("run-edge-recover").run(mode="backfill"))
    assert recovered.unchanged == 4
    assert graph.edge_count() == 5
    assert len(recorder.batches) == 1
    assert len(recorder.batches[0].relationships) == 5
    state.close()


def test_silent_edge_write_without_exact_ack_cannot_emit_relationship_receipt() -> None:
    graph = InMemoryGraphStore()
    silent_store = _SilentSuccessEdgeWrite(graph)
    ledger = GraphSyncLedger(make_ledger_engine())
    state = ConnectorStateStore(":memory:")
    recorder = _Recorder()
    runner = GraphSyncRunner(
        FixtureConnector(),
        salesforce_contract(),
        silent_store,
        ledger,
        state,
        salesforce_mapping(),
        actor="tester",
        audit_sink=_AuditSink(),
        config_hash=_CONFIG_HASH,
        run_id="silent-edge-write",
        evidence_recorder=recorder,
    )

    with pytest.raises(GraphStoreError, match="exact persisted edge acknowledgment"):
        asyncio.run(runner.run(mode="backfill"))
    assert graph.node_count() == 4
    assert graph.edge_count() == 0
    assert recorder.batches == []
    assert ledger.pending_mutation_count() == 1
    assert (
        ledger.edge_evidence_for_nodes("salesforce", [expected_key("Account", "Account", "A1")])
        == []
    )
    state.close()


@pytest.mark.parametrize(
    "ack_mode",
    ("partial", "duplicate", "extra", "wrong-set"),
)
def test_inexact_edge_ack_cannot_complete_intent_or_emit_receipt(ack_mode: str) -> None:
    graph = InMemoryGraphStore()
    inexact_store = _SilentSuccessEdgeWrite(graph, ack_mode=ack_mode)
    ledger = GraphSyncLedger(make_ledger_engine())
    state = ConnectorStateStore(":memory:")
    recorder = _Recorder()
    runner = GraphSyncRunner(
        FixtureConnector(),
        salesforce_contract(),
        inexact_store,
        ledger,
        state,
        salesforce_mapping(),
        actor="tester",
        audit_sink=_AuditSink(),
        config_hash=_CONFIG_HASH,
        run_id=f"inexact-edge-write-{ack_mode}",
        evidence_recorder=recorder,
    )

    with pytest.raises(GraphStoreError, match="exact persisted edge acknowledgment"):
        asyncio.run(runner.run(mode="backfill"))
    assert graph.edge_count() == 0
    assert ledger.pending_mutation_count() == 1
    assert recorder.batches == []
    state.close()


def test_lineage_failure_keeps_audited_outbox_without_failing_graph_sync() -> None:
    store = InMemoryGraphStore()
    ledger = GraphSyncLedger(make_ledger_engine())
    state = ConnectorStateStore(":memory:")
    sink = _AuditSink()
    recorder = _Recorder(fail_once=True)

    def runner(run_id: str) -> GraphSyncRunner:
        return GraphSyncRunner(
            FixtureConnector(),
            salesforce_contract(),
            store,
            ledger,
            state,
            salesforce_mapping(),
            actor="tester",
            audit_sink=sink,
            config_hash=_CONFIG_HASH,
            run_id=run_id,
            evidence_recorder=recorder,
        )

    first = asyncio.run(runner("run-1").run(mode="backfill"))
    assert first.evidence_pending == 1
    assert len([event for event in sink.events.values() if event.action == "graph.sync"]) == 1
    second = asyncio.run(runner("run-2").run(mode="backfill"))
    assert second.evidence_pending == 0
    assert len(recorder.batches) == 2
    state.close()


def test_unbound_snapshot_receipt_waits_for_later_t1_binding() -> None:
    store = InMemoryGraphStore()
    ledger = GraphSyncLedger(make_ledger_engine())
    state = ConnectorStateStore(":memory:")
    sink = _AuditSink()
    recorder = _Recorder(unbound_once=True)

    def runner(run_id: str) -> GraphSyncRunner:
        return GraphSyncRunner(
            FixtureConnector(),
            salesforce_contract(),
            store,
            ledger,
            state,
            salesforce_mapping(),
            actor="tester",
            audit_sink=sink,
            config_hash=_CONFIG_HASH,
            run_id=run_id,
            evidence_recorder=recorder,
        )

    first = asyncio.run(runner("run-1").run(mode="backfill"))
    assert first.evidence_pending == 1
    second = asyncio.run(runner("run-2").run(mode="backfill"))
    assert second.evidence_pending == 0
    assert recorder.batches[0].audit_event_id == recorder.batches[1].audit_event_id
    state.close()


def test_m365_host_owns_graph_state_and_mapping_revision_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[dict[str, str], str]] = []

    class _Connector:
        acl_quarantined = 0
        acl_fetch_failed = 0
        acl_quarantined_ids: tuple[str, ...] = ()

        def __init__(self, settings: Any, **kwargs: Any) -> None:
            self._settings = settings
            observed.append((dict(kwargs["delta_state"]), "constructed"))
            self.pending_delta_links = {"drive-a": "graph-delta"}

        async def resolve_drive_ids(self) -> tuple[str, ...]:
            return tuple(self._settings.drive_ids)

        def _recrawl(self, basis: Mapping[str, str]) -> None:
            observed[-1] = (dict(basis), observed[-1][1])

    class _Runner:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def run(self, *, mode: str) -> GraphSyncReport:
            observed[-1] = (observed[-1][0], mode)
            return GraphSyncReport(
                mode=mode,
                seen=0,
                upserted=0,
                unchanged=0,
                acl_healed=0,
                quarantined=0,
                unmapped=0,
                pruned=0,
                pending_edges=0,
            )

    monkeypatch.setattr("ne.brain_graph.construct.runner.M365GraphConnector", _Connector)
    monkeypatch.setattr("ne.brain_graph.construct.runner.GraphSyncRunner", _Runner)
    state = ConnectorStateStore(":memory:")
    state.set_cursor("m365-sharepoint", "delta:drive-a", "t1-delta")
    mapping = GraphSourceMapping(
        source_system="m365-sharepoint",
        entities=(
            GraphEntityMapping(
                source_id_prefix="drive-a",
                entity_class="Document",
                fields={
                    "id": "id",
                    "title": "name",
                    "doc_type": "graph_doc_type",
                },
            ),
        ),
    )
    host = M365GraphSyncHost(
        store=InMemoryGraphStore(),
        ledger=GraphSyncLedger(make_ledger_engine()),
        mappings=(mapping,),
        evidence_recorder=_Recorder(),
        source_authority_provider=_SourceAuthority(),
        audit_sink=_AuditSink(),
        config_hash=_CONFIG_HASH,
    )
    settings = M365ConnectorSettings(
        tenant_id="tenant",
        client_id="client",
        auth_method="secret",
        client_secret="test-only",  # type: ignore[arg-type]
        drive_ids=("drive-a",),
        owner="owner@example.test",
    )
    state.claim_target_binding(
        "graph:m365-sharepoint",
        m365_admitted_target_digest(settings),
    )
    state.set_cursor("graph:m365-sharepoint", "delta:drive-a", "stale-graph-delta")

    asyncio.run(host.run(settings, state_store=state, actor="tester"))
    asyncio.run(host.run(settings, state_store=state, actor="tester"))
    config_changed_host = M365GraphSyncHost(
        store=InMemoryGraphStore(),
        ledger=GraphSyncLedger(make_ledger_engine()),
        mappings=(mapping,),
        evidence_recorder=_Recorder(),
        source_authority_provider=_SourceAuthority(),
        audit_sink=_AuditSink(),
        config_hash="b" * 64,
    )
    asyncio.run(config_changed_host.run(settings, state_store=state, actor="tester"))

    assert observed == [
        ({}, "backfill"),
        ({"drive-a": "graph-delta"}, "incremental"),
        ({}, "backfill"),
    ]
    assert state.load_state("m365-sharepoint") == {"delta:drive-a": "t1-delta"}
    assert state.load_state("graph:m365-sharepoint") == {"delta:drive-a": "graph-delta"}
    state.close()


def test_m365_host_preflight_requires_exact_canonical_drive_mappings() -> None:
    def entity(prefix: str, **overrides: Any) -> GraphEntityMapping:
        values: dict[str, Any] = {
            "source_id_prefix": prefix,
            "entity_class": "Document",
            "fields": {
                "id": "id",
                "title": "name",
                "doc_type": "graph_doc_type",
            },
        }
        values.update(overrides)
        return GraphEntityMapping(**values)

    def host(*entities: GraphEntityMapping) -> M365GraphSyncHost:
        return M365GraphSyncHost(
            store=InMemoryGraphStore(),
            ledger=GraphSyncLedger(make_ledger_engine()),
            mappings=(GraphSourceMapping(source_system="m365-sharepoint", entities=entities),),
            evidence_recorder=_Recorder(),
            source_authority_provider=_SourceAuthority(),
            audit_sink=_AuditSink(),
            config_hash=_CONFIG_HASH,
        )

    def settings(
        *, drive_ids: tuple[str, ...] = ("drive-a",), site_ids: tuple[str, ...] = ()
    ) -> M365ConnectorSettings:
        return M365ConnectorSettings(
            tenant_id="tenant",
            client_id="client",
            auth_method="secret",
            client_secret="test-only",  # type: ignore[arg-type]
            drive_ids=drive_ids,
            site_ids=site_ids,
            owner="owner@example.test",
        )

    valid = host(entity("drive-a"), entity("drive-b"))
    mapping = valid.mapping_for(
        settings(drive_ids=("drive-a", "drive-b")),
        resolved_drive_ids=("drive-a", "drive-b"),
    )
    assert [item.source_id_prefix for item in mapping.entities] == ["drive-a", "drive-b"]
    assert all(
        item.fields == {"id": "id", "title": "name", "doc_type": "graph_doc_type"}
        for item in mapping.entities
    )
    mapped = map_record(
        Record(
            record_id="item-1",
            source_id="drive-a:item-1",
            payload={
                "id": "item-1",
                "name": "Policy.docx",
                "graph_doc_type": "other",
                "web_url": "https://contoso.example/Policy.docx",
                "last_modified": "2026-08-22T00:00:00Z",
                "mime_type": (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ),
                "source_record_id": "provider-selected-id",
            },
        ),
        mapping.entities[0],
        source_system="m365-sharepoint",
        acl=("g-risk",),
        config_hash=_CONFIG_HASH,
        schema_version="test-schema",
    )
    assert isinstance(mapped, MappedEntity)
    assert {key: mapped.node_props[key] for key in ("id", "title", "doc_type")} == {
        "id": "item-1",
        "title": "Policy.docx",
        "doc_type": "other",
    }
    assert not {
        "uri",
        "created_at",
        "updated_at",
        "mime_type",
        "source_record_id",
    }.intersection(mapped.node_props)
    assert mapped.edges == ()

    duplicate_source_mapping = GraphSourceMapping(
        source_system="m365-sharepoint", entities=(entity("drive-a"),)
    )
    duplicate_source_host = M365GraphSyncHost(
        store=InMemoryGraphStore(),
        ledger=GraphSyncLedger(make_ledger_engine()),
        mappings=(duplicate_source_mapping, duplicate_source_mapping),
        evidence_recorder=_Recorder(),
        source_authority_provider=_SourceAuthority(),
        audit_sink=_AuditSink(),
        config_hash=_CONFIG_HASH,
    )
    invalid = (
        (
            host(entity("drive-a")),
            settings(drive_ids=(), site_ids=("site-a",)),
            (),
            "resolved no drives",
        ),
        (
            host(entity("drive-a")),
            settings(drive_ids=("drive-a", "drive-b")),
            ("drive-a", "drive-b"),
            "exact resolved drive_id prefix coverage",
        ),
        (
            host(entity("drive-a"), entity("drive-b")),
            settings(),
            ("drive-a",),
            "exact resolved drive_id prefix coverage",
        ),
        (
            host(entity("drive-a")),
            settings(drive_ids=("drive-a", "drive-a")),
            ("drive-a",),
            "unique explicit drive_ids",
        ),
        (
            host(entity("drive-a")),
            settings(drive_ids=(), site_ids=("site-a",)),
            ("drive-a", "drive-a"),
            "unique resolved drive_ids",
        ),
        (
            host(entity("drive-a", fields={"title": "name"})),
            settings(),
            ("drive-a",),
            "canonical Document mapping",
        ),
        (
            host(entity("drive-a", entity_class="Account")),
            settings(),
            ("drive-a",),
            "canonical Document mapping",
        ),
        (
            host(
                entity(
                    "drive-a",
                    edges=(
                        GraphEdgeMapping(
                            payload_key="author_id",
                            edge_kind="AUTHOR",
                            target_class="Person",
                        ),
                    ),
                )
            ),
            settings(),
            ("drive-a",),
            "canonical Document mapping",
        ),
        (
            duplicate_source_host,
            settings(),
            ("drive-a",),
            "exactly one non-empty graph mapping",
        ),
    )
    for invalid_host, invalid_settings, resolved, expected in invalid:
        with pytest.raises(ValueError, match=expected):
            invalid_host.mapping_for(invalid_settings, resolved_drive_ids=resolved)


def test_m365_host_claims_exact_graph_target_before_connector_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed = 0

    class _Connector:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            nonlocal constructed
            constructed += 1

    monkeypatch.setattr("ne.brain_graph.construct.runner.M365GraphConnector", _Connector)
    mapping = GraphSourceMapping(
        source_system="m365-sharepoint",
        entities=(
            GraphEntityMapping(
                source_id_prefix="drive-a",
                entity_class="Document",
                fields={"id": "id", "title": "name", "doc_type": "graph_doc_type"},
            ),
        ),
    )
    host = M365GraphSyncHost(
        store=InMemoryGraphStore(),
        ledger=GraphSyncLedger(make_ledger_engine()),
        mappings=(mapping,),
        evidence_recorder=_Recorder(),
        source_authority_provider=_SourceAuthority(),
        audit_sink=_AuditSink(),
        config_hash=_CONFIG_HASH,
    )
    settings = M365ConnectorSettings(
        tenant_id="tenant",
        client_id="client",
        auth_method="secret",
        client_secret="test-only",  # type: ignore[arg-type]
        drive_ids=("drive-a",),
        owner="owner@example.test",
    )

    mismatched = ConnectorStateStore(":memory:")
    mismatched.claim_target_binding("graph:m365-sharepoint", "wrong-target")
    with pytest.raises(RuntimeError, match="does not match its binding"):
        asyncio.run(host.run(settings, state_store=mismatched, actor="tester"))
    mismatched.close()

    legacy = ConnectorStateStore(":memory:")
    legacy.set_cursor("graph:m365-sharepoint", "delta:drive-a", "legacy")
    with pytest.raises(RuntimeError, match="non-empty and has no target binding"):
        asyncio.run(host.run(settings, state_store=legacy, actor="tester"))
    legacy.close()
    assert constructed == 0


def test_site_discovered_drive_change_changes_revision_and_forces_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive_sets = [("drive-a",), ("drive-a", "drive-b")]
    observed: list[tuple[dict[str, str], str]] = []

    class _Connector:
        acl_quarantined = 0
        acl_fetch_failed = 0
        acl_quarantined_ids: tuple[str, ...] = ()

        def __init__(self, _settings: Any, **_kwargs: Any) -> None:
            self.pending_delta_links = {"drive-a": "graph-delta"}

        async def resolve_drive_ids(self) -> tuple[str, ...]:
            return drive_sets.pop(0)

        def _recrawl(self, basis: Mapping[str, str]) -> None:
            observed.append((dict(basis), "prepared"))

    class _Runner:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def run(self, *, mode: str) -> GraphSyncReport:
            observed[-1] = (observed[-1][0], mode)
            return GraphSyncReport(
                mode=mode,
                seen=0,
                upserted=0,
                unchanged=0,
                acl_healed=0,
                quarantined=0,
                unmapped=0,
                pruned=0,
                pending_edges=0,
            )

    monkeypatch.setattr("ne.brain_graph.construct.runner.M365GraphConnector", _Connector)
    monkeypatch.setattr("ne.brain_graph.construct.runner.GraphSyncRunner", _Runner)

    def mapping(*drive_ids: str) -> GraphSourceMapping:
        return GraphSourceMapping(
            source_system="m365-sharepoint",
            entities=tuple(
                GraphEntityMapping(
                    source_id_prefix=drive_id,
                    entity_class="Document",
                    fields={"id": "id", "title": "name", "doc_type": "graph_doc_type"},
                )
                for drive_id in drive_ids
            ),
        )

    def host(source_mapping: GraphSourceMapping) -> M365GraphSyncHost:
        return M365GraphSyncHost(
            store=InMemoryGraphStore(),
            ledger=GraphSyncLedger(make_ledger_engine()),
            mappings=(source_mapping,),
            evidence_recorder=_Recorder(),
            source_authority_provider=_SourceAuthority(),
            audit_sink=_AuditSink(),
            config_hash=_CONFIG_HASH,
        )

    first_mapping = mapping("drive-a")
    revision_host = host(first_mapping)
    assert revision_host._mapping_revision(first_mapping, ("drive-a",)) != (
        revision_host._mapping_revision(first_mapping, ("drive-a", "drive-b"))
    )

    settings = M365ConnectorSettings(
        tenant_id="tenant",
        client_id="client",
        auth_method="secret",
        client_secret="test-only",  # type: ignore[arg-type]
        site_ids=("site-a",),
        owner="owner@example.test",
    )
    state = ConnectorStateStore(":memory:")
    asyncio.run(host(first_mapping).run(settings, state_store=state, actor="tester"))
    asyncio.run(
        host(mapping("drive-a", "drive-b")).run(
            settings,
            state_store=state,
            actor="tester",
        )
    )
    state.close()
    assert observed == [({}, "backfill"), ({}, "backfill")]
