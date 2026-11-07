# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SalesforceClient: OAuth token flows, path guard, retries, C12 hygiene (NE-047)."""

from __future__ import annotations

import asyncio

import httpx
import jwt
import pytest

from ne.connect.errors import SalesforceSyncError
from ne.connect.salesforce.client import _RETRYABLE, SalesforceClient

from .salesforce_transport import (
    INSTANCE_URL,
    LOGIN_URL,
    build_salesforce_transport,
    salesforce_jwt_settings,
    salesforce_settings,
)


def test_jwt_bearer_assertion_claims_and_posts_to_login_host(tmp_path) -> None:
    settings = salesforce_jwt_settings(tmp_path)
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    client = SalesforceClient(settings, transport=transport)

    async def scenario() -> None:
        async with client.session() as http:
            await client._get_token(http)

    asyncio.run(scenario())

    token_posts = [r for r in recorder if r.url.path == "/services/oauth2/token"]
    assert len(token_posts) == 1
    post = token_posts[0]
    assert post.url.host == "login.salesforce.com"

    body = httpx.QueryParams(post.content.decode())
    claims = jwt.decode(body["assertion"], options={"verify_signature": False})
    assert claims["iss"] == settings.client_id
    assert claims["sub"] == settings.username
    assert claims["aud"] == settings.login_base_url
    assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"


def test_client_credentials_posts_to_instance_host() -> None:
    settings = salesforce_settings(auth_method="client_credentials", client_secret="s3cr3t")
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    client = SalesforceClient(settings, transport=transport)

    async def scenario() -> None:
        async with client.session() as http:
            await client._get_token(http)

    asyncio.run(scenario())

    token_posts = [r for r in recorder if r.url.path == "/services/oauth2/token"]
    assert len(token_posts) == 1
    assert token_posts[0].url.host == "acme.my.salesforce.com"
    body = token_posts[0].content.decode()
    assert "client_secret=s3cr3t" in body
    assert "grant_type=client_credentials" in body


def test_token_is_cached_across_calls() -> None:
    settings = salesforce_settings()
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    client = SalesforceClient(settings, transport=transport)

    async def scenario() -> None:
        async with client.session() as http:
            await client._get_token(http)
            await client._get_token(http)

    asyncio.run(scenario())
    token_posts = [r for r in recorder if r.url.path == "/services/oauth2/token"]
    assert len(token_posts) == 1  # second call served from cache


def test_401_invalidates_token_and_retries_exactly_once() -> None:
    settings = salesforce_settings()
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder, respond_401_once=True)
    client = SalesforceClient(settings, transport=transport)

    async def scenario() -> list[dict[str, object]]:
        async with client.session() as http:
            return await client.rest_query(http, "SELECT Id FROM Account")

    rows = asyncio.run(scenario())
    assert rows  # the retried request succeeded
    token_posts = [r for r in recorder if r.url.path == "/services/oauth2/token"]
    assert len(token_posts) == 2  # initial fetch + one re-acquire after the 401


def test_429_honors_retry_after_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings = salesforce_settings()
    transport = build_salesforce_transport(one_shot_429=True)
    client = SalesforceClient(settings, transport=transport)

    async def scenario() -> list[dict[str, object]]:
        async with client.session() as http:
            return await client.rest_query(http, "SELECT Id FROM Account")

    rows = asyncio.run(scenario())
    assert rows
    assert slept == [1.0]


def test_transient_5xx_are_retryable() -> None:
    # Codex P2: transient 5xx (500/502) were omitted from the retry set, so a
    # transient Salesforce/proxy fault aborted the sync instead of retrying.
    assert {429, 500, 502, 503, 504} <= _RETRYABLE
    # 501 (not implemented) / 505 are NOT transient — must stay non-retryable.
    assert 501 not in _RETRYABLE


