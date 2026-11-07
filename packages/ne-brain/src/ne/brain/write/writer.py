# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""KnowledgeWriter — the governed write path (NE-023).

:class:`KnowledgeWriter` is a production implementation of the FROZEN
``ne.contracts.knowledge.KnowledgeWrite`` Protocol (``upsert`` / ``curate`` /
``quarantine``) over the real NE-021 ``QdrantChunkStore``. Every write:

* is validated against its JSON-Schema :class:`ShapeContract` BEFORE anything
  enters the store (T1 "shapes as contract") — a failing write NEVER reaches
  ``upsert_chunks``;
* on success carries a first-class, queryable :class:`ProvenanceStamp` under the
  reserved ``metadata['provenance']`` key (who/what/when/source + a provenance
  CLASS, incl. the NE-037 ``learned-by-agent`` promotion class);
* on failure lands in the inspectable :class:`WriteQuarantine` lane WITH its full
  payload AND emits a ``ShapeViolation`` (via the ne-events factory → outbox
  sink); the rest of the batch continues (NE-026 per-item-never-aborts-batch);
* emits a config_hash-stamped ``AuditEvent`` through the frozen ``AuditSink``
  seam (``knowledge.upsert`` / ``knowledge.curate`` / ``knowledge.quarantine``).

Both the ``audit_sink`` and the ``violation_sink`` are REQUIRED constructor
arguments — constructing without either raises :class:`SinkRequiredError`. A
governed write with no audit/violation transport is fail-open; this writer
matches the sink-required-by-construction discipline of the rest of the governed
stack (NE-031 PDP / NE-033 GovernedToolExecutor / NE-035 approval gate), closing
Checkpoint-D boundary-audit #5.

C3 (model independence): this module imports NO gateway/provider code — v1
curation is deterministic (no LLM call). Composition with NE-026 (DECIDED): the
spine ingests DOCUMENTS (chunk ids ``document_id:NNNN``, always containing
``':'``); this writer owns item-granular KNOWLEDGE where ``chunk_id == item_id``.
The namespaces cannot collide because spine ids always contain ``':'`` — and the
writer REJECTS an ``item_id`` containing ``':'`` (``ValueError``) on ALL THREE
governed-write surfaces (``upsert`` / ``curate`` / ``quarantine``), BEFORE any
store fetch/park/delete, so a governed write can never delete or rewrite an
NE-026 spine chunk that shares the store.

Atomicity guarantee (NE-CKD6) — a governed write is ATOMIC: store+audit together
or NEITHER, under a single audit-store fault. ``upsert`` audits success only
AFTER the store write succeeds, and a store failure PROPAGATES (infra-caused,
retryable) rather than producing a lying receipt. If the store write succeeds but
a per-item audit append then FAILS, ``upsert`` UNWINDS the not-yet-audited items
via park-before-settlement (full payload parked in the quarantine lane, then a
store-local one-shot handle restores overwritten raw vectors and removes new
rows; the unwind itself is audited best-effort) and raises the typed
:class:`KnowledgeAuditError` — so no caller of the frozen ``KnowledgeWrite`` seam
can ever observe a durable+retrievable chunk without an audit record under a
single fault. Items whose success-audit already durably appended stay (per-item
store+audit atomicity). ``curate`` mirrors this: on an audit-append failure it
RESTORES the original (already-audited) chunk and parks the attempted patch.
Manual ``quarantine`` is fail-safe by ordering — it parks the payload BEFORE
deleting from the store (never lose data), so an audit failure there is the
over-parked/under-retrievable direction and is NOT unwound (re-adding the chunk
would create the retrievable-but-unaudited state); it raises ``KnowledgeAuditError``.

KNOWN double-fault residual (tracked, loud, non-blocking): if the audit store is
down AND the vector store also fails DURING compensation/verification, the
chunk may remain retrievable without its audit record. No distributed transaction
spans Qdrant + the audit store; this residual raises ``KnowledgeAuditError`` with
an explicit residual message to be acted on, and is deliberately out of scope for
2PC/outbox machinery here.

