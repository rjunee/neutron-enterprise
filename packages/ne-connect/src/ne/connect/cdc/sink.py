# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The governed CDC sink service (NE-084) — fail-closed ACL ingestion.

:class:`CdcSinkService` maps parsed Debezium change events onto the EXISTING
governed NE-023 ``KnowledgeWrite`` path — the SAME chokepoint the polling
connectors use. A CDC event is never an ungoverned back-door:

* INSERT / UPDATE / snapshot-read (``op`` in ``{"c", "u", "r"}``) → a
  :class:`~ne.brain.write.CuratedKnowledgeItem` written via ``writer.upsert``
  with the row's ACL derived by the SQL-connector-identical
  :func:`ne.connect.cdc.acl.resolve_row_principals`. An unresolvable / tainted /
  zero-principal ACL is FAIL-CLOSED: the row is parked in the quarantine lane
  and NEVER written; if the row previously existed (an UPDATE whose ACL turned
  unresolvable) the stale chunk is ALSO removed so it cannot linger as readable.
* DELETE (``op == "d"``) → ``writer.quarantine`` (park-then-delete); a delete of
  a never-ingested row is an idempotent no-op (no wedge, no error to Debezium).
* an UNKNOWN table, a MISSING key column, or an UNSUPPORTED op (``t`` truncate,
  ``m`` message, …) is parked in the lane and ACK'd — never a 4xx, because a
  non-2xx wedges Debezium's retry loop forever.

Every quarantine emits a ``cdc.quarantine`` ``denied`` audit event; each
``apply`` emits ONE summary ``cdc.ingest`` event carrying COUNTS ONLY — never a
row value, a principal, or a key. Item identity is a hex SHA-256 (no ``':'``, so
it can never collide with an NE-026 spine chunk id), keyed on the row's
``key_columns`` so an UPDATE replaces — and a DELETE removes — the SAME chunk an
INSERT created.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from ne.brain.write import (
    CuratedKnowledgeItem,
    KnowledgeItemNotFoundError,
    ProvenanceClass,
    QuarantinedWrite,
    WriteQuarantine,
)
from ne.connect.bridge import canonical_payload_json
from ne.connect.cdc.acl import resolve_row_principals
from ne.connect.cdc.config import CdcSinkConfig, CdcTableConfig
from ne.connect.cdc.envelope import CdcChange
from ne.contracts.governance import AuditSink
from ne.contracts.knowledge import KnowledgeWrite
from ne.events import new_audit_event
from ne.schema import AuditOutcome

__all__ = [
    "CdcApplyReport",
    "CdcSinkService",
    "ingest_detail",
    "written_from_ingest_detail",
]

_WRITE_OPS = frozenset({"c", "u", "r"})
_DELETE_OP = "d"

