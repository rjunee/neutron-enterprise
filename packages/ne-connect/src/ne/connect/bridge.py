# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The connector→brain sync bridge (NE-040).

:class:`SyncRunner` drives a :class:`~ne.contracts.integration.Connector` into
the governed NE-023 ``KnowledgeWrite`` path: each source ``Record`` becomes a
:class:`~ne.brain.write.CuratedKnowledgeItem` (ACL-stamped, provenance-classed
``ingested``, PII-classed), validated against the source's ODCS schema, dedup-
gated by content hash, and written in durable batches with resumable per-batch
cursor persistence. Exactly ONE config_hash-stamped ``connector.sync``
``AuditEvent`` is emitted per run that ATTEMPTED the source — see
:meth:`SyncRunner.run` for the single exit (a scope another reconciler
already holds) that attempts nothing and therefore states nothing.

Fail-closed discipline: an ODCS-invalid record is COUNTED and skipped (NE-026
per-item-never-aborts-batch) — and, when the runner is handed the write-
quarantine lane, PARKED there inspectably with the CONTRACT VALIDATOR'S OWN
messages as the reason (never a connector-authored string); an unresolvable ACL
raises ``AclResolutionError`` and stops the run LOUDLY (after a
``denied``-outcome sync audit event), while already-flushed durable batches
remain for the next resumable run.

Duplicate identity (NE-CONNECT-CSV): a record whose ``(source_id, record_id)``
identity was already seen IN THE SAME RUN is never written and never guessed
about — two rows claiming the same identity with different content is a source
data-integrity fault, and last-write-wins would silently pick one. The earliest
occurrence proceeds into NORMAL processing (it may be written, parked invalid,
or dedup-skipped — being first confers no validity); every later one is
rejected fail-closed (parked with a ``duplicate-identity:`` reason when the
quarantine lane is present). All later occurrences of one identity share ONE
stable park id, so the park's payload is the last occurrence seen — stated in
the park reason rather than implied. Without this check a duplicated identity
reached the writer as two items in one batch and aborted the WHOLE run with
``ValueError`` — a single duplicated key in a hand-maintained source killed
the sync.

Snapshot reconciliation (``snapshot=True``): for a source whose stream is a FULL
snapshot of the system (a polled file — every record present, every absence
meaningful), a parked row whose identity no longer appears in the stream is
REMOVED from the quarantine at the end of the run: the failing source row was
fixed under a new identity or deleted, so keeping the park would report a
failure the source no longer contains. An EMPTY snapshot (zero records) never
reconciles — absence of everything is indistinguishable from a producer's
truncate-then-rewrite race, so it is not evidence of anything. Guarded to this
connector's provenance prefix, and ``unwound:`` parks (writer audit-unwind
rescue payloads — data NOT in the store) are never reconciled away. Requires the canonical
``sha256(source\\nsource_id\\nrecord_id)`` item-id convention (the
``default_record_mapper`` / sql-mapper formula).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ne.brain.current_source_authority import SourceReconciliationScopeBusyError
from ne.brain.write import (
    CuratedKnowledgeItem,
    KnowledgeItemNotFoundError,
    ProvenanceClass,
    QuarantinedWrite,
    ShapeContract,
    WriteQuarantine,
)
from ne.connect.acl import AclMapper, build_acl_index
from ne.connect.authority_admission import (
    AdmissionPreparation,
    PermissionReadEvidence,
    PullAuthorityAdmission,
)
from ne.connect.contracts import OdcsDataContract
from ne.connect.errors import AclResolutionError
from ne.connect.identity import (
    SOURCE_ACL_METADATA_KEY,
    SOURCE_SNAPSHOT_METADATA_KEY,
    source_acl_hash,
    source_ref_for,
    source_snapshot_hash,
)
from ne.connect.state import ConnectorStateStore
from ne.contracts.failure import failure_detail
from ne.contracts.frozen import StrMap
from ne.contracts.governance import AuditSink
from ne.contracts.integration import Connector, Record
from ne.contracts.knowledge import KnowledgeWrite
from ne.contracts.source_authority import (
    NO_SOURCE_STATUS,
    SOURCE_AUTHORITY_STAMP_METADATA_KEY,
    SOURCE_STATUS_METADATA_KEY,
    CurrentSourceAuthorityReceipt,
    SourceAuthorityStamp,
    stamp_from_receipt,
)
from ne.contracts.types import Chunk
from ne.events import new_audit_event
from ne.schema import AuditOutcome

