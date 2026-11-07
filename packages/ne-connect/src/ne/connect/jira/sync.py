# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""run_jira_sync — drive the Jira connector through the governed write path (NE-093).

Wires :class:`~ne.connect.jira.connector.JiraConnector` into the framework
:class:`~ne.connect.bridge.SyncRunner`, mirroring the Salesforce FIX-D shape.
Durability contract: per-project ``updated`` cursors (``updated:{key}`` ledger
keys) are committed after every SUCCESSFUL run — including runs that parked
records. A park (writer shape, contract-invalid, duplicate-identity) is a
HANDLED outcome: deterministic from the record's bytes, durable in the write
quarantine with its full payload, and cleared by the record's accepted write
once the source fixes it (the fix bumps ``updated``, so the record re-enters
the incremental stream past any cursor). A park therefore never stalls the
cursor — stalling turns one permanently-invalid record into unbounded replay
of the whole window every cron fire. Only an ABORTED run (a
``JiraSyncError``/``InvalidRecordError``) leaves the OLD cursors in place for
the resumable re-read; the dedup ledger absorbs already-written records.
``cursor_field=None`` because resumability comes from the connector's own
per-project cursor tracking + the dedup ledger.

The SyncRunner emits exactly ONE ``config_hash``-stamped ``connector.sync``
AuditEvent. Project- and item-level quarantines are surfaced as ONE additional
``connector.acl_quarantine`` ``denied`` event via the NE-005 event spine.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict

from ne.brain.write import CuratedKnowledgeItem, ProvenanceClass, WriteQuarantine
from ne.connect.bridge import SyncReport, SyncRunner
from ne.connect.contracts import OdcsDataContract
from ne.connect.jira.adf import adf_to_text
from ne.connect.jira.client import Transport
from ne.connect.jira.config import JiraConnectorSettings
from ne.connect.jira.connector import JiraConnector
from ne.connect.jira.contract import jira_data_contract
from ne.connect.state import ConnectorStateStore
from ne.contracts.governance import AuditSink
from ne.contracts.integration import Record
from ne.contracts.knowledge import KnowledgeWrite
from ne.events import new_audit_event
from ne.schema import AuditOutcome

__all__ = ["JiraSyncOutcome", "make_jira_record_mapper", "run_jira_sync"]

_CURSOR_PREFIX = "updated:"


class JiraSyncOutcome(BaseModel):
    """Immutable outcome of a single :func:`run_jira_sync`."""

    model_config = ConfigDict(frozen=True)

    report: SyncReport
    acl_quarantined: int
    items_quarantined: int
    mode: str


