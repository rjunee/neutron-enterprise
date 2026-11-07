# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""run_salesforce_sync — drive the Salesforce connector through the governed write path (NE-047).

Wires :class:`~ne.connect.salesforce.connector.SalesforceConnector` into the
framework :class:`~ne.connect.bridge.SyncRunner`, mirroring
:func:`ne.connect.sql.sync.run_sql_sync`'s FIX-D shape exactly. Durability
contract: per-object ``SystemModstamp`` cursors (``modstamp:{object}`` ledger
keys) are committed ONLY AFTER a successful run. Only an ABORTED run (a
``SalesforceSyncError``/``InvalidRecordError``) leaves the OLD cursors in
place; the next run re-reads from them and the dedup ledger skips
already-written records — no progress is lost, no duplicate is written.
``cursor_field=None`` because resumability comes from the connector's own
per-object cursor tracking + the dedup ledger, not a lexicographic top-level
record cursor.

Also builds :func:`make_salesforce_record_mapper` — the config-driven
SObject -> document shaping function passed to :class:`SyncRunner` as
``mapper``: deterministic, config-ordered content assembly plus typed metadata
stamping (the NE-047 object->document mapping deliverable).

The SyncRunner emits exactly ONE ``config_hash``-stamped ``connector.sync``
AuditEvent (contract untouched). ACL quarantines are surfaced as ONE
additional ``connector.acl_quarantine`` ``denied`` event via the NE-005 event
spine.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from decimal import Decimal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.brain.write import CuratedKnowledgeItem, ProvenanceClass, WriteQuarantine
from ne.connect.authority_admission import (
    SqlAlchemyHierarchicalAdmission,
    SqlAlchemySweepCursor,
    SweepCadencePlan,
)
from ne.connect.bridge import (
    ChunkReader,
    SyncReport,
    SyncRunner,
    canonical_item_id,
    canonical_payload_json,
    clear_stale_source_links,
)
from ne.connect.contracts import OdcsDataContract
from ne.connect.errors import SalesforceSyncError
from ne.connect.salesforce.authority import (
    SalesforceAuthorityAdmission,
    SalesforceAuthorityFactory,
    salesforce_object_scope,
)
from ne.connect.salesforce.client import Transport
from ne.connect.salesforce.config import SalesforceConnectorSettings
from ne.connect.salesforce.connector import SalesforceConnector
from ne.connect.salesforce.contract import (
    salesforce_admitted_target_digest as _salesforce_target_binding_digest,
)
from ne.connect.salesforce.contract import (
    salesforce_contract_sla_seconds,
    salesforce_data_contract,
    salesforce_sweep_plan,
)
from ne.connect.salesforce.contract import (
    salesforce_source_link_mapper_revision as _source_link_mapper_revision,
)
from ne.connect.source_url import salesforce_record_source_url
from ne.connect.state import ConnectorStateStore
from ne.contracts.governance import AuditSink
from ne.contracts.integration import Record
from ne.contracts.knowledge import KnowledgeWrite
from ne.contracts.source_authority import SourceReconciliationClaim, SourceReconciliationScope
from ne.events import new_audit_event
from ne.schema import AuditOutcome

__all__ = ["SalesforceSyncOutcome", "make_salesforce_record_mapper", "run_salesforce_sync"]

_CURSOR_PREFIX = "modstamp:"
_AUTHORITY_LEASE_SECONDS = 900


class SalesforceSyncOutcome(BaseModel):
    """Immutable outcome of a single :func:`run_salesforce_sync`."""

    model_config = ConfigDict(frozen=True)

    report: SyncReport
    acl_quarantined: int
    mode: str