def test_retry_exhaustion_raises_status_only_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings = salesforce_settings(max_attempts=2, retry_after_cap_seconds=1)
    transport = build_salesforce_transport(fail_after=0)  # every call gets HTTP 500
    client = SalesforceClient(settings, transport=transport)

    async def scenario() -> None:
        async with client.session() as http:
            await client._get_token(http)

    with pytest.raises(SalesforceSyncError) as exc:
        asyncio.run(scenario())
    assert exc.value.status == 500


def test_error_message_excludes_secret_soql_and_query_string() -> None:
    settings = salesforce_settings(
        auth_method="client_credentials", client_secret="super-secret-value"
    )
    transport = build_salesforce_transport(fail_after=0)
    client = SalesforceClient(settings, transport=transport)

    async def scenario() -> None:
        async with client.session() as http:
            await client._get_token(http)

    with pytest.raises(SalesforceSyncError) as exc:
        asyncio.run(scenario())
    message = str(exc.value)
    assert "super-secret-value" not in message
    assert "SELECT" not in message
    assert "?" not in message


def test_guard_path_refuses_absolute_url_before_token_acquisition() -> None:
    settings = salesforce_settings()
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    client = SalesforceClient(settings, transport=transport)

    async def scenario() -> None:
        async with client.session() as http:
            await client.get_json(http, "https://evil.example.com/services/data/v65.0/query")

    with pytest.raises(SalesforceSyncError):
        asyncio.run(scenario())
    assert recorder == []  # refused before any token POST


def test_guard_path_refuses_off_prefix_next_records_url() -> None:
    settings = salesforce_settings()
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    client = SalesforceClient(settings, transport=transport)

    async def scenario() -> None:
        async with client.session() as http:
            await client.get_json(http, "/evil/path")

    with pytest.raises(SalesforceSyncError):
        asyncio.run(scenario())
    assert recorder == []


def test_rest_query_follows_relative_next_records_url_across_both_pages() -> None:
    settings = salesforce_settings()
    transport = build_salesforce_transport()
    client = SalesforceClient(settings, transport=transport)

    async def scenario() -> list[dict[str, object]]:
        async with client.session() as http:
            return await client.rest_query(
                http,
                "SELECT SobjectType, PermissionsRead, Parent.Id, Parent.IsOwnedByProfile, "
                "Parent.ProfileId FROM ObjectPermissions",
            )

    rows = asyncio.run(scenario())
    assert len(rows) == 5  # 2 rows on page 1 + 3 rows on page 2


