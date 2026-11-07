# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""M365GraphConnector fixture crawl: discovery, file-only records, ACL, quarantine."""

from __future__ import annotations

import ast
import asyncio
import copy
import os
import pickle
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from ne.brain.ingest.contracts import BlockKind, DocBlock, ParsedDocument
from ne.brain.parsed_evidence import (
    ParsedDocumentEvidenceDescriptor,
    _consume_parsed_document_evidence,
    _M365ParsedEvidenceAuthority,
)
from ne.connect.errors import (
    GraphRateLimitedError,
    GraphRedirectError,
    GraphUpstreamError,
)
from ne.connect.m365.acl import PUBLIC_LINK, TENANT_ALL
from ne.connect.m365.connector import M365GraphConnector
from ne.connect.m365.contract import m365_data_contract

from .m365_transport import (
    DELTA_LINK_1,
    DRIVE_ID,
    SITE_DRIVES_URL,
    TOKEN_URL,
    RecordedBodyParser,
    build_m365_transport,
    m365_settings,
    recording_transport,
)

_RESOLVED = {
    "drive-1:item-user": ("u-ada",),
    "drive-1:item-group": ("g-eng",),
    "drive-1:item-anon": (PUBLIC_LINK,),
    "drive-1:item-org": (TENANT_ALL,),
    "drive-1:item-everyone": (TENANT_ALL,),
}


def _connector(**kw: object) -> M365GraphConnector:
    return M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(),
        parser=RecordedBodyParser(),
        **kw,  # type: ignore[arg-type]
    )


class _EchoEvidenceParser:
    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument:
        return self._parsed(path.read_text(), document_id=document_id, source_name=path.name)

    async def parse_fd(self, file_fd: int, *, document_id: str, source_name: str) -> ParsedDocument:
        os.lseek(file_fd, 0, os.SEEK_SET)
        return self._parsed(
            os.read(file_fd, 1024 * 1024).decode(),
            document_id=document_id,
            source_name=source_name,
        )

    @staticmethod
    def _parsed(body: str, *, document_id: str, source_name: str) -> ParsedDocument:
        return ParsedDocument(
            document_id=document_id,
            source_name=source_name,
            source_format="docx",
            blocks=(DocBlock(kind=BlockKind.PARAGRAPH, text=body),),
        )


def test_discover_lists_drives_from_site_enumeration() -> None:
    async def scenario() -> None:
        objects = await _connector().discover()
        assert [o.source_id for o in objects] == [DRIVE_ID]
        assert all(o.kind == "drive" for o in objects)

    asyncio.run(scenario())


def test_explicit_drive_ids_are_crawled_without_site_enumeration() -> None:
    connector = M365GraphConnector(
        m365_settings(site_ids=(), drive_ids=(DRIVE_ID,)),
        transport=build_m365_transport(),
        parser=RecordedBodyParser(),
    )

    async def scenario() -> None:
        objects = await connector.discover()
        assert [o.source_id for o in objects] == [DRIVE_ID]

    asyncio.run(scenario())


def test_drive_resolution_is_bounded_and_explicit_scope_is_io_free() -> None:
    explicit_requests: list[httpx.Request] = []
    explicit = M365GraphConnector(
        m365_settings(site_ids=(), drive_ids=(DRIVE_ID,)),
        transport=build_m365_transport(recorder=explicit_requests),
        parser=RecordedBodyParser(),
    )
    assert asyncio.run(explicit.resolve_drive_ids()) == (DRIVE_ID,)
    assert explicit_requests == []

    site_requests: list[httpx.Request] = []
    site = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(recorder=site_requests),
        parser=RecordedBodyParser(),
    )
    assert asyncio.run(site.resolve_drive_ids()) == (DRIVE_ID,)
    assert [str(request.url) for request in site_requests] == [TOKEN_URL, SITE_DRIVES_URL]


