# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``GraphSyncRunner`` — a SEPARATE governed consumer of the Connector Protocol.

NOT a tap on the T1 ``SyncRunner``: T1 content-hash dedup skips unchanged records
(bridge.py:177-184), so a mapper/writer tap would starve into an EMPTY graph on an
already-synced deployment. This runner consumes ``Connector.backfill/incremental/
permissions`` directly with its OWN ``graph:`` state namespace and its OWN ledger,
so it can backfill independently.

It reuses ne-connect's public pieces verbatim: ``build_acl_index`` + ``AclMapper``
for fail-closed ACL resolution (the SAME resolver that stamps chunks — graph and
chunk visibility can never diverge at the source), and ``ConnectorStateStore``
(graph-namespaced). It copies the spine's 3-way unchanged/ACL-heal/full-upsert
semantics and the upsert-new -> delete-stale -> ledger-commit ordering
(spine.py:38-47), parks dangling-FK edges as pending (never placeholder nodes),
stamps ``synced_at`` only on actual writes (idempotent re-run = zero writes), and
emits exactly ONE config_hash-stamped ``graph.sync`` AuditEvent per run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from ne.brain.lineage import (
    GraphEvidenceBatch,
    GraphEvidenceBindResult,
    GraphEvidenceEntity,
    GraphEvidenceRelationship,
)
from ne.brain_graph.config import GraphSourceMapping
from ne.brain_graph.construct.ledger import GraphSyncLedger, LedgerDecision
from ne.brain_graph.construct.mappers import MappedEntity, QuarantinedRecord, map_record
from ne.brain_graph.construct.meridian import validate_meridian_m365_graph_mapping
from ne.brain_graph.errors import GraphStoreError
from ne.connect.acl import AclMapper, build_acl_index
from ne.connect.contracts import OdcsDataContract
from ne.connect.errors import AclResolutionError
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.connector import M365GraphConnector
from ne.connect.m365.contract import m365_admitted_target_digest, m365_data_contract
from ne.connect.state import ConnectorStateStore
from ne.contracts.governance import AuditSink
from ne.contracts.integration import Connector, Record
from ne.contracts.source_authority import SourceAuthorityStamp
from ne.events import new_audit_event
from ne.schema import SCHEMA_VERSION, AuditEvent, AuditOutcome

__all__ = [
    "GraphEvidenceRecorder",
    "GraphSyncReport",
    "GraphSyncRunner",
    "GraphWriteStore",
    "M365GraphSyncHost",
]


class GraphWriteStore(Protocol):
    """The narrow write surface the runner needs (frozen upserts + capabilities)."""

    async def upsert_nodes(self, nodes: Sequence[Mapping[str, Any]]) -> None: ...
    async def upsert_edges(self, edges: Sequence[Mapping[str, Any]]) -> None: ...
    async def upsert_edges_confirmed(
        self, edges: Sequence[Mapping[str, Any]]
    ) -> tuple[str, ...]: ...
    async def update_acl(self, keys: Sequence[str], acl: Sequence[str]) -> None: ...
    async def delete_where_stale(self, source_system: str, keys: Sequence[str]) -> None: ...
    async def delete_edges_from_source(
        self, source_system: str, from_key: str, keep_keys: Sequence[str]
    ) -> None: ...


class GraphEvidenceRecorder(Protocol):
    """The exact T1 lineage binding surface; implemented by the runtime store."""

    def bind_graph_evidence(self, batch: GraphEvidenceBatch) -> GraphEvidenceBindResult: ...


class GraphSourceAuthorityProvider(Protocol):
    """Resolve the independently admitted T2 stamp for one connector record."""

    async def stamps_for(
        self, record: Record, *, acl: tuple[str, ...]
    ) -> tuple[SourceAuthorityStamp, ...]: ...


class GraphSyncReport(BaseModel):
    """Immutable outcome of a single :meth:`GraphSyncRunner.run`."""

    model_config = ConfigDict(frozen=True)

    mode: str
    seen: int
    upserted: int
    unchanged: int
    acl_healed: int
    quarantined: int
    unmapped: int
    pruned: int
    pending_edges: int
    evidence_pending: int = 0


class _LedgerOperation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["upsert", "acl_heal", "touch"]
    entity_class: str
    key: str
    content_hash: str = ""
    acl_hash: str = ""


class _NodeEvidenceOperation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    source_ref: str
    source_snapshot_hash: str
    source_acl_hash: str
    entity_class: str
    display_name: str
    acl: tuple[str, ...]


class _StateMarkOperation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    content_hash: str


