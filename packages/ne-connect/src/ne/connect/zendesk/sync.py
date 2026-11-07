# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""run_zendesk_sync — drive the Zendesk connector through the governed write path (NE-093).

Wires :class:`~ne.connect.zendesk.connector.ZendeskConnector` into the framework
:class:`~ne.connect.bridge.SyncRunner`, mirroring the Salesforce FIX-D shape.
Durability contract: the tickets/articles cursors (``cursor:tickets`` /
``cursor:articles`` ledger keys) are committed after every SUCCESSFUL run —
including runs that parked records. A park (writer shape, contract-invalid,
duplicate-identity — the last is ORDINARY Zendesk pagination: a ticket updated
mid-export can appear on two pages of one incremental run) is a HANDLED
outcome: deterministic from the record's bytes, durable in the write
quarantine with its full payload, and cleared by the record's accepted write
once the source fixes it (the fix bumps ``updated_at``, so the record
re-enters the incremental stream past any cursor). A park therefore never
stalls the cursors — stalling turns one permanently-invalid record into
unbounded replay of the whole export window every cron fire. Only an ABORTED
run (a ``ZendeskSyncError``/``InvalidRecordError``) leaves the OLD cursors in
place for the resumable re-read; the dedup ledger absorbs already-written
records. ``cursor_field=None`` because resumability comes from the
connector's own cursor tracking + the dedup ledger.

The SyncRunner emits exactly ONE ``config_hash``-stamped ``connector.sync``
AuditEvent. Quarantines are surfaced as ONE additional
``connector.acl_quarantine`` ``denied`` event via the NE-005 event spine.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict

from ne.brain.write import CuratedKnowledgeItem, ProvenanceClass, WriteQuarantine
from ne.connect.bridge import SyncReport, SyncRunner
from ne.connect.contracts import OdcsDataContract
from ne.connect.state import ConnectorStateStore
from ne.connect.zendesk.client import Transport
from ne.connect.zendesk.config import ZendeskConnectorSettings
from ne.connect.zendesk.connector import ZendeskConnector
from ne.connect.zendesk.contract import zendesk_data_contract
from ne.contracts.governance import AuditSink
from ne.contracts.integration import Record
from ne.contracts.knowledge import KnowledgeWrite
from ne.events import new_audit_event
from ne.schema import AuditOutcome

__all__ = ["ZendeskSyncOutcome", "make_zendesk_record_mapper", "run_zendesk_sync"]

_CURSOR_PREFIX = "cursor:"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class ZendeskSyncOutcome(BaseModel):
    """Immutable outcome of a single :func:`run_zendesk_sync`."""

    model_config = ConfigDict(frozen=True)

    report: SyncReport
    acl_quarantined: int
    items_quarantined: int
    mode: str


def _html_to_text(html_body: object) -> str:
    """Best-effort HTML -> plain text (v1 approximation): strip tags, unescape, collapse."""
    if not isinstance(html_body, str) or not html_body:
        return ""
    stripped = _TAG_RE.sub(" ", html_body)
    unescaped = html.unescape(stripped)
    return _WS_RE.sub(" ", unescaped).strip()


def _coerce(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _stamp(metadata: dict[str, object], values: Mapping[str, object]) -> None:
    for meta_key, raw in values.items():
        coerced = _coerce(raw)
        if coerced not in (None, ""):
            metadata[meta_key] = coerced


def _join(first: object, second: object) -> str:
    parts = [str(p) for p in (first, second) if p not in (None, "")]
    return "\n\n".join(parts)


def make_zendesk_record_mapper(
    settings: ZendeskConnectorSettings,
) -> Callable[[Record, OdcsDataContract, tuple[str, ...]], CuratedKnowledgeItem]:
    """Build the Zendesk record -> document shaping mapper (source_id/record_id dispatch)."""

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

        if record.source_id.startswith("article/"):
            content = _join(payload.get("title"), _html_to_text(payload.get("body")))
            _stamp(
                metadata,
                {
                    "locale": payload.get("locale"),
                    "section_id": payload.get("section_id"),
                    "updated_at": payload.get("updated_at"),
                },
            )
        elif record.record_id.startswith("comment-"):
            body = payload.get("body")
            content = str(body) if body not in (None, "") else ""
            _stamp(
                metadata,
                {
                    "ticket_id": payload.get("ticket_id"),
                    "public": payload.get("public"),
                    "author_id": payload.get("author_id"),
                    "created_at": payload.get("created_at"),
                },
            )
        else:  # ticket
            content = _join(payload.get("subject"), payload.get("description"))
            _stamp(
                metadata,
                {
                    "status": payload.get("status"),
                    "priority": payload.get("priority"),
                    "created_at": payload.get("created_at"),
                    "updated_at": payload.get("updated_at"),
                    "group_id": payload.get("group_id"),
                    "organization_id": payload.get("organization_id"),
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


async def run_zendesk_sync(
    settings: ZendeskConnectorSettings,
    *,
    writer: KnowledgeWrite,
    state_store: ConnectorStateStore,
    audit_sink: AuditSink | None,
    actor: str,
    transport: Transport | None = None,
    kpi_tag: str | None = None,
    quarantine: WriteQuarantine | None = None,
    batch_size: int = 50,
) -> ZendeskSyncOutcome:
    """Run one durable, fail-closed Zendesk sync; return its :class:`ZendeskSyncOutcome`."""
    contract = zendesk_data_contract(settings)

    cursor_state = {
        key.removeprefix(_CURSOR_PREFIX): value
        for key, value in state_store.load_state(contract.source).items()
        if key.startswith(_CURSOR_PREFIX)
    }
    connector = ZendeskConnector(settings, transport=transport, cursor_state=cursor_state)
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
        mapper=make_zendesk_record_mapper(settings),
    )
    report = await runner.run(mode=mode)  # type: ignore[arg-type]

    # Commit stream cursors after every SUCCESSFUL run — a park is a HANDLED
    # outcome, never a cursor stall. Every park (writer shape,
    # contract-invalid, duplicate-identity — the last is ORDINARY Zendesk
    # pagination: a ticket updated mid-export can appear on two pages of one
    # incremental run) is deterministic from the record's content and sits
    # durably in the quarantine with its full payload, so re-reading the same
    # bytes next cron can only re-produce the same park; gating the cursor on
    # quarantined == 0 turned ONE permanently-invalid record into unbounded
    # replay of the whole window every fire, forever. Recovery is the
    # quarantine contract instead: fixing the record in Zendesk bumps its
    # ``updated_at``, it re-enters the incremental stream past any cursor, and
    # its accepted write clears the park (shared canonical item id). A FAILED
    # run still raises before this point, so its cursors are never committed
    # (resumability unchanged).
    for stream, cursor in connector.pending_cursors.items():
        state_store.set_cursor(contract.source, f"{_CURSOR_PREFIX}{stream}", cursor)

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
                    f"objects_quarantined={connector.acl_quarantined} "
                    f"items_quarantined={connector.items_quarantined} "
                    f"object_ids={connector.acl_quarantined_ids} "
                    f"item_ids={connector.items_quarantined_ids}"
                ),
                kpi_tag=kpi_tag,
            )
        )

    return ZendeskSyncOutcome(
        report=report,
        acl_quarantined=connector.acl_quarantined,
        items_quarantined=connector.items_quarantined,
        mode=mode,
    )
