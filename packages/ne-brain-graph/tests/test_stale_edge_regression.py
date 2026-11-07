# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""DECISION-2 stale-edge battery: the vacuous-delete fix.

The NE-081 stale-edge delete only fired on the UPSERT path, but with an FK target
excluded from ``content_hash`` an FK-only change was classified UNCHANGED, so the
delete was VACUOUS. NE-082 folds each edge's ``(edge_kind, target_key)`` into
``content_hash`` (mappers.py), so a changed/removed FK registers as a content
change → the 3-way UPSERT path → ``delete_edges_from_source`` fires. This battery
pins:

(a) changed-FK re-sync → the old neighbor edge is GONE and the new one present;
(b) FK removed → zero outgoing sync edges of that source_system from the node;
(c) a manually-upserted SAME_AS edge (source_system='ne:er') SURVIVES a full
    connector re-sync of its endpoint (the source_system filter protects it);
(d) map_record content_hash DIFFERS on an FK-only change (attributes identical);
(e) a stale parked (pending) edge is purged when the FK that parked it changes.
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa

from ne.brain_graph.construct.ledger import GraphSyncLedger
from ne.brain_graph.construct.mappers import map_record
from ne.brain_graph.construct.runner import GraphSyncRunner
from ne.brain_graph.model import edge_key_for, entity_key_for
from ne.brain_graph.testing.fixtures import (
    SALESFORCE,
    FixtureConnector,
    make_ledger_engine,
    salesforce_contract,
    salesforce_mapping,
)
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.connect.identity import source_snapshot_hash
from ne.connect.state import ConnectorStateStore
from ne.contracts.integration import Record

_ACCOUNT_A1 = Record(record_id="A1", source_id="Account", payload={"Id": "A1", "Name": "Acme"})
_ACCOUNT_A2 = Record(record_id="A2", source_id="Account", payload={"Id": "A2", "Name": "Beta"})
_ACCOUNT_A99 = Record(record_id="A99", source_id="Account", payload={"Id": "A99", "Name": "Late"})


def _contact(account_id: str | None, *, name: str = "Alice") -> Record:
    payload: dict[str, object] = {
        "Id": "C1",
        "FirstName": name,
        "LastName": "Ng",
        "Email": "alice@acme.com",
    }
    if account_id is not None:
        payload["AccountId"] = account_id
    return Record(record_id="C1", source_id="Contact", payload=payload)


def _person_key() -> str:
    return entity_key_for(SALESFORCE, "Person", "Contact", "C1")


def _account_key(record_id: str) -> str:
    return entity_key_for(SALESFORCE, "Account", "Account", record_id)


def _works_at_key(account_record_id: str) -> str:
    target = _account_key(account_record_id)
    return edge_key_for("WORKS_AT", _person_key(), target, "Contact:C1")


class _Deps:
    def __init__(self) -> None:
        self.store = InMemoryGraphStore()
        self.ledger = GraphSyncLedger(make_ledger_engine())
        self.state = ConnectorStateStore(":memory:")

    def run(self, records: tuple[Record, ...], *, run_id: str, mode: str = "backfill") -> None:
        runner = GraphSyncRunner(
            FixtureConnector(records=records),
            salesforce_contract(),
            self.store,
            self.ledger,
            self.state,
            salesforce_mapping(),
            actor="tester",
            config_hash="cfg",
            run_id=run_id,
        )
        asyncio.run(runner.run(mode=mode))  # type: ignore[arg-type]

    def outgoing_salesforce_edges(self) -> list[dict]:
        pk = _person_key()
        return [
            e
            for e in self.store._edges.values()
            if e.get("from_key") == pk and e.get("source_system") == SALESFORCE
        ]


