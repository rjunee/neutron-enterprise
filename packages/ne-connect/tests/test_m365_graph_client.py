# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GraphClient — C2 fail-closed construction, SSRF link guard, retry/410 semantics."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ne.connect.endpoints import AirGapViolationError, UnsafeEndpointError
from ne.connect.errors import (
    DeltaTokenExpiredError,
    GraphForbiddenError,
    GraphParseError,
    GraphRateLimitedError,
    GraphRedirectError,
    GraphResponseParseError,
    GraphSyncError,
    GraphTransportError,
    GraphUnauthorizedError,
    GraphUpstreamError,
)
from ne.connect.m365.graph import GraphClient

from .m365_transport import (
    GRAPH,
    SITE_ID,
    TOKEN_URL,
    build_m365_transport,
    m365_settings,
    recording_transport,
    sequenced_transport,
)

_HOSTILE_GRAPH = [
    "http://graph.microsoft.com/v1.0",  # not https
    "https://169.254.169.254/v1.0",  # metadata IP literal
    "https://2130706433/v1.0",  # packed-integer IPv4
    "https://0x7f000001/v1.0",  # hex packed IPv4
    "https://graph.evil.example.com/v1.0",  # https but NOT allowlisted
]


def _deep_json_body(sentinel: str) -> str:
    quoted = json.dumps(sentinel)
    if len(quoted) % 2 == 0:
        quoted = json.dumps(f"{sentinel}X")
    depth = (20_001 - len(quoted)) // 2
    body = "[" * depth + quoted + "]" * depth
    assert len(body.encode()) == 20_001
    return body


@pytest.mark.parametrize("bad_url", _HOSTILE_GRAPH)
def test_construction_rejects_hostile_graph_url(bad_url: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        GraphClient(m365_settings(graph_base_url=bad_url))


def test_construction_rejects_off_allowlist_login_url() -> None:
    with pytest.raises(UnsafeEndpointError):
        GraphClient(m365_settings(login_base_url="https://login.evil.example.com"))


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",
        "2130706433",
        "user:pass@contoso.sharepoint.com",
        "contoso.sharepoint.com/path",
        "contoso.sharepoint.com:8443",
    ],
)
def test_construction_rejects_unsafe_content_redirect_allowlist_host(host: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        GraphClient(m365_settings(content_allowed_hosts=(host,)))


def test_air_gap_aborts_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    recorder: list[httpx.Request] = []
    with pytest.raises(AirGapViolationError):
        GraphClient(m365_settings(), transport=build_m365_transport(recorder=recorder))
    assert recorder == []  # zero requests recorded


def test_nextlink_to_off_allowlist_host_is_refused_without_request() -> None:
    recorder: list[httpx.Request] = []
    transport = httpx.MockTransport(recording_transport(recorder))
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphRedirectError):
                await client.get_json(
                    http, "https://graph.evil.example.com/v1.0/drives/d/root/delta"
                )

    asyncio.run(scenario())
    assert recorder == []  # the off-host link is never fetched


def test_429_then_success_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    transport = sequenced_transport(
        [
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"value": [{"id": "ok"}]}),
        ]
    )
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            data = await client.get_json(
                http, "https://graph.microsoft.com/v1.0/drives/d/root/delta"
            )
            assert data["value"] == [{"id": "ok"}]

    asyncio.run(scenario())
    assert slept == [3.0, 2.0]  # honored the (capped) Retry-After sequence


def test_retry_exhaustion_raises_graph_sync_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    transport = sequenced_transport([httpx.Response(503) for _ in range(5)])
    client = GraphClient(m365_settings(max_attempts=5), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphUpstreamError) as exc:
                await client.get_json(http, "https://graph.microsoft.com/v1.0/drives/d/root/delta")
            assert "503" in str(exc.value)

    asyncio.run(scenario())


def test_410_raises_delta_token_expired() -> None:
    transport = sequenced_transport([httpx.Response(410)])
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(DeltaTokenExpiredError):
                await client.get_json(http, "https://graph.microsoft.com/v1.0/drives/d/root/delta")

    asyncio.run(scenario())


