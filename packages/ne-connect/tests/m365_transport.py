# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Recorded-fixture Graph transport for the M365 connector suites (NE-044).

``build_m365_transport`` returns an :class:`httpx.MockTransport` that replays the
hand-recorded (sanitized-fake) Graph JSON under ``fixtures/m365`` with ZERO
network sockets — the CI deliverable per NE-000. It asserts every Graph call
carries a Bearer ``Authorization`` header, optionally records requests, and
exposes fault-injection knobs (fail-after-N Graph requests; a 410 for a stale
delta token) used by the durability / delta-expiry tests.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

from ne.brain.ingest.contracts import BlockKind, DocBlock, ParsedDocument
from ne.connect.m365.config import M365ConnectorSettings

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "m365"

TENANT_ID = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
SITE_ID = "site-eng"
DRIVE_ID = "drive-1"

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
TOKEN_URL = f"{LOGIN}/{TENANT_ID}/oauth2/v2.0/token"
SITE_DRIVES_URL = f"{GRAPH}/sites/{SITE_ID}/drives"
DELTA_ROOT = f"{GRAPH}/drives/{DRIVE_ID}/root/delta"
DELTA_PAGE2 = f"{DELTA_ROOT}?$skiptoken=PAGE2TOKEN"
DELTA_LINK_1 = f"{DELTA_ROOT}?$deltatoken=DELTA1TOKEN"
DELTA_LINK_3 = f"{GRAPH}/drives/{DRIVE_ID}/root/delta?$deltatoken=DELTA3TOKEN"
DELTA_LINK_2 = f"{DELTA_ROOT}?$deltatoken=DELTA2TOKEN"

_PERMS_RE = re.compile(rf"^{re.escape(GRAPH)}/drives/{DRIVE_ID}/items/(?P<item>[^/]+)/permissions$")
_CONTENT_RE = re.compile(rf"^{re.escape(GRAPH)}/drives/{DRIVE_ID}/items/(?P<item>[^/]+)/content$")
#: The bare driveItem metadata endpoint the bounded permission sweep re-reads a
#: quiet point through. It replays the SAME fixture dict the delta carried, so an
#: unchanged point produces byte-identical canonical content on a re-read — which
#: is what makes "the sweep reuses the artifact" a real property rather than an
#: artefact of the fixture.
_ITEM_RE = re.compile(rf"^{re.escape(GRAPH)}/drives/{DRIVE_ID}/items/(?P<item>[^/]+)$")


def _fixture_items() -> dict[str, dict[str, object]]:
    items: dict[str, dict[str, object]] = {}
    for name in ("delta_page1.json", "delta_page2.json", "delta_incremental.json"):
        values = _load(name).get("value")
        assert isinstance(values, list)
        for item in values:
            assert isinstance(item, dict)
            if "deleted" not in item and "root" not in item and "folder" not in item:
                items[str(item["id"])] = item
    return items


MERIDIAN_DOCX = (
    Path(__file__).resolve().parents[3]
    / "tools"
    / "meridian"
    / "fixtures"
    / "meridian-v1"
    / "assets"
    / "debit-card-dispute-handling-sop-v3.docx"
)
MERIDIAN_BODY_PHRASE = "provide provisional credit within 10 business days after customer notice"


