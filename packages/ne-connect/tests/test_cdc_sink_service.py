# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Full CDC sink pipeline over REAL components (NE-084) — no mock past the seam.

recorded Debezium envelope -> parse_change_events -> CdcSinkService ->
KnowledgeWriter -> QdrantChunkStore(:memory:) + real sqlite audit + real sqlite
quarantine lane. Proves fail-closed ACL ingestion, DELETE removal, idempotent
delete, the planted-forbidden-row non-leak, and the counts-only audit summary.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.connect.cdc import (
    CdcApplyReport,
    CdcChange,
    CdcSinkConfig,
    CdcSinkService,
    CdcTableConfig,
    ingest_detail,
    parse_change_events,
    written_from_ingest_detail,
)
from ne.connect.cdc.config import CdcTableAclConfig

from .conftest import WriterStack

_FIX = Path(__file__).parent / "fixtures" / "cdc"


def _load(name: str) -> bytes:
    return (_FIX / name).read_bytes()


def _column_config() -> CdcSinkConfig:
    return CdcSinkConfig(
        source_name="debezium",
        tables={
            "public.customers": CdcTableConfig(
                key_columns=("id",),
                content_columns=("name", "email"),
                acl=CdcTableAclConfig(principal_column="acl_group"),
            )
        },
    )


def _service(stack: WriterStack, config: CdcSinkConfig | None = None) -> CdcSinkService:
    return CdcSinkService(
        stack.writer,
        stack.quarantine,
        config or _column_config(),
        audit_sink=stack.sink,
    )


def _audit_rows(stack: WriterStack) -> list[dict[str, Any]]:
    with stack.engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(sa.select(ne_audit_log).order_by(ne_audit_log.c.id)).mappings()
        ]


def _events(stack: WriterStack, action: str) -> list[dict[str, Any]]:
    return [r for r in _audit_rows(stack) if r["action"] == action]


def _detail(row: dict[str, Any]) -> str:
    """The audit ``detail`` lives inside the hash-covered ``payload`` column."""
    payload = row.get("payload")
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    if isinstance(payload, dict):
        return str(payload.get("detail") or "")
    return ""


def _item_id(svc: CdcSinkService, table: str, key: dict[str, Any]) -> str:
    from ne.connect.bridge import canonical_payload_json

    return svc._item_id(table, canonical_payload_json(key))


