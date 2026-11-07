# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Ingestion pipeline spine (NE-026).

:class:`IngestionPipeline` orchestrates the frozen NE-020 stage Protocols
(PARSE -> CHUNK -> optional ENRICH) plus embed+index into a
:class:`~ne.contracts.knowledge.ChunkStore`, backed by a durable
:class:`~ne.brain.ingest.ledger.IngestLedger`. It is a package-local
orchestrator, NOT a frozen-contract slot adapter, so it has no plugin
entry-point.

Durability properties
---------------------
* **Idempotent (ACL/metadata-aware, failure-fenced).** The UNCHANGED
  short-circuit gates on THREE conditions together — ``indexed_content_hash ==
  content_hash`` AND ``state.content_hash == content_hash`` AND ``state.reason is
  None`` — not on ``indexed_content_hash`` alone. Identical bytes with identical
  acl/metadata AND a clean (non-failed, same-version) ledger row is a true
  ``UNCHANGED`` no-op (zero parser/store calls); identical bytes with a *changed*
  acl/metadata (still clean row) takes the cheap payload-refresh path below.
  Crucially, if a NEWER version was attempted and failed after it had already
  overwritten/deleted shared positional point ids, ``begin`` has moved
  ``state.content_hash`` and ``record_failure`` has set ``state.reason`` — so a
  byte-revert to the old version no longer matches the gate and FAILS CLOSED into
  the full pipeline, which re-upserts the reverted content and heals the store
  (wrong content + stale ACL) instead of serving a lying ``UNCHANGED``. Chunk ids
  are stable (``document_id:NNNN``) and map to stable UUIDv5 point ids in the
  store, so even a forced re-upsert overwrites in place rather than duplicating.
* **Resumable.** Re-calling :meth:`ingest` on the same source IS the resume
  API. Each completed stage persists its output as a hash-keyed artifact and
  checkpoints; a resumed run replays only the stages that have no artifact yet
  (a crash after PARSE re-uses the parse artifact, etc.).
* **Doc-versioned.** A changed document (new content hash under the same
  ``document_id``) supersedes the old version: the ledger preserves the live
  version's chunk ids across the restart, and INDEX deletes exactly the
  old-minus-new chunk ids.

INDEX durability ordering (DECIDED)
-----------------------------------
``upsert(new) -> delete(stale) -> ledger.record_indexed`` — new chunks are
written BEFORE stale ones are deleted, so a crash anywhere in the window never
makes the document unretrievable, and the ledger's ``INDEXED`` state (committed
LAST) implies the index holds exactly this version's chunks. The accepted
trade-off is a bounded, single-document transient window in which a query may
briefly see chunks from BOTH versions of that one document; it is healed by the
next resume (re-upsert is idempotent, the stale delete re-runs). The alternative
— version-suffixed chunk ids — would break ``chunk_id`` stability and citations.

Failure taxonomy (DECIDED)
--------------------------
* **Document-caused** (unreadable source, parse-subprocess/validation error,
  chunker error, empty/no-chunk result) -> ``QUARANTINE``. The batch continues;
  the document is recorded and inspectable; the index is never partially written
  (nothing is upserted before parse+chunk validation passes). Same-hash re-ingest
  does NOT retry a quarantined document unless ``force=True``.
* **Infrastructure-caused** (isolated parser-env provisioning/lock/network via
  :class:`~ne.brain.ingest.contracts.ParserEnvironmentError` or the AIR_GAP-policy
  :class:`~ne.brain.sovereignty.AirGapViolationError`, enricher/gateway error,
  embed/index/store error) -> ``FAILED`` / retryable. The checkpoint stays at the
  last completed stage so a later resume skips the completed stages and completes
  the ingest. This split matters at the PARSE step specifically: a failed
  ``uv pip install`` or an AIR_GAP-blocked cold venv must NOT quarantine a
  perfectly good document — a transient infra outage would otherwise become a
  permanent silent index gap.

ACL/metadata staleness (DECIDED)
--------------------------------
The identical-bytes short-circuit is 3-way:

