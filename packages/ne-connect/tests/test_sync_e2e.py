# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end sync seam over REAL components (no mock past the seam).

FixtureConnector -> SyncRunner -> KnowledgeWriter -> QdrantChunkStore(:memory:).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from ne.audit.store import ne_audit_log
from ne.brain.retriever import HybridRetriever, citation_for
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.write import CuratedKnowledgeItem, ProvenanceClass, provenance_from_chunk
from ne.connect import (
    AclResolutionError,
    OdcsDataContract,
    PermissionMode,
    PermissionModelSpec,
    PiiClass,
    SyncRunner,
    default_record_mapper,
)
from ne.connect.testing.fixture_connector import FixtureConnector
from ne.contracts import ConfigHashNotFrozenError, reset_config_hash_for_testing
from ne.contracts.integration import AclEntry, Record
from ne.contracts.knowledge import KnowledgeItem, KnowledgeWrite, UpsertReceipt
from ne.contracts.types import Principal

from .conftest import FIXTURE_SOURCE_PATH, WriterStack

_STRICT_SCHEMA = {
    "type": "object",
    "required": ["name", "email"],
    "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
}
_PERMISSIVE_SCHEMA = {
    "type": "object",
    "required": ["name"],
    "properties": {"name": {"type": "string"}},
}


def _contract(schema: dict[str, object], **overrides: object) -> OdcsDataContract:
    kwargs: dict[str, object] = {
        "source": "acme-crm",
        "owner": "data-eng@acme.test",
        "schema_def": schema,
        "freshness_sla": "PT30M",
        "pii": PiiClass.PERSONAL,
        "permissions": PermissionModelSpec(mode=PermissionMode.SOURCE_ACL),
    }
    kwargs.update(overrides)
    return OdcsDataContract(**kwargs)  # type: ignore[arg-type]


def _item_id(source: str, source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{source}\n{source_id}\n{record_id}".encode()).hexdigest()


def _fixture_data() -> dict[str, object]:
    return json.loads(FIXTURE_SOURCE_PATH.read_text(encoding="utf-8"))


class _FailingAclIterator:
    """Source-boundary failure whose message must never reach terminal audit detail."""

    def __aiter__(self) -> _FailingAclIterator:
        return self

    async def __anext__(self) -> AclEntry:
        raise ConnectionError("https://user:secret@source.invalid/permissions")


class _FailingPermissionsConnector(FixtureConnector):
    def permissions(self) -> _FailingAclIterator:
        return _FailingAclIterator()


def _runner(
    stack: WriterStack,
    connector: FixtureConnector,
    contract: OdcsDataContract,
    state_store,
    **kwargs: object,
) -> SyncRunner:
    return SyncRunner(
        connector,
        contract,
        stack.writer,
        state_store,
        actor="connector:test",
        audit_sink=stack.sink,
        cursor_field="updated_at",
        **kwargs,  # type: ignore[arg-type]
    )


def _audit_rows(stack: WriterStack) -> list[dict[str, object]]:
    with stack.engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(sa.select(ne_audit_log).order_by(ne_audit_log.c.id)).mappings()
        ]


def _sync_events(stack: WriterStack) -> list[dict[str, object]]:
    return [r for r in _audit_rows(stack) if r["action"] == "connector.sync"]


def test_default_mapper_stamps_graph_equal_source_identity_without_changing_governance() -> None:
    contract = _contract(_PERMISSIVE_SCHEMA)
    record = Record(
        record_id="contact-7",
        source_id="acme-crm/contacts",
        payload={"name": "Ada"},
    )

    item = default_record_mapper(record, contract, ("oidc:sales",))

    assert item.item_id == _item_id("acme-crm", "acme-crm/contacts", "contact-7")
    assert item.metadata == {
        "connector": "acme-crm",
        "source_id": "acme-crm/contacts",
        "record_id": "contact-7",
        "source_system": "acme-crm",
        "source_ref": "acme-crm/contacts:contact-7",
        "source_snapshot_hash": (
            "dff8e5a3ad92ebd4a2a21934bdc639fa3258e98d175ca47dd3f502aad435a77c"
        ),
        "source_acl_hash": "4906553244f027192f07e7d2cc843ae5c8d896766e8e22522f6e29c8d89a6800",
        "pii_class": "personal",
    }
    assert item.acl == ("oidc:sales",)
    assert item.provenance == "connector:acme-crm:contact-7"
    assert item.provenance_class == ProvenanceClass.INGESTED