#: The stamp fields that are AUTHORITY TRUTH. Deliberately excludes the receipt
#: id/hash: a permission sweep mints a fresh observation receipt for byte-identical
#: source/artifact/revision/ACL/contract truth, and
#: ``SqlAlchemyCurrentSourceAuthorityReader`` keeps the older authentic stamp
#: authorized against it — receipts are append-only, so the one a retained stamp
#: names stays loadable forever. Comparing the WHOLE stamp here would therefore
#: see a difference on every sweep and rewrite every chunk for no readable change.
_STAMP_TRUTH_FIELDS = frozenset(
    {
        "source_identity",
        "artifact_identity",
        "selected_head_revision",
        "acl_snapshot_hash",
        "source_contract",
        "contract_epoch",
    }
)


def _same_stamp_truth(retained: object, current: SourceAuthorityStamp) -> bool:
    """True iff a chunk's retained stamp still names ``current``'s admitted truth."""
    try:
        parsed = SourceAuthorityStamp.model_validate(retained)
    except (TypeError, ValueError):
        # Unreadable means unavailable — restamp rather than leave it refused.
        return False
    keep = {"include": _STAMP_TRUTH_FIELDS, "mode": "json"}
    return parsed.model_dump(**keep) == current.model_dump(**keep)  # type: ignore[arg-type]


#: Narrow READ seam over the composed chunk store: ``item_id -> Chunk | None``.
#: Deliberately a bare callable rather than the ``ChunkStore`` protocol — the
#: bridge is a write path and must not acquire query, delete or bulk-read reach
#: just to ask whether one item already carries the stamp it is about to apply.
ChunkReader = Callable[[str], Awaitable["Chunk | None"]]

#: The mapping-decision revision of the SERVER-AUTHORED metadata shape
#: :func:`_with_source_identity` writes. A source-free connector passes it as its
#: ``mapper_revision`` so the dedup content hash changes when the shape does, and
#: the next ordinary governed sync REWRITES every retained record instead of
#: reporting it ``skipped_unchanged``. That is the governed backfill: no operator
#: writes a row, no state file is deleted, and the run is idempotent again
#: afterwards because the new hash is what the ledger then holds.
#:
#: Bump on any change to the keys :func:`_with_source_identity` authors.
#: ``2`` adds ``source_status`` (015A0-ENFORCEMENT: chunks written before it
#: carry no source-authority class and are correctly refused until rewritten).
SOURCE_IDENTITY_MAPPER_REVISION = "ne.connect.source-identity@2"

#: The shape contract every connector record is validated against by the writer.
CONNECTOR_RECORD_SHAPE = ShapeContract(
    name="ne.connect.record",
    version="1",
    json_schema={
        "type": "object",
        "required": ["content"],
        "properties": {
            "content": {"type": "string", "minLength": 1},
            "metadata": {"type": "object"},
        },
    },
)


def _to_plain(value: Any) -> Any:
    """Recursively convert a frozen/JSON-shaped value into plain ``dict``/``list``."""
    if isinstance(value, Mapping):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_plain(v) for v in value]
    return value