* identical bytes + identical acl + identical metadata -> ``UNCHANGED``;
* identical bytes but changed acl/metadata -> a payload-only correction via the
  optional :class:`PayloadUpdatable` capability (a Qdrant ``set_payload``) with NO
  re-embed; stores lacking ``PayloadUpdatable`` fall back to a full
  ``upsert_chunks`` (correct, but not cheap);
* anything else -> the full pipeline.

This exists because a stale (over-permissive) ACL on unchanged bytes would let the
NE-043 read-path filter keep serving a chunk to a principal a narrowed acl now
excludes. Separately, ``IngestSource.metadata`` is merged into every indexed
chunk's metadata under the reserved, namespaced ``'source'`` key (NEVER
flat-merged; see :func:`_merged_metadata`) at INDEX time, on BOTH the fresh-index
path and the cheap payload-refresh path.

The full-pipeline INDEX step (used by both the fresh path and any
resume-after-checkpoint retry) also always stamps the CURRENT ``source.acl`` onto
indexed chunks rather than trusting a possibly-stale acl baked into a reloaded
CHUNKED/ENRICHED artifact — a document that fails after the CHUNK checkpoint and
is retried with a narrower acl must not leave the OLD wider acl live in the index
while the ledger believes the narrower one is already in effect.

C3 (model independence): this module imports no gateway/provider code —
enrichment reaches models ONLY through the injected
:class:`~ne.brain.ingest.contracts.Enricher` Protocol.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ne.brain.ingest.contracts import (
    Chunker,
    DocumentParser,
    Enricher,
    ParsedDocument,
    ParserEnvironmentError,
)
from ne.brain.ingest.docling_parser import parsed_document_from_json
from ne.brain.ingest.ledger import DocStage, IngestLedger, LedgerState
from ne.brain.sovereignty import AirGapViolationError
from ne.contracts.frozen import AnyMap, FrozenDict
from ne.contracts.knowledge import ChunkStore
from ne.contracts.types import Chunk


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class IngestSource(_Frozen):
    """One document to ingest: a source path plus its stable identity/labels."""

    path: Path
    document_id: str
    acl: tuple[str, ...] = ()
    metadata: AnyMap = FrozenDict()


class IngestStatus(StrEnum):
    """Terminal outcome of ingesting one document."""

    INDEXED = "indexed"
    UNCHANGED = "unchanged"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class IngestOutcome(_Frozen):
    """The result of ingesting one document."""

    document_id: str
    content_hash: str
    status: IngestStatus
    chunk_count: int = 0
    reason: str | None = None


@runtime_checkable
class PayloadUpdatable(Protocol):
    """Optional ``ChunkStore`` capability: a cheap acl/metadata-only correction.

    A store implementing this can overwrite the acl/metadata payload of chunks
    that are ALREADY indexed WITHOUT a re-embed or re-encode. It is a
    package-local, duck-typed capability Protocol — deliberately NOT part of the
    FROZEN ne-contracts ``ChunkStore`` Protocol, following the exact precedent of
    :class:`ne.brain.retriever.FilteredChunkFetch` (checked via ``isinstance``,
    never imported by the class that implements it). Adapters that do NOT
    implement it must be handled by the caller via a full ``upsert_chunks``
    fallback (correct, but not cheap).
    """

    async def update_payload(self, chunks: Sequence[Chunk]) -> None:
        """Overwrite ONLY the acl/metadata payload fields of each chunk's
        already-indexed point (matched by ``chunk_id``); vectors and all other
        point data are left untouched. Each chunk's acl/metadata passed in is the
        COMPLETE desired value (a top-level replace of those two fields, not a
        deep merge — the caller computes the full merged value first). Chunk ids
        with no existing point are silently skipped, mirroring
        :meth:`ChunkStore.get`'s omit-unknown-ids behavior."""
        ...


