# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pipeline-spine orchestration tests (NE-026): idempotency, resume, versioning,
quarantine, and the durability ordering — with in-test stub stages.

The seam under test here is the SPINE's checkpoint/resume/supersede logic, so
below-seam stores are stubbed to inject failures (§1.4 allows below-seam
mocking where the spine's orchestration is the unit). The real embed+index
claim is proven separately against qdrant-client local mode in
``test_ingest_spine_qdrant.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ne.brain.ingest.chunker import StructuralChunker
from ne.brain.ingest.contracts import (
    BlockKind,
    Chunker,
    DocBlock,
    ParsedDocument,
    ParserEnvironmentError,
)
from ne.brain.ingest.ledger import DocStage, SqliteIngestLedger
from ne.brain.ingest.spine import (
    IngestionPipeline,
    IngestSource,
    IngestStatus,
)
from ne.brain.sovereignty import AirGapViolationError
from ne.contracts.types import Chunk, ScoredChunk, SparseVector


class FakeParser:
    """Builds a ParsedDocument from a file's non-empty lines (one section each).

    Sentinels: a file containing ``__FAIL__`` raises; ``__EMPTY__`` yields a
    zero-block document (drives the validation/quarantine lane).
    """

    def __init__(self) -> None:
        self.calls = 0

    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument:
        self.calls += 1
        text = Path(path).read_text()
        if "__FAIL__" in text:
            raise RuntimeError("parser blew up")
        if "__EMPTY__" in text:
            return ParsedDocument(
                document_id=document_id,
                source_name=Path(path).name,
                source_format="md",
                blocks=(),
            )
        lines = [line for line in text.splitlines() if line.strip()]
        blocks: list[DocBlock] = []
        for index, line in enumerate(lines):
            blocks.append(DocBlock(kind=BlockKind.HEADING, text=f"Section {index}", level=1))
            blocks.append(DocBlock(kind=BlockKind.PARAGRAPH, text=line))
        return ParsedDocument(
            document_id=document_id,
            source_name=Path(path).name,
            source_format="md",
            blocks=tuple(blocks),
        )


class SpyChunker:
    """Wraps a real Chunker, counting chunk() invocations (resume must not recompute)."""

    def __init__(self, inner: Chunker) -> None:
        self._inner = inner
        self.calls = 0

    def chunk(self, doc: ParsedDocument, *, acl: Sequence[str] = ()) -> tuple[Chunk, ...]:
        self.calls += 1
        return self._inner.chunk(doc, acl=acl)


class FakeChunkStore:
    """In-memory ChunkStore recording upserts/deletes, with injectable failures."""

    def __init__(self) -> None:
        self.points: dict[str, Chunk] = {}
        self.upsert_calls = 0
        self.deleted: list[str] = []
        self.fail_upsert_times = 0
        self.fail_delete_times = 0
        self.payload_update_calls = 0

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        if self.fail_upsert_times > 0:
            self.fail_upsert_times -= 1
            raise RuntimeError("store upsert failed")
        self.upsert_calls += 1
        for chunk in chunks:
            self.points[chunk.chunk_id] = chunk

    async def update_payload(self, chunks: Sequence[Chunk]) -> None:
        """PayloadUpdatable: overwrite only acl/metadata of already-indexed chunks."""
        self.payload_update_calls += 1
        for chunk in chunks:
            existing = self.points.get(chunk.chunk_id)
            if existing is None:
                continue  # unknown id silently skipped, mirroring the real store
            self.points[chunk.chunk_id] = Chunk(
                chunk_id=existing.chunk_id,
                document_id=existing.document_id,
                text=existing.text,
                metadata=chunk.metadata,
                acl=chunk.acl,
            )

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        return [self.points[cid] for cid in chunk_ids if cid in self.points]

    async def query(
        self,
        *,
        dense: Sequence[float] | None = None,
        sparse: SparseVector | None = None,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[ScoredChunk]:  # pragma: no cover - unused in spine tests
        raise NotImplementedError

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        if self.fail_delete_times > 0:
            self.fail_delete_times -= 1
            raise RuntimeError("store delete failed")
        for cid in chunk_ids:
            self.points.pop(cid, None)
            self.deleted.append(cid)


class FakeChunkStoreNoPayloadUpdate:
    """A minimal ChunkStore WITHOUT update_payload (isinstance PayloadUpdatable is False).

    Proves the spine's cheap-path fallback: an acl/metadata change on a store
    lacking the capability is corrected via a full upsert_chunks instead.
    """

    def __init__(self) -> None:
        self.points: dict[str, Chunk] = {}
        self.upsert_calls = 0
        self.deleted: list[str] = []

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        self.upsert_calls += 1
        for chunk in chunks:
            self.points[chunk.chunk_id] = chunk

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        return [self.points[cid] for cid in chunk_ids if cid in self.points]

    async def query(
        self,
        *,
        dense: Sequence[float] | None = None,
        sparse: SparseVector | None = None,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[ScoredChunk]:  # pragma: no cover - unused in spine tests
        raise NotImplementedError

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        for cid in chunk_ids:
            self.points.pop(cid, None)
            self.deleted.append(cid)


class FakeEnricher:
    """Stub Enricher: prepends a context marker; can raise a fixed number of times."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    async def enrich(self, doc: ParsedDocument, chunks: Sequence[Chunk]) -> tuple[Chunk, ...]:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("enricher gateway failed")
        return tuple(
            Chunk(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                text=f"context\n\n{chunk.text}",
                metadata=chunk.metadata,
                acl=chunk.acl,
            )
            for chunk in chunks
        )


def _write(path: Path, lines: Sequence[str]) -> Path:
    path.write_text("\n".join(lines) + "\n")
    return path


def _pipeline(
    tmp_path: Path,
    *,
    parser: FakeParser | None = None,
    chunker: Chunker | None = None,
    store: FakeChunkStore | None = None,
    enricher: FakeEnricher | None = None,
) -> tuple[IngestionPipeline, FakeParser, FakeChunkStore, SqliteIngestLedger]:
    parser = parser or FakeParser()
    store = store or FakeChunkStore()
    ledger = SqliteIngestLedger(tmp_path / "ledger.db")
    pipeline = IngestionPipeline(
        parser,
        chunker or StructuralChunker(),
        store,
        ledger,
        enricher=enricher,
    )
    return pipeline, parser, store, ledger


def test_reingest_unchanged_is_noop_unchanged_status(tmp_path: Path) -> None:
    src = IngestSource(
        path=_write(tmp_path / "doc.md", ["Alpha content", "Beta content", "Gamma content"]),
        document_id="doc",
    )
    pipeline, parser, store, _ = _pipeline(tmp_path)

    first = asyncio.run(pipeline.ingest(src))
    assert first.status == IngestStatus.INDEXED
    assert first.chunk_count == 3

    second = asyncio.run(pipeline.ingest(src))
    assert second.status == IngestStatus.UNCHANGED
    assert second.chunk_count == 3
    # Idempotent: no re-parse, no second upsert, no duplicate chunk ids.
    assert parser.calls == 1
    assert store.upsert_calls == 1
    assert len(store.points) == 3
    assert len(set(store.points)) == len(store.points)


def test_changed_document_supersedes_and_deletes_stale_chunks(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    src = IngestSource(path=_write(path, ["one", "two", "three"]), document_id="doc")
    pipeline, _, store, ledger = _pipeline(tmp_path)

    asyncio.run(pipeline.ingest(src))
    assert set(store.points) == {"doc:0000", "doc:0001", "doc:0002"}

    # v2 has FEWER chunks under the same document_id.
    _write(path, ["one", "two"])
    outcome = asyncio.run(pipeline.ingest(src))
    assert outcome.status == IngestStatus.INDEXED
    # Exactly the old-minus-new chunk id was deleted.
    assert store.deleted == ["doc:0002"]
    assert set(store.points) == {"doc:0000", "doc:0001"}
    state = ledger.get_state("doc")
    assert state is not None
    assert state.indexed_content_hash == outcome.content_hash
    assert state.indexed_chunk_ids == ("doc:0000", "doc:0001")


def test_parse_failure_quarantines_and_batch_continues(tmp_path: Path) -> None:
    sources = [
        IngestSource(path=_write(tmp_path / "a.md", ["alpha"]), document_id="A"),
        IngestSource(path=_write(tmp_path / "b.md", ["__FAIL__"]), document_id="B"),
        IngestSource(path=_write(tmp_path / "c.md", ["gamma"]), document_id="C"),
    ]
    pipeline, _, store, ledger = _pipeline(tmp_path)

    outcomes = asyncio.run(pipeline.ingest_batch(sources))
    statuses = [o.status for o in outcomes]
    assert statuses == [
        IngestStatus.INDEXED,
        IngestStatus.QUARANTINED,
        IngestStatus.INDEXED,
    ]
    # The index holds ONLY the good docs' chunks (bad doc never partially wrote).
    assert {c.document_id for c in store.points.values()} == {"A", "C"}
    quarantined = ledger.list_quarantined()
    assert [q.document_id for q in quarantined] == ["B"]
    assert "parse failed" in quarantined[0].reason


def test_quarantined_same_hash_not_reattempted_without_force(tmp_path: Path) -> None:
    src = IngestSource(path=_write(tmp_path / "bad.md", ["__FAIL__"]), document_id="bad")
    pipeline, parser, _, _ = _pipeline(tmp_path)

    first = asyncio.run(pipeline.ingest(src))
    assert first.status == IngestStatus.QUARANTINED
    assert parser.calls == 1

    second = asyncio.run(pipeline.ingest(src))
    assert second.status == IngestStatus.QUARANTINED
    # Deterministic failure on identical bytes: no re-parse.
    assert parser.calls == 1


def test_force_reattempts_quarantined_document(tmp_path: Path) -> None:
    src = IngestSource(path=_write(tmp_path / "bad.md", ["__FAIL__"]), document_id="bad")
    pipeline, parser, _, _ = _pipeline(tmp_path)

    asyncio.run(pipeline.ingest(src))
    assert parser.calls == 1

    forced = asyncio.run(pipeline.ingest(src, force=True))
    assert forced.status == IngestStatus.QUARANTINED
    # force bypasses the quarantine short-circuit and re-attempts the parse.
    assert parser.calls == 2


def test_empty_parse_result_quarantines(tmp_path: Path) -> None:
    src = IngestSource(path=_write(tmp_path / "empty.md", ["__EMPTY__"]), document_id="e")
    pipeline, _, store, ledger = _pipeline(tmp_path)

    outcome = asyncio.run(pipeline.ingest(src))
    assert outcome.status == IngestStatus.QUARANTINED
    assert outcome.reason == "empty document"
    assert not store.points
    assert [q.document_id for q in ledger.list_quarantined()] == ["e"]


def test_index_failure_is_retryable_resume_completes_without_reparse(tmp_path: Path) -> None:
    src = IngestSource(path=_write(tmp_path / "doc.md", ["one", "two"]), document_id="doc")
    store = FakeChunkStore()
    store.fail_upsert_times = 1
    pipeline, parser, _, ledger = _pipeline(tmp_path, store=store)

    failed = asyncio.run(pipeline.ingest(src))
    assert failed.status == IngestStatus.FAILED
    state = ledger.get_state("doc")
    assert state is not None
    # Checkpoint stayed at CHUNKED (retryable), reason recorded.
    assert state.stage == DocStage.CHUNKED
    assert state.reason is not None and "index failed" in state.reason

    resumed = asyncio.run(pipeline.ingest(src))
    assert resumed.status == IngestStatus.INDEXED
    # Resume reused the PARSE checkpoint — the parser ran only once.
    assert parser.calls == 1
    assert set(store.points) == {"doc:0000", "doc:0001"}


def test_resume_after_chunk_checkpoint_indexes_current_acl_not_stale_chunk_acl(
    tmp_path: Path,
) -> None:
    """Resume-path variant of the P2a stale-ACL class (regression for the P2c fix).

    A doc fails after the CHUNKED checkpoint persists (with acl=(group-a,
    group-b) baked into the checkpointed chunk artifact by that first attempt's
    chunker.chunk(..., acl=source.acl) call), then is retried with a NARROWED
    acl=(group-a,). Because the first attempt never reached ledger.record_indexed,
    indexed_content_hash stays None, so the retry does NOT take the 3-way
    unchanged/refresh short-circuit (that only fires once a version is already
    live) -- it re-enters the full pipeline and reloads the STALE, wider-acl
    CHUNKED artifact at step (4). Step (6) must still index the CURRENT
    (narrowed) acl, not the stale one baked into the reloaded chunks, and the
    ledger's indexed_acl must match what actually landed in the store.
    """
    path = _write(tmp_path / "doc.md", ["Alpha", "Beta"])
    store = FakeChunkStore()
    store.fail_upsert_times = 1
    pipeline, parser, _, ledger = _pipeline(tmp_path, store=store)

    failed = asyncio.run(
        pipeline.ingest(IngestSource(path=path, document_id="doc", acl=("group-a", "group-b")))
    )
    assert failed.status == IngestStatus.FAILED
    state = ledger.get_state("doc")
    assert state is not None
    # Checkpoint persisted at CHUNKED with the WIDER acl baked into the artifact;
    # never reached record_indexed, so indexed_content_hash is still None.
    assert state.stage == DocStage.CHUNKED
    assert state.indexed_content_hash is None

    # Retry with a NARROWED acl -- group-b is dropped.
    resumed = asyncio.run(
        pipeline.ingest(IngestSource(path=path, document_id="doc", acl=("group-a",)))
    )
    assert resumed.status == IngestStatus.INDEXED
    # Resume reused the PARSE checkpoint: no re-parse.
    assert parser.calls == 1

    # The INDEXED chunks in the store carry the CURRENT (narrowed) acl -- a
    # group-b principal provably cannot retrieve them.
    assert store.points
    assert all(c.acl == ("group-a",) for c in store.points.values())
    assert all("group-b" not in c.acl for c in store.points.values())

    # Ledger and store must AGREE: no index/ledger disagreement.
    final_state = ledger.get_state("doc")
    assert final_state is not None
    assert final_state.indexed_acl == ("group-a",)
    for cid in final_state.indexed_chunk_ids:
        assert store.points[cid].acl == final_state.indexed_acl


def test_enrich_failure_is_retryable_and_resume_skips_parse_and_chunk(tmp_path: Path) -> None:
    src = IngestSource(path=_write(tmp_path / "doc.md", ["one", "two"]), document_id="doc")
    chunker = SpyChunker(StructuralChunker())
    enricher = FakeEnricher(fail_times=1)
    pipeline, parser, store, ledger = _pipeline(tmp_path, chunker=chunker, enricher=enricher)

    failed = asyncio.run(pipeline.ingest(src))
    assert failed.status == IngestStatus.FAILED
    state = ledger.get_state("doc")
    assert state is not None
    # Gateway-stage failure is FAILED (not quarantine) and stays at CHUNKED.
    assert state.stage == DocStage.CHUNKED
    assert parser.calls == 1
    assert chunker.calls == 1

    resumed = asyncio.run(pipeline.ingest(src))
    assert resumed.status == IngestStatus.INDEXED
    # Resume skipped BOTH parse and chunk (their checkpoints were reused).
    assert parser.calls == 1
    assert chunker.calls == 1
    assert enricher.calls == 2
    # The enrichment context landed in the indexed chunks.
    assert all(chunk.text.startswith("context\n\n") for chunk in store.points.values())


def test_crash_between_upsert_and_ledger_commit_recovers(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    src = IngestSource(path=_write(path, ["one", "two", "three"]), document_id="doc")
    store = FakeChunkStore()
    pipeline, _, _, ledger = _pipeline(tmp_path, store=store)

    asyncio.run(pipeline.ingest(src))
    assert set(store.points) == {"doc:0000", "doc:0001", "doc:0002"}

    # v2 has fewer chunks -> supersede must delete doc:0002; make that delete
    # crash once (mid-supersede, after the new upsert has already landed).
    _write(path, ["one", "two"])
    store.fail_delete_times = 1
    failed = asyncio.run(pipeline.ingest(src))
    assert failed.status == IngestStatus.FAILED

    # Retry heals: stale id gone, ledger reflects exactly v2.
    resumed = asyncio.run(pipeline.ingest(src))
    assert resumed.status == IngestStatus.INDEXED
    assert set(store.points) == {"doc:0000", "doc:0001"}
    state = ledger.get_state("doc")
    assert state is not None
    assert state.indexed_chunk_ids == ("doc:0000", "doc:0001")


def test_resumed_chunks_identical_to_fresh(tmp_path: Path) -> None:
    lines = ["alpha section body", "beta section body"]

    # Fresh, from-scratch run.
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh_src = IngestSource(path=_write(fresh_dir / "doc.md", lines), document_id="doc")
    fresh_pipeline, _, fresh_store, _ = _pipeline(fresh_dir)
    assert asyncio.run(fresh_pipeline.ingest(fresh_src)).status == IngestStatus.INDEXED

    # Resumed run (first upsert fails, then retry completes).
    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    resume_src = IngestSource(path=_write(resume_dir / "doc.md", lines), document_id="doc")
    resume_store = FakeChunkStore()
    resume_store.fail_upsert_times = 1
    resume_pipeline, _, _, _ = _pipeline(resume_dir, store=resume_store)
    assert asyncio.run(resume_pipeline.ingest(resume_src)).status == IngestStatus.FAILED
    assert asyncio.run(resume_pipeline.ingest(resume_src)).status == IngestStatus.INDEXED

    # The normalization rule: resumed chunks are byte-identical (id/text/metadata)
    # to the from-scratch run.
    assert fresh_store.points == resume_store.points


def test_reingest_same_bytes_changed_acl_updates_payload_not_full_rebuild(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.md", ["Alpha", "Beta"])
    pipeline, parser, store, _ = _pipeline(tmp_path)

    first = asyncio.run(
        pipeline.ingest(IngestSource(path=path, document_id="doc", acl=("group-a", "group-b")))
    )
    assert first.status == IngestStatus.INDEXED
    assert all(c.acl == ("group-a", "group-b") for c in store.points.values())

    # SAME bytes, narrowed acl.
    second = asyncio.run(
        pipeline.ingest(IngestSource(path=path, document_id="doc", acl=("group-a",)))
    )
    assert second.status == IngestStatus.INDEXED
    assert second.content_hash == first.content_hash
    # The newly-excluded principal ('group-b') is gone from every indexed chunk.
    assert all(c.acl == ("group-a",) for c in store.points.values())
    assert all("group-b" not in c.acl for c in store.points.values())
    # Cheap payload-only correction: no re-parse, no full upsert.
    assert parser.calls == 1
    assert store.upsert_calls == 1
    assert store.payload_update_calls == 1


def test_reingest_same_bytes_changed_source_metadata_updates_payload(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.md", ["Alpha", "Beta"])
    pipeline, _, store, _ = _pipeline(tmp_path)

    asyncio.run(
        pipeline.ingest(IngestSource(path=path, document_id="doc", metadata={"department": "eng"}))
    )
    assert all(dict(c.metadata["source"]) == {"department": "eng"} for c in store.points.values())

    second = asyncio.run(
        pipeline.ingest(
            IngestSource(path=path, document_id="doc", metadata={"department": "legal"})
        )
    )
    assert second.status == IngestStatus.INDEXED
    # Old source metadata fully replaced, not merged.
    assert all(dict(c.metadata["source"]) == {"department": "legal"} for c in store.points.values())
    assert store.payload_update_calls == 1


def test_source_metadata_propagates_into_indexed_chunk_metadata(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.md", ["Alpha", "Beta"])
    pipeline, _, store, _ = _pipeline(tmp_path)

    outcome = asyncio.run(
        pipeline.ingest(
            IngestSource(
                path=path,
                document_id="doc",
                metadata={"department": "eng", "provenance": "sharepoint:doc123"},
            )
        )
    )
    assert outcome.status == IngestStatus.INDEXED
    for chunk in store.points.values():
        assert dict(chunk.metadata["source"]) == {
            "department": "eng",
            "provenance": "sharepoint:doc123",
        }
        # The chunker's structural keys survive untouched alongside 'source'.
        assert "headings" in chunk.metadata


def test_reingest_same_bytes_changed_acl_falls_back_to_upsert_without_payload_updatable(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path / "doc.md", ["Alpha", "Beta"])
    store = FakeChunkStoreNoPayloadUpdate()
    pipeline = IngestionPipeline(
        FakeParser(),
        StructuralChunker(),
        store,
        SqliteIngestLedger(tmp_path / "ledger.db"),
    )

    asyncio.run(
        pipeline.ingest(IngestSource(path=path, document_id="doc", acl=("group-a", "group-b")))
    )
    assert store.upsert_calls == 1

    second = asyncio.run(
        pipeline.ingest(IngestSource(path=path, document_id="doc", acl=("group-a",)))
    )
    assert second.status == IngestStatus.INDEXED
    # No PayloadUpdatable capability -> the acl correction goes via a full upsert.
    assert store.upsert_calls == 2
    assert all(c.acl == ("group-a",) for c in store.points.values())


# -- NE-CKC2: 4th stale-short-circuit variant + infra-vs-doc parse taxonomy ----


class _FlakyEnvParser:
    """Wraps an inner parser; raises a fixed exception for the first ``fail_times``
    calls, then delegates. Models an infra-caused parse-env failure that later heals.
    """

    def __init__(self, inner: FakeParser, *, fail_times: int, exc: Exception) -> None:
        self._inner = inner
        self.fail_times = fail_times
        self._exc = exc
        self.calls = 0

    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self._exc
        return await self._inner.parse(path, document_id=document_id)


def test_revert_after_failed_newer_version_reingests_not_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    src = IngestSource(path=_write(path, ["one", "two", "three"]), document_id="doc")
    pipeline, _, store, _ = _pipeline(tmp_path, store=FakeChunkStore())

    first = asyncio.run(pipeline.ingest(src))
    assert first.status == IngestStatus.INDEXED
    assert set(store.points) == {"doc:0000", "doc:0001", "doc:0002"}

    # A newer version whose delete-stale step crashes AFTER it has already
    # overwritten the shared positional point ids doc:0000/doc:0001 with v2 text.
    _write(path, ["ONE-v2", "TWO-v2"])
    store.fail_delete_times = 1
    v2 = asyncio.run(pipeline.ingest(IngestSource(path=path, document_id="doc")))
    assert v2.status == IngestStatus.FAILED
    # The store is now poisoned: doc:0000/doc:0001 hold v2 content, doc:0002 stale.
    assert any("v2" in c.text for c in store.points.values())

    # Byte-revert to v1. The OLD (indexed_content_hash-only) gate would short-
    # circuit to UNCHANGED and serve the poisoned content forever; the strengthened
    # gate FAILS CLOSED into the full pipeline and heals the store.
    _write(path, ["one", "two", "three"])
    reverted = asyncio.run(pipeline.ingest(IngestSource(path=path, document_id="doc")))
    assert reverted.status == IngestStatus.INDEXED
    assert set(store.points) == {"doc:0000", "doc:0001", "doc:0002"}
    assert all("v2" not in c.text for c in store.points.values())  # wrong content healed

    # Steady state restored (reason cleared, same version) -> a true no-op.
    fourth = asyncio.run(pipeline.ingest(IngestSource(path=path, document_id="doc")))
    assert fourth.status == IngestStatus.UNCHANGED


def test_revert_after_failed_newer_version_with_changed_acl_takes_full_pipeline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "doc.md"
    store = FakeChunkStore()
    pipeline, _, _, _ = _pipeline(tmp_path, store=store)

    _write(path, ["one", "two", "three"])
    first = asyncio.run(
        pipeline.ingest(IngestSource(path=path, document_id="doc", acl=("group-a",)))
    )
    assert first.status == IngestStatus.INDEXED

    _write(path, ["ONE-v2", "TWO-v2"])
    store.fail_delete_times = 1
    v2 = asyncio.run(pipeline.ingest(IngestSource(path=path, document_id="doc", acl=("group-a",))))
    assert v2.status == IngestStatus.FAILED

    # Revert bytes AND change the acl. The cheap payload-refresh path must NOT run
    # (it would rewrite acl over the still-poisoned v2 point content); the full
    # pipeline runs instead and heals both content and acl.
    _write(path, ["one", "two", "three"])
    reverted = asyncio.run(
        pipeline.ingest(IngestSource(path=path, document_id="doc", acl=("group-b",)))
    )
    assert reverted.status == IngestStatus.INDEXED
    assert store.payload_update_calls == 0
    assert all("v2" not in c.text for c in store.points.values())
    assert all(c.acl == ("group-b",) for c in store.points.values())


def _flaky_pipeline(
    tmp_path: Path, parser: _FlakyEnvParser
) -> tuple[IngestionPipeline, SqliteIngestLedger]:
    ledger = SqliteIngestLedger(tmp_path / "ledger.db")
    pipeline = IngestionPipeline(parser, StructuralChunker(), FakeChunkStore(), ledger)
    return pipeline, ledger


def test_parser_environment_error_is_failed_not_quarantined(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.md", ["alpha", "beta"])
    parser = _FlakyEnvParser(
        FakeParser(), fail_times=1, exc=ParserEnvironmentError("venv build blew up")
    )
    pipeline, ledger = _flaky_pipeline(tmp_path, parser)

    src = IngestSource(path=path, document_id="doc")
    first = asyncio.run(pipeline.ingest(src))
    assert first.status == IngestStatus.FAILED
    assert ledger.list_quarantined() == ()  # infra-caused: never quarantined

    # The env healed — a retry completes (retryable proof).
    second = asyncio.run(pipeline.ingest(src))
    assert second.status == IngestStatus.INDEXED


def test_air_gap_violation_during_parse_is_failed_not_quarantined(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.md", ["alpha", "beta"])
    parser = _FlakyEnvParser(
        FakeParser(), fail_times=1, exc=AirGapViolationError("air-gapped, no cache")
    )
    pipeline, ledger = _flaky_pipeline(tmp_path, parser)

    outcome = asyncio.run(pipeline.ingest(IngestSource(path=path, document_id="doc")))
    assert outcome.status == IngestStatus.FAILED
    assert ledger.list_quarantined() == ()