def test_forbidden_raises_closed_status_only_error() -> None:
    transport = sequenced_transport([httpx.Response(403)])
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphForbiddenError) as exc:
                await client.get_json(http, "https://graph.microsoft.com/v1.0/drives/d/root/delta")
            assert "403" in str(exc.value)
            assert exc.value.status == 403

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, GraphUnauthorizedError),
        (403, GraphForbiddenError),
        (429, GraphRateLimitedError),
        (500, GraphUpstreamError),
        (502, GraphUpstreamError),
        (503, GraphUpstreamError),
    ],
)
def test_http_failure_classes_are_a_closed_nonsecret_projection(
    status: int, error_type: type[GraphSyncError]
) -> None:
    sentinel = "PROVIDER-BODY-MUST-NOT-LEAK"
    transport = sequenced_transport([httpx.Response(status, text=sentinel)])
    client = GraphClient(m365_settings(max_attempts=1), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(error_type) as exc:
                await client.get_json(http, f"{GRAPH}/drives/d/root/delta")
            assert type(exc.value) is error_type
            assert exc.value.status == status
            assert sentinel not in str(exc.value)

    asyncio.run(scenario())


def test_success_with_malformed_json_is_parse_category_without_body_leak() -> None:
    sentinel = "PROVIDER-BODY-MUST-NOT-LEAK"
    transport = sequenced_transport([httpx.Response(200, text=sentinel)])
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphResponseParseError) as exc:
                await client.get_json(http, f"{GRAPH}/drives/d/root/delta")
            assert type(exc.value) is GraphResponseParseError
            assert isinstance(exc.value, GraphParseError)
            assert exc.value.status is None
            assert sentinel not in str(exc.value)

    asyncio.run(scenario())


def test_deep_valid_json_is_closed_response_parse_failure_without_body_leak() -> None:
    sentinel = "PROVIDER-DEEP-JSON-SENTINEL-MUST-NOT-LEAK"
    body = _deep_json_body(sentinel)
    transport = sequenced_transport(
        [httpx.Response(200, text=body, headers={"Content-Type": "application/json"})]
    )
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphResponseParseError) as exc:
                await client.get_json(http, f"{GRAPH}/drives/d/root/delta")
            assert type(exc.value) is GraphResponseParseError
            assert isinstance(exc.value, GraphParseError)
            assert exc.value.status is None
            assert sentinel not in str(exc.value)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [300, 301, 302, 307, 308, 399])
def test_graph_get_redirect_is_refused_before_json_or_location_use(status: int) -> None:
    sentinel_location = "https://evil.example/steal-bearer"
    sentinel_cursor = "REDIRECT-CURSOR-MUST-NOT-BE-ADOPTED"
    recorder: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(
            status,
            headers={"Location": sentinel_location},
            json={"value": [], "@odata.deltaLink": sentinel_cursor},
        )

    transport = httpx.MockTransport(handler)
    client = GraphClient(m365_settings(), transport=transport)
    url = f"{GRAPH}/drives/d/root/delta"

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphRedirectError) as exc:
                await client.get_paginated(http, url)
            assert type(exc.value) is GraphRedirectError
            assert exc.value.status == status
            assert sentinel_location not in str(exc.value)
            assert sentinel_cursor not in str(exc.value)

    asyncio.run(scenario())
    assert [str(request.url) for request in recorder] == [TOKEN_URL, url]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"value": {}},
        {"value": ["not-an-object"]},
        {"value": [], "@odata.nextLink": None},
        {"value": [], "@odata.nextLink": ""},
        {"value": [], "@odata.deltaLink": 7},
        {"value": [], "@odata.deltaLink": "   "},
        {
            "value": [],
            "@odata.nextLink": f"{GRAPH}/drives/d/root/delta?$skiptoken=N",
            "@odata.deltaLink": f"{GRAPH}/drives/d/root/delta?$deltatoken=D",
        },
    ],
)
def test_paginated_envelope_rejects_malformed_value_and_links_before_adoption(
    payload: object,
) -> None:
    sentinel = "PROVIDER-PAYLOAD-MUST-NOT-LEAK"
    assert isinstance(payload, dict)
    response = {**payload, "provider_detail": sentinel}
    transport = sequenced_transport([httpx.Response(200, json=response)])
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphResponseParseError) as exc:
                await client.get_paginated(http, f"{GRAPH}/drives/d/root/delta")
            assert type(exc.value) is GraphResponseParseError
            assert isinstance(exc.value, GraphParseError)
            assert sentinel not in str(exc.value)

    asyncio.run(scenario())