class _AclOperation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    keys: tuple[str, ...]
    acl: tuple[str, ...]


class _EdgeReconcileOperation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_key: str
    keep_keys: tuple[str, ...]


class _PendingEdgeOperation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    props: dict[str, Any]
    target_key: str
    origin_source_ref: str
    origin_snapshot_hash: str
    origin_entity: dict[str, Any]


class _EdgeEvidenceOperation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    edge_kind: str
    from_key: str
    to_key: str


class _MutationPlan(BaseModel):
    """Strict internal replay wire persisted before its first graph write."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    node_upserts: tuple[dict[str, Any], ...] = ()
    edge_upserts: tuple[dict[str, Any], ...] = ()
    acl_updates: tuple[_AclOperation, ...] = ()
    ledger_operations: tuple[_LedgerOperation, ...] = ()
    node_evidence: tuple[_NodeEvidenceOperation, ...] = ()
    edge_evidence: tuple[_EdgeEvidenceOperation, ...] = ()
    state_marks: tuple[_StateMarkOperation, ...] = ()
    edge_reconciles: tuple[_EdgeReconcileOperation, ...] = ()
    pending_adds: tuple[_PendingEdgeOperation, ...] = ()
    pending_deletes: tuple[int, ...] = ()
    prune_keys: tuple[str, ...] = ()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _audit_config_hash(value: str) -> str | None:
    if len(value) == 64 and all(char in "0123456789abcdef" for char in value):
        return value
    return hashlib.sha256(value.encode()).hexdigest()


class GraphSyncRunner:
    def __init__(
        self,
        connector: Connector,
        contract: OdcsDataContract,
        store: GraphWriteStore,
        ledger: GraphSyncLedger,
        state_store: ConnectorStateStore,
        mapping: GraphSourceMapping,
        *,
        actor: str,
        audit_sink: AuditSink | None = None,
        batch_size: int = 50,
        kpi_tag: str | None = None,
        config_hash: str = "",
        run_id: str | None = None,
        evidence_recorder: GraphEvidenceRecorder | None = None,
        source_authority_provider: GraphSourceAuthorityProvider | None = None,
    ) -> None:
        self._connector = connector
        self._contract = contract
        self._store = store
        self._ledger = ledger
        self._state_store = state_store
        self._mapping = mapping
        self._actor = actor
        self._audit_sink = audit_sink
        self._batch_size = batch_size
        self._kpi_tag = kpi_tag
        self._config_hash = config_hash
        self._run_id = run_id
        self._evidence_recorder = evidence_recorder
        self._source_authority_provider = source_authority_provider

    @property
    def _graph_source(self) -> str:
        """The graph-namespaced source key (separate from the T1 SyncRunner's)."""
        return f"graph:{self._contract.source}"

    async def run(self, *, mode: Literal["backfill", "incremental"]) -> GraphSyncReport:
        await self._recover_mutations()
        if self._evidence_recorder is not None:
            if self._audit_sink is None:
                raise RuntimeError("graph evidence recording requires an audit sink")
            await self._recover_evidence_audits()
            self._deliver_evidence()

        contract = self._contract
        source_system = contract.source
        run_id = self._run_id or _now()
        acl_mapper = AclMapper(contract.permissions)
        index = build_acl_index([entry async for entry in self._connector.permissions()])
        state = self._state_store.load_state(self._graph_source)

        stream = (
            self._connector.backfill(state or None)
            if mode == "backfill"
            else self._connector.incremental(state)
        )

        seen = upserted = unchanged = acl_healed = quarantined = unmapped = 0
        buffer: list[MappedEntity] = []
        mapped_seen: list[MappedEntity] = []

        async def flush() -> None:
            nonlocal upserted
            upserted += await self._flush(buffer, run_id)
            buffer.clear()

        async for record in stream:
            seen += 1
            entity_mapping = self._mapping.resolve(record.source_id)
            if entity_mapping is None:
                unmapped += 1
                continue

            try:
                acl = acl_mapper.resolve(record, index)
            except AclResolutionError as exc:
                await self._emit_audit(mode, AuditOutcome.denied, str(exc), run_id, {})
                raise

            authority_stamps: tuple[SourceAuthorityStamp, ...] = ()
            if self._source_authority_provider is not None:
                authority_stamps = await self._source_authority_provider.stamps_for(record, acl=acl)
                if not authority_stamps:
                    self._ledger.mark_quarantined(
                        source_system,
                        entity_mapping.entity_class,
                        _quarantine_key(record, entity_mapping.entity_class),
                        reason="current source authority is unavailable",
                        run_id=run_id,
                    )
                    quarantined += 1
                    continue

            mapped = map_record(
                record,
                entity_mapping,
                source_system=source_system,
                acl=acl,
                config_hash=self._config_hash,
                schema_version=SCHEMA_VERSION,
                source_authority_stamps=authority_stamps,
            )
            if isinstance(mapped, QuarantinedRecord):
                quarantined += 1
                self._ledger.mark_quarantined(
                    source_system,
                    entity_mapping.entity_class,
                    _quarantine_key(record, entity_mapping.entity_class),
                    reason=mapped.reason,
                    run_id=run_id,
                )
                continue

            mapped_seen.append(mapped)

            decision = self._ledger.check(
                source_system, mapped.entity_class, mapped.key, mapped.content_hash, mapped.acl_hash
            )
            if decision is LedgerDecision.UNCHANGED:
                unchanged += 1
                self._ledger.touch(source_system, mapped.entity_class, mapped.key, run_id=run_id)
                self._record_node_evidence(mapped, run_id)
                continue
            if decision is LedgerDecision.ACL_STALE:
                acl_healed += 1
                await self._heal_acl(mapped, run_id)
                continue

            buffer.append(mapped)
            if len(buffer) >= self._batch_size:
                await flush()

        if buffer:
            await flush()

        pruned = 0
        if mode == "backfill":
            pruned = await self._prune(source_system, run_id)
        await self._repair_edges(mapped_seen, run_id)
        await self._retry_pending(source_system, run_id)
        pending_remaining = self._ledger.pending_edge_count(source_system)

        report = GraphSyncReport(
            mode=mode,
            seen=seen,
            upserted=upserted,
            unchanged=unchanged,
            acl_healed=acl_healed,
            quarantined=quarantined,
            unmapped=unmapped,
            pruned=pruned,
            pending_edges=pending_remaining,
        )
        detail = (
            f"mode={mode} seen={seen} upserted={upserted} unchanged={unchanged} "
            f"acl_healed={acl_healed} quarantined={quarantined} unmapped={unmapped} "
            f"pruned={pruned} pending_edges={pending_remaining}"
        )
        if self._evidence_recorder is None:
            if self._audit_sink is not None:
                await self._emit_audit(mode, AuditOutcome.success, detail, run_id, {})
            return report

        event = self._audit_event(mode, AuditOutcome.success, detail, run_id)
        entities = tuple(self._entity_evidence(mapped) for mapped in mapped_seen)
        relationships_by_key = {
            relationship.key: relationship for relationship in self._relationships_for(mapped_seen)
        }
        batch = GraphEvidenceBatch(
            audit_event_id=event.event_id,
            occurred_at=event.occurred_at,
            entities=tuple(sorted(entities, key=lambda item: item.key)),
            relationships=tuple(sorted(relationships_by_key.values(), key=lambda item: item.key)),
        )
        outbox_id = self._ledger.enqueue_evidence(
            event.event_id,
            audit_event=event.model_dump(mode="json"),
            evidence=batch.to_payload(),
        )
        assert self._audit_sink is not None
        await self._audit_sink.append(event)
        self._ledger.mark_evidence_audited(outbox_id)
        self._deliver_evidence()
        return report.model_copy(update={"evidence_pending": self._ledger.pending_evidence_count()})

    async def _flush(self, buffer: Sequence[MappedEntity], run_id: str) -> int:
        """Prepare one exact node/edge/reconcile intent, then apply it."""
        if not buffer:
            return 0
        synced_at = _now()
        source_system = self._contract.source
        planned_nodes = {mapped.key for mapped in buffer}
        ready: list[dict[str, Any]] = []
        edge_evidence: list[_EdgeEvidenceOperation] = []
        pending: list[_PendingEdgeOperation] = []
        for mapped in buffer:
            for edge in mapped.edges:
                if edge.target_key in planned_nodes or self._ledger.has_node(
                    source_system, edge.target_key
                ):
                    ready.append({**edge.props, "synced_at": synced_at})
                    edge_evidence.append(self._edge_evidence_operation(edge.props))
                else:
                    pending.append(
                        _PendingEdgeOperation(
                            props=edge.props,
                            target_key=edge.target_key,
                            origin_source_ref=mapped.source_ref,
                            origin_snapshot_hash=mapped.source_snapshot_hash,
                            origin_entity=asdict(self._entity_evidence(mapped)),
                        )
                    )
        plan = _MutationPlan(
            run_id=run_id,
            node_upserts=tuple({**mapped.node_props, "synced_at": synced_at} for mapped in buffer),
            edge_upserts=tuple(ready),
            ledger_operations=tuple(
                _LedgerOperation(
                    kind="upsert",
                    entity_class=mapped.entity_class,
                    key=mapped.key,
                    content_hash=mapped.content_hash,
                    acl_hash=mapped.acl_hash,
                )
                for mapped in buffer
            ),
            node_evidence=tuple(self._node_evidence_operation(mapped) for mapped in buffer),
            edge_evidence=tuple(edge_evidence),
            state_marks=tuple(
                _StateMarkOperation(key=mapped.key, content_hash=mapped.content_hash)
                for mapped in buffer
            ),
            edge_reconciles=tuple(
                _EdgeReconcileOperation(
                    from_key=mapped.key,
                    keep_keys=tuple(str(edge.props["key"]) for edge in mapped.edges),
                )
                for mapped in buffer
            ),
            pending_adds=tuple(pending),
        )
        await self._execute_plan(plan)
        return len(buffer)

    async def _heal_acl(self, mapped: MappedEntity, run_id: str) -> None:
        plan = _MutationPlan(
            run_id=run_id,
            acl_updates=(
                _AclOperation(
                    keys=(mapped.key, *(str(edge.props["key"]) for edge in mapped.edges)),
                    acl=mapped.acl,
                ),
            ),
            ledger_operations=(
                _LedgerOperation(
                    kind="acl_heal",
                    entity_class=mapped.entity_class,
                    key=mapped.key,
                    content_hash=mapped.content_hash,
                    acl_hash=mapped.acl_hash,
                ),
            ),
            node_evidence=(self._node_evidence_operation(mapped),),
        )
        await self._execute_plan(plan)

    async def _repair_edges(self, mapped_seen: Sequence[MappedEntity], run_id: str) -> None:
        source_system = self._contract.source
        existing = {
            str(row["key"]): row
            for row in self._ledger.edge_evidence_for_nodes(
                source_system, [mapped.key for mapped in mapped_seen]
            )
        }
        synced_at = _now()
        ready: list[dict[str, Any]] = []
        evidence: list[_EdgeEvidenceOperation] = []
        pending: list[_PendingEdgeOperation] = []
        for mapped in mapped_seen:
            for edge in mapped.edges:
                row = existing.get(str(edge.props["key"]))
                if row is not None and (
                    str(row["from_key"]) == mapped.key and str(row["to_key"]) == edge.target_key
                ):
                    continue
                if self._ledger.node_evidence(source_system, edge.target_key) is not None:
                    ready.append({**edge.props, "synced_at": synced_at})
                    evidence.append(self._edge_evidence_operation(edge.props))
                else:
                    pending.append(
                        _PendingEdgeOperation(
                            props=edge.props,
                            target_key=edge.target_key,
                            origin_source_ref=mapped.source_ref,
                            origin_snapshot_hash=mapped.source_snapshot_hash,
                            origin_entity=asdict(self._entity_evidence(mapped)),
                        )
                    )
        if ready or pending:
            await self._execute_plan(
                _MutationPlan(
                    run_id=run_id,
                    edge_upserts=tuple(ready),
                    edge_evidence=tuple(evidence),
                    pending_adds=tuple(pending),
                )
            )

    async def _retry_pending(self, source_system: str, run_id: str) -> None:
        synced_at = _now()
        ready: list[dict[str, Any]] = []
        ready_pending_ids: list[int] = []
        evidence: list[_EdgeEvidenceOperation] = []
        for pending_id, props, target_key, origin in self._ledger.take_pending_edges_with_origin(
            source_system
        ):
            subject = self._ledger.node_evidence(source_system, str(props.get("from_key", "")))
            target = self._ledger.node_evidence(source_system, target_key)
            if subject is None or target is None or not origin:
                continue
            origin_entity = GraphEvidenceEntity.from_payload(origin)
            if (
                origin_entity.key != str(subject["key"])
                or origin_entity.source_snapshot_hash != str(subject["source_snapshot_hash"])
                or origin_entity.source_acl_hash != str(subject["source_acl_hash"])
            ):
                continue
            ready.append({**props, "synced_at": synced_at})
            evidence.append(self._edge_evidence_operation(props))
            ready_pending_ids.append(pending_id)
        if ready:
            await self._execute_plan(
                _MutationPlan(
                    run_id=run_id,
                    edge_upserts=tuple(ready),
                    edge_evidence=tuple(evidence),
                    pending_deletes=tuple(ready_pending_ids),
                )
            )

    async def _prune(self, source_system: str, run_id: str) -> int:
        stale = self._ledger.untouched_keys(source_system, run_id)
        if not stale:
            return 0
        await self._execute_plan(_MutationPlan(run_id=run_id, prune_keys=tuple(stale)))
        return len(stale)

    async def _execute_plan(self, plan: _MutationPlan) -> None:
        payload = plan.model_dump(mode="json")
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        event = new_audit_event(
            action="graph.sync.intent",
            resource=f"graph://{self._contract.source}",
            outcome=AuditOutcome.success,
            actor=self._actor,
            detail=f"prepared exact graph mutation intent sha256={fingerprint}",
            kpi_tag=self._kpi_tag,
            config_hash=_audit_config_hash(self._config_hash),
            provenance={"run_id": plan.run_id, "plan_sha256": fingerprint},
        )
        intent_id = self._ledger.enqueue_mutation_intent(
            event.event_id,
            source_system=self._contract.source,
            audit_event=event.model_dump(mode="json"),
            plan=payload,
        )
        if self._audit_sink is not None:
            await self._audit_sink.append(event)
        self._ledger.mark_mutation_audited(intent_id)
        await self._apply_plan(plan)
        self._ledger.mark_mutation_applied(intent_id)

    async def _apply_plan(self, plan: _MutationPlan) -> None:
        source_system = self._contract.source
        if plan.node_upserts:
            await self._store.upsert_nodes(plan.node_upserts)
        for operation in plan.acl_updates:
            await self._store.update_acl(operation.keys, operation.acl)
        if plan.edge_upserts:
            expected_edge_keys = tuple(str(edge["key"]) for edge in plan.edge_upserts)
            persisted_edge_keys = tuple(
                str(key) for key in await self._store.upsert_edges_confirmed(plan.edge_upserts)
            )
            if (
                len(expected_edge_keys) != len(persisted_edge_keys)
                or len(set(expected_edge_keys)) != len(expected_edge_keys)
                or len(set(persisted_edge_keys)) != len(persisted_edge_keys)
                or set(expected_edge_keys) != set(persisted_edge_keys)
            ):
                raise GraphStoreError(
                    "graph store did not return the exact persisted edge acknowledgment"
                )
        for operation in plan.edge_reconciles:
            await self._store.delete_edges_from_source(
                source_system, operation.from_key, operation.keep_keys
            )
        if plan.prune_keys:
            await self._store.delete_where_stale(source_system, plan.prune_keys)

        # A ledger row is clean only after every graph-store mutation in this
        # exact intent has succeeded. A crash anywhere above replays the whole
        # idempotent plan before another connector record can be classified.
        for operation in plan.ledger_operations:
            if operation.kind == "upsert":
                self._ledger.mark_upserted(
                    source_system,
                    operation.entity_class,
                    operation.key,
                    content_hash=operation.content_hash,
                    acl_hash=operation.acl_hash,
                    run_id=plan.run_id,
                )
            elif operation.kind == "acl_heal":
                self._ledger.mark_acl_healed(
                    source_system,
                    operation.entity_class,
                    operation.key,
                    content_hash=operation.content_hash,
                    acl_hash=operation.acl_hash,
                    run_id=plan.run_id,
                )
            else:
                self._ledger.touch(
                    source_system, operation.entity_class, operation.key, run_id=plan.run_id
                )
        for operation in plan.node_evidence:
            self._ledger.record_node_evidence(
                source_system,
                operation.key,
                source_ref=operation.source_ref,
                source_snapshot_hash=operation.source_snapshot_hash,
                source_acl_hash=operation.source_acl_hash,
                entity_class=operation.entity_class,
                display_name=operation.display_name,
                acl=operation.acl,
                run_id=plan.run_id,
            )
        for operation in plan.state_marks:
            self._state_store.mark(
                self._graph_source,
                operation.key,
                operation.key,
                operation.content_hash,
            )
        for operation in plan.edge_evidence:
            self._ledger.record_edge_evidence(
                source_system,
                key=operation.key,
                edge_kind=operation.edge_kind,
                from_key=operation.from_key,
                to_key=operation.to_key,
                run_id=plan.run_id,
            )
        for operation in plan.edge_reconciles:
            self._ledger.purge_pending_from(source_system, operation.from_key, operation.keep_keys)
            self._ledger.purge_edge_evidence_from(
                source_system, operation.from_key, operation.keep_keys
            )
        for operation in plan.pending_adds:
            self._ledger.add_pending_edge(
                source_system,
                operation.props,
                operation.target_key,
                plan.run_id,
                origin_source_ref=operation.origin_source_ref,
                origin_snapshot_hash=operation.origin_snapshot_hash,
                origin_entity=operation.origin_entity,
            )
        for pending_id in plan.pending_deletes:
            self._ledger.delete_pending_edge(pending_id)
        if plan.prune_keys:
            self._ledger.delete_keys(source_system, plan.prune_keys)

    async def _recover_mutations(self) -> None:
        source_system = self._contract.source
        for intent_id, payload in self._ledger.pending_mutation_audits(source_system):
            event = AuditEvent.model_validate(payload)
            if self._audit_sink is not None:
                await self._audit_sink.append(event)
            self._ledger.mark_mutation_audited(intent_id)
        for intent_id, payload in self._ledger.pending_mutation_plans(source_system):
            await self._apply_plan(_MutationPlan.model_validate(payload))
            self._ledger.mark_mutation_applied(intent_id)

    @staticmethod
    def _edge_evidence_operation(props: Mapping[str, Any]) -> _EdgeEvidenceOperation:
        return _EdgeEvidenceOperation(
            key=str(props["key"]),
            edge_kind=str(props["edge_kind"]),
            from_key=str(props["from_key"]),
            to_key=str(props["to_key"]),
        )

    @staticmethod
    def _node_evidence_operation(mapped: MappedEntity) -> _NodeEvidenceOperation:
        return _NodeEvidenceOperation(
            key=mapped.key,
            source_ref=mapped.source_ref,
            source_snapshot_hash=mapped.source_snapshot_hash,
            source_acl_hash=mapped.acl_hash,
            entity_class=mapped.entity_class,
            display_name=str(mapped.node_props.get("display_name", "")),
            acl=mapped.acl,
        )

    def _audit_event(
        self,
        mode: str,
        outcome: AuditOutcome,
        detail: str,
        run_id: str,
    ) -> AuditEvent:
        return new_audit_event(
            action="graph.sync",
            resource=f"graph://{self._contract.source}",
            outcome=outcome,
            actor=self._actor,
            detail=detail,
            kpi_tag=self._kpi_tag,
            config_hash=_audit_config_hash(self._config_hash),
            provenance={"run_id": run_id, "mode": mode},
        )

    async def _emit_audit(
        self,
        mode: str,
        outcome: AuditOutcome,
        detail: str,
        run_id: str,
        counts: Mapping[str, Any],
    ) -> None:
        del counts
        if self._audit_sink is not None:
            await self._audit_sink.append(self._audit_event(mode, outcome, detail, run_id))

    def _record_node_evidence(self, mapped: MappedEntity, run_id: str) -> None:
        self._ledger.record_node_evidence(
            mapped.source_system,
            mapped.key,
            source_ref=mapped.source_ref,
            source_snapshot_hash=mapped.source_snapshot_hash,
            source_acl_hash=mapped.acl_hash,
            entity_class=mapped.entity_class,
            display_name=str(mapped.node_props.get("display_name", "")),
            acl=mapped.acl,
            run_id=run_id,
        )

    @staticmethod
    def _entity_evidence(mapped: MappedEntity) -> GraphEvidenceEntity:
        return GraphEvidenceEntity(
            source_system=mapped.source_system,
            source_ref=mapped.source_ref,
            source_snapshot_hash=mapped.source_snapshot_hash,
            source_acl_hash=mapped.acl_hash,
            acl=mapped.acl,
            key=mapped.key,
            entity_class=mapped.entity_class,
            display_name=str(mapped.node_props.get("display_name", "")),
        )

    @staticmethod
    def _entity_from_registry(row: Mapping[str, Any]) -> GraphEvidenceEntity:
        return GraphEvidenceEntity(
            source_system=str(row["source_system"]),
            source_ref=str(row["source_ref"]),
            source_snapshot_hash=str(row["source_snapshot_hash"]),
            source_acl_hash=str(row["source_acl_hash"]),
            acl=tuple(str(value) for value in row["acl"]),
            key=str(row["key"]),
            entity_class=str(row["entity_class"]),
            display_name=str(row["display_name"]),
        )

    def _relationships_for(
        self, mapped_seen: Sequence[MappedEntity]
    ) -> tuple[GraphEvidenceRelationship, ...]:
        relationships: dict[str, GraphEvidenceRelationship] = {}
        source_system = self._contract.source
        rows = self._ledger.edge_evidence_for_nodes(
            source_system, [mapped.key for mapped in mapped_seen]
        )
        for row in rows:
            subject = self._ledger.node_evidence(source_system, str(row["from_key"]))
            target = self._ledger.node_evidence(source_system, str(row["to_key"]))
            if subject is None or target is None:
                continue
            relationship = GraphEvidenceRelationship(
                key=str(row["key"]),
                edge_kind=str(row["edge_kind"]),
                subject=self._entity_from_registry(subject),
                object=self._entity_from_registry(target),
            )
            relationships[relationship.key] = relationship
        return tuple(relationships.values())

    async def _recover_evidence_audits(self) -> None:
        assert self._audit_sink is not None
        for outbox_id, payload in self._ledger.pending_evidence_audits():
            event = AuditEvent.model_validate(payload)
            await self._audit_sink.append(event)
            self._ledger.mark_evidence_audited(outbox_id)

    def _deliver_evidence(self) -> None:
        assert self._evidence_recorder is not None
        for outbox_id, payload in self._ledger.pending_evidence_deliveries():
            try:
                result = self._evidence_recorder.bind_graph_evidence(
                    GraphEvidenceBatch.from_payload(payload)
                )
            except Exception:
                break
            if result.entities_unbound or result.relationships_unbound:
                # A sequential T1/T2 source read can observe a newer provider
                # snapshot in T2. Keep that exact receipt for a later T1 bind;
                # never fall back to mutable current-state identity.
                continue
            self._ledger.mark_evidence_delivered(outbox_id)


class M365GraphSyncHost:
    """Deliberately M365-only production host for the independent graph runner.

    The worker invokes this only after the governed T1 M365 sync succeeds.  It
    owns a distinct ``graph:m365`` delta/dedup namespace, so T1 progress can
    neither starve nor advance T2.  Mapping revisions force a safe full backfill.
    """

    def __init__(
        self,
        *,
        store: GraphWriteStore,
        ledger: GraphSyncLedger,
        mappings: Sequence[GraphSourceMapping],
        evidence_recorder: GraphEvidenceRecorder,
        source_authority_provider: GraphSourceAuthorityProvider,
        audit_sink: AuditSink,
        config_hash: str,
    ) -> None:
        if not mappings:
            raise ValueError("M365 graph-sync host requires at least one source mapping")
        self._store = store
        self._ledger = ledger
        self._mappings = tuple(mappings)
        self._evidence_recorder = evidence_recorder
        self._source_authority_provider = source_authority_provider
        self._audit_sink = audit_sink
        self._config_hash = config_hash

    def _mapping_revision(
        self, mapping: GraphSourceMapping, resolved_drive_ids: Sequence[str]
    ) -> str:
        payload = json.dumps(
            {
                "config_hash": self._config_hash,
                "mapping": mapping.model_dump(mode="json"),
                "resolved_drive_ids": sorted(resolved_drive_ids),
                "schema_version": SCHEMA_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def mapping_for(
        self,
        settings: M365ConnectorSettings,
        *,
        resolved_drive_ids: Sequence[str] | None = None,
    ) -> GraphSourceMapping:
        """Resolve the configured contract identity and exact drive coverage."""
        source_system = m365_data_contract(settings).source
        matches = [mapping for mapping in self._mappings if mapping.source_system == source_system]
        if len(matches) != 1 or not matches[0].entities:
            raise ValueError(
                "scheduled M365 graph sync requires exactly one non-empty graph "
                f"mapping for configured source_system={source_system!r}"
            )
        explicit_drive_ids = tuple(settings.drive_ids)
        if len(set(explicit_drive_ids)) != len(explicit_drive_ids):
            raise ValueError(
                "scheduled M365 graph sync requires unique explicit drive_ids; "
                f"configured drive_ids={explicit_drive_ids!r} contain duplicates"
            )

        mapping = matches[0]
        is_meridian = any(
            entity.entity_class in {"Customer", "AutoLoan", "PaymentSchedule", "ServicingNote"}
            for entity in mapping.entities
        )
        if is_meridian:
            drives = tuple(explicit_drive_ids if resolved_drive_ids is None else resolved_drive_ids)
            if resolved_drive_ids is None and settings.site_ids and not drives:
                raise ValueError(
                    "Meridian M365 graph sync requires an explicit manifest-bound drive id"
                )
            validate_meridian_m365_graph_mapping(mapping, drive_ids=drives)
            return mapping

        prefixes = tuple(entity.source_id_prefix for entity in mapping.entities)
        if len(set(prefixes)) != len(prefixes):
            raise ValueError(
                "scheduled M365 graph sync requires one unique mapping per drive_id; "
                f"mapping prefixes={prefixes!r} contain duplicates"
            )

        required_fields = {
            "id": "id",
            "title": "name",
            "doc_type": "graph_doc_type",
        }
        mismatched = [
            entity.source_id_prefix
            for entity in mapping.entities
            if entity.entity_class != "Document" or entity.fields != required_fields or entity.edges
        ]
        if mismatched:
            raise ValueError(
                "scheduled M365 graph sync requires the canonical Document mapping "
                "fields id=id, title=name, doc_type=graph_doc_type with no edges; "
                f"mismatched prefixes={mismatched!r}"
            )

        if resolved_drive_ids is None and settings.site_ids:
            # Production resolves site drives before its exact check. The API
            # composition root can only perform this credential-free structural
            # preflight, so it pins every explicit drive and defers discovered
            # drive equality to run().
            missing_explicit = sorted(set(explicit_drive_ids) - set(prefixes))
            if missing_explicit:
                raise ValueError(
                    "scheduled M365 graph sync mapping is missing explicit drive_id prefixes; "
                    f"missing={missing_explicit!r}"
                )
            return mapping

        drive_ids = tuple(explicit_drive_ids if resolved_drive_ids is None else resolved_drive_ids)
        if not drive_ids:
            raise ValueError(
                "scheduled M365 graph sync resolved no drives for the configured target"
            )
        if len(set(drive_ids)) != len(drive_ids):
            raise ValueError(
                "scheduled M365 graph sync requires unique resolved drive_ids; "
                f"resolved drive_ids={drive_ids!r} contain duplicates"
            )
        missing = sorted(set(drive_ids) - set(prefixes))
        surplus = sorted(set(prefixes) - set(drive_ids))
        if missing or surplus or len(prefixes) != len(drive_ids):
            raise ValueError(
                "scheduled M365 graph sync requires exact resolved drive_id prefix coverage; "
                f"missing={missing!r} surplus={surplus!r}"
            )
        return mapping

    async def run(
        self,
        settings: M365ConnectorSettings,
        *,
        state_store: ConnectorStateStore,
        actor: str,
        kpi_tag: str | None = None,
        transport: Any | None = None,
        parser: Any | None = None,
    ) -> GraphSyncReport:
        contract = m365_data_contract(settings)
        graph_source = f"graph:{contract.source}"
        state_store.claim_target_binding(
            graph_source,
            m365_admitted_target_digest(settings),
        )
        connector = M365GraphConnector(
            settings,
            transport=transport,
            delta_state={},
            parser=parser,
        )
        resolved_drive_ids = await connector.resolve_drive_ids()
        mapping = self.mapping_for(settings, resolved_drive_ids=resolved_drive_ids)
        revision = self._mapping_revision(mapping, resolved_drive_ids)
        refresh_required = state_store.begin_mapper_refresh(graph_source, revision)
        refresh_committed = False
        try:
            state = {} if refresh_required else state_store.load_state(graph_source)
            delta_state = {
                key.removeprefix("delta:"): value
                for key, value in state.items()
                if key.startswith("delta:")
            }
            connector._recrawl(delta_state)
            mode: Literal["backfill", "incremental"] = "incremental" if delta_state else "backfill"
            report = await GraphSyncRunner(
                connector,
                contract,
                self._store,
                self._ledger,
                state_store,
                mapping,
                actor=actor,
                audit_sink=self._audit_sink,
                kpi_tag=kpi_tag,
                config_hash=self._config_hash,
                evidence_recorder=self._evidence_recorder,
                source_authority_provider=self._source_authority_provider,
            ).run(mode=mode)

            if connector.acl_quarantined:
                await self._audit_sink.append(
                    new_audit_event(
                        action="graph.acl_quarantine",
                        resource="graph://m365",
                        outcome=AuditOutcome.denied,
                        actor=actor,
                        detail=(
                            f"quarantined={connector.acl_quarantined} "
                            f"fetch_failed={connector.acl_fetch_failed} "
                            f"ids={connector.acl_quarantined_ids}"
                        ),
                        kpi_tag=kpi_tag,
                        config_hash=self._config_hash,
                    )
                )
            for drive_id, link in connector.pending_delta_links.items():
                state_store.set_cursor(graph_source, f"delta:{drive_id}", link)
            if refresh_required:
                state_store.commit_mapper_refresh(graph_source, revision)
                refresh_committed = True
            return report
        except BaseException:
            if refresh_required:
                if refresh_committed:
                    state_store.invalidate_mapper_revision(graph_source, revision)
                else:
                    state_store.rollback_mapper_refresh(graph_source, revision)
            raise


def _quarantine_key(record: Any, entity_class: str) -> str:
    return f"quarantine:{entity_class}:{record.source_id}:{record.record_id}"