def test_backfill_yields_only_resolved_file_records() -> None:
    connector = _connector()

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        assert {r.source_id for r in records} == set(_RESOLVED)
        # root + folder skipped; one deleted item skipped and counted.
        assert connector.deleted_count == 1
        # quarantined items appear in NEITHER stream.
        assert "drive-1:item-q-unresolvable" not in {r.source_id for r in records}
        assert "drive-1:item-q-forged" not in {r.source_id for r in records}
        # payload never carries the credential-bearing downloadUrl.
        for record in records:
            assert not any("download" in k.lower() for k in record.payload)

        by_id = {record.record_id: record for record in records}
        assert {record.payload["graph_doc_type"] for record in records} == {"other"}
        assert by_id["item-user"].payload["body_ingestion_status"] == "parsed"
        assert by_id["item-user"].payload["body_source_format"] == "docx"
        assert (
            by_id["item-user"].payload["body_parser_identity"] == "neutron-m365-document-parser/v1"
        )
        assert by_id["item-user"].payload["body_block_count"] == 1
        assert by_id["item-group"].payload["body_ingestion_status"] == "unsupported"
        assert "body" not in by_id["item-group"].payload

    asyncio.run(scenario())


def test_graph_document_type_is_connector_owned_not_provider_selected() -> None:
    connector = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(
            item_overrides={
                "item-user": {
                    "graph_doc_type": "contract",
                    "doc_type": "invoice",
                }
            }
        ),
        parser=RecordedBodyParser(),
    )

    async def scenario() -> None:
        records = [record async for record in connector.backfill()]
        item = next(record for record in records if record.record_id == "item-user")
        assert item.payload["graph_doc_type"] == "other"
        assert "doc_type" not in item.payload

    asyncio.run(scenario())


def test_parser_evidence_is_connector_local_one_shot_and_clears_on_recrawl() -> None:
    first = _connector()
    second = _connector()

    async def scenario() -> None:
        first_records, second_records = await asyncio.gather(
            _records(first),
            _records(second),
        )
        first_record = next(row for row in first_records if row.record_id == "item-user")
        second_record = next(row for row in second_records if row.record_id == "item-user")
        acl = _RESOLVED[first_record.source_id]

        first_descriptor = first.take_parsed_evidence(
            first_record.source_id,
            first_record.record_id,
            first_record.payload,
            acl,
            "canonical-first-item",
        )
        assert first_descriptor is not None
        assert (
            first.take_parsed_evidence(
                first_record.source_id,
                first_record.record_id,
                first_record.payload,
                acl,
                "canonical-first-item",
            )
            is None
        )
        # Consuming the first instance never consumes the second instance.
        assert (
            second.take_parsed_evidence(
                second_record.source_id,
                second_record.record_id,
                second_record.payload,
                acl,
                "canonical-second-item",
            )
            is not None
        )

        recrawled = _connector()
        [record] = [row for row in await _records(recrawled) if row.record_id == "item-user"]
        recrawled._recrawl({})
        assert (
            recrawled.take_parsed_evidence(
                record.source_id,
                record.record_id,
                record.payload,
                acl,
                "canonical-recrawled-item",
            )
            is None
        )

    asyncio.run(scenario())


async def _records(connector: M365GraphConnector):
    return [row async for row in connector.backfill()]


def test_parser_evidence_duplicate_identity_queue_is_fifo() -> None:
    connector = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(),
        parser=_EchoEvidenceParser(),
    )

    async def scenario() -> None:
        first_payload = await connector._body_parser.parse(
            b"first",
            document_id="drive:item",
            record_id="item",
            source_format="docx",
            acl=("team",),
        )
        second_payload = await connector._body_parser.parse(
            b"second",
            document_id="drive:item",
            record_id="item",
            source_format="docx",
            acl=("team",),
        )

        first = connector.take_parsed_evidence(
            "drive:item", "item", first_payload, ("team",), "canonical-first"
        )
        second = connector.take_parsed_evidence(
            "drive:item", "item", second_payload, ("team",), "canonical-second"
        )
        assert first is not None
        assert second is not None
        assert first.body_sha256 != second.body_sha256

    asyncio.run(scenario())