class _SalesforceAuthorityDriver:
    """Claim object scopes before I/O and rotate bounded quiet-point re-reads."""

    def __init__(
        self,
        admission: SalesforceAuthorityAdmission,
        *,
        engine: sa.Engine,
        settings: SalesforceConnectorSettings,
    ) -> None:
        self._admission = admission
        self._settings = settings
        self._hierarchy = SqlAlchemyHierarchicalAdmission(engine)
        self._cursors = SqlAlchemySweepCursor(engine)
        self._claims: dict[str, SourceReconciliationClaim] = {}

    def _scope(self, object_name: str) -> SourceReconciliationScope:
        return salesforce_object_scope(
            deployment_id=self._admission.deployment_id,
            connector_instance_id=self._admission.connector_instance_id,
            organization_id=self._admission.organization_id,
            object_name=object_name,
        )

    async def claim_objects(self) -> None:
        """Claim every configured object before ObjectPermissions or content I/O."""
        for cfg in self._settings.objects:
            claim = await self._admission.claim_scope(
                self._scope(cfg.name), lease_seconds=_AUTHORITY_LEASE_SECONDS
            )
            self._claims[cfg.name] = claim
            self._admission.bind_drive_claim(cfg.name, claim)

    async def sweep_quiet_points(
        self,
        connector: SalesforceConnector,
        *,
        plan: SweepCadencePlan,
        sla_seconds: Decimal,
    ) -> int:
        swept = 0
        for object_name, claim in self._claims.items():
            evidence = connector.permission_evidence.get(object_name)
            if evidence is None:
                continue
            candidates = self._hierarchy.unswept_points(
                claim.scope, epoch=claim.reconciliation_epoch
            )
            if candidates:
                batch = self._cursors.plan(
                    claim.scope, candidates=candidates, batch_size=plan.batch_size
                )
                for identity_hash, record_id in batch.points:
                    record = await connector.read_point_record(object_name, record_id)
                    if record is None:
                        continue
                    canonical_bytes = canonical_payload_json(record.payload).encode()
                    prepared = await self._admission.prepare(
                        record,
                        acl=evidence.principals,
                        canonical_bytes=canonical_bytes,
                        item_id=canonical_item_id(
                            self._settings.source_name,
                            record.source_id,
                            record.record_id,
                        ),
                        evidence=evidence,
                    )
                    if prepared.source_identity.identity_hash != identity_hash:
                        raise SalesforceSyncError(
                            "Salesforce quiet sweep returned a foreign source identity"
                        )
                    await self._admission.finalize(prepared)
                    self._cursors.complete_point(
                        claim.scope,
                        identity_hash=identity_hash,
                    )
                    swept += 1
            self._hierarchy.require_points_swept_within_sla(claim.scope, sla_seconds=sla_seconds)
        return swept

    async def release(self) -> None:
        for object_name, claim in self._claims.items():
            self._admission.release_drive_claim(object_name)
            await self._admission.release_scope(claim)


def make_salesforce_record_mapper(
    settings: SalesforceConnectorSettings,
) -> Callable[[Record, OdcsDataContract, tuple[str, ...]], CuratedKnowledgeItem]:
    """Build the SObject -> document shaping mapper for ``settings.objects``.

    Deterministic, config-ordered document shaping: ``content`` joins
    ``content_fields`` in configured order (a ``None``/empty-string field is
    skipped — Bulk CSV renders a Salesforce NULL as ``''``); an all-empty row
    therefore yields ``""``, which the writer's shape contract
    (``minLength: 1``) quarantines — counted in ``report.quarantined``, not
    silently dropped. ``item_id`` is IDENTICAL to ``default_record_mapper``'s
    convention (sha256 of ``source\\nsource_id\\nrecord_id``); ``record.source_id``
    IS the configured object name (no prefix parsing needed, unlike the SQL
    connector's ``table_ref:id`` scheme).
    """
    object_by_name = {cfg.name: cfg for cfg in settings.objects}

    def _map(
        record: Record, contract: OdcsDataContract, acl: tuple[str, ...]
    ) -> CuratedKnowledgeItem:
        cfg = object_by_name.get(record.source_id)
        if cfg is None:
            # Impossible by construction (the connector always stamps
            # source_id=cfg.name) — fail loud rather than mis-map silently.
            raise SalesforceSyncError(
                f"record source_id {record.source_id!r} matches no configured object"
            )

        item_id = hashlib.sha256(
            f"{contract.source}\n{record.source_id}\n{record.record_id}".encode()
        ).hexdigest()

        content = "\n\n".join(
            str(record.payload[field])
            for field in cfg.content_fields
            if record.payload.get(field) not in (None, "")
        )

        metadata: dict[str, object] = {
            "connector": contract.source,
            "source_id": record.source_id,
            "record_id": record.record_id,
            "pii_class": contract.pii.value,
            "object": cfg.name,
        }
        for field in cfg.metadata_fields:
            # ``source_url`` is server-owned provenance, never a source row's
            # arbitrary field. It is stamped from validated authorities below.
            if field == "source_url":
                continue
            value = record.payload.get(field)
            if value in (None, ""):
                continue
            metadata[field] = value if isinstance(value, (str, int, float, bool)) else str(value)

        source_url = salesforce_record_source_url(
            instance_url=settings.instance_url,
            instance_allowed_hosts=settings.source_link_allowed_hosts,
            object_name=cfg.name,
            record_id=record.record_id,
        )
        if source_url is not None:
            metadata["source_url"] = source_url

        return CuratedKnowledgeItem(
            item_id=item_id,
            content=content,
            metadata=metadata,
            provenance=f"connector:{contract.source}:{record.record_id}",
            provenance_class=ProvenanceClass.INGESTED,
            acl=acl,
        )

    return _map


