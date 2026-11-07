# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""run_slack_sync — drive the Slack connector through the governed write path (NE-045).

Wires :class:`~ne.connect.slack.connector.SlackConnector` into the framework
:class:`~ne.connect.bridge.SyncRunner`. Durability contract: per-channel ``ts``
cursors are committed to the ledger ONLY AFTER a successful run. A run with
quarantined OR rate-limited channels STILL commits the OTHER channels'
cursors — progress is durable and a single wedged (or rate-limited) channel
cannot stall the crawl. A rate-limited channel (NE-CLEAN3 item 2) keeps its OLD
cursor exactly like an aborted run would, and is re-fetched next run — the
``(source, source_id, record_id)`` dedup ledger absorbs the overlap so no
duplicate is written. Only a genuinely ABORTED run (run-level throttling
exhaustion, a 5xx, or a crash) leaves ALL cursors in place; the next run
re-fetches everything and the same dedup ledger skips already-written
messages — no progress is lost, no duplicate is written. This is the
post-2026-03 operational requirement: Slack's tightened rate limits make
throttling routine, so it must never cost correctness.

``cursor_field=None`` because resumability comes from the persisted per-channel
``ts`` cursor + dedup ledger, not the SyncRunner's own lexicographic cursor
field.

The SyncRunner emits exactly ONE ``config_hash``-stamped ``connector.sync``
AuditEvent (contract untouched). ACL quarantines are surfaced as ONE additional
``connector.acl_quarantine`` ``denied`` event via the NE-005 event spine.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ne.brain.write import WriteQuarantine
from ne.connect.bridge import SyncReport, SyncRunner
from ne.connect.slack.client import Transport
from ne.connect.slack.config import SlackConnectorSettings
from ne.connect.slack.connector import SlackConnector
from ne.connect.slack.contract import slack_data_contract
from ne.connect.state import ConnectorStateStore
from ne.contracts.governance import AuditSink
from ne.contracts.knowledge import KnowledgeWrite
from ne.events import new_audit_event
from ne.schema import AuditOutcome

__all__ = ["SlackSyncOutcome", "run_slack_sync"]

_CURSOR_PREFIX = "ts:"


class SlackSyncOutcome(BaseModel):
    """Immutable outcome of a single :func:`run_slack_sync`."""

    model_config = ConfigDict(frozen=True)

    report: SyncReport
    acl_quarantined: int
    mode: str
    rate_limited: int = 0


async def run_slack_sync(
    settings: SlackConnectorSettings,
    *,
    writer: KnowledgeWrite,
    state_store: ConnectorStateStore,
    audit_sink: AuditSink | None,
    actor: str,
    transport: Transport | None = None,
    kpi_tag: str | None = None,
    quarantine: WriteQuarantine | None = None,
) -> SlackSyncOutcome:
    """Run one durable, fail-closed Slack sync; return its :class:`SlackSyncOutcome`."""
    contract = slack_data_contract(settings)

    cursor_state = {
        key.removeprefix(_CURSOR_PREFIX): value
        for key, value in state_store.load_state(contract.source).items()
        if key.startswith(_CURSOR_PREFIX)
    }
    connector = SlackConnector(settings, transport=transport, cursor_state=cursor_state)
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

    # Commit ts cursors ONLY after a successful run (durability contract).
    for channel_id, ts in connector.pending_cursors.items():
        state_store.set_cursor(contract.source, f"{_CURSOR_PREFIX}{channel_id}", ts)

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
                    f"quarantined={connector.acl_quarantined} "
                    f"fetch_failed={connector.acl_fetch_failed} "
                    f"ids={connector.acl_quarantined_ids}"
                ),
                kpi_tag=kpi_tag,
            )
        )

    return SlackSyncOutcome(
        report=report,
        acl_quarantined=connector.acl_quarantined,
        mode=mode,
        rate_limited=connector.rate_limited,
    )