def test_a_changed_fk_deletes_old_edge_and_adds_new() -> None:
    deps = _Deps()
    deps.run((_ACCOUNT_A1, _ACCOUNT_A2, _contact("A1")), run_id="r1")
    assert deps.store.raw_edge(_works_at_key("A1")) is not None

    deps.run((_ACCOUNT_A1, _ACCOUNT_A2, _contact("A2")), run_id="r2")
    # Old neighbor edge GONE; new one PRESENT.
    assert deps.store.raw_edge(_works_at_key("A1")) is None
    assert deps.store.raw_edge(_works_at_key("A2")) is not None
    edges = deps.outgoing_salesforce_edges()
    assert [e["to_key"] for e in edges] == [_account_key("A2")]


def test_b_removed_fk_leaves_zero_outgoing_edges() -> None:
    deps = _Deps()
    deps.run((_ACCOUNT_A1, _contact("A1")), run_id="r1")
    assert deps.outgoing_salesforce_edges()

    deps.run((_ACCOUNT_A1, _contact(None)), run_id="r2")
    assert deps.outgoing_salesforce_edges() == []
    assert deps.store.raw_edge(_works_at_key("A1")) is None


def test_c_ne_er_same_as_edge_survives_connector_resync() -> None:
    deps = _Deps()
    deps.run((_ACCOUNT_A1, _ACCOUNT_A2, _contact("A1")), run_id="r1")

    # A steward-applied SAME_AS edge out of the Person node, source_system 'ne:er'.
    same_as_key = edge_key_for("SAME_AS", _person_key(), _account_key("A2"), "er:proposal-1")
    same_as_edge = {
        "key": same_as_key,
        "edge_kind": "SAME_AS",
        "from_key": _person_key(),
        "to_key": _account_key("A2"),
        "source_system": "ne:er",
        "source_ref": "er:proposal-1",
        "acl": ("tenant",),
        "config_hash": "cfg",
        "synced_at": "2026-07-09T00:00:00+00:00",
    }
    asyncio.run(deps.store.upsert_edges([same_as_edge]))

    # A full connector re-sync that CHANGES the Person (forces the upsert path).
    deps.run((_ACCOUNT_A1, _ACCOUNT_A2, _contact("A1", name="Alicia")), run_id="r2")

    # The connector delete filters on source_system, so the 'ne:er' edge survives.
    survivor = deps.store.raw_edge(same_as_key)
    assert survivor is not None
    assert survivor["source_system"] == "ne:er"


def test_d_content_hash_differs_on_fk_only_change() -> None:
    mapping = salesforce_mapping().resolve("Contact")
    assert mapping is not None
    common = {
        "source_system": SALESFORCE,
        "acl": ("tenant",),
        "config_hash": "cfg",
        "schema_version": "1.0.0",
    }
    a1 = map_record(_contact("A1"), mapping, **common)
    a2 = map_record(_contact("A2"), mapping, **common)
    # Attributes are byte-identical; ONLY the FK differs.
    assert a1.node_props["given_name"] == a2.node_props["given_name"]  # type: ignore[union-attr]
    assert a1.content_hash != a2.content_hash  # type: ignore[union-attr]


def test_e_stale_pending_edge_purged_on_fk_change() -> None:
    deps = _Deps()
    # AccountId=A99 is never synced -> the WORKS_AT edge parks as pending.
    deps.run((_contact("A99"),), run_id="r1")
    parked = deps.ledger.take_pending_edges(SALESFORCE)
    assert any(t == _account_key("A99") for _, _, t in parked)
    with_origin = deps.ledger.take_pending_edges_with_origin(SALESFORCE)
    assert len(with_origin) == 1
    assert with_origin[0][3]["source_snapshot_hash"] == source_snapshot_hash(
        SALESFORCE, _contact("A99")
    )

    # Re-sync C1 pointing at A1 (which now exists) -> the A99 parked edge is purged.
    deps.run((_ACCOUNT_A1, _contact("A1")), run_id="r2")
    remaining = deps.ledger.take_pending_edges(SALESFORCE)
    assert all(t != _account_key("A99") for _, _, t in remaining)
    assert deps.store.raw_edge(_works_at_key("A1")) is not None


