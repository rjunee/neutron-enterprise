# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""run_teams_sync — drive the Teams connector through the governed write path (NE-080).

Wires :class:`~ne.connect.teams.connector.TeamsConnector` into the framework
:class:`~ne.connect.bridge.SyncRunner`, mirroring the M365 delta-connector shape.
Durability contract: the per-channel ``deltaLink`` cursors (``cursor:delta:<team>/
<channel>`` ledger keys) are committed ONLY AFTER a successful run — an ABORTED
run (a ``GraphSyncError`` / ``InvalidRecordError``) raises out before the commit
and leaves the OLD cursors in place; the next run re-reads from them and the
dedup ledger skips already-written records. ``cursor_field=None`` because
resumability comes from the connector's own delta cursors + the dedup ledger.

The SyncRunner emits exactly ONE ``config_hash``-stamped ``connector.sync``
AuditEvent. Channel quarantines are surfaced as ONE additional
``connector.acl_quarantine`` ``denied`` event via the NE-005 event spine.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ne.brain.write import WriteQuarantine
from ne.connect.bridge import SyncReport, SyncRunner
from ne.connect.state import ConnectorStateStore
from ne.connect.teams.config import TeamsConnectorSettings
from ne.connect.teams.connector import TeamsConnector
from ne.connect.teams.contract import teams_data_contract
from ne.connect.teams.graph import Transport
from ne.contracts.governance import AuditSink
from ne.contracts.knowledge import KnowledgeWrite
from ne.events import new_audit_event
from ne.schema import AuditOutcome

__all__ = ["TeamsSyncOutcome", "run_teams_sync"]

_CURSOR_PREFIX = "cursor:"


class TeamsSyncOutcome(BaseModel):
    """Immutable outcome of a single :func:`run_teams_sync`."""

    model_config = ConfigDict(frozen=True)

    report: SyncReport
    acl_quarantined: int
    mode: str


async def run_teams_sync(
    settings: TeamsConnectorSettings,
    *,
    writer: KnowledgeWrite,
    state_store: ConnectorStateStore,
    audit_sink: AuditSink | None,
    actor: str,
    transport: Transport | None = None,
    kpi_tag: str | None = None,
    quarantine: WriteQuarantine | None = None,
) -> TeamsSyncOutcome:
    """Run one durable, fail-closed Teams sync; return its :class:`TeamsSyncOutcome`."""
    contract = teams_data_contract(settings)

    cursor_state = {
        key.removeprefix(_CURSOR_PREFIX): value
        for key, value in state_store.load_state(contract.source).items()
        if key.startswith(_CURSOR_PREFIX)
    }
    connector = TeamsConnector(settings, transport=transport, cursor_state=cursor_state)
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
    )
    report = await runner.run(mode=mode)  # type: ignore[arg-type]

    # Commit per-channel deltaLink cursors ONLY after a successful run (an abort
    # raises out of runner.run above, leaving the OLD cursors in place).
    for key, cursor in connector.pending_cursors.items():
        state_store.set_cursor(contract.source, f"{_CURSOR_PREFIX}{key}", cursor)

    # Re-read cursors AFTER the commit so the report is not stale.
    report = report.model_copy(update={"cursors": state_store.load_state(contract.source)})

    if connector.acl_quarantined > 0 and audit_sink is not None:
        await audit_sink.append(
            new_audit_event(
                action="connector.acl_quarantine",
                resource=f"connector://{contract.source}",
                outcome=AuditOutcome.denied,
                actor=actor,
                detail=(
                    f"channels_quarantined={connector.acl_quarantined} "
                    f"ids={connector.acl_quarantined_ids}"
                ),
                kpi_tag=kpi_tag,
            )
        )

    return TeamsSyncOutcome(report=report, acl_quarantined=connector.acl_quarantined, mode=mode)