def test_parser_evidence_is_nonreconstructible_cross_issuer_and_one_shot() -> None:
    first = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(),
        parser=_EchoEvidenceParser(),
    )
    second = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(),
        parser=_EchoEvidenceParser(),
    )
    with pytest.raises(TypeError, match="sealed to M365GraphConnector"):
        _M365ParsedEvidenceAuthority()
    with pytest.raises(TypeError, match="cannot be subclassed"):

        class _ForgedAuthority(_M365ParsedEvidenceAuthority):
            pass

    with pytest.raises(TypeError, match="already composed"):
        first._compose_parsed_evidence_channel()
    assert not hasattr(first, "_parsed_evidence_issuer")

    async def issue():
        return await first._body_parser.parse(
            b"exact body",
            document_id="drive:item",
            record_id="item",
            source_format="docx",
            acl=("team",),
        )

    payload = asyncio.run(issue())
    issued = first._parsed_evidence[("drive:item", "item")][0]
    with pytest.raises(TypeError, match="issued only"):
        ParsedDocumentEvidenceDescriptor()
    with pytest.raises(TypeError, match="cannot be copied"):
        copy.copy(issued)
    with pytest.raises(TypeError, match="cannot be copied"):
        copy.deepcopy(issued)
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(issued)

    second._record_parsed_evidence("drive:item", "item", issued)
    with pytest.raises(ValueError, match="round trip failed"):
        second.take_parsed_evidence("drive:item", "item", payload, ("team",), "canonical-item")

    claimed = first.take_parsed_evidence("drive:item", "item", payload, ("team",), "canonical-item")
    assert claimed is not None
    assert claimed is issued
    binding = (
        claimed,
        "canonical-item",
        "drive:item",
        "item",
        ("team",),
        payload,
    )

    def consume() -> bool:
        try:
            _consume_parsed_document_evidence([binding])
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _index: consume(), range(2))) == [False, True]
    with pytest.raises(ValueError, match="binding is invalid"):
        _consume_parsed_document_evidence([binding])