def content_hash_for(path: Path) -> str:
    """Stable content hash (sha256 of the file bytes) — the version identity."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


_SOURCE_METADATA_KEY = "source"


def _serialize_chunks(chunks: Sequence[Chunk]) -> str:
    return json.dumps([c.model_dump(mode="json") for c in chunks])


def _chunks_from_json(payload: str) -> tuple[Chunk, ...]:
    return tuple(Chunk.model_validate(item) for item in json.loads(payload))


def _merged_metadata(
    chunk_metadata: Mapping[str, Any], source_metadata: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge ``IngestSource.metadata`` into a chunk's metadata at INDEX time.

    DECIDED precedence: source metadata is always NAMESPACED under
    ``chunk.metadata['source']`` as its own sub-dict, NEVER flat-merged into the
    top level, so it can never collide with (or be silently overwritten by) the
    chunker's structural keys (headings/pages/content_type/sequence/source_name/
    source_format) or the parser's ``doc.metadata``. It is overwritten wholesale
    on every call (not deep-merged with any stale prior ``'source'`` value), so a
    later metadata change never leaves stale leftover keys behind. Retrieval
    filters address it as the dotted path ``'metadata.source.<key>'`` using the
    store's existing verbatim filter-key grammar (see README).
    """
    merged = dict(chunk_metadata)
    merged[_SOURCE_METADATA_KEY] = dict(source_metadata)
    return merged