@pytest.mark.parametrize("link_field", ["@odata.nextLink", "@odata.deltaLink"])
def test_paginated_links_are_guarded_before_continuation_or_cursor_return(
    link_field: str,
) -> None:
    hostile_link = "https://evil.example/steal?$deltatoken=SECRET-CURSOR"
    recorder: list[httpx.Request] = []
    url = f"{GRAPH}/drives/d/root/delta"

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        assert str(request.url) == url
        return httpx.Response(200, json={"value": [], link_field: hostile_link})

    transport = httpx.MockTransport(handler)
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphRedirectError) as exc:
                await client.get_paginated(http, url)
            assert "SECRET-CURSOR" not in str(exc.value)

    asyncio.run(scenario())
    assert [str(request.url) for request in recorder] == [TOKEN_URL, url]


def test_transport_failure_is_closed_category_without_provider_message() -> None:
    sentinel = "TRANSPORT-MESSAGE-MUST-NOT-LEAK"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        raise httpx.ConnectError(sentinel, request=request)

    transport = httpx.MockTransport(handler)
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphTransportError) as exc:
                await client.get_json(http, f"{GRAPH}/drives/d/root/delta")
            assert sentinel not in str(exc.value)

    asyncio.run(scenario())


# -- FIX-A: full-policy re-guard of every followed link ----------------------

_HOSTILE_LINK = [
    "http://graph.microsoft.com/v1.0/drives/d/root/delta",  # allowed host, cleartext
    "https://user:pass@graph.microsoft.com/v1.0/drives/d/root/delta",  # credentialed
    "https://graph.evil.example.com/v1.0/drives/d/root/delta",  # off-allowlist host
    "https://169.254.169.254/v1.0/x",  # metadata IP literal
    "https://2130706433/v1.0/x",  # packed-integer IPv4
    "https://microsoftgraph.chinacloudapi.cn/v1.0/drives/d/root/delta",  # other MS cloud
    "https://graph.microsoft.us/v1.0/drives/d/root/delta",  # US-Gov cloud, not configured
]


@pytest.mark.parametrize("bad_url", _HOSTILE_LINK)
def test_hostile_followed_link_is_refused_without_request(bad_url: str) -> None:
    recorder: list[httpx.Request] = []
    transport = httpx.MockTransport(recording_transport(recorder))
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphRedirectError):
                await client.get_json(http, bad_url)

    asyncio.run(scenario())
    assert recorder == []  # ZERO requests — no token POST, no GET


def test_hostile_nextlink_mid_pagination_aborts_without_second_graph_get() -> None:
    gets = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                200, json={"token_type": "Bearer", "expires_in": 3600, "access_token": "T"}
            )
        gets["count"] += 1
        return httpx.Response(
            200,
            json={"value": [], "@odata.nextLink": "http://graph.microsoft.com/v1.0/x"},
        )

    transport = httpx.MockTransport(handler)
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphRedirectError):
                await client.get_paginated(http, f"{GRAPH}/drives/d/root/delta")

    asyncio.run(scenario())
    assert gets["count"] == 1  # the cleartext nextLink is never followed


# -- Cross-sovereign-cloud credential redirect: pin follows to configured cloud


def test_cross_cloud_nextlink_mid_pagination_aborts_without_cross_cloud_request() -> None:
    recorder: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                200, json={"token_type": "Bearer", "expires_in": 3600, "access_token": "T"}
            )
        return httpx.Response(
            200,
            json={
                "value": [{"id": "a"}],
                "@odata.nextLink": "https://microsoftgraph.chinacloudapi.cn/v1.0/x",
            },
        )

    transport = httpx.MockTransport(handler)
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphRedirectError):
                await client.get_paginated(http, f"{GRAPH}/drives/d/root/delta")

    asyncio.run(scenario())
    graph_gets = [r for r in recorder if str(r.url) != TOKEN_URL]
    assert len(graph_gets) == 1  # exactly one same-cloud GET before the abort
    assert not any(
        r.url.host == "microsoftgraph.chinacloudapi.cn" for r in recorder
    )  # the token never reaches the other cloud


