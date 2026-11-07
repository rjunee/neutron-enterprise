# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SalesforceWriteClient.create_record: identifier gate, bound JSON body, C12 hygiene (NE-050)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ne.connect.errors import SalesforceSyncError
from ne.connect.salesforce.client import SalesforceClient
from ne.connect.salesforce.write import SalesforceWriteClient

from .salesforce_transport import API_VERSION, salesforce_settings

_CREATE_PATH = f"/services/data/{API_VERSION}/sobjects/Note"
_TOKEN_PATH = "/services/oauth2/token"
_NOTE_ID = "00Q000000000123AAA"
_SECRET = "top-secret-value-not-real"


def _write_client(transport: httpx.MockTransport) -> SalesforceWriteClient:
    client = SalesforceClient(
        salesforce_settings(auth_method="client_credentials", client_secret=_SECRET),
        transport=transport,
    )
    return SalesforceWriteClient(client, api_version=API_VERSION)


def _transport(
    *,
    recorder: list[httpx.Request] | None = None,
    success: bool = True,
    create_status: int = 200,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        if request.url.path == _TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "fake-token", "token_type": "Bearer"})
        if request.url.path == _CREATE_PATH and request.method == "POST":
            if create_status >= 400:
                return httpx.Response(create_status, json={"error": "bad"})
            return httpx.Response(
                create_status, json={"id": _NOTE_ID, "success": success, "errors": []}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_create_record_happy_path_posts_note_body() -> None:
    recorder: list[httpx.Request] = []
    client = _write_client(_transport(recorder=recorder))

    note_id = asyncio.run(
        client.create_record(
            "Note", {"ParentId": "001000000000001AAA", "Title": "Hi", "Body": "world"}
        )
    )

    assert note_id == _NOTE_ID
    posts = [r for r in recorder if r.url.path == _CREATE_PATH]
    assert len(posts) == 1
    import json

    body = json.loads(posts[0].content.decode())
    assert body == {"ParentId": "001000000000001AAA", "Title": "Hi", "Body": "world"}


@pytest.mark.parametrize(
    ("sobject", "fields"),
    [
        ("Note WHERE 1=1", {"Body": "x"}),
        ("Note", {"Body FROM x--": "x"}),
    ],
)
def test_injection_shaped_names_raise_before_any_request(
    sobject: str, fields: dict[str, str]
) -> None:
    recorder: list[httpx.Request] = []
    client = _write_client(_transport(recorder=recorder))

    with pytest.raises(ValueError):
        asyncio.run(client.create_record(sobject, fields))

    assert recorder == []  # rejected at the boundary — not even a token POST


def test_success_false_raises_sync_error() -> None:
    client = _write_client(_transport(success=False))
    with pytest.raises(SalesforceSyncError):
        asyncio.run(client.create_record("Note", {"Body": "x"}))


def test_http_400_is_status_only_error() -> None:
    client = _write_client(_transport(create_status=400))
    with pytest.raises(SalesforceSyncError) as exc:
        asyncio.run(client.create_record("Note", {"Body": _SECRET}))
    message = str(exc.value)
    assert _SECRET not in message  # never echo a field value
    assert "top-secret" not in message
