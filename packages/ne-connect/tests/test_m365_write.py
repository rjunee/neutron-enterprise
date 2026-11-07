# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GraphWriteClient.create_draft + GraphClient.post_json: SSRF guard, C12 hygiene (NE-050)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ne.connect.errors import (
    GraphForbiddenError,
    GraphRedirectError,
    GraphSyncError,
    GraphUpstreamError,
)
from ne.connect.m365.graph import GraphClient
from ne.connect.m365.write import GraphWriteClient

from .m365_transport import GRAPH, TOKEN_URL, m365_settings

_MAILBOX = "sender@contoso.test"
_MESSAGE_ID = "AAMkAGZ-fake-message-id"
_DRAFT_URL = f"{GRAPH}/users/{_MAILBOX}/messages"


def _graph(transport: httpx.MockTransport) -> GraphClient:
    return GraphClient(m365_settings(), transport=transport)


def _transport(
    *,
    recorder: list[httpx.Request] | None = None,
    create_status: int = 201,
    one_shot_429: bool = False,
) -> httpx.MockTransport:
    state = {"ratelimited": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        url = str(request.url)
        if url == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
        auth = request.headers.get("Authorization", "")
        assert auth.startswith("Bearer "), f"missing bearer on {url}"
        if request.url.path.endswith("/messages") and request.method == "POST":
            if one_shot_429 and not state["ratelimited"]:
                state["ratelimited"] = True
                return httpx.Response(429, headers={"Retry-After": "0"})
            if create_status >= 400:
                return httpx.Response(create_status, json={"error": {"code": "x"}})
            return httpx.Response(create_status, json={"id": _MESSAGE_ID, "isDraft": True})
        return httpx.Response(404, json={"error": {"code": "notFound"}})

    return httpx.MockTransport(handler)


def test_post_json_guards_url_before_any_token_spend() -> None:
    recorder: list[httpx.Request] = []
    graph = _graph(_transport(recorder=recorder))

    async def scenario() -> None:
        async with graph.session() as http:
            # An absolute URL to another host must be refused BEFORE any token POST.
            await graph.post_json(http, "https://evil.example.com/v1.0/x", {"a": 1})

    with pytest.raises(GraphRedirectError):
        asyncio.run(scenario())
    assert recorder == []  # no token spent, no request emitted


def test_post_json_guards_cleartext_url() -> None:
    graph = _graph(_transport())

    async def scenario() -> None:
        async with graph.session() as http:
            await graph.post_json(http, "http://graph.microsoft.com/v1.0/x", {"a": 1})

    with pytest.raises(GraphRedirectError):
        asyncio.run(scenario())


def test_post_json_redirect_is_refused_without_using_location_or_payload() -> None:
    sentinel_location = "https://evil.example/steal-write"
    sentinel_body = "REDIRECT-PAYLOAD-MUST-NOT-LEAK"
    recorder: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
        return httpx.Response(
            302,
            headers={"Location": sentinel_location},
            json={"id": sentinel_body},
        )

    transport = httpx.MockTransport(handler)
    graph = _graph(transport)

    async def scenario() -> None:
        async with graph.session() as http:
            with pytest.raises(GraphRedirectError) as exc:
                await graph.post_json(http, _DRAFT_URL, {"body": sentinel_body})
            assert type(exc.value) is GraphRedirectError
            assert exc.value.status == 302
            assert sentinel_location not in str(exc.value)
            assert sentinel_body not in str(exc.value)

    asyncio.run(scenario())
    assert [str(request.url) for request in recorder] == [TOKEN_URL, _DRAFT_URL]


def test_create_draft_happy_path_posts_exact_message() -> None:
    recorder: list[httpx.Request] = []
    client = GraphWriteClient(_graph(_transport(recorder=recorder)))

    message_id = asyncio.run(
        client.create_draft(
            mailbox=_MAILBOX, subject="Hello", body="Body text", to="dest@contoso.test"
        )
    )

    assert message_id == _MESSAGE_ID
    posts = [r for r in recorder if r.url.path.endswith("/messages")]
    assert len(posts) == 1
    body = json.loads(posts[0].content.decode())
    assert body == {
        "subject": "Hello",
        "body": {"contentType": "Text", "content": "Body text"},
        "toRecipients": [{"emailAddress": {"address": "dest@contoso.test"}}],
    }


def test_malformed_mailbox_raises_before_request() -> None:
    recorder: list[httpx.Request] = []
    client = GraphWriteClient(_graph(_transport(recorder=recorder)))

    with pytest.raises(GraphSyncError) as exc:
        asyncio.run(
            client.create_draft(
                mailbox="not a mailbox!", subject="s", body="b", to="d@contoso.test"
            )
        )
    assert "not a mailbox" not in str(exc.value)  # never echo the value
    assert recorder == []


def test_http_400_is_status_only_error() -> None:
    client = GraphWriteClient(_graph(_transport(create_status=400)))
    with pytest.raises(GraphSyncError) as exc:
        asyncio.run(
            client.create_draft(
                mailbox=_MAILBOX, subject="s", body="secret-body", to="d@contoso.test"
            )
        )
    assert "secret-body" not in str(exc.value)


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(403, GraphForbiddenError), (500, GraphUpstreamError)],
)
def test_post_json_projects_closed_http_failure_class(
    status: int, error_type: type[GraphSyncError]
) -> None:
    client = GraphWriteClient(_graph(_transport(create_status=status)))
    with pytest.raises(error_type) as exc:
        asyncio.run(
            client.create_draft(
                mailbox=_MAILBOX, subject="s", body="secret-body", to="d@contoso.test"
            )
        )
    assert type(exc.value) is error_type
    assert exc.value.status == status
    assert "secret-body" not in str(exc.value)


def test_retry_on_429_then_succeeds() -> None:
    client = GraphWriteClient(_graph(_transport(one_shot_429=True)))
    message_id = asyncio.run(
        client.create_draft(mailbox=_MAILBOX, subject="s", body="b", to="d@contoso.test")
    )
    assert message_id == _MESSAGE_ID


@pytest.mark.parametrize(
    "payload",
    [
        {"id": _MESSAGE_ID, "isDraft": False},
        {"id": _MESSAGE_ID},
        {"id": "", "isDraft": True},
        {"isDraft": True},
    ],
)
def test_create_draft_requires_provider_draft_attestation(payload: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
        return httpx.Response(201, json=payload)

    client = GraphWriteClient(_graph(httpx.MockTransport(handler)))
    with pytest.raises(GraphSyncError, match="attest creation of an unsent draft"):
        asyncio.run(
            client.create_draft(
                mailbox=_MAILBOX,
                subject="s",
                body="sensitive body",
                to="d@contoso.test",
            )
        )
