# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Full M365 pipeline over REAL components (NE-044) — no mock past the seam.

recorded Graph delta+permissions -> M365GraphConnector -> SyncRunner ->
KnowledgeWriter -> QdrantChunkStore(:memory:) + real sqlite audit. Proves
ACL-stamped chunks, exactly-one config_hash-stamped connector.sync event, the
acl_quarantine denied event, deltaLink persist/advance-only-on-success, an
incremental second run, and mid-run-failure resumability with zero duplicates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import cast

import httpx
import pytest
import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.brain.lineage import DocumentStage, SqlAlchemyDocumentLineageStore
from ne.brain.parsed_evidence import PARSED_RECORD_METADATA_KEY
from ne.brain.write import KnowledgeWriter, OutboxShapeViolationSink, ProvenanceClass
from ne.connect import CONNECTOR_RECORD_SHAPE, ConnectorStateStore, GraphSyncError
from ne.connect.errors import (
    GraphParseError,
    GraphRedirectError,
    GraphResponseParseError,
    GraphUnauthorizedError,
)
from ne.connect.m365 import PUBLIC_LINK, TENANT_ALL, run_m365_sync
from ne.connect.m365.connector import M365GraphConnector
from ne.connect.m365.contract import m365_data_contract
from ne.connect.m365.sync import _m365_target_binding_digest, _parsed_document_prose
from ne.contracts import compute_config_hash
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditSink
from ne.schema import AuditEvent

from .conftest import WriterStack
from .m365_transport import (
    DELTA_LINK_1,
    DELTA_LINK_2,
    MERIDIAN_BODY_PHRASE,
    MERIDIAN_DOCX,
    RecordedBodyParser,
    build_m365_transport,
    m365_settings,
)

_SOURCE = "m365-sharepoint"


def _deep_json_body(sentinel: str) -> str:
    quoted = json.dumps(sentinel)
    if len(quoted) % 2 == 0:
        quoted = json.dumps(f"{sentinel}X")
    depth = (20_001 - len(quoted)) // 2
    body = "[" * depth + quoted + "]" * depth
    assert len(body.encode()) == 20_001
    return body


class _FailConnectorSuccessAudit:
    def __init__(self, delegate: AuditSink) -> None:
        self._delegate = delegate

    async def append(self, event: AuditEventLike) -> None:
        assert isinstance(event, AuditEvent)
        if event.action == "connector.sync" and str(event.outcome) == "success":
            raise RuntimeError("terminal audit failed")
        await self._delegate.append(event)


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


def _audit_rows(stack: WriterStack) -> list[dict[str, object]]:
    with stack.engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(sa.select(ne_audit_log).order_by(ne_audit_log.c.id)).mappings()
        ]


def _events(stack: WriterStack, action: str) -> list[dict[str, object]]:
    return [r for r in _audit_rows(stack) if r["action"] == action]


def test_parsed_document_prose_prefers_blocks_and_never_the_envelope() -> None:
    """The searchable text of a parsed document is its PROSE, never JSON scaffolding.

    The corpus defect this pins: ``default_record_mapper`` serialized the whole
    Graph record (ETag, ULIDs, parser hashes, ``body_blocks``) as chunk text, so
    retrieval scored queries against a vector dominated by envelope noise and
    the governing SOP prose never ranked. The rendering must also stay equal to
    the parser's pre-joined ``body`` (same join), because lineage's
    document-version hash and ``answer_ledger._lineage_content_hash`` both hash
    ``body``.
    """
    rule = (
        "An auto loan is actionable only when it is active, has an unpaid scheduled "
        "payment due from the as-of date through 14 calendar days inclusive, has no "
        "prior hardship offer, and is not in repossession."
    )
    payload = {
        "body": f"Disaster Hardship Response SOP (2026)\n\n{rule}",
        "body_blocks": [
            {"kind": "heading", "level": 1, "text": "Disaster Hardship Response SOP (2026)"},
            {"kind": "paragraph", "level": None, "text": f"  {rule}  "},
            {"kind": "paragraph", "level": None, "text": "   "},
        ],
        "body_ingestion_status": "parsed",
        "etag": '"etag-user-v1"',
        "mime_type": "text/markdown",
    }
    prose = _parsed_document_prose(payload)
    assert prose == payload["body"]
    assert "etag" not in prose and not prose.startswith("{")
    # body fallback when the structural blocks are missing or malformed.
    assert _parsed_document_prose({"body": "plain body"}) == "plain body"
    assert _parsed_document_prose({"body": "plain body", "body_blocks": "junk"}) == "plain body"
    # nothing renderable -> "" (caller keeps canonical-JSON content).
    assert _parsed_document_prose({"body": "   ", "body_blocks": []}) == ""
    assert _parsed_document_prose({}) == ""