def canonical_payload_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON of a record payload (sorted keys) for content + hashing."""
    return json.dumps(_to_plain(payload), sort_keys=True, ensure_ascii=False, default=str)


def _duplicate_park_id(source: str, source_id: str, record_id: str) -> str:
    """A distinct, stable park id for a duplicate occurrence of an identity.

    Distinct from :func:`canonical_item_id` (the FIRST occurrence's id) so the
    writer's accept-path ``quarantine.remove`` of the accepted first row can
    never delete the duplicate's park; stable across runs so a persisting
    duplicate re-parks the SAME row (idempotent count).
    """
    return hashlib.sha256(f"{source}\n{source_id}\n{record_id}\nduplicate".encode()).hexdigest()


def canonical_item_id(source: str, source_id: str, record_id: str) -> str:
    """The canonical connector item id: hex sha256 of the identity triple.

    Shared by :func:`default_record_mapper`, the sql mapper, and the bridge's
    invalid-record park path — the park and the eventual accepted write MUST
    share an id so a fixed record's successful upsert clears its parked row
    (the writer's accept path calls ``quarantine.remove(item_id)``).
    """
    return hashlib.sha256(f"{source}\n{source_id}\n{record_id}".encode()).hexdigest()


async def clear_stale_source_links(
    writer: KnowledgeWrite,
    state_store: ConnectorStateStore,
    *,
    source: str,
    mapper_revision: str,
) -> None:
    """Clear locators on prior identities omitted by an authority refresh.

    Deleted or newly ACL-quarantined records do not reach the mapper. Their old
    content may be governed by a separate retention policy, but an obsolete
    external locator must not survive a source-link authority change.
    """
    for source_id, record_id in state_store.stale_mapper_refresh_identities(
        source, mapper_revision
    ):
        item_id = canonical_item_id(source, source_id, record_id)
        try:
            await writer.curate(
                item_id,
                {"metadata": {"source_url": None}},
                provenance=f"connector:{source}:source-link-reconcile",
            )
        except KnowledgeItemNotFoundError:
            # Dedup state can outlive a store retention/deletion. There is no
            # locator to clear and no client-visible existence signal to emit.
            continue


def _source_identity_metadata(
    record: Record, contract: OdcsDataContract, acl: tuple[str, ...]
) -> dict[str, str]:
    """Return the citation identity shared with Tier-2 graph evidence.

    ``source_ref`` deliberately mirrors ``ne.brain_graph.model.node_source_ref``
    without importing the graph package into the connector write boundary. The
    source-native pair remains readable and stable while ``item_id`` keeps its
    existing opaque SHA-256 identity for storage, dedup, and quarantine.
    """
    return {
        "source_system": contract.source,
        "source_ref": source_ref_for(record),
        SOURCE_ACL_METADATA_KEY: source_acl_hash(acl),
        SOURCE_SNAPSHOT_METADATA_KEY: source_snapshot_hash(contract.source, record),
    }


def _with_source_identity(
    item: CuratedKnowledgeItem,
    record: Record,
    contract: OdcsDataContract,
    *,
    source_free: bool,
) -> CuratedKnowledgeItem:
    """Enforce canonical citation identity for default and custom mappers.

    SQL, Salesforce, Jira, and Zendesk supply custom document-shaping mappers.
    Normalizing once at the shared runner boundary keeps those sibling paths
    aligned with :func:`default_record_mapper` without changing their content,
    item identity, governance fields, or connector-specific metadata.

    This is ALSO the one place ``source_status`` is decided. ``source_free`` comes
    from the code-authored ``SYNC_SOURCES`` disposition by way of
    :func:`~ne.connect.runner.run_configured_sync`; a mapper or a record payload
    that set the key itself is OVERWRITTEN when the grant exists and STRIPPED when
    it does not, so no source can label itself outside the authority gate. Absent
    the key, the read path treats the record as source-bearing and refuses it
    without a stamp — the fail-closed direction.
    """
    identity = _source_identity_metadata(record, contract, item.acl)
    metadata = {key: value for key, value in item.metadata.items()}
    metadata.pop(SOURCE_STATUS_METADATA_KEY, None)
    # A stamp is applied from the admission receipt AFTER this write lands
    # (:meth:`SyncRunner._stamp_source_authority`); one presented by a mapper is
    # dropped here so the writer's refusal can never abort a whole batch over a
    # value the server was going to overwrite anyway.
    metadata.pop(SOURCE_AUTHORITY_STAMP_METADATA_KEY, None)
    metadata.update(identity)
    if source_free:
        metadata[SOURCE_STATUS_METADATA_KEY] = NO_SOURCE_STATUS
    if metadata == dict(item.metadata):
        return item
    return CuratedKnowledgeItem.model_validate({**item.model_dump(), "metadata": metadata})


def default_record_mapper(
    record: Record, contract: OdcsDataContract, acl: tuple[str, ...]
) -> CuratedKnowledgeItem:
    """Map a source record to a governed ``CuratedKnowledgeItem``.

    ``item_id`` is a hex SHA-256 (never contains ``':'``, respecting the
    KnowledgeWriter spine-namespace rejection). ``source_system`` and the
    graph-equal ``source_id:record_id`` ``source_ref`` remain readable citation
    metadata. The record payload is stored as canonical JSON ``content``;
    governance attributes (``acl``, provenance class ``ingested``, ``pii_class``
    metadata) are stamped as TYPED fields — the reserved ``'source'`` /
    ``'provenance'`` metadata keys are never used.
    """
    item_id = canonical_item_id(contract.source, record.source_id, record.record_id)
    return CuratedKnowledgeItem(
        item_id=item_id,
        content=canonical_payload_json(record.payload),
        metadata={
            "connector": contract.source,
            "source_id": record.source_id,
            "record_id": record.record_id,
            **_source_identity_metadata(record, contract, acl),
            "pii_class": contract.pii.value,
        },
        provenance=f"connector:{contract.source}:{record.record_id}",
        provenance_class=ProvenanceClass.INGESTED,
        acl=acl,
    )


class SyncReport(BaseModel):
    """Immutable outcome of a single :meth:`SyncRunner.run`.

    ``invalid`` counts records REJECTED BEFORE the write path: ODCS-contract
    failures plus same-run duplicate identities. ``quarantined`` counts records
    PARKED in the write-quarantine lane this run — the writer's shape rejections
    plus, when the runner holds the quarantine lane, the parked forms of those
    same invalid/duplicate records (so one bad record can appear in both
    counts; ``quarantined`` is "rows an operator can open in the quarantine",
    ``invalid`` is "rows the contract refused").
    """

    model_config = ConfigDict(frozen=True)

    mode: str
    records_seen: int
    written: int
    skipped_unchanged: int
    invalid: int
    quarantined: int
    cursors: StrMap


@dataclass
class _Buffered:
    item: CuratedKnowledgeItem
    record: Record
    content_hash: str
    cursor_value: str | None
    #: The prepared immutable artifact for this record, when the runner holds the
    #: authority seam. It is created BEFORE the governed write and finalized only
    #: for an ACCEPTED item, so a quarantined or failed write leaves an
    #: unreferenced artifact rather than a selected one.
    admission: AdmissionPreparation | None = None


class SyncRunner:
    """Bridge a connector's records into the governed KnowledgeWrite path."""

    def __init__(
        self,
        connector: Connector,
        contract: OdcsDataContract,
        writer: KnowledgeWrite,
        state_store: ConnectorStateStore,
        *,
        actor: str,
        audit_sink: AuditSink | None = None,
        mapper: Callable[
            [Record, OdcsDataContract, tuple[str, ...]], CuratedKnowledgeItem
        ] = default_record_mapper,
        cursor_field: str | None = None,
        batch_size: int = 50,
        kpi_tag: str | None = None,
        quarantine: WriteQuarantine | None = None,
        snapshot: bool = False,
        mapper_revision: str = "",
        source_free: bool = False,
        before_success: Callable[[SyncReport], Awaitable[None]] | None = None,
        authority: PullAuthorityAdmission | None = None,
        permission_evidence: Mapping[str, PermissionReadEvidence] | None = None,
        chunk_reader: ChunkReader | None = None,
    ) -> None:
        self._connector = connector
        self._contract = contract
        self._writer = writer
        self._state_store = state_store
        self._actor = actor
        self._audit_sink = audit_sink
        self._mapper = mapper
        self._cursor_field = cursor_field
        self._batch_size = batch_size
        self._kpi_tag = kpi_tag
        self._quarantine = quarantine
        self._snapshot = snapshot
        self._mapper_revision = mapper_revision
        #: The registry-granted source-free capability (see
        #: :func:`_with_source_identity`). Fail-closed default: without the grant
        #: every record is written source-bearing and is refused at read until a
        #: source-authority stamp exists for it.
        self._source_free = source_free
        self._before_success = before_success
        self._authority = authority
        #: The connector's completed permission reads, keyed by ``source_id``. A
        #: record with no entry here was never evidenced and therefore cannot
        #: advance authority, however fresh its content looks.
        # ``is None``, never ``or {}``: the connector hands over its LIVE mapping,
        # which is empty at construction and filled during the crawl. Truthiness
        # here would swap in a private copy and silently admit nothing.
        self._permission_evidence: Mapping[str, PermissionReadEvidence] = (
            {} if permission_evidence is None else permission_evidence
        )
        #: Narrow READ seam over the same composed chunk store the writer writes
        #: to. It exists for exactly one question — does this item already carry
        #: the stamp this admission would apply — so an unchanged record's
        #: re-admission does not rewrite and re-audit an identical chunk on every
        #: sweep. ``None`` means the question cannot be asked, so the stamp is
        #: applied unconditionally: correct, just noisier.
        self._chunk_reader = chunk_reader

    async def run(self, *, mode: Literal["backfill", "incremental"]) -> SyncReport:
        """Run one sync and emit exactly one truthful terminal health event.

        The shared runner is the only seam all pull connectors traverse while
        talking to their source and writer.  Success, ACL refusal, and every
        other failure therefore terminate here as distinct audit outcomes.

        ONE exit writes no event at all: a run that could not claim its
        reconciliation scope because another reconciler holds it never reached
        the source, so it has no health fact to state.  See the
        ``SourceReconciliationScopeBusyError`` branch.

        A failure detail carries its mode, its class, and — when the fault came
        from a database driver — the driver's own error CODE (a SQLSTATE, or a
        ``SQLITE_*`` name) plus the wrapped origin class. Those are closed
        code-authored identifiers, and they are the difference between a
        schedulable diagnosis and ``error_class=OperationalError``, which names
        connection-refused, deadlock, lock/statement timeout and sqlite-readonly
        alike. Provider MESSAGES still never enter the ledger: they can contain
        credentials, URLs, or source data. See :mod:`ne.contracts.failure`.
        """
        try:
            report, reconciled = await self._run(mode=mode)
            if self._before_success is not None:
                await self._before_success(report)
        except SourceReconciliationScopeBusyError:
            # NOT ATTEMPTED, so NOT a health fact — emit NOTHING and re-raise.
            #
            # This is the one exit from the runner that learned nothing about the
            # source. Another reconciler holds the scope: the crawl never reached
            # the source, so the run has no evidence that it is healthy OR that it
            # is unhealthy, and the terminal event this method exists to write
            # would be a claim it cannot support.
            #
            # It is not a cosmetic distinction. ``connect_status.sync_source_
            # read_health`` grants a short run of plain ``error`` events a
            # served-but-stale grace, but at ``TRANSIENT_SYNC_FAILURE_LIMIT``
            # consecutive failures (or on any ``denied``) ``brain.source_health.
            # chunk_source_is_healthy`` withholds EVERY chunk carrying this
            # connector's stamp. A stream of ``error`` events written here for
            # runs that never reached the source would therefore still darken
            # the whole corpus — measured live on 2026-08-27 under the older
            # latest-event-only rule, where one crawl killed mid-flight at
            # 10:06:02 left its lease standing and the ``*/2`` sweep wrote seven
            # consecutive ``error`` events into it (10:08:02 → 10:20:02), during
            # which /v1/brain/search returned zero m365 chunks.
            #
            # Silence keeps the read honest in the other direction too: the
            # connector's freshness keeps ageing from the last run that actually
            # swept, so a skipped firing cannot pass itself off as a successful
            # empty one either. ``run_m365_sync`` catches the same class and
            # returns ``scope_busy=True``.
            #
            # ONLY this subclass is silent. A bare SourceAuthorityClaimConflict-
            # Error means the caller's view of authority is WRONG (the expected
            # head moved, a racing inserter won) and falls through to the error
            # branch below, degrading the source exactly as before.
            raise
        except AclResolutionError as exc:
            await self._emit_sync_audit(
                mode=mode,
                outcome=AuditOutcome.denied,
                detail=failure_detail(mode, exc),
            )
            raise
        except Exception as exc:
            await self._emit_sync_audit(
                mode=mode,
                outcome=AuditOutcome.error,
                detail=failure_detail(mode, exc),
            )
            raise

        detail = (
            f"mode={mode} seen={report.records_seen} written={report.written} "
            f"skipped={report.skipped_unchanged} invalid={report.invalid} "
            f"quarantined={report.quarantined}"
        )
        if self._snapshot:
            detail += f" reconciled={reconciled}"
        await self._emit_sync_audit(mode=mode, outcome=AuditOutcome.success, detail=detail)
        return report

    async def _run(self, *, mode: Literal["backfill", "incremental"]) -> tuple[SyncReport, int]:
        """Perform the sync body; :meth:`run` alone owns terminal auditing."""
        contract = self._contract
        source = contract.source
        acl_mapper = AclMapper(contract.permissions)
        index = build_acl_index([entry async for entry in self._connector.permissions()])
        state = self._state_store.load_state(source)

        if mode == "backfill":
            stream = self._connector.backfill(state or None)
        else:
            stream = self._connector.incremental(state)

        records_seen = 0
        written = 0
        skipped_unchanged = 0
        invalid = 0
        quarantined = 0
        buffer: list[_Buffered] = []
        #: Identities seen THIS run — the duplicate-identity fail-closed check.
        seen_identities: set[tuple[str, str]] = set()
        #: Every quarantine item_id this run touched (parked, accepted, or
        #: dedup-skipped) — the snapshot-reconciliation survivor set.
        touched_item_ids: set[str] = set()

        async for record in stream:
            records_seen += 1

            identity = (record.source_id, record.record_id)
            if identity in seen_identities:
                # Fail-closed: never guess which of two same-identity rows is
                # true, and never let a duplicated key reach the writer (a
                # same-batch duplicate aborts the whole upsert with ValueError).
                invalid += 1
                if self._quarantine is not None:
                    park_id = _duplicate_park_id(source, record.source_id, record.record_id)
                    # The wording claims ONLY what is invariantly true: the
                    # earliest occurrence went through normal processing (it
                    # may itself have been written, parked invalid, or
                    # dedup-skipped), and all later occurrences share THIS one
                    # park (stable id), whose payload is the last one seen.
                    self._park(
                        park_id,
                        record,
                        reason=(
                            f"duplicate-identity: record_id {record.record_id!r} from "
                            f"source_id {record.source_id!r} appeared more than once in "
                            "one sync run; the earliest occurrence was processed on its "
                            "own merits and every later occurrence is rejected "
                            "fail-closed into this single park (payload = the last "
                            "occurrence seen)"
                        ),
                    )
                    touched_item_ids.add(park_id)
                    quarantined += 1
                continue
            seen_identities.add(identity)

            violations = contract.validate(record.payload)
            if violations:
                invalid += 1
                if self._quarantine is not None:
                    # The reason IS the contract validator's output — never a
                    # connector-authored string (NE-CONNECT-CSV honesty rule).
                    park_id = canonical_item_id(source, record.source_id, record.record_id)
                    self._park(park_id, record, reason="; ".join(violations))
                    touched_item_ids.add(park_id)
                    quarantined += 1
                continue

            canonical_bytes = canonical_payload_json(record.payload).encode()
            payload_bytes = canonical_bytes
            if self._mapper_revision:
                payload_bytes += b"\x00mapper-revision\x00" + self._mapper_revision.encode()
            content_hash = hashlib.sha256(payload_bytes).hexdigest()
            item_id = canonical_item_id(source, record.source_id, record.record_id)

            # On the authority path the AUTHORITATIVE permission entry is
            # resolved ahead of the content-dedup gate: a quiet identical sweep
            # must still prove the current ACL before anything refreshes as-of,
            # and resolving after the dedup shortcut would skip the only step
            # that can observe a contraction of access. A connector without the
            # authority seam keeps its existing skip-before-resolve order, so
            # this unit changes no sibling connector's abort semantics.
            authority = self._authority
            evidence = self._permission_evidence.get(record.source_id)
            early_acl = acl_mapper.resolve(record, index) if authority is not None else None

            if self._state_store.is_unchanged(
                source, record.source_id, record.record_id, content_hash
            ):
                skipped_unchanged += 1
                touched_item_ids.add(item_id)
                if authority is not None and early_acl is not None and evidence is not None:
                    # The unchanged payload still claim-or-loads its exact
                    # existing immutable artifact and re-admits under it. That
                    # verified artifact plus the completed permission read —
                    # never a timer, an empty poll or a blind head touch — is the
                    # only thing that may refresh as-of.
                    prepared = await authority.prepare(
                        record,
                        acl=early_acl,
                        canonical_bytes=canonical_bytes,
                        item_id=item_id,
                        evidence=evidence,
                    )
                    # The stamp is applied HERE too, not only on the write path.
                    # An unchanged payload under a CHANGED ACL re-admits through
                    # this branch (the dedup ledger keys on content alone), so a
                    # stamp only applied at write time would leave the record
                    # naming an ACL snapshot the source has since replaced — and
                    # a retained corpus that predates stamping would never
                    # acquire one at all without a forced rewrite.
                    await self._stamp_source_authority(item_id, await authority.finalize(prepared))
                continue

            acl = early_acl if early_acl is not None else acl_mapper.resolve(record, index)

            cursor_value = (
                str(record.payload[self._cursor_field])
                if self._cursor_field and self._cursor_field in record.payload
                else None
            )
            admission = (
                None
                if authority is None or evidence is None
                else await authority.prepare(
                    record,
                    acl=acl,
                    canonical_bytes=canonical_bytes,
                    item_id=item_id,
                    evidence=evidence,
                )
            )
            buffer.append(
                _Buffered(
                    item=_with_source_identity(
                        self._mapper(record, contract, acl),
                        record,
                        contract,
                        source_free=self._source_free,
                    ),
                    record=record,
                    content_hash=content_hash,
                    cursor_value=cursor_value,
                    admission=admission,
                )
            )

            if len(buffer) >= self._batch_size:
                batch_written, batch_quarantined = await self._flush(
                    source, buffer, touched_item_ids
                )
                written += batch_written
                quarantined += batch_quarantined
                buffer = []

        if buffer:
            batch_written, batch_quarantined = await self._flush(source, buffer, touched_item_ids)
            written += batch_written
            quarantined += batch_quarantined

        reconciled = self._reconcile_snapshot(source, touched_item_ids, records_seen=records_seen)

        return (
            SyncReport(
                mode=mode,
                records_seen=records_seen,
                written=written,
                skipped_unchanged=skipped_unchanged,
                invalid=invalid,
                quarantined=quarantined,
                cursors=self._state_store.load_state(source),
            ),
            reconciled,
        )

    async def _stamp_source_authority(
        self, item_id: str, receipt: CurrentSourceAuthorityReceipt
    ) -> None:
        """Apply the disclosure stamp for one point that authority just admitted.

        ORDERING IS THE CONTROL, and it is unchanged: ``finalize`` has already
        returned, so the head has moved and this item's lineage and audit records
        already landed. Only then does a stamp exist to write — the receipt id and
        hash it carries do not exist before that, which is why this cannot be
        folded into the write itself.

        ``curate`` is the right verb and is safe here: it rewrites this chunk's
        metadata in place through the governed audited path, preserving
        ``chunk_id``/``document_id`` and content, and it never touches document
        lineage — only ``upsert`` prepares and finalizes versions. So the version
        ``bind_source_authority`` bound during ``finalize`` stays current, and the
        ``_source_snapshots``/``_source_authority`` join that
        ``retained_source_authority_for_snapshot`` walks is undisturbed.

        Every failure leaves the chunk UNSTAMPED and therefore refused, which is
        the fail-closed direction. A missing item is the one non-error case: a
        record whose chunk is not in the store has nothing to stamp.
        """
        stamp = stamp_from_receipt(receipt)
        if stamp is None:
            # A non-present admission (a tombstone) has no disclosable identity.
            return
        payload = stamp.model_dump(mode="json")
        if self._chunk_reader is not None:
            existing = await self._chunk_reader(item_id)
            if existing is not None and _same_stamp_truth(
                _to_plain(existing.metadata.get(SOURCE_AUTHORITY_STAMP_METADATA_KEY)), stamp
            ):
                # A later sweep mints a fresh observation receipt for byte-identical
                # authority truth; the retained stamp stays authorized against it
                # (SqlAlchemyCurrentSourceAuthorityReader: rotation is freshness
                # evidence, not truth). Rewriting it would add one chunk write and
                # one audit row per record per sweep and change nothing.
                return
        try:
            await self._writer.curate(
                item_id,
                {"metadata": {SOURCE_AUTHORITY_STAMP_METADATA_KEY: payload}},
                provenance=f"connector:{self._contract.source}:source-authority-stamp",
            )
        except KnowledgeItemNotFoundError:
            # Retention or a quarantine can outlive the dedup ledger. There is no
            # chunk to stamp and nothing to disclose; authority is unaffected.
            return

    async def _flush(
        self, source: str, buffer: list[_Buffered], touched_item_ids: set[str]
    ) -> tuple[int, int]:
        """Upsert a batch; mark dedup + persist cursors for ACCEPTED items only."""
        by_item_id = {entry.item.item_id: entry for entry in buffer}
        receipt = await self._writer.upsert([entry.item for entry in buffer])
        touched_item_ids.update(receipt.accepted)
        touched_item_ids.update(receipt.quarantined)

        max_cursor: dict[str, str] = {}
        for item_id in receipt.accepted:
            entry = by_item_id[item_id]
            if entry.admission is not None and self._authority is not None:
                # Authority moves only after this item's governed write — and
                # therefore its lineage and audit records — actually succeeded.
                await self._stamp_source_authority(
                    item_id, await self._authority.finalize(entry.admission)
                )
            self._state_store.mark(
                source, entry.record.source_id, entry.record.record_id, entry.content_hash
            )
            if entry.cursor_value is not None:
                sid = entry.record.source_id
                current = max_cursor.get(sid)
                if current is None or entry.cursor_value > current:
                    max_cursor[sid] = entry.cursor_value

        for sid, cursor in max_cursor.items():
            self._state_store.set_cursor(source, sid, cursor)

        return len(receipt.accepted), len(receipt.quarantined)

    def _park(self, item_id: str, record: Record, *, reason: str) -> None:
        """Park one rejected record inspectably (full payload, never dropped).

        Provenance follows the mapper convention ``connector:<source>:<record_id>``
        so the /connect status + drill-down surfaces attribute the park to this
        connector's lane (``count_with_prefix``).
        """
        assert self._quarantine is not None  # caller-guarded
        self._quarantine.record(
            QuarantinedWrite(
                item_id=item_id,
                reason=reason,
                payload=_to_plain(record.payload),
                provenance=f"connector:{self._contract.source}:{record.record_id}",
                shape=f"odcs:{self._contract.source}",
                created_at=datetime.now(UTC).isoformat(),
            )
        )

    def _reconcile_snapshot(
        self, source: str, touched_item_ids: set[str], *, records_seen: int
    ) -> int:
        """Snapshot mode: clear parks whose identity vanished from the stream.

        Only fires when ``snapshot=True`` AND the quarantine lane is present
        AND the stream carried at least one record. An EMPTY snapshot (zero
        data rows — a truncated or header-only file) is never treated as
        authoritative absence: a producer that truncates-then-rewrites its
        export would otherwise open a false-clean window in which every park
        is wrongly cleared and the ops view reports the defects gone. Parks
        kept on an empty read are at worst stale-but-visible (fail-safe); they
        reconcile normally on the next non-empty snapshot. Scope is further
        fail-safe on three axes: only THIS connector's provenance prefix, only
        rows untouched by this run, and never an ``unwound:`` park (a writer
        audit-unwind rescue payload — that data is NOT in the store, so
        removing its park would lose it). Returns the number removed.
        """
        if not self._snapshot or self._quarantine is None or records_seen == 0:
            return 0
        prefix = f"connector:{source}:"
        removed = 0
        for parked in self._quarantine.list_quarantined():
            if not parked.provenance.startswith(prefix):
                continue
            if parked.item_id in touched_item_ids:
                continue
            if parked.reason.startswith("unwound:"):
                continue
            self._quarantine.remove(parked.item_id)
            removed += 1
        return removed

    async def _emit_sync_audit(self, *, mode: str, outcome: AuditOutcome, detail: str) -> None:
        if self._audit_sink is None:
            return
        await self._audit_sink.append(
            new_audit_event(
                action="connector.sync",
                resource=f"connector://{self._contract.source}",
                outcome=outcome,
                actor=self._actor,
                detail=detail,
                kpi_tag=self._kpi_tag,
            )
        )