def _coerce(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _nested_name(value: object) -> object:
    if isinstance(value, Mapping):
        return value.get("name")
    return None


def _nested(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return value.get(key)
    return None


def make_jira_record_mapper(
    settings: JiraConnectorSettings,
) -> Callable[[Record, OdcsDataContract, tuple[str, ...]], CuratedKnowledgeItem]:
    """Build the Jira record -> document shaping mapper (source_id-prefix dispatch)."""

    def _map(
        record: Record, contract: OdcsDataContract, acl: tuple[str, ...]
    ) -> CuratedKnowledgeItem:
        item_id = hashlib.sha256(
            f"{contract.source}\n{record.source_id}\n{record.record_id}".encode()
        ).hexdigest()
        payload = record.payload
        metadata: dict[str, object] = {
            "connector": contract.source,
            "source_id": record.source_id,
            "record_id": record.record_id,
            "pii_class": contract.pii.value,
        }

        source_id = record.source_id
        if source_id.startswith("project/"):
            content = _join(payload.get("name"), payload.get("description"))
            metadata["project"] = payload.get("key")
        elif source_id.startswith("issues/"):
            content = _join(payload.get("summary"), adf_to_text(payload.get("description")))
            _stamp(
                metadata,
                {
                    "project": payload.get("project_key"),
                    "status": _nested_name(payload.get("status")),
                    "issuetype": _nested_name(payload.get("issuetype")),
                    "updated": payload.get("updated"),
                },
            )
        elif source_id.startswith("comments/"):
            content = adf_to_text(payload.get("body"))
            _stamp(
                metadata,
                {
                    "issue_key": payload.get("issue_key"),
                    "author": _nested(payload.get("author"), "accountId"),
                    "created": payload.get("created"),
                    "updated": payload.get("updated"),
                },
            )
        else:  # sprints/
            content = _join(payload.get("name"), payload.get("goal"))
            _stamp(
                metadata,
                {
                    "state": payload.get("state"),
                    "startDate": payload.get("startDate"),
                    "endDate": payload.get("endDate"),
                    "boardId": payload.get("boardId"),
                },
            )

        return CuratedKnowledgeItem(
            item_id=item_id,
            content=content,
            metadata=metadata,
            provenance=f"connector:{contract.source}:{record.record_id}",
            provenance_class=ProvenanceClass.INGESTED,
            acl=acl,
        )

    return _map


def _join(first: object, second: object) -> str:
    parts = [str(p) for p in (first, second) if p not in (None, "")]
    return "\n\n".join(parts)


def _stamp(metadata: dict[str, object], values: Mapping[str, object]) -> None:
    for meta_key, raw in values.items():
        coerced = _coerce(raw)
        if coerced not in (None, ""):
            metadata[meta_key] = coerced


async def run_jira_sync(
    settings: JiraConnectorSettings,
    *,
    writer: KnowledgeWrite,
    state_store: ConnectorStateStore,
    audit_sink: AuditSink | None,
    actor: str,
    transport: Transport | None = None,
    kpi_tag: str | None = None,
    quarantine: WriteQuarantine | None = None,
    batch_size: int = 50,
) -> JiraSyncOutcome:
    """Run one durable, fail-closed Jira sync; return its :class:`JiraSyncOutcome`."""
    contract = jira_data_contract(settings)

    cursor_state = {
        key.removeprefix(_CURSOR_PREFIX): value
        for key, value in state_store.load_state(contract.source).items()
        if key.startswith(_CURSOR_PREFIX)
    }
    connector = JiraConnector(settings, transport=transport, cursor_state=cursor_state)
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
        mapper=make_jira_record_mapper(settings),
    )
    report = await runner.run(mode=mode)  # type: ignore[arg-type]

    # Commit per-project cursors after every SUCCESSFUL run — a park is a
    # HANDLED outcome, never a cursor stall. Every park (writer shape,
    # contract-invalid, duplicate-identity) is deterministic from the record's
    # content and sits durably in the quarantine with its full payload, so
    # re-reading the same bytes next cron can only re-produce the same park;
    # gating the cursor on quarantined == 0 turned ONE permanently-invalid
    # record into unbounded replay of the whole window every fire, forever.
    # Recovery is the quarantine contract instead: fixing the record in Jira
    # bumps its ``updated``, it re-enters the incremental stream past any
    # cursor, and its accepted write clears the park (shared canonical item
    # id). A FAILED run still raises before this point, so its cursors are
    # never committed (resumability unchanged).
    for project_key, cursor in connector.pending_cursors.items():
        state_store.set_cursor(contract.source, f"{_CURSOR_PREFIX}{project_key}", cursor)

    # Re-read cursors AFTER the commit so the report is not stale (FIX-D parity).
    report = report.model_copy(update={"cursors": state_store.load_state(contract.source)})

    total_quarantined = connector.acl_quarantined + connector.items_quarantined
    if total_quarantined > 0 and audit_sink is not None:
        await audit_sink.append(
            new_audit_event(
                action="connector.acl_quarantine",
                resource=f"connector://{contract.source}",
                outcome=AuditOutcome.denied,
                actor=actor,
                detail=(
                    f"projects_quarantined={connector.acl_quarantined} "
                    f"items_quarantined={connector.items_quarantined} "
                    f"project_ids={connector.acl_quarantined_ids} "
                    f"item_ids={connector.items_quarantined_ids}"
                ),
                kpi_tag=kpi_tag,
            )
        )

    return JiraSyncOutcome(
        report=report,
        acl_quarantined=connector.acl_quarantined,
        items_quarantined=connector.items_quarantined,
        mode=mode,
    )