def test_target_binding_digest_is_canonical_and_credential_free() -> None:
    first = m365_settings(
        client_id="client-a",
        client_secret="secret-a",
        site_ids=("site-b", "site-a"),
        drive_ids=("drive-b", "drive-a"),
        content_allowed_hosts=("B.example", "a.example"),
        source_link_allowed_hosts=("source-a.example",),
    )
    reordered_with_other_credentials = m365_settings(
        client_id="client-b",
        client_secret="secret-b",
        graph_base_url="https://GRAPH.MICROSOFT.COM/v1.0/",
        login_base_url="https://LOGIN.MICROSOFTONLINE.COM/",
        site_ids=("site-a", "site-b"),
        drive_ids=("drive-a", "drive-b"),
        content_allowed_hosts=("other-download.example",),
        source_link_allowed_hosts=("other-source.example",),
        graph_allowed_hosts=("graph.microsoft.com",),
        login_allowed_hosts=("login.microsoftonline.com",),
    )
    digest = _m365_target_binding_digest(first)
    assert digest == _m365_target_binding_digest(reordered_with_other_credentials)

    for changed in (
        m365_settings(source_name="other-source"),
        m365_settings(tenant_id="33333333-3333-3333-3333-333333333333"),
        m365_settings(graph_base_url="https://graph.microsoft.us/v1.0"),
        m365_settings(login_base_url="https://login.microsoftonline.us"),
        m365_settings(site_ids=("other-site",)),
        m365_settings(site_ids=(), drive_ids=("other-drive",)),
    ):
        assert digest != _m365_target_binding_digest(changed)