config_hash: the ne-events factories resolve config_hash from the FROZEN process
hash; if a host has not frozen it, ``ConfigHashNotFrozenError`` propagates rather
than an event being emitted unstamped (hosts freeze at process start).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import jsonschema

from ne.brain.evidence import (
    VectorEvidenceSource,
    VectorWriteSettlementHandle,
    VectorWriteSettlementSource,
    validate_vector_evidence_receipts,
)
from ne.brain.lineage import (
    DocumentLineageError,
    PreparedDocumentVersion,
    SqlAlchemyDocumentLineageStore,
)
from ne.brain.parsed_evidence import (
    ParsedDocumentEvidenceDescriptor,
    _consume_parsed_document_evidence,
    _peek_parsed_document_evidence,
    carried_record_payload,
)
from ne.brain.write.contracts import (
    PROVENANCE_METADATA_KEY as _PROV_KEY,
)
from ne.brain.write.contracts import (
    CuratedKnowledgeItem,
    InvalidPatchError,
    KnowledgeAuditError,
    KnowledgeItemNotFoundError,
    KnowledgeWriteError,
    ProvenanceClass,
    ProvenanceStamp,
    ShapeContract,
    ShapeValidationError,
    SinkRequiredError,
    UnknownShapeError,
    _to_plain,
    stamped_metadata,
)
from ne.brain.write.immutable_artifacts import AUTHORITY_ARTIFACT_PAYLOAD_KEY
from ne.brain.write.quarantine import QuarantinedWrite, WriteQuarantine
from ne.brain.write.sinks import ShapeViolationSink
from ne.contracts import ConfigHashNotFrozenError
from ne.contracts.governance import AuditSink
from ne.contracts.knowledge import ChunkStore, KnowledgeItem, UpsertReceipt
from ne.contracts.source_authority import (
    SOURCE_AUTHORITY_STAMP_METADATA_KEY,
    SOURCE_STATUS_METADATA_KEY,
)
from ne.contracts.types import Chunk
from ne.events import new_audit_event, new_shape_violation
from ne.schema import AuditOutcome, ViolationSeverity

