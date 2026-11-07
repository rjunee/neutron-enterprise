# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""run_sql_sync — drive the SQL connector through the governed write path (NE-046).

Wires :class:`~ne.connect.sql.connector.SqlConnector` into the framework
:class:`~ne.connect.bridge.SyncRunner`, mirroring
:func:`ne.connect.m365.sync.run_m365_sync`'s FIX-D shape exactly. Durability
contract: per-table cursors (``cursor:{table_ref}`` ledger keys) are committed
ONLY AFTER a successful run. A run with ACL-quarantined rows STILL commits its
cursors — progress is durable and one misconfigured/tainted row cannot stall the
whole table's resync (fix the config, then reset the ledger to force a resync —
see the package README). Only an ABORTED run (a ``SqlSyncError``/
``InvalidRecordError``) leaves the OLD cursors in place; the next run re-reads
from them and the dedup ledger skips already-written rows — no progress is
lost, no duplicate is written. ``cursor_field=None`` because resumability comes
from the connector's own typed per-table cursor tracking + the dedup ledger, not
a lexicographic top-level record cursor.

Also builds :func:`make_sql_record_mapper` — the config-driven table -> document
shaping function passed to :class:`SyncRunner` as ``mapper``: deterministic,
config-ordered content assembly plus typed metadata stamping (the NE-046
table->document mapping deliverable).

The SyncRunner emits exactly ONE ``config_hash``-stamped ``connector.sync``
AuditEvent (contract untouched). ACL quarantines are surfaced as ONE additional
``connector.acl_quarantine`` ``denied`` event via the NE-005 event spine.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.brain.write import CuratedKnowledgeItem, ProvenanceClass, WriteQuarantine
from ne.connect.bridge import SyncReport, SyncRunner
from ne.connect.contracts import OdcsDataContract
from ne.connect.errors import SqlSyncError
from ne.connect.sql.config import SqlConnectorSettings
from ne.connect.sql.connector import SqlConnector
from ne.connect.sql.contract import sql_data_contract
from ne.connect.state import ConnectorStateStore
from ne.contracts.governance import AuditSink
from ne.contracts.integration import Record
from ne.contracts.knowledge import KnowledgeWrite
from ne.events import new_audit_event
from ne.schema import AuditOutcome

__all__ = ["SqlSyncOutcome", "make_sql_record_mapper", "run_sql_sync"]

_CURSOR_PREFIX = "cursor:"


class SqlSyncOutcome(BaseModel):
    """Immutable outcome of a single :func:`run_sql_sync`."""

    model_config = ConfigDict(frozen=True)

    report: SyncReport
    acl_quarantined: int
    mode: str


def make_sql_record_mapper(
    settings: SqlConnectorSettings,
) -> Callable[[Record, OdcsDataContract, tuple[str, ...]], CuratedKnowledgeItem]:
    """Build the table -> document shaping mapper for ``settings.tables``.

    Deterministic, config-ordered document shaping: ``content`` joins
    ``content_columns`` in configured order (a NULL column is skipped, never
    stringified as ``"None"``); an all-NULL row therefore yields ``""``, which
    the writer's shape contract (``minLength: 1``) quarantines — counted in
    ``report.quarantined``, not silently dropped. ``item_id`` is IDENTICAL to
    ``default_record_mapper``'s convention (sha256 of
    ``source\\nsource_id\\nrecord_id``), preserving the e2e ``_item_id`` helper
    pattern and never containing ``':'``.
    """
    table_by_ref = {cfg.table_ref: cfg for cfg in settings.tables}
    # Longest-first so e.g. "sales.orders" is matched before a hypothetical
    # "sales" table_ref sharing the same prefix.
    sorted_refs = sorted(table_by_ref, key=len, reverse=True)

    def _map(
        record: Record, contract: OdcsDataContract, acl: tuple[str, ...]
    ) -> CuratedKnowledgeItem:
        table_ref = next(
            (ref for ref in sorted_refs if record.source_id.startswith(f"{ref}:")), None
        )
        if table_ref is None:
            # Impossible by construction (the connector always stamps
            # "{table_ref}:{id}") — fail loud rather than mis-map silently.
            raise SqlSyncError(f"record source_id {record.source_id!r} matches no configured table")
        cfg = table_by_ref[table_ref]

        item_id = hashlib.sha256(
            f"{contract.source}\n{record.source_id}\n{record.record_id}".encode()
        ).hexdigest()

        content = "\n\n".join(
            str(record.payload[column])
            for column in cfg.content_columns
            if record.payload.get(column) is not None
        )

        metadata: dict[str, object] = {
            "connector": contract.source,
            "source_id": record.source_id,
            "record_id": record.record_id,
            "pii_class": contract.pii.value,
            "table": table_ref,
        }
        for column in cfg.metadata_columns:
            value = record.payload.get(column)
            if value is None:
                continue
            metadata[column] = value if isinstance(value, (str, int, float, bool)) else str(value)

        return CuratedKnowledgeItem(
            item_id=item_id,
            content=content,
            metadata=metadata,
            provenance=f"connector:{contract.source}:{record.record_id}",
            provenance_class=ProvenanceClass.INGESTED,
            acl=acl,
        )

    return _map


async def run_sql_sync(
    settings: SqlConnectorSettings,
    *,
    writer: KnowledgeWrite,
    state_store: ConnectorStateStore,
    audit_sink: AuditSink | None,
    actor: str,
    engine: sa.Engine | None = None,
    kpi_tag: str | None = None,
    quarantine: WriteQuarantine | None = None,
    batch_size: int = 50,
) -> SqlSyncOutcome:
    """Run one durable, fail-closed SQL sync; return its :class:`SqlSyncOutcome`."""
    contract = sql_data_contract(settings)

    cursor_state = {
        key.removeprefix(_CURSOR_PREFIX): value
        for key, value in state_store.load_state(contract.source).items()
        if key.startswith(_CURSOR_PREFIX)
    }
    connector = SqlConnector(settings, engine=engine, cursor_state=cursor_state)
    mode = "incremental" if cursor_state else "backfill"

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
        mapper=make_sql_record_mapper(settings),
    )
    report = await runner.run(mode=mode)  # type: ignore[arg-type]

    # Commit per-table cursors ONLY after a successful run (durability contract).
    for table_ref, cursor in connector.pending_cursors.items():
        state_store.set_cursor(contract.source, f"{_CURSOR_PREFIX}{table_ref}", cursor)

    # Re-read cursors AFTER the commit so the report is not stale (FIX-D, m365 parity).
    report = report.model_copy(update={"cursors": state_store.load_state(contract.source)})

    if connector.acl_quarantined > 0 and audit_sink is not None:
        await audit_sink.append(
            new_audit_event(
                action="connector.acl_quarantine",
                resource=f"connector://{contract.source}",
                outcome=AuditOutcome.denied,
                actor=actor,
                detail=(
                    f"quarantined={connector.acl_quarantined} ids={connector.acl_quarantined_ids}"
                ),
                kpi_tag=kpi_tag,
            )
        )

    return SqlSyncOutcome(report=report, acl_quarantined=connector.acl_quarantined, mode=mode)