def _attribute_call_scopes(path: Path, attribute: str) -> list[tuple[str, ...]]:
    tree = ast.parse(path.read_text())
    scopes: list[tuple[str, ...]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _visit_scope(self, node: ast.AST, name: str) -> None:
            self.stack.append(name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_scope(node, node.name)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_scope(node, node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_scope(node, node.name)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and node.func.attr == attribute:
                scopes.append(tuple(self.stack))
            self.generic_visit(node)

    Visitor().visit(tree)
    return scopes


def test_parser_evidence_authority_has_only_exact_composed_call_sites() -> None:
    repo = Path(__file__).resolve().parents[3]
    authority = repo / "packages/ne-brain/src/ne/brain/parsed_evidence.py"
    connector = repo / "packages/ne-connect/src/ne/connect/m365/connector.py"
    content = repo / "packages/ne-connect/src/ne/connect/m365/content.py"
    authority_tree = ast.parse(authority.read_text())

    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_new_parsed_document_evidence_issuer"
        for node in ast.walk(authority_tree)
    )
    assert not any(
        isinstance(node, ast.ClassDef) and node.name == "_ParsedDocumentEvidenceIssuer"
        for node in ast.walk(authority_tree)
    )
    assert _attribute_call_scopes(connector, "_compose_parsed_evidence_channel") == [
        ("M365GraphConnector", "__init__")
    ]
    assert _attribute_call_scopes(connector, "__parsed_evidence_claim") == [
        ("M365GraphConnector", "take_parsed_evidence")
    ]
    assert _attribute_call_scopes(content, "__evidence_issue") == [("M365BodyParser", "parse")]


def test_records_validate_against_the_contract() -> None:
    connector = _connector()
    contract = m365_data_contract(m365_settings())

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        for record in records:
            assert contract.validate(record.payload) == []

    asyncio.run(scenario())


def test_contract_rejects_both_false_parsed_and_mixed_unsupported_body_claims() -> None:
    contract = m365_data_contract(m365_settings())
    base = {
        "id": "x",
        "name": "x.xlsx",
        "web_url": "https://contoso.sharepoint.com/x.xlsx",
        "last_modified": "2026-08-18T00:00:00Z",
        "graph_doc_type": "other",
    }
    assert contract.validate({**base, "body_ingestion_status": "parsed"})
    assert contract.validate(
        {**base, "body_ingestion_status": "unsupported", "body": "should-not-be-present"}
    )
    assert contract.validate(
        {
            **base,
            "body_ingestion_status": "unsupported",
            "body_parser_identity": "neutron-m365-document-parser/v1",
        }
    )


def test_contract_requires_connector_owned_graph_document_type() -> None:
    contract = m365_data_contract(m365_settings())
    record = {
        "id": "x",
        "name": "x.xlsx",
        "web_url": "https://contoso.sharepoint.com/x.xlsx",
        "last_modified": "2026-08-18T00:00:00Z",
        "body_ingestion_status": "unsupported",
    }
    assert contract.validate(record)
    assert contract.validate({**record, "graph_doc_type": "contract"})
    assert contract.validate({**record, "graph_doc_type": "other"}) == []


def test_permissions_yield_per_item_acl_entries_keyed_by_source_id() -> None:
    connector = _connector()

    async def scenario() -> None:
        entries = {e.resource_id: e.principals async for e in connector.permissions()}
        assert entries == _RESOLVED
        records = [r async for r in connector.backfill()]
        # resource_id == record.source_id for every emitted record.
        assert set(entries) == {r.source_id for r in records}

    asyncio.run(scenario())


def test_quarantined_items_counted_and_excluded_from_both_streams() -> None:
    connector = _connector()

    async def scenario() -> None:
        entries = [e async for e in connector.permissions()]
        records = [r async for r in connector.backfill()]
        assert connector.acl_quarantined == 2
        assert set(connector.acl_quarantined_ids) == {
            "drive-1:item-q-unresolvable",
            "drive-1:item-q-forged",
        }
        acl_ids = {e.resource_id for e in entries}
        rec_ids = {r.source_id for r in records}
        for qid in connector.acl_quarantined_ids:
            assert qid not in acl_ids
            assert qid not in rec_ids

    asyncio.run(scenario())


def test_pending_delta_link_captured() -> None:
    connector = _connector()

    async def scenario() -> None:
        _ = [r async for r in connector.backfill()]
        assert connector.pending_delta_links == {DRIVE_ID: DELTA_LINK_1}

    asyncio.run(scenario())


def test_delta_state_incremental_yields_only_changed_item() -> None:
    connector = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(),
        delta_state={DRIVE_ID: DELTA_LINK_1},
        parser=RecordedBodyParser(),
    )

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        # From the stored deltaLink, the fixture returns only the changed item.
        assert {r.source_id for r in records} == {"drive-1:item-user"}

    asyncio.run(scenario())


def test_stale_delta_token_falls_back_to_full_delta() -> None:
    connector = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(stale_delta=True),
        delta_state={DRIVE_ID: DELTA_LINK_1},
        parser=RecordedBodyParser(),
    )

    async def scenario() -> None:
        # The stored token 410s -> full-delta resync within the run.
        records = [r async for r in connector.backfill()]
        assert {r.source_id for r in records} == set(_RESOLVED)

    asyncio.run(scenario())


# -- FIX-A: the PERSISTED delta cursor is re-guarded too ----------------------