def test_f_legacy_pending_table_preserves_work_and_forces_exact_source_replay() -> None:
    engine = make_ledger_engine()
    original = GraphSyncLedger(engine)
    original._pending.drop(engine)  # exact legacy-schema fixture
    legacy = sa.MetaData()
    legacy_pending = sa.Table(
        "ne_graph_pending_edges",
        legacy,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source_system", sa.String, nullable=False),
        sa.Column("key", sa.String, nullable=False),
        sa.Column("target_key", sa.String, nullable=False),
        sa.Column("props", sa.JSON, nullable=False),
        sa.Column("first_seen_run_id", sa.String, nullable=False),
    )
    legacy.create_all(engine)
    props = {
        "key": _works_at_key("A99"),
        "edge_kind": "WORKS_AT",
        "from_key": _person_key(),
        "to_key": _account_key("A99"),
        "source_system": SALESFORCE,
        "source_ref": "Contact:C1",
        "acl": ["tenant"],
        "config_hash": "cfg",
    }
    with engine.begin() as conn:
        conn.execute(
            original._ledger.insert().values(  # seeded legacy state
                source_system=SALESFORCE,
                entity_class="Person",
                key=_person_key(),
                content_hash="legacy-content",
                acl_hash="legacy-acl",
                status="upserted",
                last_run_id="r0",
                reason=None,
                updated_at="2026-08-01T00:00:00+00:00",
            )
        )
        conn.execute(
            legacy_pending.insert().values(
                source_system=SALESFORCE,
                key=_works_at_key("A99"),
                target_key=_account_key("A99"),
                props=props,
                first_seen_run_id="r0",
            )
        )

    ledger = GraphSyncLedger(engine)
    columns = {c["name"] for c in sa.inspect(engine).get_columns("ne_graph_pending_edges")}
    assert "from_key" in columns
    assert [target for _, _, target in ledger.take_pending_edges(SALESFORCE)] == [
        _account_key("A99")
    ]

    store = InMemoryGraphStore()
    state = ConnectorStateStore(":memory:")

    def run(records: tuple[Record, ...], run_id: str) -> None:
        asyncio.run(
            GraphSyncRunner(
                FixtureConnector(records=records),
                salesforce_contract(),
                store,
                ledger,
                state,
                salesforce_mapping(),
                actor="tester",
                config_hash="cfg",
                run_id=run_id,
            ).run(mode="incremental")
        )

    run((_contact("A99"),), "r1")
    exact = ledger.take_pending_edges_with_origin(SALESFORCE)
    assert len(exact) == 1
    assert exact[0][3]["source_snapshot_hash"] == source_snapshot_hash(SALESFORCE, _contact("A99"))
    run((_ACCOUNT_A99,), "r2")
    assert ledger.take_pending_edges(SALESFORCE) == []
    assert store.raw_edge(_works_at_key("A99")) is not None
    state.close()


def test_g_pending_edge_survives_retry_store_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    deps = _Deps()
    deps.run((_contact("A1"),), run_id="r1", mode="incremental")
    assert deps.ledger.take_pending_edges(SALESFORCE)
    original = deps.store.upsert_edges_confirmed

    async def fail(_edges: object) -> tuple[str, ...]:
        raise RuntimeError("graph store unavailable")

    monkeypatch.setattr(deps.store, "upsert_edges_confirmed", fail)
    with pytest.raises(RuntimeError, match="graph store unavailable"):
        deps.run((_ACCOUNT_A1,), run_id="r2", mode="incremental")
    assert deps.ledger.take_pending_edges(SALESFORCE)

    monkeypatch.setattr(deps.store, "upsert_edges_confirmed", original)
    deps.run((_ACCOUNT_A1,), run_id="r3", mode="incremental")
    assert deps.ledger.take_pending_edges(SALESFORCE) == []
    assert deps.store.raw_edge(_works_at_key("A1")) is not None


def test_h_prune_removes_incident_pending_before_missing_subject_can_reappear() -> None:
    deps = _Deps()
    deps.run((_contact("A99"),), run_id="r1")
    assert deps.ledger.take_pending_edges(SALESFORCE)

    deps.run((), run_id="r2")
    assert deps.ledger.take_pending_edges(SALESFORCE) == []
    assert deps.store.raw_node(_person_key()) is None

    deps.run((_ACCOUNT_A99,), run_id="r3")
    assert deps.store.raw_edge(_works_at_key("A99")) is None
    assert deps.ledger.edge_evidence_for_nodes(SALESFORCE, [_account_key("A99")]) == []