def test_insert_writes_governed_item_with_acl(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    svc = _service(writer_stack)

    async def _t() -> None:
        report = await svc.apply(parse_change_events(_load("postgres_insert.json")))
        assert report.written == 1
        assert report.quarantined == 0
        item_id = _item_id(svc, "public.customers", {"id": 1})
        chunks = await writer_stack.store.get([item_id])
        assert len(chunks) == 1
        assert set(chunks[0].acl) == {"g-eng"}
        assert "Ada Synthetic" in chunks[0].text

    asyncio.run(_t())


def test_update_replaces_same_item_id(writer_stack: WriterStack, frozen_config: str) -> None:
    svc = _service(writer_stack)

    async def _t() -> None:
        await svc.apply(parse_change_events(_load("postgres_insert.json")))
        await svc.apply(parse_change_events(_load("postgres_update.json")))
        item_id = _item_id(svc, "public.customers", {"id": 1})
        chunks = await writer_stack.store.get([item_id])
        assert len(chunks) == 1
        assert "Lovelace-Synthetic" in chunks[0].text

    asyncio.run(_t())


def test_delete_removes_chunk_and_parks_payload(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    svc = _service(writer_stack)

    async def _t() -> None:
        await svc.apply(parse_change_events(_load("postgres_insert.json")))
        item_id = _item_id(svc, "public.customers", {"id": 1})
        report = await svc.apply(parse_change_events(_load("postgres_delete.json")))
        assert report.removed == 1
        assert await writer_stack.store.get([item_id]) == []
        parked = writer_stack.quarantine.get(item_id)
        assert parked is not None
        assert parked.reason == "cdc-delete"

    asyncio.run(_t())


def test_delete_unknown_item_is_idempotent_ignored(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    svc = _service(writer_stack)

    async def _t() -> None:
        report = await svc.apply(parse_change_events(_load("postgres_delete.json")))
        assert report.removed == 0
        assert report.ignored == 1
        assert report.quarantined == 0

    asyncio.run(_t())


def test_unresolvable_acl_row_quarantined_never_written(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    svc = _service(writer_stack)

    async def _t() -> None:
        report = await svc.apply(
            parse_change_events(_load("postgres_insert_unresolvable_acl.json"))
        )
        assert report.written == 0
        assert report.quarantined == 1
        item_id = _item_id(svc, "public.customers", {"id": 3})
        assert await writer_stack.store.get([item_id]) == []
        assert writer_stack.quarantine.get(item_id) is not None
        denied = _events(writer_stack, "cdc.quarantine")
        assert denied and denied[0]["outcome"] == "denied"

    asyncio.run(_t())


def test_acl_change_to_unresolvable_removes_stale_chunk(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    svc = _service(writer_stack)

    async def _t() -> None:
        await svc.apply(parse_change_events(_load("postgres_insert.json")))
        item_id = _item_id(svc, "public.customers", {"id": 1})
        assert len(await writer_stack.store.get([item_id])) == 1
        # An UPDATE that turns the ACL unresolvable (null principal column).
        stale = CdcChange(
            op="u",
            table="public.customers",
            before={"id": 1},
            after={"id": 1, "name": "Ada", "email": "a@x", "acl_group": None},
            source={"schema": "public", "table": "customers"},
            ts_ms=2,
        )
        report = await svc.apply([stale])
        assert report.quarantined == 1
        assert await writer_stack.store.get([item_id]) == []

    asyncio.run(_t())


def test_forbidden_reserved_label_row_non_leak(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    svc = _service(writer_stack)

    async def _t() -> None:
        report = await svc.apply(
            parse_change_events(_load("postgres_insert_forbidden_reserved_label.json"))
        )
        assert report.written == 0
        assert report.quarantined == 1
        item_id = _item_id(svc, "public.customers", {"id": 4})
        assert await writer_stack.store.get([item_id]) == []
        # The forged reserved label never appears in any audit detail or lane reason.
        for row in _audit_rows(writer_stack):
            assert "acl:tenant-all" not in _detail(row)
        for parked in writer_stack.quarantine.list_quarantined():
            assert "acl:tenant-all" not in parked.reason
        ingest = _events(writer_stack, "cdc.ingest")
        assert len(ingest) == 1
        assert ingest[0]["outcome"] == "error"
        assert _detail(ingest[0]) == ("received=1 written=0 removed=0 quarantined=1 ignored=0")

    asyncio.run(_t())


def test_unknown_table_quarantined_not_written(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    svc = _service(writer_stack)

    async def _t() -> None:
        report = await svc.apply(parse_change_events(_load("unknown_table_insert.json")))
        assert report.written == 0
        assert report.quarantined == 1
        denied = _events(writer_stack, "cdc.quarantine")
        assert any(_detail(r) == "unknown-table" for r in denied)

    asyncio.run(_t())


def test_truncate_op_quarantined_unsupported(writer_stack: WriterStack, frozen_config: str) -> None:
    svc = _service(writer_stack)

    async def _t() -> None:
        truncate = CdcChange(
            op="t",
            table="public.customers",
            before=None,
            after=None,
            source={"schema": "public", "table": "customers"},
            ts_ms=3,
        )
        report = await svc.apply([truncate])
        assert report.quarantined == 1
        denied = _events(writer_stack, "cdc.quarantine")
        assert any(_detail(r).startswith("unsupported-op:t") for r in denied)

    asyncio.run(_t())


def test_apply_emits_summary_audit_with_config_hash(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    svc = _service(writer_stack)

    async def _t() -> None:
        await svc.apply(parse_change_events(_load("postgres_insert.json")))
        ingest = _events(writer_stack, "cdc.ingest")
        assert len(ingest) == 1
        assert ingest[0]["config_hash"] == frozen_config
        assert "received=1" in _detail(ingest[0])
        assert ingest[0]["outcome"] == "success"

    asyncio.run(_t())


def test_apply_emitted_detail_parses_back_to_the_written_count(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    """The EMITTED ``cdc.ingest`` detail parses back through the consumer parser.

    ``ne.api.connect_status`` derives the CDC lane's "Newest record" from the
    ``written`` count in this detail, so the emission and the parser must agree
    on the LIVE event, not just on the helper pair in isolation — a format drift
    in ``apply``'s emission (the parser suddenly reading every batch as
    not-accepting) goes RED here, not silently stale on the status pane.
    """
    svc = _service(writer_stack)

    async def _t() -> None:
        # An ACCEPTING batch: written == 1 must survive the round trip.
        report = await svc.apply(parse_change_events(_load("postgres_insert.json")))
        assert report.written == 1
        ingest = _events(writer_stack, "cdc.ingest")
        assert written_from_ingest_detail(_detail(ingest[-1])) == 1

        # The HEARTBEAT shape (empty authenticated body -> apply(())): the sink
        # still emits its summary event — that is the "Last checked" fact — and
        # the parsed written count is 0, which is what keeps an empty batch from
        # ever advancing "Newest record".
        empty_report = await svc.apply(())
        assert empty_report == CdcApplyReport()
        ingest = _events(writer_stack, "cdc.ingest")
        assert len(ingest) == 2
        assert written_from_ingest_detail(_detail(ingest[-1])) == 0

    asyncio.run(_t())


def test_ingest_detail_round_trips_written_through_the_parser() -> None:
    """``written_from_ingest_detail`` is a strict inverse of ``ingest_detail``."""
    for report in (
        CdcApplyReport(),
        CdcApplyReport(received=1, written=1),
        CdcApplyReport(received=7, written=3, removed=1, quarantined=2, ignored=1),
        CdcApplyReport(received=120, written=0, removed=0, quarantined=120, ignored=0),
    ):
        assert written_from_ingest_detail(ingest_detail(report)) == report.written


def test_written_parser_rejects_non_producer_shapes() -> None:
    """Anything that is not EXACTLY the producer's counts shape parses to None.

    Each rejected shape below is a would-be freshness fabrication if accepted:
    the consumer treats None as "not known to have accepted anything", so the
    parser must fail toward absence on foreign/hand-emitted/mangled details.
    The final case is the in-test control: the exact producer shape MUST parse
    (so a mutant that makes the parser reject everything cannot pass this test).
    """
    parse = written_from_ingest_detail
    assert parse(None) is None
    assert parse("") is None
    # A bare fragment — NOT the full counts shape.
    assert parse("written=3") is None
    # The counts shape embedded in foreign text (prefix / suffix): an anchored
    # fullmatch must refuse both — a substring-search mutant goes RED here.
    assert (
        written_from_ingest_detail(
            "sync ok: received=1 written=1 removed=0 quarantined=0 ignored=0"
        )
        is None
    )
    assert (
        written_from_ingest_detail("received=1 written=1 removed=0 quarantined=0 ignored=0 (retry)")
        is None
    )
    # A non-numeric / negative count is not the producer's emission.
    assert (
        written_from_ingest_detail("received=1 written=x removed=0 quarantined=0 ignored=0") is None
    )
    assert (
        written_from_ingest_detail("received=1 written=-1 removed=0 quarantined=0 ignored=0")
        is None
    )
    # Control: the exact producer shape parses.
    assert written_from_ingest_detail("received=9 written=7 removed=1 quarantined=1 ignored=0") == 7


def test_batch_mixed_insert_update_delete(writer_stack: WriterStack, frozen_config: str) -> None:
    svc = _service(writer_stack)

    async def _t() -> None:
        report = await svc.apply(parse_change_events(_load("batch_mixed.json")))
        assert report.received == 3
        # insert + update on id=10, then delete → net zero stored.
        assert report.written == 2
        assert report.removed == 1
        item_id = _item_id(svc, "public.customers", {"id": 10})
        assert await writer_stack.store.get([item_id]) == []

    asyncio.run(_t())


def test_no_row_values_in_audit_details_or_quarantine_reasons(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    svc = _service(writer_stack)
    sentinel = "SENTINEL-SECRET-CELL-7f3a"

    async def _t() -> None:
        # Sentinel in a CONTENT cell AND in the principal column (forces taint).
        change = CdcChange(
            op="c",
            table="public.customers",
            before=None,
            after={"id": 99, "name": sentinel, "email": "x@y", "acl_group": f"acl:{sentinel}"},
            source={"schema": "public", "table": "customers"},
            ts_ms=4,
        )
        await svc.apply([change])
        for row in _audit_rows(writer_stack):
            assert sentinel not in _detail(row)
            assert sentinel not in str(row.get("resource") or "")
        for parked in writer_stack.quarantine.list_quarantined():
            assert sentinel not in parked.reason

    asyncio.run(_t())