def test_same_cloud_nextlink_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                200, json={"token_type": "Bearer", "expires_in": 3600, "access_token": "T"}
            )
        if "$skiptoken=T2" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "b"}],
                    "@odata.deltaLink": f"{GRAPH}/drives/d/root/delta?$deltatoken=D",
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [{"id": "a"}],
                "@odata.nextLink": f"{GRAPH}/drives/d/root/delta?$skiptoken=T2",
            },
        )

    transport = httpx.MockTransport(handler)
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            items, delta_link = await client.get_paginated(http, f"{GRAPH}/drives/d/root/delta")
            assert items == [{"id": "a"}, {"id": "b"}]
            assert delta_link == f"{GRAPH}/drives/d/root/delta?$deltatoken=D"

    asyncio.run(scenario())


def test_national_cloud_config_pins_to_that_cloud() -> None:
    china_graph = "https://microsoftgraph.chinacloudapi.cn/v1.0"
    recorder: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if str(request.url).endswith("/oauth2/v2.0/token"):
            return httpx.Response(
                200, json={"token_type": "Bearer", "expires_in": 3600, "access_token": "T"}
            )
        if "$skiptoken=T2" in str(request.url):
            return httpx.Response(200, json={"value": [{"id": "b"}]})
        return httpx.Response(
            200,
            json={
                "value": [{"id": "a"}],
                "@odata.nextLink": f"{china_graph}/drives/d/root/delta?$skiptoken=T2",
            },
        )

    transport = httpx.MockTransport(handler)
    settings = m365_settings(
        graph_base_url=china_graph,
        login_base_url="https://login.chinacloudapi.cn",
    )
    client = GraphClient(settings, transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            # (i) China-cloud nextLink IS followed on the configured China cloud.
            items, _ = await client.get_paginated(http, f"{china_graph}/drives/d/root/delta")
            assert items == [{"id": "a"}, {"id": "b"}]
            # (ii) a COMMERCIAL graph.microsoft.com link is refused (zero new GETs there).
            with pytest.raises(GraphRedirectError):
                await client.get_json(http, "https://graph.microsoft.com/v1.0/drives/d/root/delta")

    asyncio.run(scenario())
    assert not any(
        r.url.host == "graph.microsoft.com" for r in recorder
    )  # the pin is derived from config, not hardcoded commercial


# -- FIX-E: token TTL (expires_in) + token-endpoint retry --------------------


def test_token_cached_within_ttl_posts_once() -> None:
    rec: list[httpx.Request] = []
    transport = build_m365_transport(recorder=rec)
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            await client.get_json(http, f"{GRAPH}/sites/{SITE_ID}/drives")
            await client.get_json(http, f"{GRAPH}/sites/{SITE_ID}/drives")

    asyncio.run(scenario())
    assert sum(1 for r in rec if str(r.url) == TOKEN_URL) == 1


def test_token_refetched_after_expiry() -> None:
    rec: list[httpx.Request] = []
    transport = build_m365_transport(recorder=rec, token_expires_in=30)  # 30-60 -> TTL 0
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            await client.get_json(http, f"{GRAPH}/sites/{SITE_ID}/drives")
            await client.get_json(http, f"{GRAPH}/sites/{SITE_ID}/drives")

    asyncio.run(scenario())
    assert sum(1 for r in rec if str(r.url) == TOKEN_URL) == 2


def test_token_endpoint_429_then_success_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    token_posts = [
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3600, "access_token": "T"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return token_posts.pop(0)
        return httpx.Response(200, json={"value": []})

    transport = httpx.MockTransport(handler)
    client = GraphClient(m365_settings(), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            data = await client.get_json(http, f"{GRAPH}/drives/d/root/delta")
            assert data["value"] == []

    asyncio.run(scenario())
    assert slept == [2.0]  # honored the token-endpoint Retry-After


def test_token_endpoint_retry_exhaustion_raises_status_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(429)
        return httpx.Response(200, json={"value": []})  # pragma: no cover

    transport = httpx.MockTransport(handler)
    client = GraphClient(m365_settings(max_attempts=3), transport=transport)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GraphRateLimitedError) as exc:
                await client.get_json(http, f"{GRAPH}/drives/d/root/delta")
            assert "429" in str(exc.value)
            assert "fake-secret-not-real" not in str(exc.value)
            assert exc.value.status == 429

    asyncio.run(scenario())