_UNREGISTERED = "unregistered"
_ALLOWED_PATCH_KEYS = frozenset({"content", "metadata"})
_MAX_MESSAGES = 5


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class KnowledgeWriter:
    """Governed implementation of the frozen ``KnowledgeWrite`` Protocol."""

    def __init__(
        self,
        store: ChunkStore,
        quarantine: WriteQuarantine,
        contracts: Mapping[str, ShapeContract],
        *,
        default_shape: str,
        actor: str,
        default_provenance_class: ProvenanceClass = ProvenanceClass.CURATED,
        audit_sink: AuditSink,
        violation_sink: ShapeViolationSink,
        kpi_tag: str | None = None,
        lineage: SqlAlchemyDocumentLineageStore | None = None,
    ) -> None:
        # Runtime backstop for untyped callers: a governed write with no audit or
        # violation transport is fail-open (Checkpoint-D #5). The type signature
        # already forbids omitting either kwarg; this catches cast(..., None).
        if audit_sink is None:
            raise SinkRequiredError(
                "KnowledgeWriter requires audit_sink — no governed write without audit "
                "(Checkpoint-D #5)"
            )
        if violation_sink is None:
            raise SinkRequiredError(
                "KnowledgeWriter requires violation_sink — no governed write without a "
                "shape-violation transport (Checkpoint-D #5)"
            )
        if default_shape not in contracts:
            raise ValueError(
                f"default_shape {default_shape!r} is not among the registered contracts "
                f"{sorted(contracts)!r}"
            )
        self._store = store
        self._quarantine = quarantine
        self._contracts = dict(contracts)
        self._default_shape = default_shape
        self._actor = actor
        self._default_provenance_class = default_provenance_class
        self._audit_sink = audit_sink
        self._violation_sink = violation_sink
        self._kpi_tag = kpi_tag
        self._lineage = lineage
        # Pre-build one Draft 2020-12 validator per contract (schemas already
        # proven well-formed at ShapeContract construction).
        self._validators = {
            name: jsonschema.Draft202012Validator(_to_plain(contract.json_schema))
            for name, contract in self._contracts.items()
        }

    # -- upsert -------------------------------------------------------------
    async def upsert(self, items: Sequence[KnowledgeItem]) -> UpsertReceipt:
        item_ids = [item.item_id for item in items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("duplicate item_ids within a single upsert batch")
        for item_id in item_ids:
            if ":" in item_id:
                raise ValueError(
                    f"item_id {item_id!r} contains ':', reserved for NE-026 spine chunk ids"
                )
        # 015A0-AUTHORITY-CORE: an immutable source-authority artifact must never
        # reach this unconditional path. `upsert` overwrites by item id, which is
        # exactly the operation that would replace an artifact another operation
        # already selected and retained; authority artifacts go through
        # `ne.brain.write.immutable_artifacts.ImmutableKnowledgeArtifactStore`,
        # whose create/reuse is fenced and whose delete needs a creator token.
        for item in items:
            if AUTHORITY_ARTIFACT_PAYLOAD_KEY in item.metadata:
                raise KnowledgeWriteError(
                    f"item_id {item.item_id!r} carries {AUTHORITY_ARTIFACT_PAYLOAD_KEY!r}: "
                    "source-authority artifacts are written by "
                    "ImmutableKnowledgeArtifactStore.create_if_absent, never by "
                    "the unconditional KnowledgeWrite upsert path"
                )
            # 015A0-ENFORCEMENT, same shape and same reason: a disclosure stamp
            # is derived from the receipt an authority admission RETURNED, and is
            # applied by `curate` once that admission has landed. An item may not
            # arrive carrying one, or a connector mapper could present a stamp of
            # its own choosing and the read filter would evaluate it as authored.
            if SOURCE_AUTHORITY_STAMP_METADATA_KEY in item.metadata:
                raise KnowledgeWriteError(
                    f"item_id {item.item_id!r} carries "
                    f"{SOURCE_AUTHORITY_STAMP_METADATA_KEY!r}: a source-authority stamp is "
                    "applied from the admission receipt after the governed write, never "
                    "presented by the item being written"
                )

        accepted_ids: list[str] = []
        quarantined_ids: list[str] = []
        accepted_chunks: list[Chunk] = []
        parsed_descriptors: dict[str, ParsedDocumentEvidenceDescriptor | None] = {}
        # item_id -> (full validated payload dict, "<name>@<version>" shape, provenance).
        # Retained so a post-store audit-append failure can unwind (park-before-delete)
        # the not-yet-audited items with their FULL payload (NE-CKD6 atomicity).
        accepted_payloads: dict[str, tuple[dict[str, Any], str, str]] = {}

        for item in items:
            shape_name = self._shape_name_for(item)
            contract = self._contracts.get(shape_name)
            if contract is None:
                await self._reject(
                    item,
                    shape_name=shape_name,
                    shape_version=_UNREGISTERED,
                    message=f"unknown shape contract: {shape_name}",
                )
                quarantined_ids.append(item.item_id)
                continue

            errors = self._validate(contract, item)
            if errors:
                await self._reject(
                    item,
                    shape_name=contract.name,
                    shape_version=contract.version,
                    message=errors,
                )
                quarantined_ids.append(item.item_id)
                continue

            stamp = ProvenanceStamp(
                provenance_class=self._class_for(item),
                source=item.provenance,
                actor=self._actor,
                written_at=_now_iso(),
                write_id=uuid.uuid4().hex,
                shape=f"{contract.name}@{contract.version}",
            )
            accepted_chunks.append(
                Chunk(
                    chunk_id=item.item_id,
                    document_id=item.item_id,
                    text=item.content,
                    metadata=stamped_metadata(item.metadata, stamp),
                    acl=self._acl_for(item),
                )
            )
            accepted_ids.append(item.item_id)
            if isinstance(item, CuratedKnowledgeItem):
                parsed_descriptors[item.item_id] = item.parsed_source_evidence
            accepted_payloads[item.item_id] = (
                {
                    "item_id": item.item_id,
                    "content": item.content,
                    "metadata": _to_plain(item.metadata),
                    "provenance": item.provenance,
                },
                f"{contract.name}@{contract.version}",
                item.provenance,
            )

        if accepted_chunks:
            settlement: tuple[VectorWriteSettlementSource, VectorWriteSettlementHandle] | None = (
                None
            )
            prepared: dict[str, PreparedDocumentVersion] = {}
            if self._lineage is not None:
                try:
                    # Validate every item/descriptor binding before the first
                    # lineage transaction so batch order cannot partially
                    # prepare a forged or mismatched later item.
                    parsed_bindings = []
                    for chunk in accepted_chunks:
                        descriptor = parsed_descriptors.get(chunk.chunk_id)
                        # Ordinary connector records are not necessarily JSON.
                        # Only a producer-carried parsed-evidence descriptor, or
                        # the M365 connector's declared JSON payload contract,
                        # makes the parsed-document binding applicable here.
                        if (
                            descriptor is None
                            and chunk.metadata.get("connector") != "m365-sharepoint"
                        ):
                            continue
                        # A parsed document's chunk text is PROSE; its record
                        # payload rides in metadata under
                        # ``PARSED_RECORD_METADATA_KEY``. Non-parsed records
                        # still carry the payload as their canonical-JSON text.
                        payload: object = carried_record_payload(chunk.metadata)
                        if payload is None:
                            try:
                                payload = json.loads(chunk.text)
                            except json.JSONDecodeError:
                                payload = None
                        if not isinstance(payload, dict):
                            if descriptor is None:
                                continue
                            raise DocumentLineageError("parsed source evidence binding is invalid")
                        claims_m365_parser = (
                            chunk.metadata.get("connector") == "m365-sharepoint"
                            and payload.get("body_ingestion_status") == "parsed"
                        )
                        if descriptor is None:
                            if claims_m365_parser:
                                raise DocumentLineageError(
                                    "parsed source evidence binding is invalid"
                                )
                            continue
                        parsed_bindings.append(
                            (
                                descriptor,
                                chunk.chunk_id,
                                chunk.metadata.get("source_id"),
                                chunk.metadata.get("record_id"),
                                chunk.acl,
                                payload,
                            )
                        )
                    try:
                        _peek_parsed_document_evidence(parsed_bindings)
                    except ValueError as exc:
                        raise DocumentLineageError(
                            "parsed source evidence binding is invalid"
                        ) from exc
                    vector_evidence = (
                        self._store.vector_evidence_descriptor()
                        if isinstance(self._store, VectorEvidenceSource)
                        else None
                    )
                    try:
                        _consume_parsed_document_evidence(parsed_bindings)
                    except ValueError as exc:
                        raise DocumentLineageError(
                            "parsed source evidence binding is invalid"
                        ) from exc
                    parsed_evidence = {
                        chunk.chunk_id: self._lineage.prepare_parsed_source_evidence(
                            chunk,
                            parsed_descriptors.get(chunk.chunk_id),
                        )
                        for chunk in accepted_chunks
                    }
                    for chunk in accepted_chunks:
                        prepared[chunk.chunk_id] = self._lineage.prepare(
                            chunk,
                            vector_evidence=vector_evidence,
                            parse_evidence=parsed_evidence[chunk.chunk_id],
                        )
                except Exception:
                    for entry in prepared.values():
                        self._lineage.abort(entry)
                    raise
            # A store failure PROPAGATES (infra-caused, retryable) — the receipt
            # must never claim durability it does not have.
            try:
                if isinstance(self._store, VectorWriteSettlementSource):
                    outcome = await self._store.upsert_chunks_with_settlement(accepted_chunks)
                    settlement = (self._store, outcome.settlement)
                    try:
                        receipts = validate_vector_evidence_receipts(outcome.receipts, accepted_ids)
                    except (TypeError, ValueError):
                        receipts = ()
                    if self._lineage is not None and receipts:
                        evidence_by_id = {
                            receipt.chunk_id: receipt.descriptor for receipt in receipts
                        }
                        for item_id in accepted_ids:
                            prepared[item_id] = replace(
                                prepared[item_id], vector_evidence=evidence_by_id[item_id]
                            )
                else:
                    await self._store.upsert_chunks(accepted_chunks)
            except Exception:
                if self._lineage is not None:
                    for entry in prepared.values():
                        self._lineage.abort(entry)
                raise
            # Per-item store+audit atomicity (NE-CKD6): audit each accepted item;
            # on the FIRST audit-append failure, unwind every not-yet-audited item
            # (park-before-delete) so no durable+retrievable chunk lacks an audit
            # record under a single fault, then raise a typed error.
            audited: list[str] = []
            for index, item_id in enumerate(accepted_ids):
                try:
                    # A now-fixed item clears its previously-parked quarantine row.
                    self._quarantine.remove(item_id)
                    event_id, occurred_at = await self._audit(
                        action="knowledge.upsert",
                        item_id=item_id,
                        outcome=AuditOutcome.success,
                        detail=(
                            self._lineage_audit_detail(prepared[item_id])
                            if self._lineage is not None
                            else None
                        ),
                    )
                except Exception as exc:
                    unaudited = accepted_ids[index:]
                    try:
                        await self._unwind_upsert(
                            unaudited,
                            accepted_payloads,
                            exc,
                            settlement=settlement,
                            audited=audited,
                        )
                    finally:
                        if self._lineage is not None:
                            for pending_id in unaudited:
                                self._lineage.abort(prepared[pending_id])
                    if isinstance(exc, ConfigHashNotFrozenError):
                        # The unwind still ran, but the config-freeze error keeps its own
                        # loud, distinct type (existing fail-loud test depends on it).
                        raise
                    raise KnowledgeAuditError(
                        f"knowledge.upsert audit append failed after store commit; "
                        f"unwound {unaudited!r} (park-before-delete), {audited!r} remain "
                        f"durable+audited",
                        action="knowledge.upsert",
                        unwound=tuple(unaudited),
                        audited=tuple(audited),
                    ) from exc
                audited.append(item_id)
                if self._lineage is None:
                    continue
                try:
                    self._lineage.finalize(
                        prepared[item_id], audit_event_id=event_id, occurred_at=occurred_at
                    )
                except Exception as exc:
                    # This item remains store+audit durable and its INDEXING row is
                    # intentionally retained for same-hash recovery. Later batch
                    # items are not audited, so remove them before returning failure.
                    remaining = accepted_ids[index + 1 :]
                    try:
                        await self._unwind_upsert(
                            remaining,
                            accepted_payloads,
                            exc,
                            settlement=settlement,
                            audited=audited,
                        )
                        settlement = None
                    finally:
                        for pending_id in remaining:
                            self._lineage.abort(prepared[pending_id])
                    raise DocumentLineageError(
                        "knowledge.upsert is durable+audited but document lineage "
                        "finalization failed; retry resumes the prepared hash"
                    ) from exc

            if settlement is not None:
                source, handle = settlement
                await source.settle_vector_write(
                    handle,
                    keep_chunk_ids=accepted_ids,
                    restore_chunk_ids=(),
                )

        return UpsertReceipt(accepted=tuple(accepted_ids), quarantined=tuple(quarantined_ids))

    async def _unwind_upsert(
        self,
        item_ids: Sequence[str],
        payloads: Mapping[str, tuple[dict[str, Any], str, str]],
        cause: Exception,
        *,
        settlement: tuple[VectorWriteSettlementSource, VectorWriteSettlementHandle] | None = None,
        audited: Sequence[str] = (),
    ) -> None:
        """Roll back the store writes of items whose audit append failed (NE-CKD6).

        Park-before-settlement: the FULL attempted payload is parked before the
        one-shot adapter handle restores unaudited overwritten rows and removes
        unaudited new rows while retaining audited writes. Legacy stores retain
        the prior delete fallback. A settlement failure is a loud double-fault
        residual; this is process-local compensation, not crash/cross-process ACID.
        """
        if not item_ids and settlement is not None:
            source, handle = settlement
            await source.settle_vector_write(
                handle,
                keep_chunk_ids=audited,
                restore_chunk_ids=(),
            )
            return
        if not item_ids:
            return
        # (1) PARK FIRST — never lose the payload.
        for item_id in item_ids:
            payload_dict, shape_str, prov = payloads[item_id]
            self._quarantine.record(
                QuarantinedWrite(
                    item_id=item_id,
                    reason=f"unwound: knowledge.upsert audit append failed: {cause}",
                    payload=payload_dict,
                    provenance=prov,
                    shape=shape_str,
                    created_at=_now_iso(),
                )
            )
        # (2) Then delete from the store (one call). A delete failure here is the
        # double-fault residual: audit down AND vector store down during unwind.
        try:
            if settlement is not None:
                source, handle = settlement
                await source.settle_vector_write(
                    handle,
                    keep_chunk_ids=audited,
                    restore_chunk_ids=item_ids,
                )
            else:
                await self._store.delete(list(item_ids))
        except Exception:
            raise KnowledgeAuditError(
                f"knowledge.upsert audit append failed AND exact vector settlement failed for "
                f"{list(item_ids)!r} — vector state is uncertain "
                f"(KNOWN follow-up double-fault residual, act on this error)",
                action="knowledge.upsert",
                unwound=(),
                audited=(),
            ) from cause
        # (3) Best-effort audit of the unwind (the sink is likely still down; the
        # raised typed error covers it — matches promote()'s unwind-audit discipline).
        for item_id in item_ids:
            with contextlib.suppress(Exception):
                await self._audit(
                    action="knowledge.quarantine",
                    item_id=item_id,
                    outcome=AuditOutcome.success,
                    detail=f"unwound: knowledge.upsert audit append failed: {cause}",
                )

    # -- curate -------------------------------------------------------------
    async def curate(self, item_id: str, patch: Mapping[str, Any], *, provenance: str) -> None:
        if ":" in item_id:
            raise ValueError(
                f"item_id {item_id!r} contains ':', reserved for NE-026 spine chunk ids"
            )
        existing = await self._store.get([item_id])
        if not existing:
            raise KnowledgeItemNotFoundError(f"no knowledge item {item_id!r} to curate")
        chunk = existing[0]

        extra = set(patch) - _ALLOWED_PATCH_KEYS
        if extra:
            raise InvalidPatchError(f"unsupported patch keys {sorted(extra)!r}")
        patch_metadata = patch.get("metadata", {})
        if _PROV_KEY in patch_metadata:
            raise InvalidPatchError(f"patch metadata may not set the reserved {_PROV_KEY!r} key")
        # 015A0-ENFORCEMENT: a record's source-authority class is authored by the
        # governed ingest path from the code-authored connector registry. A patch
        # that could set it would let curated content promote ITSELF out of the
        # source-authority gate, which is the one thing the gate exists to stop.
        if SOURCE_STATUS_METADATA_KEY in patch_metadata:
            raise InvalidPatchError(
                f"patch metadata may not set the server-authored {SOURCE_STATUS_METADATA_KEY!r} key"
            )

        prior = provenance_from_chunk_or_error(chunk)
        contract_name = prior.shape.split("@", 1)[0]
        contract = self._contracts.get(contract_name)
        if contract is None:
            raise UnknownShapeError(
                f"shape contract {contract_name!r} for {item_id!r} is no longer registered"
            )

        new_content = patch.get("content", chunk.text)
        base_metadata = dict(chunk.metadata)
        base_metadata.pop(_PROV_KEY, None)
        base_metadata.update(patch_metadata)

        errors = self._validate_payload(
            contract,
            {
                "item_id": item_id,
                "content": new_content,
                "metadata": _to_plain(base_metadata),
                "provenance": provenance,
            },
        )
        if errors:
            # Store UNCHANGED (curating good data on a bad patch would destroy it);
            # signal a NON-quarantining violation and raise.
            self._emit_violation(
                shape_name=contract.name,
                shape_version=contract.version,
                subject_ref=item_id,
                message=errors,
                quarantined=False,
            )
            await self._audit(
                action="knowledge.curate",
                item_id=item_id,
                outcome=AuditOutcome.denied,
                detail=errors,
            )
            raise ShapeValidationError(item_id, f"{contract.name}@{contract.version}", (errors,))

        stamp = ProvenanceStamp(
            provenance_class=ProvenanceClass.CURATED,
            source=provenance,
            actor=self._actor,
            written_at=_now_iso(),
            write_id=uuid.uuid4().hex,
            shape=f"{contract.name}@{contract.version}",
        )
        await self._store.upsert_chunks(
            [
                Chunk(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    text=new_content,
                    metadata=stamped_metadata(base_metadata, stamp),
                    acl=chunk.acl,
                )
            ]
        )
        # A now-fixed item clears any previously-parked failed-curate row so a
        # retry-heal converges to exactly one audited state (NE-CKD6).
        self._quarantine.remove(item_id)
        try:
            await self._audit(
                action="knowledge.curate",
                item_id=item_id,
                outcome=AuditOutcome.success,
            )
        except Exception as exc:
            # NE-CKD6 atomicity for curate: the patched chunk is durable but its
            # audit failed. Restore the original (already-audited) chunk and park
            # the attempted patch so the store never holds unaudited-modified
            # content and the curator's work is never lost.
            self._quarantine.record(
                QuarantinedWrite(
                    item_id=item_id,
                    reason=f"unwound: knowledge.curate audit append failed: {exc}",
                    payload={
                        "item_id": item_id,
                        "content": new_content,
                        "metadata": _to_plain(base_metadata),
                        "provenance": provenance,
                    },
                    provenance=provenance,
                    shape=f"{contract.name}@{contract.version}",
                    created_at=_now_iso(),
                )
            )
            try:
                await self._store.upsert_chunks([chunk])
            except Exception:
                raise KnowledgeAuditError(
                    "knowledge.curate audit append failed AND restore failed — "
                    "curated content may be retrievable without its audit record "
                    "(double-fault residual)",
                    action="knowledge.curate",
                    unwound=(item_id,),
                ) from exc
            with contextlib.suppress(Exception):
                await self._audit(
                    action="knowledge.curate",
                    item_id=item_id,
                    outcome=AuditOutcome.error,
                    detail="unwound: audit append failed; original chunk restored, "
                    "attempted patch parked",
                )
            if isinstance(exc, ConfigHashNotFrozenError):
                raise
            raise KnowledgeAuditError(
                f"knowledge.curate audit append failed for {item_id!r}; original "
                f"chunk restored, attempted patch parked",
                action="knowledge.curate",
                unwound=(item_id,),
            ) from exc

    # -- quarantine (manual curator pull) ----------------------------------
    async def quarantine(self, item_id: str, reason: str) -> None:
        if ":" in item_id:
            raise ValueError(
                f"item_id {item_id!r} contains ':', reserved for NE-026 spine chunk ids"
            )
        existing = await self._store.get([item_id])
        if not existing:
            raise KnowledgeItemNotFoundError(f"no knowledge item {item_id!r} to quarantine")
        chunk = existing[0]
        stamp = provenance_from_chunk_or_none(chunk)
        # Park BEFORE delete (never lose data) — the NE-026 durability ordering.
        self._quarantine.record(
            QuarantinedWrite(
                item_id=item_id,
                reason=reason,
                payload={
                    "item_id": chunk.chunk_id,
                    "content": chunk.text,
                    "metadata": _to_plain(chunk.metadata),
                    "provenance": stamp.source if stamp is not None else "",
                },
                provenance=stamp.source if stamp is not None else "",
                shape=stamp.shape if stamp is not None else "",
                created_at=_now_iso(),
            )
        )
        await self._store.delete([item_id])
        # A manual pull is NOT a shape failure — no ShapeViolation is emitted.
        try:
            await self._audit(
                action="knowledge.quarantine",
                item_id=item_id,
                outcome=AuditOutcome.success,
                detail=reason,
            )
        except Exception as exc:
            if isinstance(exc, ConfigHashNotFrozenError):
                raise
            # Fail-safe direction (NE-CKD6): park+delete PRECEDE this audit, so the
            # failure leaves the item over-parked/under-retrievable — the invariant
            # (no RETRIEVABLE chunk without audit) holds. Deliberately NOT unwound:
            # re-adding the chunk would create the retrievable-but-unaudited state.
            raise KnowledgeAuditError(
                f"knowledge.quarantine audit append failed for {item_id!r}; item is "
                f"already parked (full payload) and deleted from the store — fail-safe "
                f"direction, NOT unwound (re-adding the chunk would create a "
                f"retrievable state under the same fault)",
                action="knowledge.quarantine",
                unwound=(),
            ) from exc

    # -- helpers ------------------------------------------------------------
    def _shape_name_for(self, item: KnowledgeItem) -> str:
        if isinstance(item, CuratedKnowledgeItem) and item.shape is not None:
            return item.shape
        return self._default_shape

    def _class_for(self, item: KnowledgeItem) -> ProvenanceClass:
        if isinstance(item, CuratedKnowledgeItem):
            return item.provenance_class
        return self._default_provenance_class

    def _acl_for(self, item: KnowledgeItem) -> tuple[str, ...]:
        if isinstance(item, CuratedKnowledgeItem):
            return item.acl
        return ()

    def _validate(self, contract: ShapeContract, item: KnowledgeItem) -> str:
        return self._validate_payload(
            contract,
            {
                "item_id": item.item_id,
                "content": item.content,
                "metadata": _to_plain(item.metadata),
                "provenance": item.provenance,
            },
        )

    def _validate_payload(self, contract: ShapeContract, payload: dict[str, Any]) -> str:
        validator = self._validators[contract.name]
        errors = sorted(validator.iter_errors(payload), key=lambda e: str(e.json_path))
        if not errors:
            return ""
        return "; ".join(f"{e.json_path}: {e.message}" for e in errors[:_MAX_MESSAGES])

    async def _reject(
        self,
        item: KnowledgeItem,
        *,
        shape_name: str,
        shape_version: str,
        message: str,
    ) -> None:
        self._quarantine.record(
            QuarantinedWrite(
                item_id=item.item_id,
                reason=message,
                payload={
                    "item_id": item.item_id,
                    "content": item.content,
                    "metadata": _to_plain(item.metadata),
                    "provenance": item.provenance,
                },
                provenance=item.provenance,
                shape=shape_name,
                created_at=_now_iso(),
            )
        )
        self._emit_violation(
            shape_name=shape_name,
            shape_version=shape_version,
            subject_ref=item.item_id,
            message=message,
            quarantined=True,
        )
        await self._audit(
            action="knowledge.upsert",
            item_id=item.item_id,
            outcome=AuditOutcome.denied,
            detail=message,
        )

    def _emit_violation(
        self,
        *,
        shape_name: str,
        shape_version: str,
        subject_ref: str,
        message: str,
        quarantined: bool,
    ) -> None:
        self._violation_sink.emit(
            new_shape_violation(
                schema_name=shape_name,
                schema_version=shape_version,
                subject_ref=subject_ref,
                message=message,
                severity=ViolationSeverity.error,
                actor=self._actor,
                quarantined=quarantined,
                kpi_tag=self._kpi_tag,
            )
        )

    async def _audit(
        self,
        *,
        action: str,
        item_id: str,
        outcome: AuditOutcome,
        detail: str | None = None,
    ) -> tuple[str, datetime]:
        event = new_audit_event(
            action=action,
            resource=f"knowledge://{item_id}",
            outcome=outcome,
            actor=self._actor,
            detail=detail,
            kpi_tag=self._kpi_tag,
        )
        await self._audit_sink.append(event)
        return event.event_id, event.occurred_at

    @staticmethod
    def _lineage_audit_detail(prepared: PreparedDocumentVersion) -> str:
        """C7-clean hash-chain anchor for a document-version transition."""
        return json.dumps(
            {
                "content_hash": prepared.content_hash,
                "prev_content_hash": prepared.prev_content_hash,
                "version_seq": prepared.version_seq,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def provenance_from_chunk_or_none(chunk: Chunk) -> ProvenanceStamp | None:
    """Local re-export wrapper (see :func:`ne.brain.write.contracts.provenance_from_chunk`)."""
    from ne.brain.write.contracts import provenance_from_chunk

    return provenance_from_chunk(chunk)


def provenance_from_chunk_or_error(chunk: Chunk) -> ProvenanceStamp:
    """The stored chunk must carry a stamp for curate's contract resolution."""
    stamp = provenance_from_chunk_or_none(chunk)
    if stamp is None:
        raise UnknownShapeError(
            f"chunk {chunk.chunk_id!r} carries no provenance stamp; not a governed write"
        )
    return stamp


__all__ = ["KnowledgeWriter"]