class RecordedBodyParser:
    """Fixture implementation of the real ``DocumentParser`` seam.

    ``item-user`` must receive the checked-in Meridian DOCX bytes, proving the
    Graph body reaches the parser boundary. The returned structure mirrors the
    Docling contract while keeping ordinary connector CI free of the isolated
    heavyweight parser runtime; production defaults to ``DoclingParser``.
    """

    def __init__(self, *, fail: bool = False, item_user_text: str | None = None) -> None:
        self.fail = fail
        self.item_user_text = item_user_text
        self.calls: list[tuple[str, str, bytes]] = []

    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument:
        content = path.read_bytes()
        return self._parsed(content, document_id=document_id, source_name=path.name)

    async def parse_fd(
        self,
        file_fd: int,
        *,
        document_id: str,
        source_name: str,
    ) -> ParsedDocument:
        os.lseek(file_fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(file_fd, 64 * 1024):
            chunks.append(chunk)
        return self._parsed(b"".join(chunks), document_id=document_id, source_name=source_name)

    def _parsed(self, content: bytes, *, document_id: str, source_name: str) -> ParsedDocument:
        source_format = Path(source_name).suffix.lstrip(".")
        self.calls.append((document_id, source_format, content))
        if self.fail:
            raise RuntimeError(
                "fixture parse failure with https://host/download?tempauth=DO-NOT-LEAK"
            )
        if document_id == f"{DRIVE_ID}:item-user":
            assert content == MERIDIAN_DOCX.read_bytes()
            text = self.item_user_text or (
                f"Standard tier: {MERIDIAN_BODY_PHRASE} when eligibility criteria are met."
            )
        else:
            text = f"Parsed fixture body for {document_id}."
        return ParsedDocument(
            document_id=document_id,
            source_name=Path(source_name).name,
            source_format=source_format,
            blocks=(DocBlock(kind=BlockKind.PARAGRAPH, text=text),),
            metadata={},
        )


def _load(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def m365_settings(**overrides: object) -> M365ConnectorSettings:
    """A valid secret-mode settings object pointed at the fixture tenant."""
    kwargs: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "client_id": CLIENT_ID,
        "auth_method": "secret",
        "client_secret": "fake-secret-not-real",
        "site_ids": (SITE_ID,),
        "owner": "data-eng@contoso.test",
    }
    kwargs.update(overrides)
    return M365ConnectorSettings(**kwargs)  # type: ignore[arg-type]


def _json(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def build_m365_transport(
    *,
    recorder: list[httpx.Request] | None = None,
    fail_after: int | None = None,
    stale_delta: bool = False,
    token_expires_in: int = 3600,
    perm_status_overrides: Mapping[str, int] | None = None,
    permission_payload_overrides: Mapping[str, Mapping[str, object]] | None = None,
    content_status_overrides: Mapping[str, int] | None = None,
    content_bytes_overrides: Mapping[str, bytes] | None = None,
    content_headers_overrides: Mapping[str, Mapping[str, str]] | None = None,
    content_redirect_overrides: Mapping[str, str] | None = None,
    item_overrides: Mapping[str, Mapping[str, object]] | None = None,
    item_metadata_overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> httpx.MockTransport:
    """Build the fixture-replay transport.

    ``recorder`` collects every request. ``fail_after`` returns HTTP 500 on Graph
    requests beyond the Nth (token POSTs are not counted) — drives the
    retry-exhaustion / durability tests. ``stale_delta`` returns 410 for the
    stored delta token, forcing the connector's full-delta resync.
    ``token_expires_in`` overrides the token fixture's ``expires_in`` (drives the
    FIX-E TTL tests). ``perm_status_overrides`` maps an item id to an HTTP status
    served for that item's permissions endpoint instead of the fixture (drives the
    FIX-B per-item permissions-fetch failure tests). ``permission_payload_overrides``
    replaces only the recorded provider response for a named permissions endpoint;
    it does not select the querying principal or bypass the connector's mapper.
    Content overrides drive the BD-4 bounded-download / redirect / parse-failure tests.
    """
    state = {"graph_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        url = str(request.url)

        if url == TOKEN_URL:
            assert request.method == "POST"
            token = _load("token.json")
            token["expires_in"] = token_expires_in
            return _json(token)

        # A response-supplied download redirect is pre-authenticated. The Graph
        # bearer MUST have been stripped before it reaches this host.
        if not url.startswith(GRAPH):
            assert "Authorization" not in request.headers
            item = request.url.path.rsplit("/", 1)[-1]
            content = (
                content_bytes_overrides.get(item, b"redirected fixture body")
                if content_bytes_overrides is not None
                else b"redirected fixture body"
            )
            return httpx.Response(200, content=content)

        # Every Graph (non-token) call must carry the bearer token.
        auth = request.headers.get("Authorization", "")
        assert auth.startswith("Bearer "), f"missing bearer on {url}"

        state["graph_calls"] += 1
        if fail_after is not None and state["graph_calls"] > fail_after:
            return httpx.Response(500)

        if url == SITE_DRIVES_URL:
            return _json(_load("site_drives.json"))
        if url == DELTA_ROOT:
            page = _load("delta_page1.json")
            if item_overrides is not None:
                values = page.get("value")
                assert isinstance(values, list)
                for item in values:
                    assert isinstance(item, dict)
                    override = item_overrides.get(str(item.get("id")))
                    if override is not None:
                        item.update(override)
            return _json(page)
        if url == DELTA_PAGE2:
            return _json(_load("delta_page2.json"))
        if url == DELTA_LINK_1:
            if stale_delta:
                return httpx.Response(410)
            return _json(_load("delta_incremental.json"))
        if url in {DELTA_LINK_2, DELTA_LINK_3}:
            # A steady-state incremental run: the delta is empty and the ONLY
            # Graph traffic left is whatever the bounded permission sweep does.
            return _json(_load("delta_empty.json"))

        content_match = _CONTENT_RE.match(url)
        if content_match is not None:
            item = content_match.group("item")
            if content_status_overrides is not None and item in content_status_overrides:
                return httpx.Response(content_status_overrides[item])
            if content_redirect_overrides is not None and item in content_redirect_overrides:
                return httpx.Response(302, headers={"Location": content_redirect_overrides[item]})
            if content_bytes_overrides is not None and item in content_bytes_overrides:
                content = content_bytes_overrides[item]
            elif item == "item-user":
                content = MERIDIAN_DOCX.read_bytes()
            else:
                content = f"fixture body for {item}".encode()
            headers = (
                dict(content_headers_overrides[item])
                if content_headers_overrides is not None and item in content_headers_overrides
                else {}
            )
            return httpx.Response(200, headers=headers, content=content)

        item_match = _ITEM_RE.match(url)
        if item_match is not None:
            item_id = item_match.group("item")
            known = _fixture_items()
            if item_id not in known:
                return httpx.Response(404, json={"error": {"code": "itemNotFound"}})
            payload = dict(known[item_id])
            if item_metadata_overrides is not None and item_id in item_metadata_overrides:
                payload.update(item_metadata_overrides[item_id])
            return _json(payload)

        perms = _PERMS_RE.match(url)
        if perms is not None:
            item = perms.group("item")
            if perm_status_overrides is not None and item in perm_status_overrides:
                return httpx.Response(perm_status_overrides[item])
            if permission_payload_overrides is not None and item in permission_payload_overrides:
                return _json(dict(permission_payload_overrides[item]))
            return _json(_load(f"perm_{item}.json"))

        return httpx.Response(404, json={"error": {"code": "notFound", "message": url}})

    return httpx.MockTransport(handler)


def sequenced_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    """A transport that returns ``responses`` in order for successive Graph GETs.

    The token POST is served first (from ``token.json``); each subsequent GET pops
    the next queued response. Used by the graph-client retry / status tests.
    """
    queue: list[httpx.Response] = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/oauth2/v2.0/token"):
            return _json(_load("token.json"))
        if queue:
            return queue.pop(0)
        return httpx.Response(404)  # pragma: no cover

    return httpx.MockTransport(handler)


def recording_transport(recorder: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    """A bare handler that records and 200s — proves zero requests on rejection."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={})

    return handler