def test_source_identity_degrades_exactly_as_the_existing_record_contract() -> None:
    contract = _contract(_PERMISSIVE_SCHEMA)

    # Empty strings are valid in the frozen Record contract. Preserve that
    # behavior and the graph's exact source_id:record_id grammar; do not invent
    # a URL or silently substitute the opaque document hash.
    empty = Record(record_id="", source_id="", payload={})
    item = default_record_mapper(empty, contract, ())
    assert item.metadata["source_ref"] == ":"
    assert item.metadata["source_system"] == "acme-crm"

    # Non-string identifiers remain the Record contract's validation error.
    with pytest.raises(ValidationError):
        Record(record_id=None, source_id="records", payload={})  # type: ignore[arg-type]


def test_backfill_writes_acl_pii_provenance_stamped_chunks(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    from ne.connect import ConnectorStateStore

    stack = writer_stack
    contract = _contract(_STRICT_SCHEMA)
    connector = FixtureConnector.from_path(FIXTURE_SOURCE_PATH)
    runner = _runner(stack, connector, contract, ConnectorStateStore())

    async def scenario() -> None:
        report = await runner.run(mode="backfill")
        # a1 (accounts) lacks 'email' -> invalid; c1, c2 written.
        assert report.records_seen == 3
        assert report.written == 2
        assert report.invalid == 1
        assert report.skipped_unchanged == 0
        assert report.quarantined == 0

        item_id = _item_id("acme-crm", "acme-crm/contacts", "c1")
        assert ":" not in item_id
        chunks = await stack.store.get([item_id])
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.acl == ("oidc:sales",)
        assert chunk.metadata["pii_class"] == "personal"
        assert chunk.metadata["source_system"] == "acme-crm"
        assert chunk.metadata["source_ref"] == "acme-crm/contacts:c1"
        citation = citation_for(chunk)
        assert citation.source_system == "acme-crm"
        assert citation.source_ref == "acme-crm/contacts:c1"
        assert citation.source_ref != citation.document_id
        stamp = provenance_from_chunk(chunk)
        assert stamp is not None
        assert stamp.provenance_class == ProvenanceClass.INGESTED

        # Two source-native record identities remain distinct through storage
        # and citation construction; neither collapses to the document hash.
        c2_id = _item_id("acme-crm", "acme-crm/contacts", "c2")
        [c2_chunk] = await stack.store.get([c2_id])
        c2_citation = citation_for(c2_chunk)
        assert c2_id != item_id
        assert c2_citation.source_ref == "acme-crm/contacts:c2"
        assert c2_citation.source_ref != citation.source_ref
        assert c2_citation.source_ref != c2_citation.document_id

        # "schema honored": the invalid a1 record was NOT written.
        a1_id = _item_id("acme-crm", "acme-crm/accounts", "a1")
        assert await stack.store.get([a1_id]) == []

    asyncio.run(scenario())


def test_acl_trimming_removes_citation_identity_for_invisible_record(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    from ne.connect import ConnectorStateStore

    stack = writer_stack
    runner = _runner(
        stack,
        FixtureConnector.from_path(FIXTURE_SOURCE_PATH),
        _contract(_STRICT_SCHEMA),
        ConnectorStateStore(),
    )
    retriever = HybridRetriever(
        stack.store,
        HashEmbedder(32),
        HashSparseEncoder(),
        acl_filters=lambda principal: {"acl": (principal.id,)},
        audit_sink=None,
    )
    item_id = _item_id("acme-crm", "acme-crm/contacts", "c1")

    async def scenario() -> None:
        await runner.run(mode="backfill")

        visible = await retriever.retrieve([item_id], principal=Principal(id="oidc:sales"))
        assert [citation.source_ref for citation in visible.citations] == ["acme-crm/contacts:c1"]

        invisible = await retriever.retrieve([item_id], principal=Principal(id="oidc:finance"))
        assert invisible.chunks == ()
        assert invisible.citations == ()

    asyncio.run(scenario())


def test_sync_runner_normalizes_source_identity_from_custom_mapper(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    """Custom SQL/sibling-style mappers cannot silently drift from the default."""
    from ne.connect import ConnectorStateStore

    def custom_mapper(
        record: Record, contract: OdcsDataContract, acl: tuple[str, ...]
    ) -> CuratedKnowledgeItem:
        return CuratedKnowledgeItem(
            item_id=_item_id(contract.source, record.source_id, record.record_id),
            content=json.dumps(dict(record.payload), sort_keys=True),
            metadata={
                "custom": "preserved",
                "source_url": "https://source.example/records/contact-7",
                "source_system": "stale-mapper-value",
                "source_ref": "stale-mapper-value",
            },
            provenance=f"connector:{contract.source}:{record.record_id}",
            provenance_class=ProvenanceClass.INGESTED,
            acl=acl,
        )

    stack = writer_stack
    runner = _runner(
        stack,
        FixtureConnector.from_path(FIXTURE_SOURCE_PATH),
        _contract(_STRICT_SCHEMA),
        ConnectorStateStore(),
        mapper=custom_mapper,
    )
    item_id = _item_id("acme-crm", "acme-crm/contacts", "c1")

    async def scenario() -> None:
        await runner.run(mode="backfill")
        [chunk] = await stack.store.get([item_id])
        assert chunk.chunk_id == item_id
        assert chunk.acl == ("oidc:sales",)
        assert chunk.metadata["custom"] == "preserved"
        assert chunk.metadata["source_url"] == "https://source.example/records/contact-7"
        assert chunk.metadata["source_system"] == "acme-crm"
        assert chunk.metadata["source_ref"] == "acme-crm/contacts:c1"
        assert citation_for(chunk).source_ref == "acme-crm/contacts:c1"

    asyncio.run(scenario())


def test_dedup_second_backfill_writes_zero(writer_stack: WriterStack, frozen_config: str) -> None:
    from ne.connect import ConnectorStateStore

    stack = writer_stack
    contract = _contract(_STRICT_SCHEMA)
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        first = await _runner(
            stack, FixtureConnector.from_path(FIXTURE_SOURCE_PATH), contract, state_store
        ).run(mode="backfill")
        assert first.written == 2

        second = await _runner(
            stack, FixtureConnector.from_path(FIXTURE_SOURCE_PATH), contract, state_store
        ).run(mode="backfill")
        assert second.written == 0
        assert second.skipped_unchanged == 2

    asyncio.run(scenario())


def test_mapper_revision_refreshes_source_link_metadata_and_failed_refresh_preserves_state(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    from ne.connect import ConnectorStateStore

    stack = writer_stack
    contract = _contract(_STRICT_SCHEMA)
    state_store = ConnectorStateStore()

    def mapper(source_url: str | None):  # type: ignore[no-untyped-def]
        def _map(record, contract, acl):  # type: ignore[no-untyped-def]
            item = default_record_mapper(record, contract, acl)
            metadata = dict(item.metadata)
            if source_url is None:
                metadata.pop("source_url", None)
            else:
                metadata["source_url"] = source_url
            return CuratedKnowledgeItem.model_validate({**item.model_dump(), "metadata": metadata})

        return _map

    class _FailingWriter:
        async def upsert(self, items: Sequence[KnowledgeItem]) -> UpsertReceipt:
            del items
            raise RuntimeError("controlled refresh failure")

        async def curate(self, item_id: str, patch: Mapping[str, Any], *, provenance: str) -> None:
            await stack.writer.curate(item_id, patch, provenance=provenance)

        async def quarantine(self, item_id: str, reason: str) -> None:
            await stack.writer.quarantine(item_id, reason)

    async def run_with(
        *,
        revision: str,
        source_url: str | None,
        writer: KnowledgeWrite | None = None,
    ):
        return await SyncRunner(
            FixtureConnector.from_path(FIXTURE_SOURCE_PATH),
            contract,
            writer or stack.writer,
            state_store,
            actor="connector:test",
            audit_sink=stack.sink,
            cursor_field="updated_at",
            mapper=mapper(source_url),
            mapper_revision=revision,
        ).run(mode="backfill")

    async def scenario() -> None:
        enabled = await run_with(
            revision="source-links-enabled-v1",
            source_url="https://source.example/records/contact-1",
        )
        assert enabled.written == 2
        item_id = _item_id("acme-crm", "acme-crm/contacts", "c1")
        [linked] = await stack.store.get([item_id])
        assert linked.metadata["source_url"] == "https://source.example/records/contact-1"

        same = await run_with(
            revision="source-links-enabled-v1",
            source_url="https://source.example/records/contact-1",
        )
        assert same.written == 0
        assert same.skipped_unchanged == 2

        revoked = await run_with(revision="source-links-revoked-v2", source_url=None)
        assert revoked.written == 2
        [unlinked] = await stack.store.get([item_id])
        assert unlinked.chunk_id == linked.chunk_id
        assert unlinked.text == linked.text
        assert "source_url" not in unlinked.metadata

        with pytest.raises(RuntimeError, match="controlled refresh failure"):
            await run_with(
                revision="source-links-new-host-v3",
                source_url="https://new-source.example/records/contact-1",
                writer=_FailingWriter(),
            )
        [after_failure] = await stack.store.get([item_id])
        assert after_failure == unlinked

        # The failed v3 refresh never advances durable dedup. Returning to the
        # accepted v2 revision remains an ordinary idempotent no-op.
        after_failure_retry = await run_with(revision="source-links-revoked-v2", source_url=None)
        assert after_failure_retry.written == 0
        assert after_failure_retry.skipped_unchanged == 2

    asyncio.run(scenario())


def test_incremental_resume_writes_only_new_record(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    from ne.connect import ConnectorStateStore

    stack = writer_stack
    contract = _contract(_PERMISSIVE_SCHEMA)
    state_store = ConnectorStateStore()

    augmented = _fixture_data()
    records = augmented["records"]
    assert isinstance(records, list)
    records.append(
        {
            "record_id": "c3",
            "source_id": "acme-crm/contacts",
            "cursor": "2026-01-03T00:00:00Z",
            "payload": {
                "name": "Katherine Johnson",
                "email": "kj@example.test",
                "updated_at": "2026-01-03T00:00:00Z",
            },
        }
    )

    async def scenario() -> None:
        # Backfill the ORIGINAL fixture; persists cursors per source.
        base = await _runner(
            stack, FixtureConnector.from_path(FIXTURE_SOURCE_PATH), contract, state_store
        ).run(mode="backfill")
        assert base.written == 3  # permissive: c1, c2, a1 all valid

        # Incremental over the augmented connector: only c3 is beyond the cursor.
        inc = await _runner(
            stack, FixtureConnector.from_data(deepcopy(augmented)), contract, state_store
        ).run(mode="incremental")
        assert inc.records_seen == 1
        assert inc.written == 1
        c3_id = _item_id("acme-crm", "acme-crm/contacts", "c3")
        assert await stack.store.get([c3_id])

        # Re-running incremental is idempotent — c3 now dedup-skipped.
        again = await _runner(
            stack, FixtureConnector.from_data(deepcopy(augmented)), contract, state_store
        ).run(mode="incremental")
        assert again.written == 0

    asyncio.run(scenario())


def test_exactly_one_config_hash_stamped_sync_audit_per_run(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    from ne.connect import ConnectorStateStore

    stack = writer_stack
    contract = _contract(_STRICT_SCHEMA)
    runner = _runner(
        stack, FixtureConnector.from_path(FIXTURE_SOURCE_PATH), contract, ConnectorStateStore()
    )

    async def scenario() -> None:
        await runner.run(mode="backfill")
        events = _sync_events(stack)
        assert len(events) == 1
        config_hash = str(events[0]["config_hash"])
        assert config_hash == frozen_config
        assert len(config_hash) == 64
        assert all(c in "0123456789abcdef" for c in config_hash)
        payload = events[0]["payload"]
        assert isinstance(payload, dict)
        detail = str(payload["detail"])
        assert "written=2" in detail
        assert "invalid=1" in detail

        verification = await stack.sink.verify()
        assert verification.valid

    asyncio.run(scenario())


def test_fail_closed_unresolvable_acl_raises_and_audits_denied(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    from ne.connect import ConnectorStateStore

    stack = writer_stack
    # SOURCE_ACL, NO default_acl; a record on a source_id with no ACL entry.
    contract = _contract(_PERMISSIVE_SCHEMA)
    orphan = {
        "source": "acme-crm",
        "objects": [{"source_id": "acme-crm/orphan", "kind": "resource", "name": "orphan"}],
        "records": [
            {
                "record_id": "o1",
                "source_id": "acme-crm/orphan",
                "cursor": "2026-01-01T00:00:00Z",
                "payload": {"name": "Nemo", "updated_at": "2026-01-01T00:00:00Z"},
            }
        ],
        "acl": [],
    }
    runner = _runner(stack, FixtureConnector.from_data(orphan), contract, ConnectorStateStore())

    async def scenario() -> None:
        with pytest.raises(AclResolutionError):
            await runner.run(mode="backfill")
        events = _sync_events(stack)
        assert len(events) == 1
        assert events[0]["outcome"] == "denied"
        payload = events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == "mode=backfill error_class=AclResolutionError"

    asyncio.run(scenario())


def test_source_failure_raises_and_audits_one_class_only_error(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    """Auth/network failures become connector health instead of leaving stale green."""
    from ne.connect import ConnectorStateStore

    stack = writer_stack
    runner = _runner(
        stack,
        _FailingPermissionsConnector(_fixture_data()),
        _contract(_PERMISSIVE_SCHEMA),
        ConnectorStateStore(),
    )

    async def scenario() -> None:
        with pytest.raises(ConnectionError, match="secret@source"):
            await runner.run(mode="backfill")

        events = _sync_events(stack)
        assert len(events) == 1
        assert events[0]["outcome"] == "error"
        payload = events[0]["payload"]
        assert isinstance(payload, dict)
        detail = str(payload["detail"])
        assert detail == "mode=backfill error_class=ConnectionError"
        assert "secret" not in detail
        assert "source.invalid" not in detail

    asyncio.run(scenario())


def test_unfrozen_config_hash_propagates(writer_stack: WriterStack) -> None:
    from ne.connect import ConnectorStateStore

    reset_config_hash_for_testing()
    stack = writer_stack
    contract = _contract(_PERMISSIVE_SCHEMA)
    runner = _runner(
        stack, FixtureConnector.from_path(FIXTURE_SOURCE_PATH), contract, ConnectorStateStore()
    )

    async def scenario() -> None:
        with pytest.raises(ConfigHashNotFrozenError):
            await runner.run(mode="backfill")

    asyncio.run(scenario())


def test_duplicate_identity_never_crashes_and_first_occurrence_wins(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    """NE-CONNECT-CSV bridge hardening: pre-unit, two same-identity records in one
    batch reached the writer together and aborted the WHOLE run with
    ``ValueError('duplicate item_ids...')``. Now the later occurrence is rejected
    fail-closed (counted invalid; parked iff a quarantine lane is present — the
    no-lane path here proves backward compatibility)."""
    from ne.connect import ConnectorStateStore

    stack = writer_stack
    contract = _contract(_PERMISSIVE_SCHEMA)
    data = _fixture_data()
    records = data["records"]
    assert isinstance(records, list)
    dup = deepcopy(records[0])
    dup["payload"]["name"] = "Ada Byron (stale duplicate row)"
    records.append(dup)
    runner = _runner(stack, FixtureConnector.from_data(data), contract, ConnectorStateStore())

    async def scenario() -> None:
        report = await runner.run(mode="backfill")  # no ValueError — the crash is fixed
        assert report.records_seen == 4
        assert report.written == 3
        assert report.invalid == 1  # the duplicate, rejected fail-closed
        assert report.quarantined == 0  # no quarantine lane handed in -> no park

        # FIRST occurrence wins; the duplicate's content is nowhere.
        chunk = (await stack.store.get([_item_id("acme-crm", "acme-crm/contacts", "c1")]))[0]
        assert "stale duplicate row" not in chunk.text
        assert stack.quarantine.list_quarantined() == ()

    asyncio.run(scenario())


def test_invalid_record_parks_with_the_contract_validators_reason(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    """With the quarantine lane handed in, an ODCS-invalid record is PARKED with
    the contract validator's own message — and the reason changes when the
    validator's output changes (the plumbing derives from ``contract.validate``,
    never a connector-side reason table)."""
    from unittest.mock import patch

    from ne.connect import ConnectorStateStore

    stack = writer_stack
    contract = _contract(_STRICT_SCHEMA)
    connector = FixtureConnector.from_path(FIXTURE_SOURCE_PATH)
    runner = _runner(stack, connector, contract, ConnectorStateStore(), quarantine=stack.quarantine)

    async def scenario() -> None:
        report = await runner.run(mode="backfill")
        assert report.invalid == 1
        assert report.quarantined == 1  # the park is now visible, not a bare counter
        a1_id = _item_id("acme-crm", "acme-crm/accounts", "a1")
        parked = stack.quarantine.get(a1_id)
        assert parked is not None
        # The validator's own jsonschema message, verbatim.
        assert parked.reason == "$: 'email' is a required property"
        assert parked.provenance == "connector:acme-crm:a1"
        assert parked.payload["name"] == "Globex Industries"

    asyncio.run(scenario())

    # The live mutation probe: swap the validator's output -> the surfaced
    # reason MUST follow it (a scripted reason table would stay green here).
    stack2_quarantine = stack.quarantine

    async def mutated() -> None:
        state = ConnectorStateStore()
        runner2 = _runner(
            stack,
            FixtureConnector.from_path(FIXTURE_SOURCE_PATH),
            _contract(_STRICT_SCHEMA),
            state,
            quarantine=stack2_quarantine,
        )
        with patch.object(
            OdcsDataContract, "validate", return_value=["$: swapped-validator-message"]
        ):
            await runner2.run(mode="backfill")
        for record_id in ("c1", "c2", "a1"):
            source_id = "acme-crm/contacts" if record_id.startswith("c") else "acme-crm/accounts"
            parked = stack2_quarantine.get(_item_id("acme-crm", source_id, record_id))
            assert parked is not None
            assert parked.reason == "$: swapped-validator-message"

    asyncio.run(mutated())