def test_persisted_http_delta_cursor_is_rejected_before_any_graph_get() -> None:
    recorder: list[httpx.Request] = []
    connector = M365GraphConnector(
        m365_settings(site_ids=(), drive_ids=(DRIVE_ID,)),
        transport=httpx.MockTransport(recording_transport(recorder)),
        delta_state={
            DRIVE_ID: f"http://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/root/delta?token=STALE"
        },
        parser=RecordedBodyParser(),
    )

    async def scenario() -> None:
        with pytest.raises(GraphRedirectError):
            _ = [r async for r in connector.backfill()]

    asyncio.run(scenario())
    assert recorder == []  # the hostile persisted cursor is never fetched


def test_persisted_cross_cloud_delta_cursor_is_rejected_before_any_request() -> None:
    recorder: list[httpx.Request] = []
    connector = M365GraphConnector(
        m365_settings(site_ids=(), drive_ids=(DRIVE_ID,)),
        transport=httpx.MockTransport(recording_transport(recorder)),
        delta_state={
            DRIVE_ID: (
                f"https://microsoftgraph.chinacloudapi.cn/v1.0/drives/{DRIVE_ID}"
                "/root/delta?token=STALE"
            )
        },
        parser=RecordedBodyParser(),
    )

    async def scenario() -> None:
        with pytest.raises(GraphRedirectError):
            _ = [r async for r in connector.backfill()]

    asyncio.run(scenario())
    assert recorder == []  # zero requests incl. the token POST — token never redirected


# -- FIX-B: per-item non-retryable permissions failure quarantines that item --


def test_perm_fetch_403_quarantines_item_and_crawl_continues() -> None:
    connector = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(perm_status_overrides={"item-user": 403}),
        parser=RecordedBodyParser(),
    )

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        assert "drive-1:item-user" not in {r.source_id for r in records}
        assert {r.source_id for r in records} == {
            "drive-1:item-group",
            "drive-1:item-anon",
            "drive-1:item-org",
            "drive-1:item-everyone",
        }
        entries = {e.resource_id async for e in connector.permissions()}
        assert "drive-1:item-user" not in entries
        assert connector.acl_fetch_failed == 1
        assert connector.acl_quarantined == 3  # 2 mapping + 1 fetch
        assert "drive-1:item-user" in connector.acl_quarantined_ids
        assert connector.pending_delta_links == {DRIVE_ID: DELTA_LINK_1}  # crawl completed

    asyncio.run(scenario())


def test_perm_fetch_404_quarantines_item_and_crawl_continues() -> None:
    connector = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(perm_status_overrides={"item-user": 404}),
        parser=RecordedBodyParser(),
    )

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        assert len(records) == 4
        assert "drive-1:item-user" not in {r.source_id for r in records}
        assert connector.acl_fetch_failed == 1
        assert connector.acl_quarantined == 3

    asyncio.run(scenario())


def test_perm_fetch_410_quarantines_item() -> None:
    # 410 on a permissions URL means the ITEM is gone, not a delta-basis expiry.
    connector = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(perm_status_overrides={"item-user": 410}),
        parser=RecordedBodyParser(),
    )

    async def scenario() -> None:
        records = [r async for r in connector.backfill()]
        assert len(records) == 4
        assert connector.acl_fetch_failed == 1
        assert "drive-1:item-user" in connector.acl_quarantined_ids
        assert connector.pending_delta_links == {DRIVE_ID: DELTA_LINK_1}  # crawl completed

    asyncio.run(scenario())


def test_perm_fetch_429_exhaustion_aborts_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    connector = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(perm_status_overrides={"item-user": 429}),
        parser=RecordedBodyParser(),
    )

    async def scenario() -> None:
        with pytest.raises(GraphRateLimitedError):
            _ = [r async for r in connector.backfill()]  # run-level throttling: NOT quarantined

    asyncio.run(scenario())


def test_perm_fetch_500_aborts_run() -> None:
    connector = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(perm_status_overrides={"item-user": 500}),
        parser=RecordedBodyParser(),
    )

    async def scenario() -> None:
        with pytest.raises(GraphUpstreamError):
            _ = [r async for r in connector.backfill()]  # server fault keeps aborting

    asyncio.run(scenario())