def _rest_pages_transport(pages: dict[str, dict[str, object]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/oauth2/token":
            return httpx.Response(200, json={"access_token": "fake"})
        payload = pages.get(request.url.path)
        return httpx.Response(404) if payload is None else httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _run_rest_pages(
    pages: dict[str, dict[str, object]], **settings_overrides: object
) -> list[dict[str, object]]:
    client = SalesforceClient(
        salesforce_settings(**settings_overrides), transport=_rest_pages_transport(pages)
    )

    async def scenario() -> list[dict[str, object]]:
        async with client.session() as http:
            return await client.rest_query(http, "SELECT Id FROM Account")

    return asyncio.run(scenario())


def test_rest_query_refuses_nonfinal_page_without_next_url() -> None:
    with pytest.raises(SalesforceSyncError, match="incomplete without nextRecordsUrl"):
        _run_rest_pages(
            {
                "/services/data/v65.0/query": {
                    "totalSize": 1,
                    "done": False,
                    "records": [],
                }
            }
        )


def test_rest_query_refuses_changed_total_size() -> None:
    next_path = "/services/data/v65.0/query/next"
    with pytest.raises(SalesforceSyncError, match="totalSize changed"):
        _run_rest_pages(
            {
                "/services/data/v65.0/query": {
                    "totalSize": 2,
                    "done": False,
                    "nextRecordsUrl": next_path,
                    "records": [{"Id": "one"}],
                },
                next_path: {
                    "totalSize": 3,
                    "done": True,
                    "records": [{"Id": "two"}],
                },
            }
        )


def test_rest_query_refuses_final_row_count_mismatch() -> None:
    with pytest.raises(SalesforceSyncError, match="row count does not match"):
        _run_rest_pages(
            {
                "/services/data/v65.0/query": {
                    "totalSize": 2,
                    "done": True,
                    "records": [{"Id": "only-one"}],
                }
            }
        )


def test_rest_query_refuses_repeated_next_url() -> None:
    next_path = "/services/data/v65.0/query/next"
    with pytest.raises(SalesforceSyncError, match="repeated nextRecordsUrl"):
        _run_rest_pages(
            {
                "/services/data/v65.0/query": {
                    "totalSize": 2,
                    "done": False,
                    "nextRecordsUrl": next_path,
                    "records": [{"Id": "one"}],
                },
                next_path: {
                    "totalSize": 2,
                    "done": False,
                    "nextRecordsUrl": next_path,
                    "records": [],
                },
            }
        )


def test_rest_query_refuses_before_following_beyond_page_bound() -> None:
    next_path = "/services/data/v65.0/query/next"
    with pytest.raises(SalesforceSyncError, match="result-page bound"):
        _run_rest_pages(
            {
                "/services/data/v65.0/query": {
                    "totalSize": 2,
                    "done": False,
                    "nextRecordsUrl": next_path,
                    "records": [{"Id": "one"}],
                },
                next_path: {
                    "totalSize": 2,
                    "done": True,
                    "records": [{"Id": "two"}],
                },
            },
            rest_max_result_pages=1,
        )


def test_instance_and_login_hosts_are_distinct_in_fixture_settings() -> None:
    assert INSTANCE_URL != LOGIN_URL


# --- NE-SF-ERRORCODE: a 403 must say WHICH 403 -------------------------------
#
# Salesforce reports "your daily API allowance is spent" and "you may not do
# that" as the SAME status. This client discarded the body, so both read as
# `Salesforce API GET returned HTTP 403` and an operator could not tell a
# condition that clears by WAITING from one that needs an access grant.
#
# Measured on the demo org 2026-08-31: every call returned 403 with
# `REQUEST_LIMIT_EXCEEDED` in the body, and nothing in our own output said so.
# That is the same defect shape as a refusal reported only as five integers:
# a real failure reduced to a value that cannot distinguish its own causes.


def test_a_403_names_its_error_code_so_quota_and_permission_are_distinguishable() -> None:
    import httpx

    from ne.connect.salesforce.client import _error_code_suffix

    quota = httpx.Response(403, json=[{"errorCode": "REQUEST_LIMIT_EXCEEDED", "message": "x"}])
    denied = httpx.Response(403, json=[{"errorCode": "INSUFFICIENT_ACCESS", "message": "x"}])
    assert _error_code_suffix(quota) == " (REQUEST_LIMIT_EXCEEDED)"
    assert _error_code_suffix(denied) == " (INSUFFICIENT_ACCESS)"
    assert _error_code_suffix(quota) != _error_code_suffix(denied)


def test_no_provider_PROSE_reaches_the_message_only_the_closed_code() -> None:
    """`message` is provider-authored text on an egress boundary. The code is a
    machine-readable identifier; the prose is not, and it never leaves the frame."""
    import httpx

    from ne.connect.salesforce.client import _error_code_suffix

    resp = httpx.Response(
        403, json=[{"errorCode": "API_DISABLED_FOR_ORG", "message": "SECRETPROSE"}]
    )
    suffix = _error_code_suffix(resp)
    assert "SECRETPROSE" not in suffix
    assert suffix == " (API_DISABLED_FOR_ORG)"


def test_an_ill_shaped_code_yields_NOTHING_rather_than_a_sanitised_guess() -> None:
    import httpx

    from ne.connect.salesforce.client import _error_code_suffix

    for payload in (
        [{"errorCode": "A" * 200}],  # over the length cap
        [{"errorCode": "not a code; drop tables"}],  # not the closed shape
        [{"errorCode": 17}],  # not a string
        [{}],  # no code at all
        {"unexpected": "shape"},
    ):
        assert _error_code_suffix(httpx.Response(403, json=payload)) == ""
    assert _error_code_suffix(httpx.Response(403, text="<html>not json</html>")) == ""
