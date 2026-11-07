# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""run_csv_sync — drive the CSV-file connector through the governed write path.

Wires :class:`~ne.connect.csvfile.connector.CsvConnector` into the framework
:class:`~ne.connect.bridge.SyncRunner`, mirroring
:func:`ne.connect.fixture_sync.run_fixture_sync`'s shape. The run mode is ALWAYS
``backfill`` — every poll re-reads the file as a full snapshot; the content-hash
dedup ledger makes an unchanged file report ``skipped_unchanged`` and write
nothing.

``snapshot=True`` + the passed-through quarantine lane are what make the
quarantine a LIVE view of the file: a contract-invalid row parks with the
validator's own reason, a duplicated key parks fail-closed, a FIXED row's
accepted write clears its park (writer accept path), and a row deleted from the
file reconciles its park away on the next poll. An EMPTY read (zero data rows —
a truncated or header-only file, e.g. mid truncate-then-rewrite) never
reconciles: the parks stay visible until a non-empty snapshot confirms.

Cursor: the file's mtime (ISO-8601 UTC, lexicographically monotonic) is
committed after every successful run under the connector's single source id —
"the file state as of T is fully reconciled". This is what makes the lane's
freshness on /connect honest for a polled file.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ne.brain.write import WriteQuarantine
from ne.connect.bridge import SOURCE_IDENTITY_MAPPER_REVISION, SyncReport, SyncRunner
from ne.connect.csvfile.config import CsvConnectorSettings
from ne.connect.csvfile.connector import CsvConnector
from ne.connect.csvfile.contract import csv_data_contract
from ne.connect.state import ConnectorStateStore
from ne.contracts.governance import AuditSink
from ne.contracts.knowledge import KnowledgeWrite

__all__ = ["CsvSyncOutcome", "run_csv_sync"]


class CsvSyncOutcome(BaseModel):
    """Immutable outcome of a single :func:`run_csv_sync`."""

    model_config = ConfigDict(frozen=True)

    report: SyncReport
    mode: str


async def run_csv_sync(
    settings: CsvConnectorSettings,
    *,
    writer: KnowledgeWrite,
    state_store: ConnectorStateStore,
    audit_sink: AuditSink | None,
    actor: str,
    kpi_tag: str | None = None,
    quarantine: WriteQuarantine | None = None,
    batch_size: int = 50,
    source_free: bool = False,
) -> CsvSyncOutcome:
    """Run one governed full-snapshot CSV sync; return its counts outcome.

    ``source_free`` is the registry grant from ``SYNC_SOURCES`` (never a caller's
    opinion) that lets the shared runner author ``source_status=no_source`` on
    this deployment-supplied file. Fail-closed default ``False``.
    """
    contract = csv_data_contract(settings)
    connector = CsvConnector(settings.path, source=contract.source, id_column=settings.id_column)
    # Read the mtime BEFORE the run: if the file changes mid-run, the next poll
    # sees a newer mtime and re-reads — never a cursor claiming more than was read.
    cursor = connector.file_mtime_iso()

    runner = SyncRunner(
        connector,
        contract,
        writer,
        state_store,
        actor=actor,
        audit_sink=audit_sink,
        cursor_field=None,
        kpi_tag=kpi_tag,
        batch_size=batch_size,
        quarantine=quarantine,
        snapshot=True,
        source_free=source_free,
        mapper_revision=SOURCE_IDENTITY_MAPPER_REVISION,
    )
    report = await runner.run(mode="backfill")

    # Commit the file cursor ONLY after a successful run, then re-read so the
    # report is not stale (the sql/m365 FIX-D parity).
    state_store.set_cursor(contract.source, connector.source_id, cursor)
    report = report.model_copy(update={"cursors": state_store.load_state(contract.source)})

    return CsvSyncOutcome(report=report, mode="backfill")
