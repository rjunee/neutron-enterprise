# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Durable ingest-ledger unit tests (NE-026)."""

from __future__ import annotations

from pathlib import Path

from ne.brain.ingest.ledger import DocStage, SqliteIngestLedger


def test_state_roundtrip_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    writer = SqliteIngestLedger(db)
    writer.begin("doc-1", "hash-abc")
    writer.checkpoint("doc-1", "hash-abc", DocStage.PARSED)
    writer.checkpoint("doc-1", "hash-abc", DocStage.CHUNKED)

    # A brand-new ledger instance on the SAME file (a fresh process) must see the
    # committed state — durability of the checkpoints.
    reader = SqliteIngestLedger(db)
    state = reader.get_state("doc-1")
    assert state is not None
    assert state.document_id == "doc-1"
    assert state.content_hash == "hash-abc"
    assert state.stage == DocStage.CHUNKED
    assert reader.get_state("missing") is None


def test_artifact_save_load_and_cleanup_on_indexed(tmp_path: Path) -> None:
    ledger = SqliteIngestLedger(tmp_path / "ledger.db")
    ledger.begin("doc-2", "h2")
    ledger.save_artifact("doc-2", "h2", DocStage.PARSED, '{"parsed": true}')
    ledger.save_artifact("doc-2", "h2", DocStage.CHUNKED, "[1,2,3]")

    assert ledger.load_artifact("doc-2", "h2", DocStage.PARSED) == '{"parsed": true}'
    assert ledger.load_artifact("doc-2", "h2", DocStage.CHUNKED) == "[1,2,3]"
    # Hash-keyed: a different content hash does not read another version's artifact.
    assert ledger.load_artifact("doc-2", "other-hash", DocStage.PARSED) is None

    ledger.record_indexed("doc-2", "h2", ["doc-2:0000", "doc-2:0001"])

    state = ledger.get_state("doc-2")
    assert state is not None
    assert state.stage == DocStage.INDEXED
    assert state.indexed_content_hash == "h2"
    assert state.indexed_chunk_ids == ("doc-2:0000", "doc-2:0001")
    # record_indexed is transactional and prunes ALL of the document's artifacts.
    assert ledger.load_artifact("doc-2", "h2", DocStage.PARSED) is None
    assert ledger.load_artifact("doc-2", "h2", DocStage.CHUNKED) is None


def test_quarantine_recorded_and_listed(tmp_path: Path) -> None:
    ledger = SqliteIngestLedger(tmp_path / "ledger.db")
    ledger.quarantine("bad-doc", "hbad", "parse failed: boom")

    records = ledger.list_quarantined()
    assert len(records) == 1
    record = records[0]
    assert record.document_id == "bad-doc"
    assert record.content_hash == "hbad"
    assert record.reason == "parse failed: boom"
    # created_at is surfaced (inspectable, not silently dropped).
    assert record.created_at
    # The documents-row mirror also reflects the quarantine.
    state = ledger.get_state("bad-doc")
    assert state is not None
    assert state.stage == DocStage.QUARANTINED
    assert state.reason == "parse failed: boom"


def test_record_indexed_persists_acl_and_metadata(tmp_path: Path) -> None:
    ledger = SqliteIngestLedger(tmp_path / "ledger.db")
    ledger.begin("doc-4", "h4")
    ledger.record_indexed(
        "doc-4", "h4", ["doc-4:0000"], acl=("group-a",), metadata={"department": "eng"}
    )
    state = ledger.get_state("doc-4")
    assert state is not None
    assert state.indexed_acl == ("group-a",)
    assert dict(state.indexed_metadata) == {"department": "eng"}


def test_record_indexed_defaults_acl_and_metadata_when_omitted(tmp_path: Path) -> None:
    ledger = SqliteIngestLedger(tmp_path / "ledger.db")
    ledger.begin("doc-5", "h5")
    # The pre-existing positional call form (no acl/metadata kwargs) must keep working.
    ledger.record_indexed("doc-5", "h5", ["doc-5:0000"])
    state = ledger.get_state("doc-5")
    assert state is not None
    assert state.indexed_acl == ()
    assert dict(state.indexed_metadata) == {}


def test_changed_hash_begin_preserves_live_index_version(tmp_path: Path) -> None:
    # A version change must PRESERVE the currently-live indexed_* fields so a
    # crash before the new version indexes can still supersede the old one.
    ledger = SqliteIngestLedger(tmp_path / "ledger.db")
    ledger.begin("doc-3", "v1")
    ledger.record_indexed("doc-3", "v1", ["doc-3:0000", "doc-3:0001"])
    ledger.save_artifact("doc-3", "v1", DocStage.PARSED, "stale")

    ledger.begin("doc-3", "v2")
    state = ledger.get_state("doc-3")
    assert state is not None
    assert state.content_hash == "v2"
    assert state.stage is None  # fresh start for the new version
    assert state.indexed_content_hash == "v1"
    assert state.indexed_chunk_ids == ("doc-3:0000", "doc-3:0001")
    # Old-version artifacts are dropped on the version change.
    assert ledger.load_artifact("doc-3", "v1", DocStage.PARSED) is None