class IngestionPipeline:
    """Orchestrates PARSE -> CHUNK -> (ENRICH) -> embed+index over a ledger."""

    def __init__(
        self,
        parser: DocumentParser,
        chunker: Chunker,
        store: ChunkStore,
        ledger: IngestLedger,
        *,
        enricher: Enricher | None = None,
    ) -> None:
        self._parser = parser
        self._chunker = chunker
        self._store = store
        self._ledger = ledger
        self._enricher = enricher

    async def ingest_batch(
        self, sources: Sequence[IngestSource], *, force: bool = False
    ) -> tuple[IngestOutcome, ...]:
        """Ingest each source sequentially; a per-doc outcome NEVER aborts the batch."""
        return tuple([await self.ingest(source, force=force) for source in sources])

    async def ingest(self, source: IngestSource, *, force: bool = False) -> IngestOutcome:
        document_id = source.document_id

        # (1) Hash the file. An unreadable source is a document-caused failure.
        try:
            content_hash = content_hash_for(source.path)
        except OSError as exc:
            self._ledger.quarantine(document_id, "", f"unreadable source: {exc}")
            return IngestOutcome(
                document_id=document_id,
                content_hash="",
                status=IngestStatus.QUARANTINED,
                reason=f"unreadable source: {exc}",
            )

        # (2) Short-circuits.
        state = self._ledger.get_state(document_id)
        # UNCHANGED short-circuit (and the acl/metadata refresh short-circuit
        # below) gate on THREE conditions, not just indexed_content_hash. A newer
        # version attempt moves state.content_hash (begin) and/or sets state.reason
        # (record_failure) — so a byte-revert AFTER a failed newer attempt that
        # already overwrote/deleted shared positional point ids FAILS CLOSED here
        # into the full pipeline, which re-upserts the reverted content and heals
        # the store (wrong content + stale ACL) rather than short-circuiting to a
        # lying UNCHANGED. Steady state (a clean prior INDEXED of these exact
        # bytes) still satisfies all three and stays a true no-op.
        if (
            not force
            and state is not None
            and state.indexed_content_hash == content_hash
            and state.content_hash == content_hash
            and state.reason is None
        ):
            if state.indexed_acl == source.acl and state.indexed_metadata == source.metadata:
                # Identical bytes AND identical acl/metadata: a true no-op.
                return IngestOutcome(
                    document_id=document_id,
                    content_hash=content_hash,
                    status=IngestStatus.UNCHANGED,
                    chunk_count=len(state.indexed_chunk_ids),
                )
            # SECURITY FIX: identical bytes but the caller-supplied acl/metadata
            # changed since the version currently live in the index. A stale ACL
            # on unchanged bytes would let the NE-043 read-path filter keep
            # serving a chunk to a principal the new acl excludes. This path is a
            # cheap payload-only correction (no re-parse, no re-chunk, no re-embed).
            return await self._refresh_acl_metadata(document_id, content_hash, source, state)
        if not force:
            existing = self._find_quarantine(document_id, content_hash)
            if existing is not None:
                # Deterministic document failure on identical bytes: don't retry.
                return IngestOutcome(
                    document_id=document_id,
                    content_hash=content_hash,
                    status=IngestStatus.QUARANTINED,
                    reason=existing,
                )
        self._ledger.begin(document_id, content_hash)

        # (3) PARSE.
        parsed_payload = self._ledger.load_artifact(document_id, content_hash, DocStage.PARSED)
        if parsed_payload is None:
            try:
                doc = await self._parser.parse(source.path, document_id=document_id)
            except (ParserEnvironmentError, AirGapViolationError) as exc:
                # INFRA-caused (isolated-env provisioning / AIR_GAP policy) — not a
                # property of the document. FAILED/retryable, NOT quarantine, so a
                # transient infra outage never becomes a permanent silent index gap.
                return self._fail(document_id, content_hash, f"parser environment failure: {exc}")
            except Exception as exc:
                return self._quarantine(document_id, content_hash, f"parse failed: {exc}")
            invalid = self._validate_parsed(doc)
            if invalid is not None:
                return self._quarantine(document_id, content_hash, invalid)
            self._ledger.save_artifact(
                document_id, content_hash, DocStage.PARSED, doc.model_dump_json()
            )
            self._ledger.checkpoint(document_id, content_hash, DocStage.PARSED)
        else:
            doc = parsed_document_from_json(parsed_payload)

        # (4) CHUNK.
        chunked_payload = self._ledger.load_artifact(document_id, content_hash, DocStage.CHUNKED)
        if chunked_payload is None:
            try:
                fresh = self._chunker.chunk(doc, acl=source.acl)
            except Exception as exc:
                return self._quarantine(document_id, content_hash, f"chunk failed: {exc}")
            if not fresh:
                return self._quarantine(document_id, content_hash, "no chunks")
            chunked_payload = _serialize_chunks(fresh)
            self._ledger.save_artifact(document_id, content_hash, DocStage.CHUNKED, chunked_payload)
            self._ledger.checkpoint(document_id, content_hash, DocStage.CHUNKED)
        # Normalization: fresh and resumed runs BOTH reload from the serialized
        # form so downstream sees byte-identical chunk objects (resume parity).
        chunks = _chunks_from_json(chunked_payload)

        # (5) ENRICH (optional). Infrastructure-caused failure -> FAILED/retryable.
        if self._enricher is not None:
            enriched_payload = self._ledger.load_artifact(
                document_id, content_hash, DocStage.ENRICHED
            )
            if enriched_payload is None:
                try:
                    enriched = await self._enricher.enrich(doc, chunks)
                except Exception as exc:
                    return self._fail(document_id, content_hash, f"enrich failed: {exc}")
                enriched_payload = _serialize_chunks(enriched)
                self._ledger.save_artifact(
                    document_id, content_hash, DocStage.ENRICHED, enriched_payload
                )
                self._ledger.checkpoint(document_id, content_hash, DocStage.ENRICHED)
            final_chunks = _chunks_from_json(enriched_payload)
        else:
            final_chunks = chunks

        # (6) INDEX: upsert-new -> delete-stale -> ledger-commit (durability order).
        # Stamp the CURRENT source.acl (never the chunk's own `c.acl`) and merge
        # IngestSource.metadata under the namespaced 'source' key (see
        # _merged_metadata) onto every indexed chunk at INDEX time. This must be
        # `source.acl`, NOT `c.acl`: on a resume-after-checkpoint path, `c.acl` is
        # whatever acl was baked into the chunk by the earlier chunker.chunk(...,
        # acl=...) call that produced the now-reloaded CHUNKED artifact, which can
        # be STALE (wider) relative to a narrowed acl on this retry. Stamping
        # source.acl here makes both the fresh-index path (where source.acl IS
        # already the chunks' acl, so this is a no-op change) and the resume path
        # (where it corrects the staleness) always index the LIVE source acl,
        # keeping the ledger's indexed_acl and the store's chunk acl in agreement.
        indexed_chunks = tuple(
            Chunk(
                chunk_id=c.chunk_id,
                document_id=c.document_id,
                text=c.text,
                metadata=_merged_metadata(c.metadata, source.metadata),
                acl=source.acl,
            )
            for c in final_chunks
        )
        prior_ids = set(state.indexed_chunk_ids) if state is not None else set()
        new_ids = [c.chunk_id for c in indexed_chunks]
        try:
            await self._store.upsert_chunks(indexed_chunks)
            stale = prior_ids - set(new_ids)
            if stale:
                await self._store.delete(sorted(stale))
            self._ledger.record_indexed(
                document_id, content_hash, new_ids, acl=source.acl, metadata=source.metadata
            )
        except Exception as exc:
            return self._fail(document_id, content_hash, f"index failed: {exc}")

        return IngestOutcome(
            document_id=document_id,
            content_hash=content_hash,
            status=IngestStatus.INDEXED,
            chunk_count=len(indexed_chunks),
        )

    async def _refresh_acl_metadata(
        self, document_id: str, content_hash: str, source: IngestSource, state: LedgerState
    ) -> IngestOutcome:
        """Cheap payload-only correction: identical bytes, changed acl/metadata.

        Overwrites the acl/metadata payload of the already-indexed chunks with the
        new caller-supplied values — no re-parse/re-chunk/re-embed. Uses the
        optional :class:`PayloadUpdatable` capability when the store implements it
        (a Qdrant ``set_payload``), else falls back to a full ``upsert_chunks``
        (correct, but re-embeds). An infra failure here is FAILED/retryable, exactly
        like a normal index failure.
        """
        try:
            existing = await self._store.get(state.indexed_chunk_ids)
            updated = tuple(
                Chunk(
                    chunk_id=c.chunk_id,
                    document_id=c.document_id,
                    text=c.text,
                    metadata=_merged_metadata(c.metadata, source.metadata),
                    acl=source.acl,
                )
                for c in existing
            )
            if isinstance(self._store, PayloadUpdatable):
                await self._store.update_payload(updated)
            else:
                await self._store.upsert_chunks(updated)
            self._ledger.record_indexed(
                document_id,
                content_hash,
                state.indexed_chunk_ids,
                acl=source.acl,
                metadata=source.metadata,
            )
        except Exception as exc:
            return self._fail(document_id, content_hash, f"acl/metadata refresh failed: {exc}")
        return IngestOutcome(
            document_id=document_id,
            content_hash=content_hash,
            status=IngestStatus.INDEXED,
            chunk_count=len(updated),
            reason="acl_metadata_refreshed",
        )

    def _validate_parsed(self, doc: ParsedDocument) -> str | None:
        if not doc.blocks or all(not block.text.strip() for block in doc.blocks):
            return "empty document"
        return None

    def _find_quarantine(self, document_id: str, content_hash: str) -> str | None:
        for record in self._ledger.list_quarantined():
            if record.document_id == document_id and record.content_hash == content_hash:
                return record.reason
        return None

    def _quarantine(self, document_id: str, content_hash: str, reason: str) -> IngestOutcome:
        self._ledger.quarantine(document_id, content_hash, reason)
        return IngestOutcome(
            document_id=document_id,
            content_hash=content_hash,
            status=IngestStatus.QUARANTINED,
            reason=reason,
        )

    def _fail(self, document_id: str, content_hash: str, reason: str) -> IngestOutcome:
        self._ledger.record_failure(document_id, content_hash, reason)
        return IngestOutcome(
            document_id=document_id,
            content_hash=content_hash,
            status=IngestStatus.FAILED,
            reason=reason,
        )