#: The EXACT counts-only shape of a ``cdc.ingest`` audit detail — the producer/
#: consumer contract between :func:`ingest_detail` (this sink's emission) and
#: :func:`written_from_ingest_detail` (ne-api's connect-status derivation).
#: Anchored via ``fullmatch``: a substring hit inside foreign text is NOT this
#: sink's summary and must not be read as one.
_INGEST_DETAIL_RE = re.compile(
    r"received=(\d+) written=(\d+) removed=(\d+) quarantined=(\d+) ignored=(\d+)"
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class CdcApplyReport(BaseModel):
    """Immutable per-``apply`` outcome (counts only — never row data)."""

    model_config = ConfigDict(frozen=True)

    received: int = 0
    written: int = 0
    removed: int = 0
    quarantined: int = 0
    ignored: int = 0


def ingest_detail(report: CdcApplyReport) -> str:
    """The counts-only ``cdc.ingest`` audit detail for one ``apply`` batch.

    This string is a PARSED contract, not just prose: ``ne.api.connect_status``
    derives the CDC lane's newest-record freshness from the ``written`` count it
    carries (a batch advances "Newest record" ONLY when it accepted a record).
    :func:`written_from_ingest_detail` is the strict inverse; the pair round-trips
    by test (``tests/test_cdc_sink_service.py``), so a format edit here that the
    parser cannot read goes RED instead of silently blinding the status surface.
    Counts only — never a row value, a principal, or a key.
    """
    return (
        f"received={report.received} written={report.written} removed={report.removed} "
        f"quarantined={report.quarantined} ignored={report.ignored}"
    )


def written_from_ingest_detail(detail: str | None) -> int | None:
    """The ``written`` count carried by a ``cdc.ingest`` detail, else ``None``.

    Strict inverse of :func:`ingest_detail`: the WHOLE detail must be the counts
    shape (anchored ``fullmatch`` — a prefix/suffix around it, a bare
    ``written=N`` fragment, or any foreign/hand-emitted detail returns ``None``).
    ``None`` means "not known to have accepted anything": the consumer fails
    toward honest absence, never toward fabricated freshness.
    """
    if detail is None:
        return None
    match = _INGEST_DETAIL_RE.fullmatch(detail)
    return int(match.group(2)) if match else None


class CdcSinkService:
    """Apply parsed Debezium changes to the governed KnowledgeWrite path."""

    def __init__(
        self,
        writer: KnowledgeWrite,
        quarantine: WriteQuarantine,
        config: CdcSinkConfig,
        *,
        audit_sink: AuditSink | None = None,
        actor: str = "cdc:debezium",
        kpi_tag: str | None = None,
    ) -> None:
        self._writer = writer
        self._quarantine = quarantine
        self._config = config
        self._audit_sink = audit_sink
        self._actor = actor
        self._kpi_tag = kpi_tag

    async def apply(self, changes: Sequence[CdcChange]) -> CdcApplyReport:
        received = written = removed = quarantined = ignored = 0
        acl_failed = False

        for change in changes:
            received += 1
            table_cfg = self._config.tables.get(change.table)
            if table_cfg is None:
                await self._park(
                    item_id=self._synthetic_id(change.table, "unknown-table"),
                    reason="unknown-table",
                    table=change.table,
                )
                quarantined += 1
                continue

            # An op that is neither a write (c/u/r) nor a delete (d) — 't' truncate,
            # 'm' message, or anything unknown — carries no per-row identity we can
            # govern. Fail-closed park + ACK (never a 4xx that would wedge Debezium).
            if change.op not in _WRITE_OPS and change.op != _DELETE_OP:
                await self._park(
                    item_id=self._synthetic_id(change.table, f"unsupported-op:{change.op}"),
                    reason=f"unsupported-op:{change.op}",
                    table=change.table,
                )
                quarantined += 1
                continue

            row = change.after if change.op in _WRITE_OPS else change.before
            record_id = self._record_id(row, table_cfg)
            if record_id is None:
                await self._park(
                    item_id=self._synthetic_id(change.table, "missing-key-column"),
                    reason="missing-key-column",
                    table=change.table,
                )
                quarantined += 1
                continue

            item_id = self._item_id(change.table, record_id)

            if change.op == _DELETE_OP:
                try:
                    await self._writer.quarantine(item_id, reason="cdc-delete")
                except KnowledgeItemNotFoundError:
                    ignored += 1
                else:
                    removed += 1
                continue

            assert row is not None  # narrowed by _record_id; op is a write op here
            acl, acl_reason = self._resolve_acl(row, table_cfg)
            if not acl:
                acl_failed = True
                await self._park(item_id=item_id, reason=acl_reason, table=change.table)
                # An UPDATE whose ACL became unresolvable must not leave the STALE
                # chunk readable — remove it (idempotent if absent).
                with contextlib.suppress(KnowledgeItemNotFoundError):
                    await self._writer.quarantine(item_id, reason=acl_reason)
                quarantined += 1
                continue
            item = self._build_item(change, table_cfg, record_id, item_id, acl)
            receipt = await self._writer.upsert([item])
            written += len(receipt.accepted)
            quarantined += len(receipt.quarantined)

        report = CdcApplyReport(
            received=received,
            written=written,
            removed=removed,
            quarantined=quarantined,
            ignored=ignored,
        )
        await self._audit(
            action="cdc.ingest",
            resource=f"cdc://{self._config.source_name}",
            # An ACK keeps Debezium moving; it is not evidence that ACL
            # resolution succeeded.  The counts remain available while the
            # terminal health event truthfully degrades the lane.
            outcome=AuditOutcome.error if acl_failed else AuditOutcome.success,
            detail=ingest_detail(report),
        )
        return report

    # -- helpers ------------------------------------------------------------
    def _record_id(self, row: Mapping[str, Any] | None, table_cfg: CdcTableConfig) -> str | None:
        """Canonical JSON of the row's key columns, or ``None`` if unresolvable."""
        if row is None:
            return None
        try:
            key = {col: row[col] for col in table_cfg.key_columns}
        except KeyError:
            return None
        return canonical_payload_json(key)

    def _item_id(self, table: str, record_id: str) -> str:
        return hashlib.sha256(
            f"{self._config.source_name}\n{table}\n{record_id}".encode()
        ).hexdigest()

    def _synthetic_id(self, table: str, tag: str) -> str:
        return hashlib.sha256(f"{self._config.source_name}\n{table}\n{tag}".encode()).hexdigest()

    def _resolve_acl(
        self, row: Mapping[str, Any], table_cfg: CdcTableConfig
    ) -> tuple[tuple[str, ...], str]:
        """Fail-closed ACL: static tuple, or per-row column resolution.

        Returns ``(principals, reason)``. ``principals`` is empty whenever the
        ACL is unresolvable / tainted / zero-principal — the caller quarantines
        on an empty result. ``reason`` is a SAFE, categorized label that never
        echoes a row value (the underlying ``RowAclOutcome.reason`` can carry the
        forged reserved-label value, which must never enter a lane reason / audit
        detail — the C7/C12 no-row-data invariant).
        """
        acl_cfg = table_cfg.acl
        if acl_cfg.principal_column is not None:
            outcome = resolve_row_principals(
                row.get(acl_cfg.principal_column), separator=acl_cfg.principal_separator
            )
            if outcome.tainted:
                return (), "cdc-acl-tainted"
            if not outcome.principals:
                return (), "cdc-acl-empty"
            return outcome.principals, "ok"
        # Static mode: the config validator guarantees a non-empty tuple, but
        # stay fail-closed defensively.
        if not acl_cfg.principals:
            return (), "cdc-acl-empty"
        return acl_cfg.principals, "ok"

    def _build_item(
        self,
        change: CdcChange,
        table_cfg: CdcTableConfig,
        record_id: str,
        item_id: str,
        acl: tuple[str, ...],
    ) -> CuratedKnowledgeItem:
        row = change.after
        assert row is not None
        if table_cfg.content_columns:
            projected: dict[str, Any] = {
                col: row[col] for col in table_cfg.content_columns if col in row
            }
        else:
            projected = dict(row)
        return CuratedKnowledgeItem(
            item_id=item_id,
            content=canonical_payload_json(projected),
            metadata={
                "connector": f"cdc:{self._config.source_name}",
                "source_id": change.table,
                # Hash the record_id so no raw key VALUE ever enters metadata.
                "record_id": hashlib.sha256(record_id.encode()).hexdigest(),
                "pii_class": table_cfg.pii_class.value,
            },
            provenance=f"cdc:{self._config.source_name}:{change.table}",
            provenance_class=ProvenanceClass.INGESTED,
            acl=acl,
        )

    async def _park(self, *, item_id: str, reason: str, table: str) -> None:
        self._quarantine.record(
            QuarantinedWrite(
                item_id=item_id,
                reason=reason,
                payload={"table": table},
                provenance=f"cdc:{self._config.source_name}",
                shape="",
                created_at=_now_iso(),
            )
        )
        await self._audit(
            action="cdc.quarantine",
            resource=f"cdc://{self._config.source_name}/{table}",
            outcome=AuditOutcome.denied,
            detail=reason,
        )

    async def _audit(
        self, *, action: str, resource: str, outcome: AuditOutcome, detail: str
    ) -> None:
        if self._audit_sink is None:
            return
        await self._audit_sink.append(
            new_audit_event(
                action=action,
                resource=resource,
                outcome=outcome,
                actor=self._actor,
                detail=detail,
                kpi_tag=self._kpi_tag,
            )
        )