async def run_salesforce_sync(
    settings: SalesforceConnectorSettings,
    *,
    writer: KnowledgeWrite,
    state_store: ConnectorStateStore,
    audit_sink: AuditSink | None,
    actor: str,
    transport: Transport | None = None,
    kpi_tag: str | None = None,
    quarantine: WriteQuarantine | None = None,
    batch_size: int = 50,
    authority: SalesforceAuthorityFactory | None = None,
    chunk_reader: ChunkReader | None = None,
) -> SalesforceSyncOutcome:
    """Run one durable, fail-closed Salesforce sync; return its :class:`SalesforceSyncOutcome`."""
    if settings.authority_enabled and authority is None:
        raise SalesforceSyncError(
            "Salesforce source authority is enabled but its producer is unavailable"
        )
    contract = salesforce_data_contract(settings)
    mapper_revision = _source_link_mapper_revision(settings)
    refresh_required = False
    refresh_committed = False
    driver: _SalesforceAuthorityDriver | None = None

    try:
        cursor_state = {
            key.removeprefix(_CURSOR_PREFIX): value
            for key, value in (
                {} if refresh_required else state_store.load_state(contract.source)
            ).items()
            if key.startswith(_CURSOR_PREFIX)
        }
        connector = SalesforceConnector(settings, transport=transport, cursor_state=cursor_state)
        # OAuth is the sole permitted pre-binding provider call: its provider-issued
        # Identity URL/organization_id establishes the immutable organization. The
        # exact target is then durably claimed before ObjectPermissions/Bulk/content I/O.
        organization_id = await connector.acquire_organization_id()
        admitted_target_digest = _salesforce_target_binding_digest(settings, organization_id)
        state_store.claim_target_binding(
            contract.source,
            admitted_target_digest,
        )
        admission = None
        sweep_plan = None
        if authority is not None:
            if not isinstance(authority, SalesforceAuthorityFactory):
                raise SalesforceSyncError(
                    "Salesforce sync received a foreign source-authority capability"
                )
            admission = authority.create(
                organization_id=organization_id,
                admitted_target_digest=admitted_target_digest,
            )
            sweep_plan = salesforce_sweep_plan(settings)
            if sweep_plan is None:
                raise SalesforceSyncError(
                    "Salesforce authority requires an explicit bounded sweep plan"
                )
            sweep_plan.require_sub_sla(salesforce_contract_sla_seconds(settings))
            driver = _SalesforceAuthorityDriver(
                admission,
                engine=admission.engine,
                settings=settings,
            )
            await driver.claim_objects()
        refresh_required = state_store.begin_mapper_refresh(contract.source, mapper_revision)
        if refresh_required:
            connector._recrawl({})
            cursor_state = {}
        mode = "incremental" if cursor_state else "backfill"

        async def _before_success(report: SyncReport) -> None:
            nonlocal refresh_committed
            del report
            if driver is not None and sweep_plan is not None:
                await driver.sweep_quiet_points(
                    connector,
                    plan=sweep_plan,
                    sla_seconds=salesforce_contract_sla_seconds(settings),
                )
            if connector.acl_quarantined > 0 and audit_sink is not None:
                await audit_sink.append(
                    new_audit_event(
                        action="connector.acl_quarantine",
                        resource=f"connector://{contract.source}",
                        outcome=AuditOutcome.denied,
                        actor=actor,
                        detail=(
                            f"quarantined={connector.acl_quarantined} "
                            f"fetch_failed={connector.acl_fetch_failed} "
                            f"ids={connector.acl_quarantined_ids}"
                        ),
                        kpi_tag=kpi_tag,
                    )
                )
            if not refresh_required:
                return
            await clear_stale_source_links(
                writer,
                state_store,
                source=contract.source,
                mapper_revision=mapper_revision,
            )
            for object_name, cursor in connector.pending_cursors.items():
                state_store.set_cursor(contract.source, f"{_CURSOR_PREFIX}{object_name}", cursor)
            state_store.commit_mapper_refresh(contract.source, mapper_revision)
            refresh_committed = True

        runner = SyncRunner(
            connector,
            contract,
            writer,
            state_store,
            actor=actor,
            audit_sink=audit_sink,
            cursor_field=None,
            kpi_tag=kpi_tag,
            quarantine=quarantine,
            batch_size=batch_size,
            mapper=make_salesforce_record_mapper(settings),
            mapper_revision=mapper_revision,
            before_success=_before_success,
            authority=admission,
            permission_evidence=connector.permission_evidence,
            chunk_reader=chunk_reader,
        )
        report = await runner.run(mode=mode)  # type: ignore[arg-type]
        if not refresh_required:
            for object_name, cursor in connector.pending_cursors.items():
                state_store.set_cursor(contract.source, f"{_CURSOR_PREFIX}{object_name}", cursor)
    except BaseException:
        if refresh_required:
            if refresh_committed:
                state_store.invalidate_mapper_revision(contract.source, mapper_revision)
            else:
                state_store.rollback_mapper_refresh(contract.source, mapper_revision)
        raise
    finally:
        if driver is not None:
            await driver.release()

    # Re-read cursors AFTER the atomic finalizer (FIX-D, sql/m365 parity).
    report = report.model_copy(update={"cursors": state_store.load_state(contract.source)})

    return SalesforceSyncOutcome(
        report=report, acl_quarantined=connector.acl_quarantined, mode=mode
    )