def test_i_detach_pruned_target_reappearance_repairs_unchanged_subject_edge() -> None:
    deps = _Deps()
    deps.run((_ACCOUNT_A1, _contact("A1")), run_id="r1")
    assert deps.store.raw_edge(_works_at_key("A1")) is not None

    deps.run((_contact("A1"),), run_id="r2")
    assert deps.store.raw_node(_account_key("A1")) is None
    assert deps.store.raw_edge(_works_at_key("A1")) is None
    assert deps.ledger.take_pending_edges(SALESFORCE)

    deps.run((_ACCOUNT_A1, _contact("A1")), run_id="r3")
    assert deps.store.raw_edge(_works_at_key("A1")) is not None
    assert deps.ledger.take_pending_edges(SALESFORCE) == []
    edges = deps.ledger.edge_evidence_for_nodes(SALESFORCE, [_person_key()])
    assert [row["key"] for row in edges] == [_works_at_key("A1")]


def test_j_stale_delete_failure_keeps_projection_dirty_until_exact_intent_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = _Deps()
    deps.run((_ACCOUNT_A1, _ACCOUNT_A2, _contact("A1")), run_id="r1")
    before = deps.ledger.node_evidence(SALESFORCE, _person_key())
    assert before is not None
    original = deps.store.delete_edges_from_source
    failed = False

    async def fail_after_delete(
        source_system: str, from_key: str, keep_keys: tuple[str, ...]
    ) -> None:
        nonlocal failed
        await original(source_system, from_key, keep_keys)
        if from_key == _person_key() and not failed:
            failed = True
            raise RuntimeError("crash after stale delete")

    monkeypatch.setattr(deps.store, "delete_edges_from_source", fail_after_delete)
    with pytest.raises(RuntimeError, match="crash after stale delete"):
        deps.run((_ACCOUNT_A1, _ACCOUNT_A2, _contact("A2")), run_id="r2")
    assert deps.ledger.pending_mutation_count() == 1
    evidence_keys = {
        str(row["key"]) for row in deps.ledger.edge_evidence_for_nodes(SALESFORCE, [_person_key()])
    }
    assert _works_at_key("A2") not in evidence_keys
    after = deps.ledger.node_evidence(SALESFORCE, _person_key())
    assert after is not None
    assert after["source_snapshot_hash"] == before["source_snapshot_hash"]

    monkeypatch.setattr(deps.store, "delete_edges_from_source", original)
    deps.run((_ACCOUNT_A1, _ACCOUNT_A2, _contact("A2")), run_id="r3")
    assert deps.ledger.pending_mutation_count() == 0
    assert deps.store.raw_edge(_works_at_key("A1")) is None
    assert deps.store.raw_edge(_works_at_key("A2")) is not None


def test_k_pending_retry_refuses_missing_subject_evidence() -> None:
    deps = _Deps()
    deps.run((_contact("A99"),), run_id="r1", mode="incremental")
    assert deps.ledger.take_pending_edges(SALESFORCE)
    with deps.ledger._engine.begin() as conn:  # exact corrupted-ledger fixture
        conn.execute(
            deps.ledger._evidence_nodes.delete().where(  # exact endpoint-evidence loss
                deps.ledger._evidence_nodes.c.source_system == SALESFORCE,
                deps.ledger._evidence_nodes.c.key == _person_key(),
            )
        )
        conn.execute(
            deps.ledger._ledger.delete().where(
                deps.ledger._ledger.c.source_system == SALESFORCE,
                deps.ledger._ledger.c.key == _person_key(),
            )
        )

    deps.run((_ACCOUNT_A99,), run_id="r2", mode="incremental")
    assert deps.store.raw_edge(_works_at_key("A99")) is None
    assert deps.ledger.take_pending_edges(SALESFORCE)