def test_target_mismatch_and_legacy_state_fail_before_provider_io(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    del frozen_config

    async def expect_no_io(state_store: ConnectorStateStore, error: str) -> None:
        requests: list[httpx.Request] = []
        with pytest.raises(RuntimeError, match=error):
            await run_m365_sync(
                m365_settings(tenant_id="33333333-3333-3333-3333-333333333333"),
                writer=writer_stack.writer,
                state_store=state_store,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(recorder=requests),
                parser=RecordedBodyParser(),
            )
        assert requests == []

    bound = ConnectorStateStore()
    bound.claim_target_binding(
        _SOURCE,
        _m365_target_binding_digest(m365_settings()),
    )
    asyncio.run(expect_no_io(bound, "does not match its binding"))
    bound.close()

    legacy = ConnectorStateStore()
    legacy.set_cursor(_SOURCE, "delta:drive-1", DELTA_LINK_1)
    asyncio.run(expect_no_io(legacy, "non-empty and has no target binding"))
    legacy.close()


async def _acl_of(stack: WriterStack, source_id: str, record_id: str) -> tuple[str, ...]:
    chunks = await stack.store.get([_item_id(source_id, record_id)])
    assert len(chunks) == 1
    return chunks[0].acl


def test_backfill_stamps_acls_and_emits_single_sync_event(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    lineage = SqlAlchemyDocumentLineageStore(stack.engine)
    writer = KnowledgeWriter(
        stack.store,
        stack.quarantine,
        {CONNECTOR_RECORD_SHAPE.name: CONNECTOR_RECORD_SHAPE},
        default_shape=CONNECTOR_RECORD_SHAPE.name,
        actor="connector:test",
        default_provenance_class=ProvenanceClass.INGESTED,
        audit_sink=stack.sink,
        violation_sink=OutboxShapeViolationSink(stack.engine),
        lineage=lineage,
    )
    state_store = ConnectorStateStore()
    requests: list[httpx.Request] = []
    redirect_secret = "SHORT-LIVED-REDIRECT-CREDENTIAL"
    redirect_url = f"https://contoso.sharepoint.com/download/item-user?tempauth={redirect_secret}"
    transport = build_m365_transport(
        recorder=requests,
        content_redirect_overrides={"item-user": redirect_url},
        content_bytes_overrides={"item-user": MERIDIAN_DOCX.read_bytes()},
    )

    async def scenario() -> None:
        outcome = await run_m365_sync(
            m365_settings(
                content_allowed_hosts=("contoso.sharepoint.com",),
                source_link_allowed_hosts=("contoso.sharepoint.com",),
            ),
            writer=writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=transport,
            parser=RecordedBodyParser(),
        )
        assert outcome.mode == "backfill"
        assert outcome.report.written == 5
        assert outcome.acl_quarantined == 2

        # (a) chunks stamped exactly as the fixtures grant.
        assert await _acl_of(stack, "drive-1:item-user", "item-user") == ("u-ada",)
        assert await _acl_of(stack, "drive-1:item-group", "item-group") == ("g-eng",)
        assert await _acl_of(stack, "drive-1:item-anon", "item-anon") == (PUBLIC_LINK,)
        assert await _acl_of(stack, "drive-1:item-org", "item-org") == (TENANT_ALL,)
        assert await _acl_of(stack, "drive-1:item-everyone", "item-everyone") == (TENANT_ALL,)

        # (a2) the literal Meridian Word body reached the real governed writer,
        # together with version/MIME provenance. The xlsx stays explicitly and
        # honestly metadata-only.
        user_chunk = (await stack.store.get([_item_id("drive-1:item-user", "item-user")]))[0]
        # A PARSED document's searchable text is the document PROSE — never the
        # record envelope JSON. The exact record payload rides in metadata.
        assert MERIDIAN_BODY_PHRASE in user_chunk.text
        assert not user_chunk.text.startswith("{")
        user_payload = user_chunk.metadata[PARSED_RECORD_METADATA_KEY]
        assert user_chunk.text == user_payload["body"]
        assert "web_url" not in user_payload
        assert user_chunk.metadata["source_url"] == (
            "https://contoso.sharepoint.com/sites/eng/Shared%20Documents/Q1%20Plan.docx"
        )
        assert MERIDIAN_BODY_PHRASE in user_payload["body"]
        assert user_payload["body_ingestion_status"] == "parsed"
        assert user_payload["body_source_format"] == "docx"
        assert user_payload["body_parser_identity"] == "neutron-m365-document-parser/v1"
        assert user_payload["mime_type"].endswith("wordprocessingml.document")
        assert user_payload["etag"] == '"etag-user-v1"'
        semantic = lineage.stage_evidence(
            user_chunk.document_id,
            1,
            DocumentStage.CHUNK,
            ("u-ada",),
        )
        assert semantic is not None
        assert semantic.event.audit_event_id is not None
        assert semantic.semantic_chunks
        assert all(row.identifier is not None for row in semantic.semantic_chunks)
        group_chunk = (await stack.store.get([_item_id("drive-1:item-group", "item-group")]))[0]
        group_payload = json.loads(group_chunk.text)
        assert group_payload["body_ingestion_status"] == "unsupported"
        assert "body" not in group_payload

        # (b) quarantined items produced NO chunk.
        assert (
            await stack.store.get([_item_id("drive-1:item-q-unresolvable", "item-q-unresolvable")])
            == []
        )
        assert await stack.store.get([_item_id("drive-1:item-q-forged", "item-q-forged")]) == []

        # (c) exactly one connector.sync event, config_hash-stamped, chain valid.
        sync_events = _events(stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        assert sync_events[0]["outcome"] == "success"
        verification = await stack.sink.verify()
        assert verification.valid

        # (d) one acl_quarantine denied event carrying the quarantined ids.
        quarantine_events = _events(stack, "connector.acl_quarantine")
        assert len(quarantine_events) == 1
        assert quarantine_events[0]["outcome"] == "denied"
        quarantine_id = quarantine_events[0]["id"]
        sync_id = sync_events[0]["id"]
        assert isinstance(quarantine_id, int)
        assert isinstance(sync_id, int)
        assert quarantine_id < sync_id
        payload = quarantine_events[0]["payload"]
        assert isinstance(payload, dict)
        detail = str(payload["detail"])
        assert "quarantined=2" in detail
        assert "item-q-forged" in detail

        # (d2) the pre-authenticated redirect was allowlisted and followed with
        # NO Graph bearer; it never entered payloads or audit/error surfaces.
        redirected = [request for request in requests if str(request.url) == redirect_url]
        assert len(redirected) == 1
        assert "Authorization" not in redirected[0].headers
        persisted = (
            json.dumps(_audit_rows(stack), default=str)
            + user_chunk.text
            + json.dumps(user_chunk.metadata, default=str)
        )
        assert "parsed_source_evidence" not in persisted
        assert "ne.structural_chunker/default-v1" not in persisted
        assert redirect_secret not in persisted
        assert "@microsoft.graph.downloadUrl" not in persisted
        assert "FAKE.ACCESS.TOKEN-not-a-real-credential" not in persisted

        # (e) delta cursor persisted for the drive.
        assert state_store.load_state(_SOURCE) == {"delta:drive-1": DELTA_LINK_1}
        # (f) report.cursors reflects the POST-commit state (FIX-D), not stale.
        assert dict(outcome.report.cursors) == {"delta:drive-1": DELTA_LINK_1}

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_parsed_records_require_the_exact_connector_instance_carrier(
    writer_stack: WriterStack,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    original = M365GraphConnector.take_parsed_evidence

    def broken(self, source_id, record_id, payload, acl, item_id):
        if mutation == "missing":
            return None
        forged = dict(payload)
        forged["body"] = f"{payload['body']} tampered"
        return original(self, source_id, record_id, forged, acl, item_id)

    monkeypatch.setattr(M365GraphConnector, "take_parsed_evidence", broken)

    async def scenario() -> None:
        with pytest.raises(ValueError, match=r"missing exact parser evidence|round trip failed"):
            await run_m365_sync(
                m365_settings(),
                writer=writer_stack.writer,
                state_store=ConnectorStateStore(),
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(),
                parser=RecordedBodyParser(),
            )
        assert await writer_stack.store.get([_item_id("drive-1:item-user", "item-user")]) == []

    asyncio.run(scenario())


def test_auth_failure_audit_projects_only_closed_class(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    sentinel_body = "PROVIDER-BODY-MUST-NOT-LEAK"
    sentinel_token = "TOKEN-MUST-NOT-LEAK"

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            401,
            json={"error": sentinel_body, "access_token": sentinel_token},
        )

    async def scenario() -> None:
        with pytest.raises(GraphUnauthorizedError):
            await run_m365_sync(
                m365_settings(),
                writer=writer_stack.writer,
                state_store=ConnectorStateStore(),
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=httpx.MockTransport(handler),
                parser=RecordedBodyParser(),
            )

        sync_events = _events(writer_stack, "connector.sync")
        assert len(sync_events) == 1
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == "mode=backfill error_class=GraphUnauthorizedError"
        persisted = json.dumps(_audit_rows(writer_stack), default=str)
        assert sentinel_body not in persisted
        assert sentinel_token not in persisted
        assert "login.microsoftonline.com" not in persisted

    asyncio.run(scenario())


def test_permission_401_aborts_without_delta_commit_and_audits_closed_class(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    state_store = ConnectorStateStore()
    sentinel_body = "PERMISSION-BODY-MUST-NOT-LEAK"
    transport = build_m365_transport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/items/item-user/permissions"):
            return httpx.Response(401, json={"error": sentinel_body})
        return await transport.handle_async_request(request)

    async def scenario() -> None:
        with pytest.raises(GraphUnauthorizedError):
            await run_m365_sync(
                m365_settings(),
                writer=writer_stack.writer,
                state_store=state_store,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=httpx.MockTransport(handler),
                parser=RecordedBodyParser(),
            )

        assert state_store.load_state(_SOURCE) == {}
        assert (
            await writer_stack.store.get(
                [
                    _item_id("drive-1:item-user", "item-user"),
                    _item_id("drive-1:item-group", "item-group"),
                ]
            )
            == []
        )
        sync_events = _events(writer_stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["outcome"] == "error"
        assert sync_events[0]["config_hash"] == frozen_config
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == "mode=backfill error_class=GraphUnauthorizedError"
        assert sentinel_body not in json.dumps(_audit_rows(writer_stack), default=str)

    asyncio.run(scenario())


def test_malformed_page_cannot_adopt_delta_and_audits_closed_parse_class(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    state_store = ConnectorStateStore()
    sentinel_body = "PROVIDER-PAYLOAD-MUST-NOT-LEAK"
    transport = build_m365_transport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/root/delta"):
            return httpx.Response(
                200,
                json={
                    "value": {},
                    "@odata.deltaLink": DELTA_LINK_1,
                    "provider_detail": sentinel_body,
                },
            )
        return await transport.handle_async_request(request)

    async def scenario() -> None:
        with pytest.raises(GraphResponseParseError) as excinfo:
            await run_m365_sync(
                m365_settings(site_ids=(), drive_ids=("drive-1",)),
                writer=writer_stack.writer,
                state_store=state_store,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=httpx.MockTransport(handler),
                parser=RecordedBodyParser(),
            )
        assert type(excinfo.value) is GraphResponseParseError
        assert isinstance(excinfo.value, GraphParseError)

        assert state_store.load_state(_SOURCE) == {}
        sync_events = _events(writer_stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["outcome"] == "error"
        assert sync_events[0]["config_hash"] == frozen_config
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == ("mode=backfill error_class=GraphResponseParseError")
        assert sentinel_body not in json.dumps(_audit_rows(writer_stack), default=str)

    asyncio.run(scenario())


def test_deep_json_page_aborts_without_cursor_or_write_and_audits_closed_class(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    state_store = ConnectorStateStore()
    sentinel_body = "PROVIDER-DEEP-JSON-SENTINEL-MUST-NOT-LEAK"
    body = _deep_json_body(sentinel_body)
    transport = build_m365_transport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/root/delta"):
            return httpx.Response(
                200,
                text=body,
                headers={"Content-Type": "application/json"},
            )
        return await transport.handle_async_request(request)

    async def scenario() -> None:
        with pytest.raises(GraphResponseParseError) as excinfo:
            await run_m365_sync(
                m365_settings(site_ids=(), drive_ids=("drive-1",)),
                writer=writer_stack.writer,
                state_store=state_store,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=httpx.MockTransport(handler),
                parser=RecordedBodyParser(),
            )
        assert type(excinfo.value) is GraphResponseParseError
        assert isinstance(excinfo.value, GraphParseError)
        assert state_store.load_state(_SOURCE) == {}
        assert (
            await writer_stack.store.get(
                [
                    _item_id("drive-1:item-user", "item-user"),
                    _item_id("drive-1:item-group", "item-group"),
                ]
            )
            == []
        )
        sync_events = _events(writer_stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["outcome"] == "error"
        assert sync_events[0]["config_hash"] == frozen_config
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == ("mode=backfill error_class=GraphResponseParseError")
        assert sentinel_body not in json.dumps(_audit_rows(writer_stack), default=str)

    asyncio.run(scenario())


def test_delta_redirect_cannot_adopt_cursor_and_audits_closed_redirect_class(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    state_store = ConnectorStateStore()
    sentinel_location = "https://evil.example/steal-bearer"
    sentinel_cursor = "REDIRECT-CURSOR-MUST-NOT-BE-ADOPTED"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url).startswith("https://login.microsoftonline.com/"):
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
        if request.url.path.endswith("/root/delta"):
            return httpx.Response(
                302,
                headers={"Location": sentinel_location},
                json={"value": [], "@odata.deltaLink": sentinel_cursor},
            )
        raise AssertionError("redirected or unexpected request")

    async def scenario() -> None:
        with pytest.raises(GraphRedirectError):
            await run_m365_sync(
                m365_settings(site_ids=(), drive_ids=("drive-1",)),
                writer=writer_stack.writer,
                state_store=state_store,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=httpx.MockTransport(handler),
                parser=RecordedBodyParser(),
            )

        assert state_store.load_state(_SOURCE) == {}
        assert all(str(request.url) != sentinel_location for request in requests)
        sync_events = _events(writer_stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["outcome"] == "error"
        assert sync_events[0]["config_hash"] == frozen_config
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == "mode=backfill error_class=GraphRedirectError"
        persisted = json.dumps(_audit_rows(writer_stack), default=str)
        assert sentinel_location not in persisted
        assert sentinel_cursor not in persisted

    asyncio.run(scenario())


def test_hostile_delta_link_is_refused_before_cursor_commit_and_audited_safely(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    state_store = ConnectorStateStore()
    hostile_host = "evil.example"
    hostile_token = "SECRET-DELTA-CURSOR"
    hostile_delta = f"https://{hostile_host}/steal?$deltatoken={hostile_token}"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url).startswith("https://login.microsoftonline.com/"):
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
        if request.url.path.endswith("/root/delta"):
            return httpx.Response(
                200,
                json={"value": [], "@odata.deltaLink": hostile_delta},
            )
        raise AssertionError("hostile cursor was requested")

    async def scenario() -> None:
        with pytest.raises(GraphRedirectError):
            await run_m365_sync(
                m365_settings(site_ids=(), drive_ids=("drive-1",)),
                writer=writer_stack.writer,
                state_store=state_store,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=httpx.MockTransport(handler),
                parser=RecordedBodyParser(),
            )

        assert state_store.load_state(_SOURCE) == {}
        assert all(request.url.host != hostile_host for request in requests)
        sync_events = _events(writer_stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["outcome"] == "error"
        assert sync_events[0]["config_hash"] == frozen_config
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == "mode=backfill error_class=GraphRedirectError"
        persisted = json.dumps(_audit_rows(writer_stack), default=str)
        assert hostile_host not in persisted
        assert hostile_token not in persisted

    asyncio.run(scenario())


def test_source_link_authority_is_config_hashed_and_fails_closed(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    assert compute_config_hash(m365_settings()) != compute_config_hash(
        m365_settings(source_link_allowed_hosts=("contoso.sharepoint.com",))
    )
    stack = writer_stack

    async def scenario() -> None:
        # A valid upstream webUrl is not trusted without the dedicated authority.
        await run_m365_sync(
            m365_settings(content_allowed_hosts=("contoso.sharepoint.com",)),
            writer=stack.writer,
            state_store=ConnectorStateStore(),
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_m365_transport(),
            parser=RecordedBodyParser(),
        )
        user_id = _item_id("drive-1:item-user", "item-user")
        [without_authority] = await stack.store.get([user_id])
        assert "source_url" not in without_authority.metadata
        assert "web_url" not in without_authority.metadata[PARSED_RECORD_METADATA_KEY]
        assert "web_url" not in without_authority.text

        # Query-bearing and off-host URLs remain absent even with an authority;
        # the fixture's credential-bearing downloadUrl is never a fallback.
        for hostile in (
            "https://contoso.sharepoint.com/sites/eng/SOP.docx?tempauth=secret",
            "https://evil.example/sites/eng/SOP.docx",
        ):
            await run_m365_sync(
                m365_settings(source_link_allowed_hosts=("contoso.sharepoint.com",)),
                writer=stack.writer,
                state_store=ConnectorStateStore(),
                audit_sink=stack.sink,
                actor="connector:test",
                transport=build_m365_transport(item_overrides={"item-user": {"webUrl": hostile}}),
                parser=RecordedBodyParser(),
            )
            [chunk] = await stack.store.get([user_id])
            assert "source_url" not in chunk.metadata
            assert "web_url" not in chunk.metadata[PARSED_RECORD_METADATA_KEY]
            assert "web_url" not in chunk.text
            persisted = json.dumps(chunk.metadata, default=str)
            assert "tempauth" not in persisted
            assert "downloadUrl" not in persisted

    asyncio.run(scenario())


def test_terminal_audit_failure_invalidates_committed_mapper_revision_for_retry(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()
    settings = m365_settings(source_link_allowed_hosts=("contoso.sharepoint.com",))

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="terminal audit failed"):
            await run_m365_sync(
                settings,
                writer=stack.writer,
                state_store=state_store,
                audit_sink=cast(AuditSink, _FailConnectorSuccessAudit(stack.sink)),
                actor="connector:test",
                transport=build_m365_transport(),
                parser=RecordedBodyParser(),
            )
        assert state_store.load_mapper_revision(_SOURCE) is None

        retry = await run_m365_sync(
            settings,
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_m365_transport(),
            parser=RecordedBodyParser(),
        )
        assert retry.mode == "backfill"
        assert state_store.load_mapper_revision(_SOURCE) is not None
        [chunk] = await stack.store.get([_item_id("drive-1:item-user", "item-user")])
        assert chunk.metadata["source_url"] == (
            "https://contoso.sharepoint.com/sites/eng/Shared%20Documents/Q1%20Plan.docx"
        )

    asyncio.run(scenario())


def test_source_link_authority_revision_reprocesses_identical_payload_and_removes_stale_link(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()
    user_id = _item_id("drive-1:item-user", "item-user")

    async def run_with(hosts: tuple[str, ...]):  # type: ignore[no-untyped-def]
        return await run_m365_sync(
            m365_settings(source_link_allowed_hosts=hosts),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_m365_transport(),
            parser=RecordedBodyParser(),
        )

    async def scenario() -> None:
        disabled = await run_with(())
        assert disabled.mode == "backfill"
        assert disabled.report.written == 5
        disabled_revision = state_store.load_mapper_revision(_SOURCE)
        assert disabled_revision is not None
        assert state_store.load_state(_SOURCE) == {"delta:drive-1": DELTA_LINK_1}
        [without_link] = await stack.store.get([user_id])
        assert "source_url" not in without_link.metadata

        enabled = await run_with(("contoso.sharepoint.com",))
        assert enabled.mode == "backfill"  # revision change ignores the delta cursor
        assert enabled.report.written == 5
        enabled_revision = state_store.load_mapper_revision(_SOURCE)
        assert enabled_revision is not None and enabled_revision != disabled_revision
        [with_link] = await stack.store.get([user_id])
        assert with_link.chunk_id == without_link.chunk_id
        assert with_link.text == without_link.text
        assert with_link.metadata["source_url"] == (
            "https://contoso.sharepoint.com/sites/eng/Shared%20Documents/Q1%20Plan.docx"
        )

        revoked = await run_m365_sync(
            m365_settings(source_link_allowed_hosts=()),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_m365_transport(perm_status_overrides={"item-user": 403}),
            parser=RecordedBodyParser(),
        )
        assert revoked.mode == "backfill"  # revocation replays every historical row
        assert revoked.report.written == 4
        assert state_store.load_mapper_revision(_SOURCE) == disabled_revision
        [after_revoke] = await stack.store.get([user_id])
        assert after_revoke.chunk_id == with_link.chunk_id
        assert after_revoke.text == with_link.text
        # The now-ACL-quarantined identity was omitted from the mapper, so the
        # run-scope reconciliation must still clear its previously stored URL.
        assert after_revoke.metadata["source_url"] is None

        unchanged = await run_with(())
        assert unchanged.mode == "incremental"
        assert unchanged.report.written == 1
        second_unchanged = await run_with(())
        assert second_unchanged.mode == "incremental"
        assert second_unchanged.report.written == 0
        assert second_unchanged.report.skipped_unchanged == 0

    asyncio.run(scenario())


def test_second_run_is_incremental_and_writes_only_the_changed_item(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        first = await run_m365_sync(
            m365_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_m365_transport(),
            parser=RecordedBodyParser(),
        )
        assert first.report.written == 5

        second = await run_m365_sync(
            m365_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_m365_transport(),
            parser=RecordedBodyParser(),
        )
        assert second.mode == "incremental"
        assert second.report.written == 1  # only the changed item-user
        # deltaLink advanced to the incremental token.
        assert state_store.load_state(_SOURCE) == {"delta:drive-1": DELTA_LINK_2}
        # report.cursors reflects the advanced post-commit state (FIX-D).
        assert dict(second.report.cursors) == {"delta:drive-1": DELTA_LINK_2}
        # the changed record is now stored: the revised NAME is envelope
        # metadata (the carried record payload), the prose stays the text.
        chunk = (await stack.store.get([_item_id("drive-1:item-user", "item-user")]))[0]
        assert "revised" in str(chunk.metadata[PARSED_RECORD_METADATA_KEY]["name"])
        assert MERIDIAN_BODY_PHRASE in chunk.text

    asyncio.run(scenario())


def test_mid_run_failure_does_not_advance_delta_and_re_run_has_no_duplicates(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        # Run 1: a Graph fault during the (atomic, permissions-first) crawl aborts
        # the run BEFORE any write; nothing is written and no delta is advanced.
        with pytest.raises(GraphSyncError):
            await run_m365_sync(
                m365_settings(),
                writer=stack.writer,
                state_store=state_store,
                audit_sink=stack.sink,
                actor="connector:test",
                transport=build_m365_transport(fail_after=3),
                parser=RecordedBodyParser(),
            )
        assert state_store.load_state(_SOURCE) == {}  # delta NOT advanced

        # Run 2 (retry, clean): completes the backfill and writes every item once.
        retry = await run_m365_sync(
            m365_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_m365_transport(),
            parser=RecordedBodyParser(),
        )
        assert retry.report.written == 5
        assert state_store.load_state(_SOURCE) == {"delta:drive-1": DELTA_LINK_1}
        committed_revision = state_store.load_mapper_revision(_SOURCE)
        assert committed_revision is not None

        # A changed source-link decision forces a backfill despite the durable
        # delta cursor. Failure preserves the old revision, cursor, and dedup;
        # the next clean attempt must still replay the whole source.
        with pytest.raises(GraphSyncError):
            await run_m365_sync(
                m365_settings(source_link_allowed_hosts=("contoso.sharepoint.com",)),
                writer=stack.writer,
                state_store=state_store,
                audit_sink=stack.sink,
                actor="connector:test",
                transport=build_m365_transport(fail_after=3),
                parser=RecordedBodyParser(),
            )
        assert state_store.load_mapper_revision(_SOURCE) == committed_revision
        assert state_store.load_state(_SOURCE) == {"delta:drive-1": DELTA_LINK_1}

        refreshed = await run_m365_sync(
            m365_settings(source_link_allowed_hosts=("contoso.sharepoint.com",)),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_m365_transport(),
            parser=RecordedBodyParser(),
        )
        assert refreshed.mode == "backfill"
        assert refreshed.report.written == 5
        assert state_store.load_mapper_revision(_SOURCE) != committed_revision

        # Run 3 (incremental) writes the changed item, advancing to DELTA_LINK_2.
        third = await run_m365_sync(
            m365_settings(source_link_allowed_hosts=("contoso.sharepoint.com",)),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_m365_transport(),
            parser=RecordedBodyParser(),
        )
        assert third.report.written == 1

        # Rewind the ledger to LINK_1 and re-run: the same item-user content is now
        # dedup-skipped — skipped_unchanged>0 and ZERO duplicate chunks written.
        state_store.set_cursor(_SOURCE, "delta:drive-1", DELTA_LINK_1)
        fourth = await run_m365_sync(
            m365_settings(source_link_allowed_hosts=("contoso.sharepoint.com",)),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_m365_transport(),
            parser=RecordedBodyParser(),
        )
        assert fourth.report.written == 0
        assert fourth.report.skipped_unchanged >= 1

        # Exactly the 5 resolved driveItems exist as chunks (no duplicates).
        contract = m365_data_contract(m365_settings())
        assert contract.source == _SOURCE
        for source_id, record_id in (
            ("drive-1:item-user", "item-user"),
            ("drive-1:item-group", "item-group"),
            ("drive-1:item-anon", "item-anon"),
            ("drive-1:item-org", "item-org"),
            ("drive-1:item-everyone", "item-everyone"),
        ):
            assert len(await stack.store.get([_item_id(source_id, record_id)])) == 1

    asyncio.run(scenario())


def test_perm_fetch_failure_quarantines_item_and_still_advances_delta(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        # item-user's permissions endpoint returns a persistent 403: that ONE item
        # is quarantined, the crawl continues, and the deltaLink still commits — the
        # wedge is gone (FIX-B), verified over the real writer/audit stack.
        outcome = await run_m365_sync(
            m365_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_m365_transport(perm_status_overrides={"item-user": 403}),
            parser=RecordedBodyParser(),
        )
        assert outcome.report.written == 4
        assert outcome.acl_quarantined == 3  # 2 mapping + 1 fetch

        # the quarantined item is NEVER stored (never world-readable).
        assert await stack.store.get([_item_id("drive-1:item-user", "item-user")]) == []

        # exactly one acl_quarantine denied event naming the item + the counters.
        quarantine_events = _events(stack, "connector.acl_quarantine")
        assert len(quarantine_events) == 1
        payload = quarantine_events[0]["payload"]
        assert isinstance(payload, dict)
        detail = str(payload["detail"])
        assert "quarantined=3" in detail
        assert "fetch_failed=1" in detail
        assert "item-user" in detail

        # the deltaLink COMMITTED despite the quarantine — durable progress.
        assert state_store.load_state(_SOURCE) == {"delta:drive-1": DELTA_LINK_1}

        # audit chain still verifies end-to-end.
        verification = await stack.sink.verify()
        assert verification.valid

    asyncio.run(scenario())
