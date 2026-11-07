# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Auth sweep — the middleware contract on every /v1 route + /healthz exemption."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ne.api.deps import current_authenticated_context
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.types import Principal

from .conftest import AppBuilder, run

_ROUTES = [
    ("GET", "/api/v1/me", None),
    ("POST", "/api/v1/chat/sessions", {"agent_name": "console-chat"}),
    ("POST", "/api/v1/chat/sessions/x/messages", {"prompt": "hi"}),
    ("POST", "/api/v1/brain/search", {"query": "q", "top_k": 5}),
    ("POST", "/api/v1/brain/retrieve", {"chunk_ids": ["c1"]}),
    ("GET", "/api/v1/approvals/pending", None),
    ("POST", "/api/v1/approvals/x/approve", {}),
    ("POST", "/api/v1/approvals/x/reject", {"reason": ""}),
    ("GET", "/api/v1/audit/events", None),
    ("GET", "/api/v1/audit/replay", None),
    ("GET", "/api/v1/eval/runs", None),
    ("GET", "/api/v1/kpi/summary", None),
    ("GET", "/api/v1/steward/proposals", None),
    ("POST", "/api/v1/steward/proposals/x/approve", {"note": ""}),
    ("POST", "/api/v1/steward/proposals/x/reject", {"code": "incorrect", "note": ""}),
    ("POST", "/api/v1/steward/proposals/x/apply", {"applied_ref": "r", "note": ""}),
    ("GET", "/api/v1/steward/digest", None),
    ("GET", "/api/v1/steward/latency", None),
]


@pytest.mark.parametrize("method,path,body", _ROUTES)
def test_no_bearer_is_401_with_challenge(
    make_app: AppBuilder, method: str, path: str, body: dict | None
) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.request(method, path, json=body)
            assert r.status_code == 401
            assert r.headers.get("www-authenticate") == "Bearer"
            assert isinstance(r.json()["detail"], str)

    run(_t())


@pytest.mark.parametrize("method,path,body", _ROUTES)
def test_garbage_bearer_is_401(
    make_app: AppBuilder, method: str, path: str, body: dict | None
) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.request(
                method, path, json=body, headers={"Authorization": "Bearer garbage"}
            )
            assert r.status_code == 401
            # No reason oracle: the body carries no failure detail beyond a generic string.
            assert isinstance(r.json()["detail"], str)

    run(_t())


@pytest.mark.parametrize("method,path,body", _ROUTES)
def test_query_param_token_is_401(
    make_app: AppBuilder, method: str, path: str, body: dict | None
) -> None:
    h = make_app()

    async def _t() -> None:
        token = h.verifier.mint("user:ada", groups=["eng"], department="engineering")
        async with h.client() as client:
            # A valid token supplied ONLY as a query param must NOT authenticate.
            r = await client.request(method, f"{path}?access_token={token}", json=body)
            assert r.status_code == 401

    run(_t())


def test_healthz_is_public(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/healthz")
            assert r.status_code == 200
            assert r.json() == {"status": "ok"}

    run(_t())


def test_healthz_exemption_is_exact_match(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            # A near-miss path is never public — 404/401, never 200.
            r = await client.get("/healthX")
            assert r.status_code != 200

    run(_t())


def test_authenticated_context_dependency_accepts_only_matching_middleware_state() -> None:
    context = AuthenticatedRequestContext(
        principal_id="user:ada",
        authorization_groups=("engineering",),
        issuer="https://issuer.example",
        expires_at=1_800_000_000,
    )
    principal = Principal(id="user:ada", groups=("engineering",))
    matching = Request(
        {
            "type": "http",
            "headers": (),
            "state": {"principal": principal, "authenticated_context": context},
        }
    )
    assert current_authenticated_context(matching) == context

    forged_transport_only = Request(
        {
            "type": "http",
            "headers": ((b"x-authenticated-context", context.context_hash.encode()),),
            "state": {},
        }
    )
    with pytest.raises(HTTPException) as missing:
        current_authenticated_context(forged_transport_only)
    assert missing.value.status_code == 401

    mismatched = Request(
        {
            "type": "http",
            "headers": (),
            "state": {
                "principal": Principal(id="user:bob"),
                "authenticated_context": context,
            },
        }
    )
    with pytest.raises(HTTPException) as swapped:
        current_authenticated_context(mismatched)
    assert swapped.value.status_code == 401
